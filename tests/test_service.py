"""Integration tests for the gateway service: accounts, scheduling, recovery, notifications."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
import archive as archive_module  # noqa: E402
from support import PREFIX, PROJECT, ServiceCase, UnixConnection  # noqa: E402
from worker.engine import session_path  # noqa: E402


class Hook:
    """A local webhook receiver."""

    def __init__(self):
        self.received = queue.Queue()
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.put(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook/secret-token"

    def get(self, timeout=10):
        return self.received.get(timeout=timeout)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ServiceTests(ServiceCase):
    def seed_session(self, account="test@example.com", domain="com"):
        cookie = session_path(self.state_dir / "sessions", domain, account)
        cookie.mkdir(parents=True, exist_ok=True)
        (cookie / "session").write_text("fixture")
        return cookie

    def test_manual_run_history_and_repeat(self):
        account_id = self.add_account()
        self.assertEqual(self.call("/api/start", {"id": account_id})[0], 200)
        self.assertEqual(self.call("/api/start", {"id": account_id})[0], 409)
        self.unlock()
        job = self.wait_job("success")
        self.assertIn("4 个", job["message"])
        self.assertTrue(job["full_scan"])
        self.assertEqual((self.dest / "2026/01/test-0.MOV").read_bytes(), b"fixture-MOV")
        self.assertEqual(self.call("/api/start", {"id": account_id})[0], 200)
        self.assertIn("0 个", self.wait_job("success")["message"])
        state = self.state()
        self.assertEqual([item["status"] for item in state["history"]], ["success", "success"])
        self.assertIsNotNone(state["accounts"][0]["last_success"])
        self.stop_service()
        stored = (self.state_dir / "archive.json").read_text()
        self.assertNotIn("test-secret", stored)
        self.assertNotIn("987654", stored)

    def test_sms_code_choice(self):
        self.control({"sms": True})
        account_id = self.add_account()
        self.call("/api/start", {"id": account_id})
        job = self.wait_job("waiting_password")
        self.call("/api/input", {"value": "test-secret", "prompt_id": job["prompt_id"]})
        job = self.wait_job("waiting_code")
        self.assertIn("a（(•••) •••-••12）", job["message"])
        self.call("/api/input", {"value": "a", "prompt_id": job["prompt_id"]})
        for _ in range(200):
            next_job = self.state()["job"]
            if next_job["status"] == "waiting_code" and next_job["prompt_id"] != job["prompt_id"]:
                break
            time.sleep(0.03)
        self.assertIn("短信", next_job["message"])
        self.call("/api/input", {"value": "987654", "prompt_id": next_job["prompt_id"]})
        self.wait_job("success")

    def test_v1_state_is_migrated_and_due_run_is_caught_up(self):
        self.stop_service()
        self.state_dir.mkdir(exist_ok=True)
        (self.state_dir / "archive.json").write_text(json.dumps(dict(
            config=dict(account="test@example.com", domain="com", destination=str(self.dest), interval_hours=24),
            next_due=time.time() - 60, job=dict(status="success", message="old", updated_at=None),
            logs=["old line"])))
        self.seed_session()
        self.start_service()
        job = self.wait_job("success")
        self.assertEqual(job["trigger"], "schedule")
        self.assertTrue(job["full_scan"], "no full scan recorded yet")
        state = self.state()
        self.assertEqual(state["accounts"][0]["interval_hours"], 24)
        self.assertIsNotNone(state["accounts"][0]["next_run"])
        self.assertEqual(json.loads((self.state_dir / "archive.json").read_text())["version"], archive_module.STATE_VERSION)

    def test_scheduled_runs_are_incremental_between_weekly_full_scans(self):
        account_id = self.add_account(interval_hours=1)
        self.seed_session()
        self.stop_service()
        record = json.loads((self.state_dir / "archive.json").read_text())
        record["accounts"][0].update(next_due=time.time() - 1, last_full_scan=time.time() - 3600)
        (self.state_dir / "archive.json").write_text(json.dumps(record))
        self.start_service()
        job = self.wait_job("success")
        self.assertFalse(job["full_scan"])
        self.assertIn("快速检查", job["message"])
        self.assertTrue(all("--until-found" in call for call in self.calls() if "--library" in call))
        account = self.state()["accounts"][0]
        self.assertEqual(account["id"], account_id)
        self.assertIsNotNone(account["next_run"])

    def test_missing_live_movie_fails_quick_scan_then_next_schedule_repairs_it(self):
        account_id = self.add_account(interval_hours=1)
        self.seed_session()
        self.call("/api/start", {"id": account_id})
        self.wait_job("success")
        baseline = self.state()["accounts"][0]
        movie = self.dest / "2026/01/test-0.MOV"
        movie.unlink()  # simulate loss of one paired file inside this test's temporary archive

        def run_next(control):
            self.stop_service()
            path = self.state_dir / "archive.json"
            record = json.loads(path.read_text())
            record["accounts"][0]["next_due"] = time.time() - 1
            path.write_text(json.dumps(record))
            self.control(control)
            self.start_service()

        run_next({"missing_live": True})
        job = self.wait_job("error")
        self.assertEqual((job["trigger"], job["full_scan"]), ("schedule", False))
        self.assertTrue(movie.with_suffix(".HEIC").is_file())
        self.assertFalse(movie.exists())
        account = self.state()["accounts"][0]
        self.assertEqual((account["last_full_scan"], account["last_success"]),
                         (baseline["last_full_scan"], baseline["last_success"]))
        self.assertTrue(account["needs_full_scan"])
        run_next({})
        job = self.wait_job("success")
        self.assertEqual((job["trigger"], job["full_scan"]), ("schedule", True))
        self.assertFalse(self.state()["accounts"][0]["needs_full_scan"])
        self.assertEqual(movie.read_bytes(), b"fixture-MOV")

    def test_cancel_while_waiting(self):
        account_id = self.add_account()
        self.call("/api/start", {"id": account_id})
        self.wait_job("waiting_password")
        self.assertEqual(self.call("/api/cancel", {})[0], 200)
        self.wait_job("cancelled")
        self.assertFalse(any(self.dest.iterdir()))
        self.assertEqual(self.state()["history"][0]["status"], "cancelled")

    def test_service_stop_interrupts_and_resumes(self):
        self.control({"files": 6, "delay": 0.25})
        self.seed_session()
        account_id = self.add_account()
        self.call("/api/start", {"id": account_id})
        deadline = time.time() + 10
        while self.state()["job"]["downloaded"] < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.stop_service()
        saved = json.loads((self.state_dir / "archive.json").read_text())
        self.assertEqual(saved["job"]["status"], "interrupted")
        self.assertTrue(saved["accounts"][0]["resume"]["full"])
        self.control({"files": 6})
        self.start_service()
        job = self.wait_job("success")
        self.assertEqual(job["trigger"], "resume")
        self.assertTrue(job["full_scan"])
        self.assertEqual(len(list((self.dest / "2026/01").glob("test-*"))), 12)
        self.assertEqual([item["status"] for item in self.state()["history"]], ["success", "interrupted"])

    def test_crash_recovery_is_limited(self):
        account_id = self.add_account()
        self.seed_session()
        self.stop_service()
        record = json.loads((self.state_dir / "archive.json").read_text())
        record["job"].update(status="running", account_id=account_id, account="test@example.com",
                             full_scan=True, downloaded=3)
        (self.state_dir / "archive.json").write_text(json.dumps(record))
        self.start_service()
        job = self.wait_job("success")
        self.assertEqual(job["trigger"], "resume")
        self.assertEqual(self.state()["history"][1]["downloaded"], 3)

        self.stop_service()
        record = json.loads((self.state_dir / "archive.json").read_text())
        record["job"].update(status="running", account_id=account_id)
        record["accounts"][0]["crashes"] = 2
        (self.state_dir / "archive.json").write_text(json.dumps(record))
        self.start_service()
        state = self.state()
        self.assertEqual(state["job"]["status"], "interrupted")
        self.assertIn("暂停自动继续", state["job"]["message"])
        self.assertFalse(state["accounts"][0]["resume_pending"])

    def test_unwritten_files_and_failed_libraries(self):
        self.seed_session()
        self.control({"libraries": ["SharedSync-ABC"], "fail_hidden_shared": True, "xmp_error": True})
        account_id = self.add_account()
        self.call("/api/start", {"id": account_id})
        self.wait_job("error")
        self.assertTrue((self.dest / "共享图库/SharedSync-ABC/2026/01/test-0.HEIC").exists())
        self.assertIsNone(self.state()["accounts"][0]["last_success"])
        self.assertTrue(self.state()["accounts"][0]["needs_full_scan"])

        self.control({"fail_primary": True})
        self.call("/api/start", {"id": account_id})
        job = self.wait_job("error")
        self.assertIn("个人图库未能完成", job["message"])

    def test_accounts_are_independent(self):
        first = self.add_account()
        other = self.dest / "家人"
        second = self.add_account(account="family@example.com", destination=str(other), domain="cn")
        self.assertEqual(self.call("/api/accounts/save", dict(
            account="TEST@example.com", destination=str(self.dest), domain="com", interval_hours=0))[0], 400)
        self.seed_session("family@example.com", "cn")
        self.call("/api/start", {"id": second})
        self.wait_job("success")
        self.assertTrue((other / "2026/01/test-0.HEIC").exists())
        self.assertFalse((self.dest / "2026").exists())
        cookie = self.seed_session()
        self.assertEqual(self.call("/api/accounts/delete", {"id": first})[0], 200)
        self.assertFalse(cookie.exists())
        self.assertEqual([item["id"] for item in self.state()["accounts"]], [second])
        self.assertEqual(self.call("/api/start", {"id": first})[0], 404)

    def test_using_another_accounts_directory_requires_explicit_choice(self):
        self.add_account()
        alias = self.dest / "alias"
        alias.symlink_to(self.dest)
        payload = dict(account="family@example.com", destination=str(alias), domain="cn", interval_hours=1)
        status, body = self.call("/api/accounts/save", payload)
        self.assertEqual((status, body["code"]), (409, "destination_in_use"))
        second = self.add_account(**payload, allow_shared_destination=True)
        self.assertEqual(self.call("/api/accounts/save", dict(payload, id=second, interval_hours=6))[0], 200)
        self.assertEqual(self.call("/api/accounts/save", dict(payload, id=second, account="other@example.com"))[0], 409)

    def test_gateway_destination_and_browse(self):
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.call("/api/state", admin="false")[0], 403)
        self.assertEqual(self.call("/api/start", {}, origin="https://elsewhere.example")[0], 403)
        self.assertEqual(self.call("")[0], 302)
        self.assertIn("照片".encode(), self.call("/")[1])
        self.assertEqual(self.call("/server.py")[0], 404)
        self.assertEqual(self.call("/icon.png")[0], 200)
        self.assertEqual(self.call("/api/accounts/save", dict(
            account="a@example.com", destination=str(self.base / "elsewhere"), domain="com",
            interval_hours=0))[0], 400)
        (self.dest / "link").symlink_to(self.base)
        self.assertEqual(self.call("/api/accounts/save", dict(
            account="a@example.com", destination=str(self.dest / "link" / "x"), domain="com",
            interval_hours=0))[0], 400)
        self.add_account(destination=str(self.dest / "家庭照片"))

        status, roots = self.call("/api/browse")
        self.assertEqual((status, roots["entries"]), (200, [{"name": str(self.dest), "path": str(self.dest)}]))
        (self.dest / ".hidden").mkdir()
        (self.dest / "@eaDir").mkdir()
        status, listing = self.call("/api/browse?path=" + str(self.dest).replace("/", "%2F"))
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in listing["entries"]], ["家庭照片"])
        self.assertEqual(listing["parent"], "")
        self.assertEqual(self.call("/api/browse?path=" + str(self.base))[0], 403)
        self.assertEqual(self.call("/api/browse?path=" + str(self.dest / "link"))[0], 403)
        self.assertEqual(self.call("/api/browse?path=" + str(self.dest / "missing"))[0], 404)

    def test_serves_bundled_appearance_assets(self):
        for name, mime in (("inter.woff2", "font/woff2"),):
            with self.subTest(name=name):
                conn = UnixConnection(self.socket)
                self.addCleanup(conn.close)
                conn.request("GET", f"{PREFIX}/{name}", headers={"X-Trim-Isadmin": "true"})
                response = conn.getresponse()
                self.assertEqual((response.status, response.getheader("Content-Type")), (200, mime))
                self.assertEqual(response.read(), (PROJECT / "web/static" / name).read_bytes())
                self.assertEqual(response.getheader("Cache-Control"), "private, no-cache")
                conn.request("GET", f"{PREFIX}/{name}", headers={"X-Trim-Isadmin": "true",
                                                                  "If-None-Match": response.getheader("ETag")})
                cached = conn.getresponse()
                self.assertEqual((cached.status, cached.read()), (304, b""))
        self.assertEqual(self.call("/inter.woff2", admin="false")[0], 403)
        for retired in ("/forest.avif", "/lora.woff2"):
            self.assertEqual(self.call(retired)[0], 404)


class ArchiveConsistencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.dest = self.base / "photos"
        self.dest.mkdir()
        self.state_dir = self.base / "state"
        self.state_dir.mkdir()
        self.payload = dict(account="first@example.com", domain="com", destination=str(self.dest),
                            interval_hours=1, scan="auto", include_shared=True)

    def archive(self):
        service = archive_module.Archive(self.state_dir, "/unused", [self.dest], startup_grace=86400)
        self.addCleanup(service.close)
        return service

    def test_migration_preserves_history_but_requires_strict_full_verification(self):
        old_result = dict(account_id="account1", account="first@example.com", status="success", message="old")
        data = dict(version=2, accounts=[dict(self.payload, id="account1", last_success=1234,
                                              last_full_scan=1234, last_run=old_result, resume={"full": False})],
                    history=[old_result], job=old_result)
        (self.state_dir / "archive.json").write_text(json.dumps(data))
        archive = self.archive()
        account = archive.accounts[0]
        self.assertIsNone(account["last_success"])
        self.assertIsNone(account["last_full_scan"])
        self.assertTrue(archive._wants_full_scan(account, "resume", account["resume"]))
        for record in [archive.history[0], account["last_run"], archive.job]:
            self.assertEqual(record["status"], "unverified")
            self.assertIn("old", record["message"])
        saved = json.loads(archive.file.read_text())
        self.assertEqual(saved["version"], 2, "0.2.x can read the same envelope if the user rolls back")
        self.assertEqual(saved["verification_version"], archive_module.VERIFICATION_VERSION)

    def test_unknown_schema_is_preserved_byte_for_byte(self):
        data = b'{"version":99,"config":{},"accounts":[{"id":"keep"}]}'
        path = self.state_dir / "archive.json"
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "版本"):
            self.archive()
        self.assertEqual(path.read_bytes(), data)

    def test_failed_quick_scan_forces_full_until_success(self):
        archive = self.archive()
        account = archive.find(archive.save_account(self.payload))
        baseline = time.time() - 3600
        account.update(last_full_scan=baseline, last_success=baseline)
        self.assertFalse(archive._wants_full_scan(account, "schedule", None))
        result = mock.Mock(downloaded=0, warnings=[], failures=["私密照片名.HEIC 缺少原件。"])
        with mock.patch.object(archive_module, "run_sync", return_value=result), mock.patch.object(archive, "notify") as notify:
            archive.download(account, self.dest, False)
        self.assertIn("私密照片名", archive.job["message"])
        self.assertNotIn("私密照片名", notify.call_args.args[2])
        self.assertEqual(archive.job["status"], "error")
        self.assertTrue(archive._wants_full_scan(account, "schedule", None))
        self.assertEqual((account["last_success"], account["last_full_scan"]), (baseline, baseline))
        self.assertIsNotNone(account["next_due"])
        self.assertTrue(json.loads(archive.file.read_text())["accounts"][0]["needs_full_scan"])
        result.failures = []
        with mock.patch.object(archive_module, "run_sync", return_value=result):
            archive.download(account, self.dest, True)
        self.assertEqual(archive.job["status"], "success")
        self.assertFalse(archive._wants_full_scan(account, "schedule", None))
        self.assertGreater(account["last_full_scan"], baseline)

    def test_changing_scope_invalidates_only_new_accounts_scan_baseline(self):
        archive = self.archive()
        payload = dict(self.payload, id=archive.save_account(self.payload))
        for change in [dict(account="second@example.com"), dict(domain="cn"),
                       dict(destination=str(self.dest / "new")), dict(include_shared=False)]:
            with self.subTest(change=change):
                account = archive.find(payload["id"])
                account.update(last_full_scan=time.time(), last_success=time.time(), last_run={"status": "success"})
                payload.update(change)
                archive.save_account(payload)
                account = archive.find(payload["id"])
                self.assertIsNone(account["last_success"])
                self.assertIsNone(account["last_run"])
                self.assertTrue(archive._wants_full_scan(account, "schedule", None))
        account.update(last_full_scan=555, last_success=555, needs_full_scan=False)
        archive.save_account(dict(payload, interval_hours=6))
        self.assertEqual(archive.find(payload["id"])["last_success"], 555)

    def test_concurrent_edits_prevent_an_obsolete_scheduled_start(self):
        archive = self.archive()
        payload = dict(self.payload, id=archive.save_account(self.payload))
        validate = archive.destination
        slow_io, allow_return = threading.Event(), threading.Event()

        def check_destination(path):
            real = validate(path)
            if threading.current_thread().name.startswith("slow-volume"):
                slow_io.set()
                if not allow_return.wait(5):
                    raise TimeoutError("test did not release volume check")
            return real

        with mock.patch.object(archive, "destination", side_effect=check_destination), \
                mock.patch.object(archive_module, "run_sync") as run, ThreadPoolExecutor(1, thread_name_prefix="slow-volume") as pool:
            for change in [dict(destination=str(self.dest / "new")), dict(interval_hours=0)]:
                with self.subTest(change=change):
                    slow_io.clear()
                    allow_return.clear()
                    original = archive.find(payload["id"])
                    original["next_due"] = time.time() - 10
                    starting = pool.submit(archive.start, payload["id"], "schedule", original)
                    try:
                        self.assertTrue(slow_io.wait(5))
                        payload.update(change)
                        archive.save_account(payload)
                    finally:
                        allow_return.set()
                    with self.assertRaises(archive_module.RequestError) as rejection:
                        starting.result(5)
                    self.assertEqual(rejection.exception.status, 409)
                    run.assert_not_called()
            self.assertIsNone(archive.find(payload["id"])["next_due"])

    def test_crash_history_reports_last_confirmed_progress_as_a_lower_bound(self):
        archive = self.archive()
        account_id = archive.save_account(self.payload)
        archive.job.update(account_id=account_id, status="running", started_at="2026-01-01T00:00:00+00:00")
        archive.log("download", "")
        archive.log("download", "")
        saved = json.loads(archive.file.read_text())
        self.assertEqual(saved["job"]["downloaded"], 1)  # the second write was inside the checkpoint interval
        recovered = self.archive()
        self.assertEqual(recovered.history[-1]["downloaded"], 1)
        self.assertTrue(recovered.history[-1]["downloaded_estimate"])
        self.assertEqual(recovered.history[-1]["status"], "interrupted")

    def test_scheduled_start_failure_respects_attendance_and_keeps_details_local(self):
        archive = self.archive()
        account = archive.find(archive.save_account(self.payload))
        archive.state()
        with mock.patch.object(archive, "notify") as notify:
            account["next_due"] = time.time() - 1
            archive._record_start_failure(account, "schedule", "private path detail")
            notify.assert_not_called()
            archive.last_seen = time.monotonic() - archive.attended_window - 1
            account["next_due"] = time.time() - 1
            archive._record_start_failure(account, "schedule", "private path detail")
            self.assertEqual(notify.call_count, 1)
            self.assertNotIn("private path detail", notify.call_args.args[2])
            self.assertEqual(archive.history[-1]["message"], "private path detail")


class NotificationTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.hook = Hook()
        self.addCleanup(self.hook.close)

    def save(self, **overrides):
        payload = dict(channel="webhook", webhook_url=self.hook.url,
                       events=dict(auth=True, error=True, partial=False, success=False))
        payload.update(overrides)
        return self.call("/api/notify/save", payload)

    def test_secrets_are_masked_and_kept(self):
        self.assertEqual(self.save()[0], 200)
        notify = self.state()["notify"]
        self.assertNotIn("webhook_url", notify)
        self.assertTrue(notify["secrets"]["webhook_url"])
        self.assertEqual(notify["previews"]["webhook_url"], "http://127.0.0.1")
        self.assertNotIn("secret-token", json.dumps(self.state()))
        self.assertEqual(self.save(webhook_url="")[0], 200)  # empty keeps the stored URL
        self.assertEqual(self.call("/api/notify/test", {})[0], 200)
        self.assertEqual(self.hook.get()["event"], "test")
        self.assertEqual(self.save(webhook_url="ftp://example.com")[0], 400)
        self.assertEqual(self.save(channel="telegram", telegram_token="bad", telegram_chat="1")[0], 400)
        self.assertEqual(self.save(channel="smtp", smtp_host="mail.example.com", smtp_to="nobody")[0], 400)
        self.assertEqual(self.save(channel="none", webhook_url="", clear=["webhook_url"])[0], 200)
        self.assertFalse(self.state()["notify"]["secrets"]["webhook_url"])

    def test_unattended_prompt_is_notified(self):
        self.save()
        status, body = self.call("/api/accounts/save", dict(
            account="test@example.com", destination=str(self.dest), domain="com", interval_hours=0))
        self.call("/api/start", {"id": body["id"]})
        message = self.hook.get(timeout=15)  # nobody polls /api/state, so the run is unattended
        self.assertEqual(message["event"], "auth")
        self.assertIn("te***@example.com", message["message"])
        self.assertNotIn("test@example.com", message["message"])
        self.assertEqual(self.call("/api/cancel", {})[0], 200)
        self.wait_job("cancelled")

    def test_only_unattended_failures_are_notified(self):
        self.save()
        cookie = session_path(self.state_dir / "sessions", "com", "test@example.com")
        cookie.mkdir(parents=True)
        (cookie / "session").write_text("fixture")
        self.control({"fail_primary": True, "delay": 0.8})
        status, body = self.call("/api/accounts/save", dict(
            account="test@example.com", destination=str(self.dest), domain="com", interval_hours=0))
        self.call("/api/start", {"id": body["id"]})
        failure = self.hook.get(timeout=15)
        self.assertEqual(failure["event"], "error")
        self.assertIn("请在飞牛应用中查看任务详情", failure["message"])

        self.call("/api/start", {"id": body["id"]})  # watched this time
        deadline = time.time() + 15
        while len(self.state()["history"]) < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.state()["history"][0]["status"], "error")
        time.sleep(0.5)
        self.assertTrue(self.hook.received.empty())

if __name__ == "__main__":
    unittest.main()
