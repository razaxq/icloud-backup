"""Unit tests for the icloudpd wrapper, pinned to upstream 1.33.0 prompts and messages."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from worker import engine  # noqa: E402
from support import install_fake  # noqa: E402

# Copied from icloudpd v1.33.0 (src/icloudpd/base.py and authentication.py).
UPSTREAM_PASSWORD = "iCloud Password for {user}:"
UPSTREAM_2FA = "Please enter two-factor authentication code: "
UPSTREAM_2FA_SMS_CHOICE = "Please enter two-factor authentication code or device index (a..b) to send SMS with a code: "
UPSTREAM_2FA_SMS = "Please enter two-factor authentication code that you received over SMS: "
UPSTREAM_2SA_CHOICE = "Please choose an option: [0]: "
UPSTREAM_2SA = "Please enter two-step authentication code: "


def line(level, text):
    return f"2026-11-18 10:00:00 {level:<8} {text}"


class AnswerTests(unittest.TestCase):
    def answer(self, text, reply, devices=()):
        run = engine._Run(sms_devices=list(devices))
        asked = []

        def prompt(kind, message):
            asked.append((kind, message))
            return reply
        return engine._answer(text, "user@example.com", prompt, run), asked

    def test_upstream_prompts(self):
        value, asked = self.answer(UPSTREAM_PASSWORD.format(user="user@example.com"), "p@ss word")
        self.assertEqual((value, asked[0][0]), ("p@ss word", "password"))
        self.assertEqual(self.answer(UPSTREAM_2FA, " 123456 ")[0], "123456")
        self.assertEqual(self.answer(UPSTREAM_2FA_SMS, "654321")[0], "654321")
        self.assertEqual(self.answer(UPSTREAM_2SA_CHOICE, "ignored")[0], "0")
        self.assertEqual(self.answer(UPSTREAM_2SA, "1234")[0], "1234")
        self.assertIsNone(self.answer("Password for someone else:", "x")[0])
        self.assertIsNone(self.answer(line("INFO", "Please enter two-factor authentication code: "), "x")[0])

    def test_sms_choice(self):
        devices = [("a", "(•••) •••-••12"), ("b", "+86 ••••••34")]
        value, asked = self.answer(UPSTREAM_2FA_SMS_CHOICE, "B", devices)
        self.assertEqual(value, "b")
        self.assertIn("a（(•••) •••-••12）", asked[0][1])
        with self.assertRaises(engine.SyncError):
            self.answer(UPSTREAM_2FA_SMS_CHOICE, "c", devices)
        with self.assertRaises(engine.SyncError):  # letters only where upstream offers SMS
            self.answer(UPSTREAM_2FA, "a", devices)

    def test_rejects_malformed_answers(self):
        for text, reply in [(UPSTREAM_2FA, "12345"), (UPSTREAM_2FA, "12a456"),
                            (UPSTREAM_PASSWORD.format(user="user@example.com"), ""), (UPSTREAM_2SA, "12")]:
            with self.subTest(reply=reply), self.assertRaises(engine.SyncError):
                self.answer(text, reply)


class LogParsingTests(unittest.TestCase):
    def feed(self, *lines):
        run, events = engine._Run(), []
        for text in lines:
            run.line(text, lambda kind, message: events.append((kind, message)))
        return run, events

    def test_progress_and_completion(self):
        run, events = self.feed(
            line("INFO", "Downloading 12 original,adjusted photos and videos to /x ..."),
            line("INFO", "Downloaded /x/2026/01/IMG_0001_ABCDEF0.HEIC"),
            line("INFO", "Found 100 consecutive previously downloaded photos. Exiting"),
            line("INFO", "All photos and videos have been downloaded"), "PrimarySync", "SharedSync-1A2B-3C")
        self.assertTrue(run.finished)
        self.assertEqual(run.downloads, 1)
        self.assertEqual(run.libraries, {"PrimarySync", "SharedSync-1A2B-3C"})
        self.assertIn(("info", "正在检查 12 个图库项目。"), events)
        self.assertIn("快速检查结束", events[-1][1])

    def test_fatal_reasons(self):
        cases = {
            "Invalid email/password combination.": "登录失败",
            "Apple insists on using com.cn for your request. Please use --domain parameter": "服务地区",
            "Failed to verify two-factor authentication code": "未接受验证码",
            "Failed to send two-factor authentication code": "未能发送验证码",
            "Web access to iCloud data is disabled: turn it on": "网页上访问",
            "Timed out waiting for the iCloud web access approval": "已超时",
            "Apple iCloud Photo Library has not finished indexing yet": "索引",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                run, _ = self.feed(line("ERROR", message))
                self.assertIn(expected, run.reason)
        with self.assertRaises(engine.SyncError):
            self.feed(line("INFO", "This account uses security keys instead of codes"))

    def test_incomplete_media_signals(self):
        run, _ = self.feed(line("ERROR", "iCloud listed only 10 of 12 items in the library; the listing ended "
                                         "early, so not everything was downloaded. Run icloudpd again"))
        self.assertIn("少于", run.warning)
        self.assertFalse(run.reason)
        run, _ = self.feed(line("ERROR", "Could not write XMP sidecar for IMG_1.HEIC"))
        self.assertIn("元数据", run.warning)
        run, _ = self.feed("KeyError: 'resOriginalRes' attribute was not found in the photo fields.")
        self.assertIn("无法识别", run.warning)
        run, _ = self.feed(line("WARNING", "Skipping asset A: its master record M was not returned"))
        self.assertIn("主记录", run.warning)
        run, events = self.feed('FNARCHIVE/1 ["live", "test.HEIC"]', 'FNARCHIVE/1 checked')
        self.assertEqual(run.issues, {"live": 1})
        self.assertTrue(run.checked)
        self.assertIn("test.HEIC", events[0][1])
        run, _ = self.feed(line("ERROR", "Session error, re-authenticating..."),
                           line("ERROR", "Error downloading IMG.HEIC, retrying after 5 seconds..."),
                           line("INFO", "Downloaded verified media"), "FNARCHIVE/1 checked")
        self.assertEqual(run.warning, "")
        self.assertFalse(run.issues)


class RunSyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="icp-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.binary = install_fake(self.base)
        self.dest = self.base / "photos"
        self.dest.mkdir()
        self.sessions = self.base / "sessions"
        self.control_file = self.base / "control.json"
        self.calls_file = self.base / "calls.jsonl"
        os.environ["FAKE_ICLOUDPD_CONTROL"] = str(self.control_file)
        os.environ["FAKE_ICLOUDPD_CALLS"] = str(self.calls_file)
        self.addCleanup(os.environ.pop, "FAKE_ICLOUDPD_CONTROL")
        self.addCleanup(os.environ.pop, "FAKE_ICLOUDPD_CALLS")
        cookie = engine.session_path(self.sessions, "com", "user@example.com")
        cookie.mkdir(parents=True)
        (cookie / "session").write_text("fixture")

    def run_sync(self, control=None, **kwargs):
        self.control_file.write_text(json.dumps(control or {}))
        events = []
        result = engine.run_sync(binary=self.binary, username="user@example.com", domain="com",
                                 destination=self.dest, session_dir=self.sessions,
                                 prompt=lambda kind, message: self.fail("unexpected prompt"),
                                 on_event=lambda kind, message: events.append((kind, message)),
                                 cancel=threading.Event(), **kwargs)
        return result, events

    def calls(self):
        return [json.loads(item) for item in self.calls_file.read_text().splitlines()]

    def test_full_scan_with_shared_library(self):
        result, _ = self.run_sync({"libraries": ["SharedSync-ABC"]})
        self.assertEqual((result.downloaded, result.warnings, result.failures), (8, [], []))
        self.assertTrue((self.dest / "共享图库/SharedSync-ABC/2026/01/test-0.MOV").exists())
        self.assertTrue((self.dest / "2026/01/hidden-0.HEIC").exists())
        self.assertFalse(any("--until-found" in call for call in self.calls()))

    def test_incremental_scan_uses_until_found(self):
        result, _ = self.run_sync(full_scan=False, include_shared=False)
        self.assertFalse(result.full_scan)
        media = [call for call in self.calls() if "--library" in call]
        self.assertEqual(len(media), 2)
        for call in media:
            self.assertEqual(call[call.index("--until-found") + 1], str(engine.INCREMENTAL_UNTIL_FOUND))

    def test_failed_library_does_not_stop_others(self):
        result, events = self.run_sync({"fail_primary": True, "libraries": ["SharedSync-ABC"],
                                        "fail_hidden_shared": True})
        self.assertEqual(len(result.failures), 1)
        self.assertIn("个人图库未能完成", result.failures[0])
        self.assertTrue(any("隐藏项目未能完成" in item for item in result.warnings))
        self.assertTrue((self.dest / "共享图库/SharedSync-ABC/2026/01/test-0.HEIC").exists())
        self.assertIn(("warning", "个人图库未能完成，继续处理其他图库。"), events)

    def test_access_denied_retries_with_web_access(self):
        result, _ = self.run_sync({"denied": True}, include_shared=False)
        self.assertEqual(result.failures, [])
        calls = self.calls()
        self.assertNotIn("--adp-web-access", calls[0])
        self.assertTrue(all("--adp-web-access" in call for call in calls[1:]))

    def test_login_failure_is_fatal(self):
        cookie = engine.session_path(self.sessions, "com", "user@example.com")
        (cookie / "session").unlink()
        self.control_file.write_text("{}")
        with self.assertRaisesRegex(engine.SyncError, "登录失败"):
            engine.run_sync(binary=self.binary, username="user@example.com", domain="com",
                            destination=self.dest, session_dir=self.sessions,
                            prompt=lambda kind, message: "wrong", on_event=lambda *_: None,
                            cancel=threading.Event())
        self.assertEqual(len(self.calls()), 1)

    def test_metadata_failure_is_reported(self):
        result, _ = self.run_sync({"xmp_error": True}, include_shared=False)
        self.assertTrue(result.failures and "元数据" in result.failures[0])

    def test_quick_scan_rejects_known_incomplete_results_and_legacy_binary(self):
        for control in ("missing_live", "short_listing", "missing_master", "unverified"):
            with self.subTest(control=control):
                result, _ = self.run_sync({control: True}, full_scan=False, include_shared=False)
                self.assertTrue(result.failures)

    def test_cancel(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(engine.SyncCancelled):
            engine.run_sync(binary=self.binary, username="user@example.com", domain="com",
                            destination=self.dest, session_dir=self.sessions, prompt=None,
                            on_event=lambda *_: None, cancel=cancel)


if __name__ == "__main__":
    unittest.main()
