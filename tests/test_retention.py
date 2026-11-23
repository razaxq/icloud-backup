"""Offline checks for portable archives, legacy migration and read-only policy previews."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_strict_media as media
from worker import retention, strict_cli


@unittest.skipUnless(importlib.util.find_spec("icloudpd"), "requires packaged icloudpd")
class ReviewTests(unittest.TestCase):
    setUp = media.AssetIntegrityTests.setUp
    provide = media.AssetIntegrityTests.provide
    archive_one = media.AssetIntegrityTests.archive_one

    def inventory(self):
        path = self.folder / "inventory.sqlite"
        retention.create_inventory(path, self.folder, [("PrimarySync", self.folder)])
        self.db = sqlite3.connect(path)
        self.addCleanup(self.db.close)
        self.archive = strict_cli.StrictArchive(self.db, "PrimarySync")
        return path

    def test_duplicate_assets_accounts_edits_and_cold_restore(self):
        from worker import catalog
        import shutil
        import tempfile
        db_path = self.inventory()
        self.provide(self.Asset.ORIGINAL, b"same bytes", "public.heic")
        for account, asset, tag in (("a", "copy1", "v1"), ("a", "copy2", "v1"), ("b", "copy1", "v1")):
            self.client.params["dsid"] = account
            self.photo._asset_record.update(recordName=asset, recordChangeTag=tag)
            self.assertTrue(self.archive_one().present)
            self.assertTrue(self.archive_one().present)
        self.assertEqual(self.db.execute("select count(*) from assets").fetchone(), (3,))
        self.db.commit()
        retention.publish_review(db_path,self.folder,"identities",retention.policy(),True)
        audit = [json.loads(row) for row in (self.folder/".icloud-archive/identities.jsonl").read_text().splitlines()]
        expected = {row["manifest"]:row["manifestSHA256"] for row in audit if row["kind"]=="asset"}
        self.archive = strict_cli.StrictArchive()
        self.photo._asset_record["recordChangeTag"] = "v2"
        self.provide(self.Asset.ORIGINAL, b"edited", "public.heic")
        self.assertTrue(self.archive_one().present)
        with tempfile.TemporaryDirectory() as tmp:
            restore = Path(tmp)
            shutil.copytree(self.folder / "2020", restore / "2020")
            manifests = sorted(restore.rglob("manifest.json"))
            self.assertEqual(len(manifests), 4)
            keys = set()
            for p in manifests:
                m = json.loads(p.read_bytes())
                keys.add((catalog.token(m["identity"]), catalog.token(m["revision"])))
                relative = str(p.relative_to(restore))
                if relative in expected:
                    retention.verify_manifest(restore, relative, expected.pop(relative))
            self.assertEqual(expected, {})
            report = self.folder/".icloud-archive/identities.jsonl"
            self.assertEqual(retention.verify_report(restore, report), 3)
            first = next(row for row in audit if row["kind"] == "asset")
            archived = restore / first["manifest"]
            movie = next(archived.parent.glob("*.HEIC"))
            movie.write_bytes(b"corrupted")
            with self.assertRaises(strict_cli.IntegrityError):
                retention.verify_report(restore, report)
            self.assertEqual(len(keys), 4)

    def test_legacy_previous_copy_and_unassigned_files_are_preserved(self):
        old = self.folder / "2020/04"
        old.mkdir(parents=True)
        source = old / ".icloud-previous/old/IMG_1234567.HEIC"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original")
        orphan = old / "unassigned.MOV"
        orphan.write_bytes(b"unknown")
        path = self.inventory()
        self.provide(self.Asset.ORIGINAL, b"original", "public.heic")
        self.payloads.clear()  # Any network transfer would fail the test.
        self.assertTrue(self.archive_one().present)
        self.db.commit()
        expected = (1, 0, 1, 1)
        for _ in range(2):
            self.assertEqual(retention.publish_review(path, self.folder, "test", retention.policy("immediate"), True),
                             expected)
        self.assertEqual(source.read_bytes(), b"original")
        self.assertEqual(orphan.read_bytes(), b"unknown")
        report = [json.loads(line) for line in (self.folder/".icloud-archive/test.jsonl").read_text().splitlines()]
        self.assertEqual([r["path"] for r in report if r.get("status")=="unassigned"], ["2020/04/unassigned.MOV"])
        target = self.folder / next(r["manifest"] for r in report if r["kind"] == "candidate")
        (target.parent / "IMG_1234567.HEIC").write_bytes(b"modified")
        with self.assertRaises(strict_cli.IntegrityError):
            retention.publish_review(path, self.folder, "test", retention.policy("immediate"), True)
        self.assertEqual(source.read_bytes(), b"original")

    def test_auxiliary_download_raw_hint_and_unknown_roles(self):
        self.inventory()
        self.provide(self.Asset.ORIGINAL, b"original", "public.heic")
        f = self.photo._asset_record["fields"]
        v = media.resource(b"apple sidecar")
        f.update(resSidecarRes={"value": {"size":v.size,"fileChecksum":v.checksum,"downloadURL":"offline-sidecar"}},
                 resSidecarFileType={"value":"public.data"})
        self.payloads[self.Asset.ORIGINAL] = b"original"
        self.payloads["offline-sidecar"] = b"apple sidecar"
        self.assertTrue(self.archive_one().present)
        self.assertEqual(next(self.folder.rglob("asset-resSidecar.bin")).read_bytes(), b"apple sidecar")
        self.photo._asset_record["recordChangeTag"] = "v2"
        self.archive = strict_cli.StrictArchive()
        f["resOriginalAltFileType"] = {"value":"com.adobe.raw-image"}
        f["resUnknownFileType"] = {"value":"unknown"}
        self.assertFalse(self.archive_one().present)
        m, = [json.loads(p.read_bytes()) for p in self.folder.rglob("manifest.json")
              if json.loads(p.read_bytes())["revision"]["assetTag"] == "v2"]
        self.assertEqual(set(m["missing"]), {"raw", "auxiliary"})
        self.assertIn("IMG_1234567.HEIC", [r["name"] for r in m["resources"]])

    def test_shared_zone_owner_and_partial_scans(self):
        from worker import catalog
        self.inventory()
        zone = "SharedSync-ABC"
        lib = SimpleNamespace(zone_id={"zoneName":zone,"ownerRecordName":"first"}, library_type="shared")
        self.client.photos.shared_libraries[zone] = lib
        first = catalog.identity(self.client, zone, self.photo)
        lib.zone_id["ownerRecordName"] = "second"
        self.assertNotEqual(first, catalog.identity(self.client, zone, self.photo))
        del lib.zone_id["ownerRecordName"]
        with self.assertRaises(ValueError):
            catalog.identity(self.client, zone, self.photo)
        path = self.folder/"inventory.sqlite"
        self.provide(self.Asset.ORIGINAL, b"still", "public.heic")
        self.provide(self.Live.MEDIUM, b"preview", "com.apple.quicktime-movie")
        self.assertFalse(self.archive_one().present)
        self.db.commit()
        self.assertEqual(retention.publish_review(path,self.folder,"test",retention.policy("immediate"),False),
                         (0,1,0,0))

    def test_retired_apply_command_cannot_authenticate_or_change_cloud(self):
        argv = ["icloudpd","--fn-cleanup-apply","stale.sqlite","/old","{}","--auth-only"]
        with patch.object(sys,"argv",argv), patch("icloudpd.base.authenticator") as auth:
            self.assertNotEqual(strict_cli.main(),0)
            auth.assert_not_called()

    def test_retention_counts_items_across_selected_libraries(self):
        self.inventory()
        for name,zone,date in (("old","PrimarySync",1),("middle","PrimarySync",2),
                               ("new","SharedSync-A",3),("undated","PrimarySync",None)):
            self.db.execute("insert into assets values(?,?,?,?,?,1)", (name,zone,date,name,"hash"))
        def selected(rule):
            return {name for name,_ in retention.candidates(self.db,rule,86400.002)}
        self.assertEqual(selected(retention.policy("latest",1)), {"old"})
        self.assertEqual(selected(retention.policy("latest",1,True)), {"old","middle"})
        self.assertEqual(selected(retention.policy("days",1)), {"old"})
        self.assertEqual(selected(retention.policy()), set())
