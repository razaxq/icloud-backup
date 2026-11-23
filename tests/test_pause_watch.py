"""Pausing a running task and real-time detection of new iCloud items."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import unittest

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


class RealTimeTests(Helpers):
    def baseline(self, **overrides):
        self.seed_session()
        account_id = self.add_account(include_shared=False, watch=True, **overrides)
        self.call("/api/start", {"id": account_id})
        self.wait_job("success")
        return account_id

    @staticmethod
    def probe_now(record):
        record["accounts"][0]["watch_due"] = time.time() - 1

    def test_probe_is_quiet_until_something_new_appears(self):
        self.baseline()
        before = self.state()
        self.assertEqual(before["accounts"][0]["watch_state"], "on")
        self.assertIsNotNone(before["accounts"][0]["watch_next"])
        time.sleep(1.1)  # check times are reported to the second
        self.edit_saved(self.probe_now)
        state = self.wait_for(lambda state: state["accounts"][0]["last_checked"] != before["accounts"][0]["last_checked"]
                              and state["job"]["status"] == "success")
        self.assertEqual(len(state["history"]), 1, "nothing new: nothing recorded")
        self.assertEqual(state["job"]["message"], before["job"]["message"], "the last real task stays on screen")
        self.assertEqual(state["logs"], before["logs"])
        self.assertIn("--only-print-filenames", self.calls()[-1])

        self.control({"files": 2})
        self.edit_saved(self.probe_now)
        state = self.wait_for(lambda state: len(state["history"]) == 2 and state["job"]["status"] == "success")
        entry = state["history"][0]
        self.assertEqual((entry["trigger"], entry["full_scan"], entry["downloaded"]), ("watch", False, 2))
        self.assertIn("test-1.MOV", self.media())
        self.assertIn("实时检测", state["logs"][0])
        self.assertIsNone(state["accounts"][0]["next_run"], "a manual-only schedule stays manual")

    def test_probe_needing_verification_pauses_detection_until_a_real_run(self):
        cookie_account = self.baseline()
        (session_path(self.state_dir / "sessions", "com", "test@example.com") / "session").unlink()
        self.edit_saved(self.probe_now)
        job = self.wait_job("waiting_password")
        self.assertEqual(job["trigger"], "watch")
        self.call("/api/input", {"value": "wrong", "prompt_id": job["prompt_id"]})
        state = self.wait_for(lambda state: state["accounts"][0]["watch_state"] == "suspended")
        self.assertEqual(state["job"]["status"], "success", "the previous result is shown again")
        self.assertEqual(len(state["history"]), 1)
        self.assertIn("需要重新验证", state["accounts"][0]["watch_note"])
        self.assertIsNone(state["accounts"][0]["watch_next"])

        self.call("/api/start", {"id": cookie_account})
        self.unlock()
        self.wait_job("success")
        self.assertEqual(self.state()["accounts"][0]["watch_state"], "on")

    def test_settings_saved_during_a_probe_keep_the_last_real_job(self):
        self.baseline()
        previous = self.saved()["job"]
        (session_path(self.state_dir / "sessions", "com", "test@example.com") / "session").unlink()
        self.edit_saved(self.probe_now)
        self.wait_job("waiting_password")
        self.assertEqual(self.call("/api/settings/save", {"watch_minutes": 15})[0], 200)
        saved = self.saved()
        self.assertEqual(saved["job"], previous)
        self.assertEqual(saved["settings"]["watch_minutes"], 15)

    def test_failed_probes_back_off(self):
        self.baseline()
        self.control({"probe_fail": True})
        self.edit_saved(self.probe_now)
        state = self.wait_for(lambda state: state["accounts"][0]["watch_state"] == "retry")
        account = state["accounts"][0]
        wait = datetime.fromisoformat(account["watch_next"]).timestamp() - time.time()
        self.assertGreater(wait, 19 * 60, "twice the 10 minute interval after one failure")
        self.assertLessEqual(wait, 20 * 60)
        self.assertEqual(len(state["history"]), 1)

    def test_continuing_a_watch_download_keeps_the_regular_schedule(self):
        self.baseline(interval_hours=1)
        self.control({"files": 4, "delay": 0.2})
        def make_probe_due(record):
            self.probe_now(record)
            record["accounts"][0]["next_due"] = time.time() + 900
        self.edit_saved(make_probe_due)
        self.wait_for(lambda state: state["job"]["trigger"] == "watch" and state["job"]["downloaded"] >= 1)
        next_run = self.state()["accounts"][0]["next_run"]
        self.assertEqual(self.call("/api/pause", {"minutes": 0})[0], 200)
        self.wait_job("paused")
        self.assertEqual(self.call("/api/resume", {})[0], 200)
        self.wait_job("success")
        state = self.state()
        self.assertEqual(state["history"][0]["trigger"], "watch")
        self.assertEqual(state["accounts"][0]["next_run"], next_run)

    def test_settings_change_the_interval(self):
        self.seed_session()
        self.add_account(include_shared=False, watch=True)
        self.assertEqual(self.state()["settings"]["watch_minutes"], 10)
        self.assertEqual(self.call("/api/settings/save", {"watch_minutes": 7})[0], 400)
        self.assertEqual(self.call("/api/settings/save", {"watch_minutes": 30})[0], 200)
        state = self.state()
        self.assertEqual(state["settings"]["watch_minutes"], 30)
        self.assertEqual(state["settings"]["watch_choices"], [5, 10, 15, 30, 60])
        nxt = state["accounts"][0]["watch_next"]
        wait = datetime.fromisoformat(nxt).timestamp() - time.time()
        self.assertGreater(wait, 29 * 60)
        self.assertLessEqual(wait, 30 * 60)
        self.stop_service()
        self.assertEqual(self.saved()["settings"]["watch_minutes"], 30)

    def test_detection_is_off_by_default_and_never_probes(self):
        self.seed_session()
        account_id = self.add_account(include_shared=False)
        self.call("/api/start", {"id": account_id})
        self.wait_job("success")
        self.assertEqual(self.state()["accounts"][0]["watch_state"], "off")
        self.edit_saved(self.probe_now)
        time.sleep(0.5)
        self.assertFalse(any("--only-print-filenames" in call for call in self.calls()))


if __name__ == "__main__":
    unittest.main()
