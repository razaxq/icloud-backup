"""Portable, versioned manifests stored alongside the original resources."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def token(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def required(value):
    if not isinstance(value, str) or not value:
        raise ValueError("missing asset identity")
    return value


def discover_libraries(service, scope):
    """The CLI selects libraries by name. Reject ambiguous names before it drops an owner."""
    from pyicloud_ipd.services.photos import PhotoLibrary
    endpoint = service.get_service_endpoint(scope)
    response = service.session.post(endpoint + "/zones/list", data="{}", headers={"Content-type": "text/plain"})
    response.raise_for_status()
    zones = {}
    for entry in response.json()["zones"]:
        if entry.get("deleted"):
            continue
        zone = entry["zoneID"]
        name = required(zone["zoneName"])
        if name in zones and zones[name] != zone:
            raise ValueError("ambiguous library name")
        zones[name] = zone
    return {name: PhotoLibrary(endpoint, service.params, service.session, zone, scope) for name, zone in zones.items()}


def identity(client, library_name, photo):
    dsid = client.params.get("dsid")
    if isinstance(dsid, bool) or not isinstance(dsid, (int, str)) or not str(dsid):
        raise ValueError("missing authenticated account")
    domain = required(client.domain)
    if domain not in ("com", "cn"):
        raise ValueError("unknown service")
    account = token([domain, str(dsid)])
    private = client.photos.private_libraries
    library = private.get(library_name)
    if library is None:
        library = client.photos.shared_libraries[library_name]
    elif library_name != "PrimarySync" and library_name in client.photos.shared_libraries:
        raise ValueError("ambiguous library name")
    zone = library.zone_id
    scope = library.library_type
    if zone.get("zoneName") != library_name or scope not in ("private", "shared"):
        raise ValueError("library scope mismatch")
    record = photo._asset_record
    owner = zone.get("ownerRecordName")
    if scope == "private" and not owner:
        owner = record.get("zoneID", {}).get("ownerRecordName") or "_defaultOwner"
    for source in (record, photo._master_record):
        returned = source.get("zoneID", {})
        if (returned.get("zoneName", library_name) != library_name
                or returned.get("ownerRecordName", owner) != owner):
            raise ValueError("record scope mismatch")
    return dict(account=account, database=scope, zone=library_name,
                owner=required(owner), asset=required(record.get("recordName")))


def revision(photo):
    asset, master = photo._asset_record, photo._master_record
    return dict(assetTag=required(asset.get("recordChangeTag")),
                master=required(master.get("recordName")), masterTag=required(master.get("recordChangeTag")))


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def publish_file(path):
    """Publish an archive file with the same directory ACL and umask as the media."""
    if path.is_symlink() or path.parent.resolve(strict=True) != path.parent:
        raise OSError("unsafe archive path")
    tmp = path.parent / (".archive-pending-" + secrets.token_hex(16))
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        with os.fdopen(fd, "wb") as out:
            yield out
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def write_atomic(path, data):
    if path.is_symlink() or path.parent.resolve(strict=True) != path.parent:
        raise OSError("unsafe archive path")
    if path.exists():
        if not path.is_file():
            raise OSError("invalid destination")
        if path.stat().st_size == len(data):
            with path.open("rb") as current:
                if current.read() == data:
                    os.fsync(current.fileno())
                    return
    with publish_file(path) as out:
        out.write(data)


def layout(month, item, version):
    return month / token(item) / token(version)


@contextmanager
def claim(folder, item, version):
    """Lock this revision while installing or repairing files and committing its manifest."""
    import fcntl
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = encoded({"identity": item, "revision": version})
        path = folder / ".identity.json"
        if path.is_symlink():
            raise ValueError("archive ownership conflict")
        if path.exists():
            if not path.is_file() or path.read_bytes() != data:
                raise ValueError("archive ownership conflict")
        else:
            if any(not entry.name.startswith(".archive-pending-") for entry in folder.iterdir()):
                raise ValueError("unclaimed archive directory")
            write_atomic(path, data)
        yield
    finally:
        os.close(fd)


def xmp_bytes(record):
    from icloudpd.xmp_sidecar import build_metadata, generate_xml
    from xml.etree import ElementTree
    return ElementTree.tostring(generate_xml(build_metadata(record)), encoding="utf-8", xml_declaration=True)


def read_manifest(folder, item, revision):
    path = folder / "manifest.json"
    if path.is_symlink():
        raise ValueError("invalid manifest path")
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or data.get("status") not in ("verified", "incomplete"):
        raise ValueError("invalid archive manifest")
    if data.get("identity") != item or data.get("revision") != revision:
        raise ValueError("manifest ownership conflict")
    return data


def save_manifest(folder, root, photo, item, version, original_name, resources, missing, previous=None):
    if previous and missing:
        return previous
    record = photo._asset_record
    # Preserve Apple-supplied editing metadata. Never store sessions, signed URLs or wrapping keys.
    fields = record["fields"]
    metadata = {k: fields[k] for k in ("adjustmentType", "adjustmentRenderType", "adjustmentSimpleDataEnc",
                                        "adjustedMediaMetaDataEnc", "assetDate", "isFavorite", "isHidden",
                                        "orientation", "assetSubtypeV2", "timeZoneOffset", "burstId")
                if k in fields and isinstance(fields[k].get("value"), (str, int, float, bool))}
    data = dict(format=1, identity=item, revision=version, originalName=original_name,
                metadata=metadata, resources=resources, missing=sorted(set(missing)),
                status="incomplete" if missing else "verified")
    if previous and data != previous:
        # Older full-scan reports refer to the digest of the manifest they saw.
        write_atomic(folder / f".manifest-{token(previous)}.json", encoded(previous))
    write_atomic(folder / "manifest.json", encoded(data))
    for parent in folder.parents:
        if not parent.is_relative_to(root):
            break
        fsync_dir(parent)
    return data
