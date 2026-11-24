"""Read-only legacy audit and retention previews for verified photo archives."""
from contextlib import closing
import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path
import sqlite3
import time
import tempfile
from itertools import chain

from . import catalog

from .strict_cli import IntegrityError, make_directory, matches, present


def policy(mode="off", value=0, shared=False):
    if mode not in ("off", "immediate", "days", "latest") or type(shared) is not bool or type(value) is not int:
        raise ValueError("iCloud 清理策略无效。")
    if mode in ("days", "latest"):
        if not 1 <= value <= 1000000:
            raise ValueError("天数或保留数量必须为 1 至 1000000 的整数。")
    elif value != 0 or (mode == "off" and shared):
        raise ValueError("iCloud 清理参数无效。")
    return {"mode": mode, "value": value, "shared": shared}


def create_inventory(path, root, libraries):
    """Index legacy paths once. Paths here are relative to the selected archive root."""
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE context(root TEXT NOT NULL);
            CREATE TABLE assets(identity TEXT PRIMARY KEY, zone TEXT NOT NULL, captured INTEGER,
                                manifest TEXT UNIQUE NOT NULL, digest TEXT NOT NULL, complete INTEGER NOT NULL);
            CREATE TABLE legacy(path TEXT PRIMARY KEY, name TEXT NOT NULL, zone TEXT NOT NULL, matched INTEGER DEFAULT 0);
            CREATE INDEX legacy_name ON legacy(zone, name);
        """)
        db.execute("INSERT INTO context VALUES (?)", (str(root),))
        for zone, folder in libraries:
            index_legacy(db, root, zone, folder)


def resume_inventory(checkpoint, inventory, root, shared, phases):
    if not phases or not checkpoint.is_file() or checkpoint.is_symlink():
        return False
    try:
        with closing(sqlite3.connect(checkpoint)) as source:
            data = source.execute("SELECT root,shared,phases FROM checkpoint").fetchone()
            if not data or (data[0], bool(data[1]), set(json.loads(data[2]))) != (str(root), shared, phases):
                return False
            with closing(sqlite3.connect(inventory)) as dest:
                source.backup(dest)
        return True
    except (sqlite3.Error, ValueError):
        Path(inventory).unlink(missing_ok=True)
        return False


def save_checkpoint(inventory, checkpoint, root, shared, phases):
    # Publish only completed phases. A crash during the next scan cannot mix its rows into this snapshot.
    with tempfile.NamedTemporaryFile(dir=checkpoint.parent, delete=False) as tmp:
        path = Path(tmp.name)
    try:
        with closing(sqlite3.connect(inventory)) as source, closing(sqlite3.connect(path)) as dest:
            source.backup(dest)
            with dest:
                dest.execute("CREATE TABLE IF NOT EXISTS checkpoint(root TEXT,shared INTEGER,phases TEXT)")
                dest.execute("DELETE FROM checkpoint")
                dest.execute("INSERT INTO checkpoint VALUES(?,?,?)", (str(root),shared,json.dumps(sorted(phases))))
        path.replace(checkpoint)
        catalog.fsync_dir(checkpoint.parent)
    finally:
        path.unlink(missing_ok=True)


def index_legacy(db, root, zone, folder):
    if not folder.is_dir():
        return
    if folder.resolve() != folder:
        raise IntegrityError("local-path")
    for month in folder.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]"):
        if month.resolve() != month or not month.is_dir():
            raise IntegrityError("local-path")
        paths = (path for path in month.iterdir() if not path.name.startswith("."))
        previous = month / ".icloud-previous"
        if previous.is_symlink():
            raise IntegrityError("local-path")
        for path in chain(paths, previous.glob("*/*")):
            if path.resolve() != path:
                raise IntegrityError("local-path")
            if path.is_file():
                db.execute("INSERT OR IGNORE INTO legacy(path,name,zone) VALUES(?,?,?)",
                           (str(path.relative_to(root)), path.name, zone))


def legacy_paths(db, month, name):
    if db is None:
        yield month / name, None
        return
    root = Path(db.execute("SELECT root FROM context").fetchone()[0])
    relative = month.relative_to(root).parts
    zone = relative[1] if len(relative) == 4 else "PrimarySync"
    for path, in db.execute("SELECT path FROM legacy WHERE name=? AND zone=?", (name, zone)):
        if (root / path).resolve() != root / path:
            raise IntegrityError("local-path")
        yield root / path, path


def match_legacy_xmp(db, month, name, data):
    if db is None:
        return
    for path, relative in legacy_paths(db, month, name):
        if present(path, len(data)) and path.read_bytes() == data:
            db.execute("UPDATE legacy SET matched=1 WHERE path=?", (relative,))


def match_legacy(db, month, name, size, digest):
    found = None
    for path, relative_path in legacy_paths(db, month, name):
        if matches(path, size, digest):
            if db is not None:
                db.execute("UPDATE legacy SET matched=1 WHERE path=?", (relative_path,))
            found = path
    return found


def record_asset(db, folder, manifest):
    root = Path(db.execute("SELECT root FROM context").fetchone()[0])
    item = manifest["identity"]
    identity = catalog.encoded(item).decode()
    captured = manifest["metadata"].get("assetDate", {}).get("value")
    values = (identity, item["zone"], captured if type(captured) is int else None,
              str((folder / "manifest.json").relative_to(root)), catalog.token(manifest),
              int(manifest["status"] == "verified"))
    old = db.execute("SELECT * FROM assets WHERE identity=?", (identity,)).fetchone()
    if old and old != values:
        raise IntegrityError("inventory")
    if not old:
        db.execute("INSERT INTO assets VALUES(?,?,?,?,?,?)", values)


def candidates(db, rule, now):
    condition = "(zone='PrimarySync' OR :shared)"
    params = dict(shared=rule["shared"])
    if rule["mode"] == "off":
        condition += " AND 0"
    elif rule["mode"] == "days":
        condition += " AND captured < :cutoff"
        params["cutoff"] = int(now * 1000) - rule["value"] * 86400000
    elif rule["mode"] == "latest":
        condition += " AND captured IS NOT NULL ORDER BY captured DESC,identity LIMIT -1 OFFSET :keep"
        params["keep"] = rule["value"]
    return db.execute("SELECT manifest,digest FROM assets WHERE " + condition, params)


def verify_manifest(root, relative, expected, *, on_issue=None, check=None, durable=True):
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts or Path(relative).name != "manifest.json":
        raise IntegrityError("local-path")
    path = root / relative
    if not path.is_relative_to(root) or path.resolve(strict=True) != path or not path.is_file():
        raise IntegrityError("local-path")
    manifest = json.loads(path.read_bytes())
    if (catalog.token(manifest) != expected or manifest["status"] != "verified" or manifest["missing"]
            or not manifest["resources"]):
        raise IntegrityError("inventory")
    if catalog.layout(path.parent.parent.parent, manifest["identity"], manifest["revision"]) != path.parent:
        raise IntegrityError("inventory")
    claim = path.parent / ".identity.json"
    owner = {"identity": manifest["identity"], "revision": manifest["revision"]}
    if claim.is_symlink() or not claim.is_file() or claim.read_bytes() != catalog.encoded(owner):
        raise IntegrityError("inventory")

    def inspect(file, valid):
        try:
            if file.resolve(strict=True) != file or not file.is_file():
                raise IntegrityError("local-path")
            if not valid():
                raise IntegrityError("mismatch")
        except (IntegrityError, OSError) as exc:
            if not on_issue:
                raise
            on_issue(str(file.relative_to(root)), issue_reason(exc))

    for resource in manifest["resources"]:
        name = resource["name"]
        if not isinstance(name, str) or Path(name).name != name or name in ("", ".", ".."):
            raise IntegrityError("local-path")
        media = path.parent / name
        def valid_media():
            content_hash = hashlib.sha256()
            return (matches(media, resource["bytes"], bytes.fromhex(resource["appleChecksum"]),
                            durable=durable, content_hash=content_hash, check=check)
                    and content_hash.hexdigest() == resource["sha256"])
        inspect(media, valid_media)
        if "xmpSHA256" in resource:
            sidecar = media.with_name(name + ".xmp")
            def valid_sidecar():
                with sidecar.open("rb") as stream:
                    return hashlib.file_digest(stream, "sha256").hexdigest() == resource["xmpSHA256"]
            inspect(sidecar, valid_sidecar)
    return manifest


def issue_reason(exc):
    if isinstance(exc, FileNotFoundError):
        return "文件缺失"
    if isinstance(exc, OSError):
        return "文件无法读取"
    if isinstance(exc, IntegrityError) and str(exc) == "mismatch":
        return "文件内容校验失败"
    return "清单、项目身份或路径校验失败"


def report_header(path):
    if path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode):
        raise IntegrityError("local-path")
    with path.open(encoding="utf-8") as stream:
        header = json.loads(stream.readline(65536))
    if (not isinstance(header, dict) or header.get("kind") != "scan" or header.get("complete") is not True
            or header.get("incomplete") != 0 or type(header.get("verified")) is not int
            or header["verified"] < 0 or type(header.get("time")) not in (int, float)
            or not 0 <= header["time"] < 253402300800):
        raise IntegrityError("inventory")
    return header


def report_choices(root, account_key):
    folder = root / ".icloud-archive"
    if not folder.exists():
        return []
    if folder.resolve(strict=True) != folder:
        raise IntegrityError("local-path")
    options = {}
    for path in folder.glob(account_key + "*.jsonl"):
        if not re.fullmatch(re.escape(account_key) + r"(?:-[0-9]+)?\.jsonl", path.name):
            continue
        try:
            header = report_header(path)
        except (OSError, ValueError, IntegrityError):
            continue
        # Prefer the immutable snapshot when latest points to the same scan.
        stamp = header["time"]
        if stamp not in options or path.stem != account_key:
            options[stamp] = dict(name=path.name, time=stamp, total=header["verified"])
    return sorted(options.values(), key=lambda item: item["time"], reverse=True)


def keep_snapshot(latest):
    try:
        header = report_header(latest)
    except (FileNotFoundError, ValueError, IntegrityError):
        return
    path = latest.with_name(f"{latest.stem}-{int(header['time'] * 1e9)}.jsonl")
    if path.exists():
        return
    try:
        os.link(latest, path)
    except OSError:
        with latest.open("rb") as original, catalog.publish_file(path) as copy:
            shutil.copyfileobj(original, copy)
    catalog.fsync_dir(latest.parent)


def publish_review(path, root, account_key, rule, scan_complete, *, recheck=False):
    """Replace the previous report only after writing the whole new, dated snapshot."""
    folder = root / ".icloud-archive"
    make_directory(folder)
    now = time.time()
    with closing(sqlite3.connect(path)) as db:
        incomplete = db.execute("SELECT count(*) FROM assets WHERE complete=0").fetchone()[0]
        unassigned = db.execute("SELECT count(*) FROM legacy WHERE matched=0").fetchone()[0]
        verified = db.execute("SELECT count(*) FROM assets WHERE complete=1").fetchone()[0]
        ready = scan_complete and not incomplete
        count = 0
        latest = folder / (account_key + ".jsonl")
        keep_snapshot(latest)  # Preserve a complete report from before the upgrade, too.
        with catalog.publish_file(latest) as report:
            def emit(row):
                report.write(catalog.encoded(row) + b"\n")
            emit(dict(kind="scan", time=now, complete=bool(ready), verified=verified,
                      incomplete=incomplete, unassigned=unassigned, previewPolicy=rule))
            for relative, digest, ok in db.execute("SELECT manifest,digest,complete FROM assets ORDER BY manifest"):
                if ok and recheck:
                    verify_manifest(root, relative, digest)
                emit(dict(kind="asset", manifest=relative, manifestSHA256=digest, verified=bool(ok)))
            for relative, matched in db.execute("SELECT path,matched FROM legacy ORDER BY path"):
                emit(dict(kind="legacy", path=relative, status="matched-bytes" if matched else "unassigned"))
            if ready:
                for relative, digest in candidates(db, rule, now):
                    verify_manifest(root, relative, digest)
                    emit(dict(kind="candidate", manifest=relative))
                    count += 1
            emit(dict(kind="summary", candidates=count))
        if ready:
            keep_snapshot(latest)
        catalog.fsync_dir(root)
    return verified, incomplete, unassigned, count


def verify_report(root, report, *, on_item=None, on_issue=None, check=None, expected_scan=None):
    """Stream the report. CLI fails fast; the web view collects problems per file."""
    root = root.resolve(strict=True)
    report = report.absolute()
    header = report_header(report)
    if expected_scan and (header["time"], header["verified"]) != expected_scan:
        raise IntegrityError("inventory")
    seen = set()
    with report.open(encoding="utf-8") as stream:
        if json.loads(stream.readline()) != header:
            raise IntegrityError("inventory")
        finished = False
        for line in stream:
            if check:
                check()
            row = json.loads(line)
            if finished or not isinstance(row, dict):
                raise IntegrityError("inventory")
            if row["kind"] == "asset":
                relative = row["manifest"]
                if (row.get("verified") is not True or not isinstance(relative, str) or relative in seen
                        or Path(relative).is_absolute() or ".." in Path(relative).parts):
                    raise IntegrityError("inventory")
                seen.add(relative)
                try:
                    verify_manifest(root, relative, row["manifestSHA256"], on_issue=on_issue, check=check, durable=False)
                except (IntegrityError, OSError, ValueError, KeyError, TypeError) as exc:
                    if not on_issue:
                        raise
                    on_issue(relative, issue_reason(exc))
                if on_item:
                    on_item(len(seen))
            elif row["kind"] == "summary":
                finished = True
            elif row["kind"] not in ("legacy", "candidate"):
                raise IntegrityError("inventory")
        if not finished or len(seen) != header["verified"]:
            raise IntegrityError("inventory")
    return len(seen)
