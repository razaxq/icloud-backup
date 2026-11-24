"""Per-asset archiving for the bundled icloudpd 1.33.0 CLI."""

from . import catalog, space

from contextlib import ExitStack
import base64
import binascii
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile


# iCloud Photos web fileChecksum, signature type 1. Verified against captured
# original HEIC/MOV bodies. Unknown signature formats must not pass verification.
SALT = b"com.apple.XattrObjectSalt\0com.apple.DataObjectSalt\0"
PROTOCOL = "FNARCHIVE/1 "


class IntegrityError(Exception):
    """A short fixed reason code, safe to send to the parent without URLs."""


def signature(version):
    try:
        value = base64.b64decode(version.checksum, validate=True)
    except (ValueError, TypeError, binascii.Error):
        raise IntegrityError("signature") from None
    if len(value) != 21 or value[0] != 1 or type(version.size) is not int or version.size <= 0:
        raise IntegrityError("signature")
    return value


def hash_file(path, hashes, *, size=None, durable=False, check=None):
    """Hash a regular file in one pass and confirm the same file stayed in place."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise IntegrityError("local-path")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if size is not None and before.st_size != size:
            return False
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if check:
                check()
            for digest in hashes:
                digest.update(chunk)
        if durable:
            os.fsync(stream.fileno())
        after = os.fstat(stream.fileno())
        same_file = os.path.samestat(after, path.lstat())
    return same_file and before.st_ctime_ns == after.st_ctime_ns and before.st_mtime_ns == after.st_mtime_ns


def matches(path, size, expected, *, durable=False, content_hash=None, check=None):
    if len(expected) != 21 or expected[0] != 1 or type(size) is not int or size <= 0:
        raise IntegrityError("signature")
    digest = hashlib.sha1(SALT, usedforsecurity=False)
    hashes = (digest,) if content_hash is None else (digest, content_hash)
    return hash_file(path, hashes, size=size, durable=durable, check=check) and digest.digest() == expected[1:]


def make_directory(path):
    path.mkdir(exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise IntegrityError("local-path")


def publish_verified(path, version, fetch, *, existing_hash=None):
    """Return True for a new write. On a verified cache hit, existing_hash contains the final digest."""
    expected = signature(version)
    if matches(path, version.size, expected, durable=existing_hash is not None, content_hash=existing_hash):
        return False
    working = path.parent / ".icloud-work"
    make_directory(working)
    # Upstream resumes in <base32(fileChecksum)>.part beside our staged file.
    # Use the same directory on repeat visits but never expose those bytes as a photo.
    key = base64.b32encode(expected).decode("ascii")
    stage = working / (key + ".verified")
    if not matches(stage, version.size, expected):
        stage.unlink(missing_ok=True)
        partial_file = working / (key + ".part")
        if partial_file.is_symlink():
            raise IntegrityError("local-path")
        if partial_file.exists() and partial_file.stat().st_size >= version.size:
            if matches(partial_file, version.size, expected):
                os.replace(partial_file, stage)
            else:
                partial_file.unlink()
        if not stage.exists():
            partial_size = partial_file.stat().st_size if partial_file.is_file() else 0
            space.check(working, max(0, version.size - partial_size))
            if not fetch(str(stage)):
                space.check(working)
                raise IntegrityError("download")
        if not matches(stage, version.size, expected):
            stage.unlink(missing_ok=True)
            raise IntegrityError("mismatch")
    if path.is_symlink():
        raise IntegrityError("local-path")
    if path.exists():
        # A same-name old edit or a damaged file can still be valuable. Keep
        # its bytes before atomically publishing the verified current resource.
        previous = path.parent / ".icloud-previous"
        make_directory(previous)
        backup = Path(tempfile.mkdtemp(dir=previous)) / path.name
        try:
            os.link(path, backup)
        except OSError:
            try:
                space.check(previous, path.stat().st_size)
                shutil.copy2(path, backup)
            except (OSError, space.LowSpace):
                backup.unlink(missing_ok=True)
                backup.parent.rmdir()
                raise
    os.replace(stage, path)
    return True


def described_resource(fields, prefix):
    from pyicloud_ipd.asset_version import AssetVersion
    data = fields.get(prefix + "Res", {}).get("value")
    uti = fields.get(prefix + "FileType", {}).get("value")
    if not (isinstance(data, dict) and isinstance(uti, str) and uti and data.get("downloadURL")
            and data.get("fileChecksum") and type(data.get("size")) is int and data["size"] > 0):
        return None
    return AssetVersion(data["size"], data["downloadURL"], uti, data["fileChecksum"])


def available_versions(photo):
    from pyicloud_ipd.item_type import AssetItemType
    try:
        return photo.versions, []
    except (KeyError, TypeError, ValueError):
        # A malformed companion makes the pinned parser discard all versions.
        # Recover each independently described file and retain a failure for this item.
        lookup = photo.VIDEO_VERSION_LOOKUP if photo.item_type == AssetItemType.MOVIE else photo.PHOTO_VERSION_LOOKUP
        found = {}
        for size, prefix in lookup.items():
            for record in (photo._asset_record, photo._master_record):
                version = described_resource(record["fields"], prefix)
                if version is not None:
                    found[size] = version
                    break
        return found, ["record"]


def renditions(photo, name, primary_sizes, lp_filename_generator):
    """Choose originals, additional advertised resources and labelled fallback copies."""
    from pyicloud_ipd.asset_version import add_suffix_to_filename, calculate_version_filename
    from pyicloud_ipd.item_type import AssetItemType
    from pyicloud_ipd.utils import disambiguate_filenames
    from pyicloud_ipd.version_size import AssetVersionSize as Asset, LivePhotoVersionSize as Live

    versions, missing = available_versions(photo)
    selected = [size for size in primary_sizes if size in versions]
    _, overrides = disambiguate_filenames(versions, selected, photo, lp_filename_generator, name)
    fields = photo._asset_record["fields"]
    records = (fields, photo._master_record["fields"])
    live = (str(fields.get("assetSubtypeV2", {}).get("value")) == "2"
            or int(fields.get("vidComplDurValue", {}).get("value") or 0) > 0
            or any(isinstance(size, Live) for size in versions)
            or any(key.startswith("resOriginalVidCompl") for record in records for key in record)
            or (photo.item_type == AssetItemType.IMAGE and
                any(record.get("hasVidCompl", {}).get("value") in (True, 1, "1") for record in records)))
    previews = set()
    required = [(Asset.ORIGINAL, (Asset.MEDIUM, Asset.THUMB), "original")]
    if live:
        required.append((Live.ORIGINAL, (Live.MEDIUM, Live.THUMB), "live"))
    expected = {resource_role(size) for size, _, _ in required}
    for original, fallbacks, reason in required:
        if original in versions:
            if original not in selected:
                selected.append(original)
            continue
        missing.append(reason)
        for fallback in fallbacks:
            if fallback in versions:
                selected.append(fallback)
                previews.add(fallback)
                break
    files = []
    for size in selected:
        version = versions[size]
        filename_size = Asset.ORIGINAL if size in previews and isinstance(size, Asset) else size
        filename = calculate_version_filename(name, version, filename_size, lp_filename_generator,
                                              photo.item_type, overrides.get(size))
        if size in previews:
            filename = add_suffix_to_filename("-preview-" + size.value, filename)
        elif Asset.ORIGINAL not in versions and size in (Asset.ADJUSTED, Asset.ALTERNATIVE):
            filename = add_suffix_to_filename("-" + size.value, filename)
        if not filename or Path(filename).name != filename or filename in (".", ".."):
            raise IntegrityError("local-path")
        files.append((size, version, filename, isinstance(size, Asset) and size not in previews))
    # The upstream versions map omits auxiliary resources and type-only records.
    from pyicloud_ipd.asset_version import ITEM_TYPE_EXTENSIONS
    known = {"resOriginal", "resOriginalAlt", "resOriginalVidCompl", "resJPEGFull",
             "resJPEGMed", "resJPEGThumb", "resVidMed", "resVidSmall", "resJPEGLarge"}
    for prefix, size, reason in (("resOriginalAlt", Asset.ALTERNATIVE, "raw"),
                                 ("resJPEGFull", Asset.ADJUSTED, "adjusted")):
        hinted = any(k.startswith(prefix) and v.get("value") for rec in records for k, v in rec.items())
        if size in versions or hinted:
            expected.add(resource_role(size))
        if hinted and size not in versions:
            missing.append(reason)
    suffixes = ("Res", "FileType", "Fingerprint", "Width", "Height")
    def add_extra(rec, prefix, source):
        role = source + "-" + prefix
        expected.add(role)
        version = described_resource(rec, prefix)
        if version is None:
            missing.append("auxiliary")
            return
        ext = ITEM_TYPE_EXTENSIONS.get(version.type, "bin")
        files.append((role, version, f"{role}.{ext}", False))

    for source, rec in zip(("asset", "master"), records):
        prefixes = {key[:-len(suffix)] for key, field in rec.items() if key.startswith("res") and field.get("value")
                    for suffix in suffixes if key.endswith(suffix)}
        for prefix in sorted(prefixes - known):
            if prefix in ("resSidecar", "resVidFull"):
                add_extra(rec, prefix, source)
            else:
                expected.add(source + "-" + prefix)
                missing.append("auxiliary")
    for prefix in ("resOriginal", "resOriginalAlt", "resOriginalVidCompl", "resJPEGFull"):
        values = [rec.get(prefix + "Res", {}).get("value") for rec in records]
        if all(isinstance(value, dict) for value in values) and values[0].get("fileChecksum") != values[1].get("fileChecksum"):
            # The pinned downloader prefers the asset field. Preserve the other resource independently.
            add_extra(records[1], prefix, "master")
            missing.append("conflicting-resource")
    edited = any(fields.get(key, {}).get("value") not in (None, "", 0, "0")
                 for key in ("adjustmentType", "adjustmentRenderType", "adjustmentSimpleDataEnc"))
    if edited and (not fields.get("adjustmentSimpleDataEnc", {}).get("value")
                   or not (Asset.ADJUSTED in versions or any("resVidFullRes" in rec for rec in records))):
        missing.append("edit")
    names = [filename.casefold() for _, _, filename, _ in files]
    if len(names) != len(set(names)) or any(n in (".identity.json", "manifest.json", ".required-roles.json",
                                                        ".icloud-work", ".icloud-previous")
                                            or n.startswith(".manifest-") or n + ".xmp" in names for n in names):
        raise IntegrityError("local-path")
    return files, list(dict.fromkeys(missing)), expected


def check_options(folder_structure, primary_sizes, force_size, set_exif_datetime, skip_live_photos,
                  live_photo_size, dry_run, file_match_policy, raw_policy):
    from pyicloud_ipd.version_size import AssetVersionSize as Asset, LivePhotoVersionSize as Live
    if (dry_run or skip_live_photos or set_exif_datetime or force_size
            or live_photo_size != Live.ORIGINAL or set(primary_sizes) != {Asset.ORIGINAL, Asset.ALTERNATIVE, Asset.ADJUSTED}
            or folder_structure != "{:%Y/%m}" or file_match_policy.value != "name-id7"
            or raw_policy.value != "as-is"):
        raise RuntimeError("Unexpected archive options")


def present(path, size=None):
    """Check regular-file metadata only. Generated sidecars have no remote size."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > 0 and (size is None or info.st_size == size)


def resource_role(size):
    return size if type(size) is str else type(size).__name__ + "." + size.name


def remember_requirements(folder, manifest, expected):
    """Remember disclosed originals before an interruption or a later sparse cloud response."""
    from pyicloud_ipd.version_size import AssetVersionSize as Asset, LivePhotoVersionSize as Live

    path = folder / ".required-roles.json"
    if path.is_symlink():
        raise IntegrityError("local-path")
    try:
        roles = json.loads(path.read_bytes())
    except FileNotFoundError:
        roles = []
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise IntegrityError("inventory")
    recorded = set(roles) | {resource_role(Asset.ORIGINAL)}
    known = recorded.copy()
    if manifest:
        previews = {resource_role(s) for s in (Asset.MEDIUM, Asset.THUMB, Live.MEDIUM, Live.THUMB)}
        archived = {r["role"] for r in manifest["resources"]} - previews
        known.update(archived)
        if manifest["status"] == "verified":
            recorded.update(archived)  # Verified manifests are preserved on a failed retry.
        else:
            # Partial manifests from older releases can already prove these roles are required.
            if str(manifest["metadata"].get("assetSubtypeV2", {}).get("value")) == "2":
                known.add(resource_role(Live.ORIGINAL))
            originals = {"live": Live.ORIGINAL, "raw": Asset.ALTERNATIVE, "adjusted": Asset.ADJUSTED}
            known.update(resource_role(originals[reason]) for reason in manifest["missing"] if reason in originals)
    required = known | expected
    if required - recorded:
        catalog.write_atomic(path, catalog.encoded(sorted(required)))
    return known - expected


def preserves_resources(previous, files, metadata):
    """A repeat observation of the same revision must cover each known original and sidecar."""
    sidecar_hash = hashlib.sha256(metadata).hexdigest() if metadata is not None else None
    described = {(resource_role(size), name): (version.size, signature(version).hex(), version.type,
                                                sidecar_hash if sidecar else None)
                 for size, version, name, sidecar in files}
    return all(described.get((r["role"], r["name"])) ==
               (r["bytes"], r["appleChecksum"], r["type"], r.get("xmpSHA256")) for r in previous["resources"])


class StrictArchive:
    """The pinned CLI supplies its resolved naming policy, session, counter and asset."""

    def __init__(self, inventory=None, zone="PrimarySync"):
        self.failed = False
        self.inventory = inventory
        self.zone = zone

    def issue(self, reason, name):
        self.failed = True
        # JSON escapes embedded newlines in Apple filenames. No record, URL, or
        # download exception is exposed to the parent process.
        printable = "".join(c if c.isprintable() else "_" for c in name[:255])
        print(PROTOCOL + json.dumps([reason, printable], ensure_ascii=True), flush=True)

    def _archive_resource(self, folder, month, size, version, filename, metadata,
                          icloud, photo, logger, filename_builder):
        from . import retention
        from icloudpd import download

        path = folder / filename
        expected = signature(version)
        # A full scan must attribute matching legacy files even when the new file exists.
        legacy = None
        if self.inventory is not None:
            legacy = retention.match_legacy(self.inventory, month, filename, version.size, expected)

        def fetch(stage):
            source = legacy
            if self.inventory is None:
                source = retention.match_legacy(None, month, filename, version.size, expected)
            if source is not None:
                space.check(Path(stage).parent, version.size)
                shutil.copyfile(source, stage)
                return True
            transfer_size = size
            if isinstance(size, str):
                from pyicloud_ipd.version_size import AssetVersionSize
                transfer_size = AssetVersionSize.ORIGINAL
            return download.download_media(logger, False, icloud, photo, stage,
                                           version, transfer_size, filename_builder)

        content_hash = hashlib.sha256()
        written = publish_verified(path, version, fetch, existing_hash=content_hash)
        if written:
            download.update_mtime(photo.created, str(path))
            content_hash = hashlib.sha256()
            if not matches(path, version.size, expected, durable=True, content_hash=content_hash):
                raise IntegrityError("mismatch")
            logger.info("Downloaded verified media")
        entry = dict(role=resource_role(size), name=filename, bytes=version.size, appleChecksum=expected.hex(),
                     type=version.type, sha256=content_hash.hexdigest())
        if metadata is not None:
            catalog.write_atomic(path.with_name(filename + ".xmp"), metadata)
            retention.match_legacy_xmp(self.inventory, month, filename + ".xmp", metadata)
            entry["xmpSHA256"] = hashlib.sha256(metadata).hexdigest()
        return written, entry

    def __call__(self, logger, folder_structure, directory, primary_sizes, force_size,
                 only_print_filenames, set_exif_datetime, skip_live_photos, live_photo_size,
                 dry_run, file_match_policy, xmp_sidecar, lp_filename_generator,
                 filename_builder, raw_policy, icloud, counter, photo):
        from . import retention
        from icloudpd import base
        from tzlocal import get_localzone

        check_options(folder_structure, primary_sizes, force_size, set_exif_datetime, skip_live_photos,
                      live_photo_size, dry_run, file_match_policy, raw_policy)
        if only_print_filenames:
            raise RuntimeError("Unexpected archive options")
        name = ""
        item_key = None
        locks = ExitStack()
        try:
            item = catalog.identity(icloud, self.zone, photo)
            item_key = catalog.token([self.zone, item["asset"]])
            name = filename_builder(photo)
            folder = Path(directory)
            # Shared library folders are validated by the worker. Reject a link
            # introduced inside the target, including in a previously used year/month.
            if not folder.is_absolute() or folder.resolve() != folder:
                raise IntegrityError("local-path")
            folder.mkdir(parents=True, exist_ok=True)
            created = photo.created.astimezone(get_localzone())
            for component in Path(folder_structure.format(created)).parts:
                folder = folder / component
                make_directory(folder)
            files, missing, expected = renditions(photo, name, primary_sizes, lp_filename_generator)
            rev = catalog.revision(photo)
            month, root = folder, Path(directory)
            folder = catalog.layout(month, item, rev)
            make_directory(folder.parent)
            make_directory(folder)
            locks.enter_context(catalog.claim(folder, item, rev))
            previous = catalog.read_manifest(folder, item, rev)
            missing_roles = remember_requirements(folder, previous, expected)
            verified = previous if previous and previous["status"] == "verified" else None
            metadata = (catalog.xmp_bytes(photo._asset_record)
                        if xmp_sidecar and any(sidecar for _, _, _, sidecar in files) else None)
            conflict = verified is not None and not preserves_resources(verified, files, metadata)
            if missing_roles or conflict:
                missing.append("archived-resource")
            if conflict:
                files = []  # Keep the established revision intact until the cloud records agree.
            resources = []
            for reason in missing:
                self.issue(reason, name)
            complete = not missing
            written = []
            for size, version, filename, needs_sidecar in files:
                path = folder / filename
                try:
                    new_file, entry = self._archive_resource(
                        folder, month, size, version, filename, metadata if needs_sidecar else None,
                        icloud, photo, logger, filename_builder)
                    if new_file:
                        written.append(str(path))
                    resources.append(entry)
                except (IntegrityError, OSError) as exc:
                    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                        raise space.LowSpace(shutil.disk_usage(folder).free, space.RESERVE) from exc
                    reason = str(exc) if isinstance(exc, IntegrityError) else "io"
                    self.issue(reason, filename)
                    missing.append(reason)
                    complete = False
            original_name = photo.calculate_filename() or name
            manifest = catalog.save_manifest(folder, root, photo, item, rev, original_name, resources, missing, verified)
            if self.inventory is not None:
                retention.record_asset(self.inventory, folder, manifest, complete=complete)
            if complete and not written:
                counter.increment()
            else:
                counter.reset()
            print(PROTOCOL + f"item {int(complete)} {item_key}", flush=True)
            return base.DownloadResult(downloaded=bool(written), present=complete, created_paths=tuple(written))
        except space.LowSpace as exc:
            print(PROTOCOL + f"space {exc.free} {exc.required}", flush=True)
            raise SystemExit(73) from exc
        except (IntegrityError, KeyError, IndexError, OSError, TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                print(PROTOCOL + f"space {shutil.disk_usage(directory).free} {space.RESERVE}", flush=True)
                raise SystemExit(73) from exc
            if not item_key:
                record = getattr(photo, "_asset_record", {}) or {}
                item_key = catalog.token([self.zone, record.get("recordName") or os.urandom(16).hex()])
            print(PROTOCOL + f"item 0 {item_key}", flush=True)
            self.issue(str(exc) if isinstance(exc, IntegrityError) else "record", name)
            counter.reset()
            return base.NOT_DOWNLOADED
        finally:
            locks.close()


def main():
    import sys
    from contextlib import closing
    import sqlite3
    args = sys.argv[1:]
    if args[:1] == ["--verify-archive"]:
        from .retention import verify_report
        if len(args) != 3:
            print("用法：icloudpd --verify-archive 还原目录 完整扫描报告.jsonl")
            return 2
        try:
            count = verify_report(Path(args[1]), Path(args[2]))
        except (OSError, ValueError, KeyError, TypeError, IntegrityError):
            print("还原核对失败，报告不完整或备份文件的身份、内容、元数据与报告不符。")
            return 1
        print(f"还原核对通过，共 {count} 个图库项目。")
        return 0
    # This release can preview retention policies. The old mutating entry point is retired.
    if any(arg.split("=", 1)[0] in ("--fn-cleanup-apply", "--delete-after-download", "--auto-delete",
                                    "--keep-icloud-recent-days") for arg in args):
        return 2
    zone = args[args.index("--library") + 1] if "--library" in args else "PrimarySync"
    if args[:1] == ["--fn-archive-record"]:
        path = args[1]
        del sys.argv[1:3]
        with closing(sqlite3.connect(path)) as db:
            status = run_cli(StrictArchive(db, zone))
            db.commit()  # Incomplete items also belong in migration reports.
            return status
    return run_cli(StrictArchive(zone=zone))


def run_cli(archive):
    from icloudpd import base, cli
    from pyicloud_ipd.services.photos import PhotosService
    run = cli.run_with_configs
    def run_read_only(global_config, users):
        if len(users) != 1:
            return 2
        user = users[0]
        if (user.delete_after_download or user.auto_delete or user.prune_orphans
                or user.keep_icloud_recent_days is not None):
            return 2
        archive.zone = user.library or "PrimarySync"
        return run(global_config, users)

    cli.run_with_configs = run_read_only
    discover = PhotosService._fetch_libraries
    PhotosService._fetch_libraries = catalog.discover_libraries
    upstream = base.download_builder
    base.download_builder = archive
    try:
        status = cli.cli()
        print(PROTOCOL + "checked", flush=True)
        return status or int(archive.failed)
    finally:
        base.download_builder = upstream
        PhotosService._fetch_libraries = discover
        cli.run_with_configs = run
