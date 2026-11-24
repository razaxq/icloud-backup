"""Pausing and resuming a running backup."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ServiceCase  # noqa: E402
from worker.engine import session_path  # noqa: E402


class Helpers(ServiceCase):
    def seed_session(self, account="test@example.com", domain="com"):
        cookie = session_path(self.state_dir / "sessions", domain, account)
        cookie.mkdir(parents=True, exist_ok=True)
        (cookie / "session").write_text("fixture")
        return cookie

    def saved(self):
        return json.loads((self.state_dir / "archive.json").read_text())

    def edit_saved(self, change):
        """Change the persisted state while the service is stopped, then start it again."""
        self.stop_service()
        record = self.saved()
        change(record)
        (self.state_dir / "archive.json").write_text(json.dumps(record))
        self.start_service()

    def media(self):
        return sorted(path.name for path in (self.dest / "2026/01").glob("*")) if (self.dest / "2026/01").exists() else []

    def wait_for(self, predicate, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.state()
            if predicate(state):
                return state
            time.sleep(0.05)
        self.fail(f"condition not reached: {state['job']} {state['accounts']}")

    def wait_downloaded(self, count):
        self.wait_for(lambda state: state["job"]["downloaded"] >= count)


class PauseTests(Helpers):
    def test_pause_until_continued_survives_restart_and_finishes_as_one_task(self):
        self.control({"files": 6, "delay": 0.25})
        self.seed_session()
        account_id = self.add_account(include_shared=False)
        other = self.add_account(account="other@example.com", destination=str(self.dest / "other"))
        self.assertEqual(self.call("/api/pause", {"minutes": 0})[0], 409, "nothing to pause yet")
        self.call("/api/start", {"id": account_id})
        self.wait_downloaded(2)
        self.assertEqual(self.call("/api/pause", {"minutes": 45})[0], 400)
        self.assertEqual(self.call("/api/pause", {"minutes": 0})[0], 200)
        job = self.wait_job("paused")
        self.assertIsNone(job["pause_until"])
        written = self.media()
        time.sleep(0.6)
        self.assertEqual(self.media(), written, "the downloader is stopped while paused")
        self.assertEqual(self.state()["history"], [], "a pause is not a finished task")

        status, body = self.call("/api/start", {"id": other})
        self.assertEqual((status, body.get("code")), (409, "paused"))
        self.assertEqual(self.call("/api/accounts/delete", {"id": account_id})[0], 409)
        self.assertEqual(self.call("/api/accounts/save", dict(
            id=account_id, account="test@example.com", destination=str(self.dest), domain="com",
            interval_hours=0, scan="auto", include_shared=False))[0], 409)

        self.stop_service()
        self.start_service()
        self.assertEqual(self.state()["job"]["status"], "paused", "a pause outlives a restart")
        time.sleep(0.3)
        self.assertEqual(self.state()["job"]["status"], "paused", "and does not continue by itself")

        self.control({"files": 6})
        self.assertEqual(self.call("/api/resume", {})[0], 200)
        job = self.wait_job("success")
        self.assertEqual(job["trigger"], "manual", "a continued task keeps how it was started")
        history = self.state()["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["downloaded"], 14, "files from before and after the pause count once")
        self.assertEqual(len([name for name in self.media() if name.startswith("test-")]), 12)
        self.assertEqual(self.call("/api/start", {"id": other})[0], 200)

    def test_timed_pause_continues_after_completed_phases(self):
        self.control({"files": 1, "delay": 0.4})
        self.seed_session()
        account_id = self.add_account(include_shared=False)
        self.call("/api/start", {"id": account_id})
        self.wait_for(lambda state: "PrimarySync" in state["job"]["done_phases"])
        self.assertEqual(self.call("/api/pause", {"minutes": 30})[0], 200)
        job = self.wait_job("paused")
        self.assertIsNotNone(job["pause_until"])
        calls_before = len(self.calls())

        def due_now(record):
            record["job"]["pause_due"] = time.time() - 1
        self.control({"files": 1})
        self.edit_saved(due_now)
        self.wait_job("success")
        later = self.calls()[calls_before:]
        self.assertEqual([call[call.index("--library") + 1] + ("/Hidden" if "--album" in call else "")
                          for call in later], ["PrimarySync/Hidden"], "the finished library is not scanned again")

    def test_timed_continue_waits_when_the_destination_is_unavailable(self):
        self.control({"files": 3, "delay": 0.3})
        self.seed_session()
        account_id = self.add_account(include_shared=False)
        self.call("/api/start", {"id": account_id})
        self.wait_downloaded(1)
        self.call("/api/pause", {"minutes": 30})
        self.wait_job("paused")

        def due_but_unwritable(record):
            record["job"]["pause_due"] = time.time() - 1
            record["accounts"][0]["destination"] = str(self.base / "unauthorized")
        self.edit_saved(due_but_unwritable)
        job = self.wait_for(lambda state: "未能自动继续" in state["job"]["message"])["job"]
        self.assertEqual(job["status"], "paused")
        time.sleep(0.5)
        self.assertEqual(len([line for line in self.state()["logs"] if "未能自动继续" in line]), 1, "no retry loop")

    def test_cancel_a_paused_task(self):
        self.control({"files": 4, "delay": 0.3})
        self.seed_session()
        account_id = self.add_account(include_shared=False)
        self.call("/api/start", {"id": account_id})
        self.wait_downloaded(1)
        self.call("/api/pause", {"minutes": 120})
        self.wait_job("paused")
        self.assertEqual(self.call("/api/cancel", {})[0], 200)
        job = self.wait_job("cancelled")
        self.assertIsNone(job["pause_until"])
        entry = self.state()["history"][0]
        self.assertEqual(entry["status"], "cancelled")
        self.assertGreaterEqual(entry["downloaded"], 1)
        self.assertEqual(self.call("/api/resume", {})[0], 409)
        self.assertFalse(self.saved()["accounts"][0]["resume"])

    def test_pause_while_waiting_for_verification(self):
        account_id = self.add_account(include_shared=False)
        self.call("/api/start", {"id": account_id})
        self.wait_job("waiting_password")
        self.assertEqual(self.call("/api/pause", {"minutes": 0})[0], 200)
        self.wait_job("paused")
        self.assertEqual(self.call("/api/resume", {})[0], 200)
        self.unlock()
        self.wait_job("success")
