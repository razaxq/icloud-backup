"""Integrity and file pairing regressions; adapter tests use the packaged dependency."""
import base64
import datetime as dt
import hashlib
import importlib.util
import io
import json
import logging
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import strict_cli


def resource(data, uti="public.heic"):
    apple_digest = hashlib.sha1(b"com.apple.XattrObjectSalt\0com.apple.DataObjectSalt\0" + data).digest()
    return SimpleNamespace(size=len(data), checksum=base64.b64encode(b"\x01" + apple_digest).decode(),
                           type=uti, url="offline")


class FileIntegrityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "test.HEIC"
        self.original = b"source bytes"
        self.version = resource(self.original)

    def fetch(self, contents):
        def write(stage):
            Path(stage).write_bytes(contents)
            return True
        return write

    def test_only_verified_bytes_are_published(self):
        old = b"not original"
        self.assertEqual(len(old), len(self.original))
        self.path.write_bytes(old)
        with self.assertRaisesRegex(strict_cli.IntegrityError, "mismatch"):
            strict_cli.publish_verified(self.path, self.version, self.fetch(old))
        self.assertEqual(self.path.read_bytes(), old)
        self.assertTrue(strict_cli.publish_verified(self.path, self.version, self.fetch(self.original)))
        self.assertEqual(self.path.read_bytes(), self.original)
        backup, = (self.path.parent / ".icloud-previous").rglob("test.HEIC")
        self.assertEqual(backup.read_bytes(), old)
        self.assertFalse(strict_cli.publish_verified(self.path, self.version,
                                                    lambda _: self.fail("verified file downloaded again")))

    def test_space_for_original_is_checked_before_fetch(self):
        payload = b"original"
        fetch = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "original.HEIC"
            available = strict_cli.space.RESERVE + len(payload) - 1
            with patch.object(strict_cli.shutil, "disk_usage", return_value=SimpleNamespace(free=available)):
                with self.assertRaises(strict_cli.space.LowSpace) as stopped:
                    strict_cli.publish_verified(target, resource(payload), fetch)
            self.assertEqual(stopped.exception.required, strict_cli.space.RESERVE + len(payload))
            self.assertFalse(target.exists())
            fetch.assert_not_called()

    def test_unknown_apple_digest_or_bad_payload_fails_without_final_file(self):
        self.version.checksum = base64.b64encode(b"\x02" + b"Z" * 20).decode()
        with self.assertRaisesRegex(strict_cli.IntegrityError, "signature"):
            strict_cli.publish_verified(self.path, self.version, lambda _: self.fail("unverifiable transfer"))
        self.assertFalse(self.path.exists())
        with self.assertRaisesRegex(strict_cli.IntegrityError, "mismatch"):
            strict_cli.publish_verified(self.path, resource(self.original), self.fetch(b"short"))
        self.assertFalse(self.path.exists())

    def test_completed_partial_recovered_and_bad_partial_restarts(self):
        folder = self.path.parent / ".icloud-work"
        folder.mkdir()
        key = base64.b32encode(base64.b64decode(self.version.checksum)).decode()
        partial = folder / (key + ".part")
        partial.write_bytes(b"X" * len(self.original))
        def restart(stage):
            self.assertFalse(partial.exists())
            Path(stage).write_bytes(self.original)
            return True
        strict_cli.publish_verified(self.path, self.version, restart)
        self.path.unlink()
        partial.write_bytes(self.original)
        strict_cli.publish_verified(self.path, self.version, lambda _: self.fail("completed partial downloaded twice"))
        self.assertEqual(self.path.read_bytes(), self.original)


@unittest.skipUnless(importlib.util.find_spec("icloudpd"), "requires packaged icloudpd 1.33.0")
class AssetIntegrityTests(unittest.TestCase):
    def setUp(self):
        from pyicloud_ipd.version_size import AssetVersionSize as Asset, LivePhotoVersionSize as Live
        from pyicloud_ipd.item_type import AssetItemType

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name).resolve()
        self.Asset, self.Live = Asset, Live
        self.photo = SimpleNamespace(
            _asset_record={"recordName": "asset-1", "recordChangeTag": "asset-v1", "fields": {}},
            _master_record={"recordName": "master", "recordChangeTag": "master-v1", "fields": {}},
            calculate_filename=lambda: "IMG.HEIC", item_type=AssetItemType.IMAGE,
            versions={}, created=dt.datetime(2020, 4, 15, 12, tzinfo=dt.timezone.utc))
        library = SimpleNamespace(zone_id={"zoneName": "PrimarySync"}, library_type="private")
        self.client = SimpleNamespace(domain="com", params={"dsid": "account1"}, photos=SimpleNamespace(
            private_libraries={"PrimarySync": library}, shared_libraries={}))
        self.payloads = {}
        self.archive = strict_cli.StrictArchive()

    def provide(self, size, contents, uti):
        self.photo.versions[size] = resource(contents, uti)
        self.payloads[size] = contents

    def archive_one(self, counter=None):
        from icloudpd import base
        from icloudpd.counter import Counter
        from pyicloud_ipd.file_match import FileMatchPolicy
        from pyicloud_ipd.raw_policy import RawTreatmentPolicy
        def write(logger, dry_run, icloud, photo, path, version, size, builder):
            Path(path).write_bytes(self.payloads[version.url] if version.url in self.payloads else self.payloads[size])
            return True
        self.log = io.StringIO()
        with redirect_stdout(self.log), patch("icloudpd.download.download_media", side_effect=write):
            return self.archive(logging.getLogger("strict-test"), "{:%Y/%m}", str(self.folder),
                                [self.Asset.ORIGINAL, self.Asset.ALTERNATIVE, self.Asset.ADJUSTED], False,
                                False, False, False, self.Live.ORIGINAL, False, FileMatchPolicy.NAME_ID7,
                                True, base.lp_filename_original, lambda _: "IMG_1234567.HEIC", RawTreatmentPolicy.AS_IS,
                                self.client, Counter() if counter is None else counter, self.photo)

    def issues(self):
        return [json.loads(line[len(strict_cli.PROTOCOL):])[0] for line in self.log.getvalue().splitlines()
                if line.startswith(strict_cli.PROTOCOL + "[")]

    def test_original_live_pair_and_raw_companion_verified_on_disk(self):
        self.provide(self.Asset.ORIGINAL, b"full still", "public.heic")
        self.provide(self.Live.ORIGINAL, b"full movie", "com.apple.quicktime-movie")
        self.provide(self.Asset.ALTERNATIVE, b"camera raw", "com.adobe.raw-image")
        self.assertTrue(self.archive_one().present)
        month = next(self.folder.glob("2020/04/*/*/manifest.json")).parent
        for ext, data in (("HEIC", b"full still"), ("MOV", b"full movie"), ("DNG", b"camera raw")):
            self.assertEqual((month / f"IMG_1234567.{ext}").read_bytes(), data)
        self.assertFalse(self.archive_one().downloaded)
        self.assertFalse(self.archive.failed)

    def test_existing_media_is_hashed_once_per_scan(self):
        self.provide(self.Asset.ORIGINAL, b"still", "public.heic")
        self.assertTrue(self.archive_one().present)
        media, = self.folder.rglob("*.HEIC")
        with patch.object(strict_cli, "matches", wraps=strict_cli.matches) as reads:
            self.assertFalse(self.archive_one().downloaded)
        self.assertEqual(sum(call.args[0] == media for call in reads.call_args_list), 1)

    def test_only_lower_quality_movie_fails_and_preview_has_distinct_name(self):
        self.provide(self.Asset.ORIGINAL, b"full still", "public.heic")
        self.provide(self.Live.MEDIUM, b"smaller movie", "com.apple.quicktime-movie")
        self.assertFalse(self.archive_one().present)
        self.assertEqual(self.issues(), ["live"])
        self.assertFalse(list(self.folder.rglob("IMG_1234567.MOV")))
        self.assertEqual(next(self.folder.rglob("IMG_1234567-preview-medium.MOV")).read_bytes(), b"smaller movie")

    def test_live_flag_without_any_video_and_thumbnail_only_still_fail(self):
        self.photo._asset_record["fields"]["assetSubtypeV2"] = {"value": 2}
        self.provide(self.Asset.THUMB, b"small still", "public.jpeg")
        self.assertFalse(self.archive_one().present)
        self.assertEqual(self.issues(), ["original", "live"])
        self.assertEqual(next(self.folder.rglob("IMG_1234567-preview-thumb.JPG")).read_bytes(), b"small still")
        self.assertFalse(list(self.folder.rglob("*.HEIC")))

    def test_mismatched_original_bytes_fail_even_if_the_filename_and_size_match(self):
        self.provide(self.Asset.ORIGINAL, b"original", "public.heic")
        self.payloads[self.Asset.ORIGINAL] = b"tampered"
        self.assertFalse(self.archive_one().present)
        self.assertEqual(self.issues(), ["mismatch"])
        self.assertFalse(list(self.folder.rglob("*.HEIC")))

    def test_quick_check_counts_only_complete_previously_archived_assets(self):
        from icloudpd.counter import Counter

        counter = Counter()
        self.provide(self.Asset.ORIGINAL, b"still", "public.heic")
        self.provide(self.Live.ORIGINAL, b"movie", "com.apple.quicktime-movie")
        self.assertTrue(self.archive_one(counter).present)
        self.assertEqual(counter.value(), 0)
        self.assertTrue(self.archive_one(counter).present)
        self.assertEqual(counter.value(), 1)
        del self.photo.versions[self.Live.ORIGINAL]
        self.photo._asset_record["fields"]["assetSubtypeV2"] = {"value": 2}
        self.assertFalse(self.archive_one(counter).present)
        self.assertEqual(counter.value(), 0)
