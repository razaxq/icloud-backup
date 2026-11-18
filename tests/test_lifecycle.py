"""Tests for package/cmd/main with a simulated fnOS environment."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import PROJECT, install_fake, request  # noqa: E402
from worker.engine import session_path  # noqa: E402

MAIN = PROJECT / "package" / "cmd" / "main"


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0); it has already stopped running
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.version_info[:2] != (3, 12) and not shutil.which("python3.12"):
            raise unittest.SkipTest("Lifecycle tests require the fnOS CPython 3.12 runtime on PATH")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="icp-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.target = self.base / "target"
        for folder in ("web", "worker"):
            shutil.copytree(PROJECT / folder, self.target / folder, ignore=shutil.ignore_patterns("__pycache__"))
        (self.target / "bin").mkdir()
        install_fake(self.target / "bin")
        self.var = self.base / "var"
        self.share = self.base / "share"
        self.extra = self.base / "extra"
        self.share.mkdir()
        self.extra.mkdir()
        self.logfile = self.base / "trim.log"
        self.control = self.base / "control.json"
        self.control.write_text("{}")
        self.socket = self.target / "app.sock"
        self.addCleanup(self.main, "stop")

    def main(self, command, **extra):
        env = dict(os.environ, TRIM_APPDEST=str(self.target), TRIM_PKGVAR=str(self.var),
                   TRIM_DATA_SHARE_PATHS=str(self.share), TRIM_TEMP_LOGFILE=str(self.logfile),
                   TRIM_APPVER="0.3.0", ICLOUD_STARTUP_GRACE="0", FAKE_ICLOUDPD_CONTROL=str(self.control),
                   PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
        env.update(extra)
        return subprocess.run(["bash", str(MAIN), command], env=env, capture_output=True, timeout=60).returncode

    def server_pid(self):
        return int((self.var / "server.pid").read_text())

    def state(self):
        status, body = request(self.socket, "/api/state")
        self.assertEqual(status, 200)
        return body

    def test_start_status_reload_stop(self):
        self.assertEqual(self.main("status"), 3)
        self.assertEqual(self.main("start"), 0)
        self.assertEqual(self.main("status"), 0)
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.var / "roots").read_text().splitlines(), [str(self.share), str(self.share)])
        state = self.state()
        self.assertEqual((state["roots"], state["default_dir"], state["version"]),
                         ([str(self.share)], str(self.share), "0.3.0"))
        pid = self.server_pid()

        self.assertEqual(self.main("reload", TRIM_DATA_ACCESSIBLE_PATHS=str(self.extra)), 0)
        for _ in range(100):
            if str(self.extra) in self.state()["roots"]:
                break
            time.sleep(0.05)
        self.assertEqual(self.state()["roots"], [str(self.share), str(self.extra)])
        self.assertEqual(self.server_pid(), pid, "reload must not restart the service")
        self.assertEqual(self.main("start"), 0)
        self.assertEqual(self.server_pid(), pid)

        self.assertEqual(self.main("stop"), 0)
        self.assertEqual(self.main("status"), 3)
        self.assertFalse(self.socket.exists())
        self.assertFalse(alive(pid))

    def start_active_job(self):
        worker_pidfile = self.base / "worker.pid"
        self.control.write_text(json.dumps({"files": 30, "delay": 0.4, "pidfile": str(worker_pidfile)}))
        cookie = session_path(self.var / "state" / "sessions", "com", "test@example.com")
        cookie.mkdir(parents=True)
        (cookie / "session").write_text("fixture")
        self.assertEqual(self.main("start"), 0)
        status, body = request(self.socket, "/api/accounts/save", dict(
            account="test@example.com", destination=str(self.share), domain="com", interval_hours=0))
        self.assertEqual(status, 200, body)
        self.assertEqual(request(self.socket, "/api/start", {"id": body["id"]})[0], 200)
        for _ in range(200):
            if worker_pidfile.exists() and worker_pidfile.read_text():
                break
            time.sleep(0.05)
        worker = int(worker_pidfile.read_text())
        server = self.server_pid()
        self.assertEqual(os.getsid(worker), server, "downloader must stay in the service session")
        self.assertNotEqual(os.getpgid(worker), server, "downloader needs its own process group")
        return server, worker

    def test_stop_ends_the_downloader_and_records_interruption(self):
        _, worker = self.start_active_job()
        self.assertEqual(self.main("stop"), 0)
        for _ in range(50):
            if not alive(worker):
                break
            time.sleep(0.1)
        self.assertFalse(alive(worker))
        saved = json.loads((self.var / "state" / "archive.json").read_text())
        self.assertEqual(saved["job"]["status"], "interrupted")

    def check_orphan_cleanup(self, action):
        server, worker = self.start_active_job()
        os.kill(server, signal.SIGKILL)
        self.assertEqual(self.main(action), 0)
        self.assertFalse(alive(worker), "an orphan from the crashed service can still write media")
        if action == "start":
            self.assertNotEqual(self.server_pid(), server)
            self.assertEqual(self.main("status"), 0)

    def test_stop_after_service_crash_reaps_orphan(self):
        self.check_orphan_cleanup("stop")

    def test_start_after_service_crash_reaps_orphan_before_recovery(self):
        self.check_orphan_cleanup("start")

    def test_errors_are_shown_to_the_user(self):
        self.assertEqual(self.main("bogus"), 1)
        self.assertIn("未知的生命周期命令", self.logfile.read_text())
        (self.target / "bin" / "icloudpd").unlink()
        self.assertEqual(self.main("start"), 1)
        self.assertIn("应用文件不完整", self.logfile.read_text())
        self.assertEqual(self.main("status"), 3)

    def test_uninstall_can_remove_private_data(self):
        self.assertEqual(self.main("start"), 0)
        self.assertEqual(self.main("stop"), 0)
        callback = PROJECT / "package" / "cmd" / "uninstall_callback"
        env = dict(os.environ, TRIM_PKGVAR=str(self.var))
        subprocess.run(["bash", str(callback)], env=env, check=True)
        self.assertTrue((self.var / "state").exists(), "data is kept by default")
        subprocess.run(["bash", str(callback)], env=dict(env, wizard_data_action="delete"), check=True)
        self.assertFalse((self.var / "state").exists())
        self.assertTrue(self.share.exists())


if __name__ == "__main__":
    unittest.main()
