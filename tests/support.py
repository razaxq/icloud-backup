"""Shared helpers: a Unix-socket HTTP client and a controllable service process."""
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name("fake_icloudpd.py")
PREFIX = "/app/icloud-photos"


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("fnos.local", timeout=8)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(8)
        self.sock.connect(str(self.socket_path))


def request(socket_path, path, data=None, admin="true", origin="https://fnos.local"):
    conn = UnixConnection(socket_path)
    headers = {"X-Trim-Isadmin": admin, "X-Requested-With": "XMLHttpRequest", "Origin": origin,
               "Content-Type": "application/json"}
    try:
        conn.request("GET" if data is None else "POST", PREFIX + path,
                     None if data is None else json.dumps(data), headers)
        resp = conn.getresponse()
        body = resp.read()
        if body and resp.getheader("Content-Type", "").startswith("application/json"):
            body = json.loads(body)
        return resp.status, body
    finally:
        conn.close()


def install_fake(directory):
    binary = Path(directory) / "icloudpd"
    shutil.copyfile(FAKE, binary)
    binary.chmod(0o755)
    return binary


class ServiceCase(unittest.TestCase):
    """Runs web/server.py against the fake downloader in a private directory."""

    extra_env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="icp-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.dest = self.base / "photos"
        self.dest.mkdir()
        self.state_dir = self.base / "state"
        self.socket = self.base / "app.sock"
        self.binary = install_fake(self.base)
        self.control_file = self.base / "control.json"
        self.calls_file = self.base / "calls.jsonl"
        self.control({})
        self.proc = None
        self.addCleanup(self.stop_service)
        self.start_service()

    def control(self, values):
        self.control_file.write_text(json.dumps(values))

    def calls(self):
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def env(self):
        return dict(os.environ, ICLOUD_STATE_DIR=str(self.state_dir), ICLOUDPD_BIN=str(self.binary),
                    ICLOUD_SOCKET=str(self.socket), ICLOUD_DEFAULT_DIR=str(self.dest),
                    ICLOUD_ALLOWED_ROOTS=str(self.dest), ICLOUD_PREFIX=PREFIX,
                    ICLOUD_STARTUP_GRACE="0", ICLOUD_RESUME_DELAY="0.3", ICLOUD_ATTENDED_WINDOW="1",
                    FAKE_ICLOUDPD_CONTROL=str(self.control_file), FAKE_ICLOUDPD_CALLS=str(self.calls_file),
                    **self.extra_env)

    def start_service(self):
        self.socket.unlink(missing_ok=True)
        self.proc = subprocess.Popen([sys.executable, "-B", str(PROJECT / "web/server.py")], env=self.env())
        for _ in range(250):
            if self.socket.exists():
                return
            self.assertIsNone(self.proc.poll(), "service exited during start-up")
            time.sleep(0.02)
        self.fail("Gateway did not start")

    def stop_service(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.assertEqual(self.proc.wait(timeout=10), 0)

    def call(self, path, data=None, **kwargs):
        return request(self.socket, path, data, **kwargs)

    def state(self):
        status, body = self.call("/api/state")
        self.assertEqual(status, 200, body)
        return body

    def add_account(self, **overrides):
        payload = dict(account="test@example.com", destination=str(self.dest), domain="com",
                       interval_hours=0, scan="auto", include_shared=True)
        payload.update(overrides)
        status, body = self.call("/api/accounts/save", payload)
        self.assertEqual(status, 200, body)
        return body["id"]

    def wait_job(self, *statuses, timeout=15):
        deadline = time.time() + timeout
        job = None
        while time.time() < deadline:
            job = self.state()["job"]
            if job["status"] in statuses:
                return job
            time.sleep(0.03)
        self.fail(f"Expected {statuses}, got {job}")

    def unlock(self, password="test-secret", code="987654"):
        job = self.wait_job("waiting_password")
        self.assertEqual(self.call("/api/input", {"value": password, "prompt_id": "stale"})[0], 409)
        self.assertEqual(self.call("/api/input", {"value": password, "prompt_id": job["prompt_id"]})[0], 200)
        job = self.wait_job("waiting_code")
        self.assertEqual(self.call("/api/input", {"value": code, "prompt_id": job["prompt_id"]})[0], 200)
