"""Per-asset archiving for the bundled icloudpd 1.33.0 CLI."""

import base64
import binascii
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


def matches(path, size, expected):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise IntegrityError("local-path")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != size:
            return False
        digest = hashlib.sha1(SALT, usedforsecurity=False)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        same_file = os.path.samestat(after, path.lstat())
    return (same_file and before.st_ctime_ns == after.st_ctime_ns and before.st_mtime_ns == after.st_mtime_ns
            and digest.digest() == expected[1:])


def make_directory(path):
    path.mkdir(exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise IntegrityError("local-path")


def publish_verified(path, version, fetch):
    """Return True for a new verified write. Keep interrupted transfers resumable."""
    expected = signature(version)
    if matches(path, version.size, expected):
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
        if not stage.exists() and not fetch(str(stage)):
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
                shutil.copy2(path, backup)
            except OSError:
                backup.unlink(missing_ok=True)
                backup.parent.rmdir()
                raise
    os.replace(stage, path)
    return True


def renditions(photo, name, primary_sizes, lp_filename_generator):
    """Choose originals, additional advertised resources and labelled fallback copies."""
    from pyicloud_ipd.asset_version import add_suffix_to_filename, calculate_version_filename
    from pyicloud_ipd.item_type import AssetItemType
    from pyicloud_ipd.utils import disambiguate_filenames
    from pyicloud_ipd.version_size import AssetVersionSize as Asset, LivePhotoVersionSize as Live

    versions = photo.versions
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
    missing = []
    previews = set()
    required = [(Asset.ORIGINAL, (Asset.MEDIUM, Asset.THUMB), "original")]
    if live:
        required.append((Live.ORIGINAL, (Live.MEDIUM, Live.THUMB), "live"))
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
    return files, missing


class StrictArchive:
    """The pinned CLI supplies its resolved naming policy, session, counter and asset."""

    def __init__(self):
        self.failed = False

    def issue(self, reason, name):
        self.failed = True
        # JSON escapes embedded newlines in Apple filenames. No record, URL, or
        # download exception is exposed to the parent process.
        printable = "".join(c if c.isprintable() else "_" for c in name[:255])
        print(PROTOCOL + json.dumps([reason, printable], ensure_ascii=True), flush=True)

    def __call__(self, logger, folder_structure, directory, primary_sizes, force_size,
                 only_print_filenames, set_exif_datetime, skip_live_photos, live_photo_size,
                 dry_run, file_match_policy, xmp_sidecar, lp_filename_generator,
                 filename_builder, raw_policy, icloud, counter, photo):
        from icloudpd import base, download
        from pyicloud_ipd.version_size import AssetVersionSize as Asset, LivePhotoVersionSize as Live
        from tzlocal import get_localzone

        if (dry_run or skip_live_photos or set_exif_datetime or only_print_filenames or force_size
                or live_photo_size != Live.ORIGINAL or set(primary_sizes) != {Asset.ORIGINAL, Asset.ALTERNATIVE, Asset.ADJUSTED}
                or folder_structure != "{:%Y/%m}" or file_match_policy.value != "name-id7"
                or raw_policy.value != "as-is"):
            raise RuntimeError("Unexpected archive options")
        name = ""
        try:
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
            files, missing = renditions(photo, name, primary_sizes, lp_filename_generator)
            for reason in missing:
                self.issue(reason, name)
            complete = not missing
            written = []
            for size, version, filename, needs_sidecar in files:
                path = folder / filename
                try:
                    def fetch(stage):
                        return download.download_media(logger, False, icloud, photo, stage,
                                                       version, size, filename_builder)
                    if publish_verified(path, version, fetch):
                        written.append(str(path))
                        download.update_mtime(photo.created, str(path))
                        logger.info("Downloaded verified media")
                    if xmp_sidecar and needs_sidecar:
                        sidecar = Path(str(path) + ".xmp")
                        if sidecar.is_symlink():
                            raise IntegrityError("local-path")
                        base.generate_xmp_file(logger, str(path), photo._asset_record, False)
                        if not sidecar.is_file() or not sidecar.stat().st_size:
                            raise IntegrityError("metadata")
                except (IntegrityError, OSError) as exc:
                    self.issue(str(exc) if isinstance(exc, IntegrityError) else "io", filename)
                    complete = False
            if complete and not written:
                counter.increment()
            else:
                counter.reset()
            return base.DownloadResult(downloaded=bool(written), present=complete, created_paths=tuple(written))
        except (IntegrityError, KeyError, IndexError, OSError, TypeError, ValueError, OverflowError) as exc:
            self.issue(str(exc) if isinstance(exc, IntegrityError) else "record", name)
            counter.reset()
            return base.NOT_DOWNLOADED


def main():
    from icloudpd import base, cli
    archive = StrictArchive()
    upstream = base.download_builder
    base.download_builder = archive
    try:
        status = cli.cli()
        print(PROTOCOL + "checked", flush=True)
        return status or int(archive.failed)
    finally:
        base.download_builder = upstream
