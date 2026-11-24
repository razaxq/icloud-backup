"""fnOS gateway service for administrator-managed iCloud photo archives.

Serves the web interface and JSON API over the Unix socket that the fnOS
gateway forwards to. Account and download logic lives in archive.py.
"""
import fcntl
from functools import lru_cache
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
import shutil
from pathlib import Path
import secrets
import signal
import socketserver
import stat
import sys
import threading
from urllib.parse import parse_qs, urlsplit

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))  # the worker package, imported by archive
from archive import Archive, RequestError  # noqa: E402


class Gateway(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@lru_cache(maxsize=8)
def static_asset(path):
    body = path.read_bytes()
    return body, '"' + hashlib.sha256(body).hexdigest() + '"'


class Handler(BaseHTTPRequestHandler):
    timeout = 60

    def log_message(self, *args):
        pass

    def send(self, status, payload, mime="application/json; charset=utf-8", headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        if status != 304:
            self.send_header("Content-Length", str(len(body)))
        if not headers or "Cache-Control" not in headers:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_asset(self, path, mime):
        body, etag = static_asset(path)
        headers = {"Cache-Control": "private, no-cache", "ETag": etag}
        candidates = [entry.strip().removeprefix("W/") for entry in self.headers.get("If-None-Match", "").split(",")]
        if "*" in candidates or etag in candidates:
            self.send(304, b"", mime, headers)
        else:
            self.send(200, body, mime, headers)

    def path_in_app(self):
        # The fnOS gateway validates the NAS session and forwards these headers.
        if self.headers.get_all("X-Trim-Isadmin") != ["true"]:
            raise RequestError("请使用飞牛管理员账户从桌面打开应用。", 403)
        path = urlsplit(self.path).path
        prefix = self.server.prefix
        if path == prefix:
            return ""
        if not path.startswith(prefix + "/"):
            raise RequestError("页面不存在。", 404)
        return path[len(prefix):]

    def csrf_token(self):
        # The gateway authenticates the administrator, but may rewrite the HTTP Host.
        # A page can read this token through the authenticated, same-origin state API.
        uid = self.headers.get("X-Trim-Userid", "")
        username = self.headers.get("X-Trim-Username", "")
        identity = json.dumps([uid, username], ensure_ascii=True).encode("ascii")
        return hmac.digest(self.server.csrf_key, identity, "sha256").hex()

    def validate_write(self, data):
        if "_csrf" in data:
            token = data.pop("_csrf")
            if not isinstance(token, str) or not token.isascii() or not hmac.compare_digest(token, self.csrf_token()):
                raise RequestError("页面验证已过期，请刷新应用后重试。", 403)
            return
        # Existing API clients can still submit directly using the original host check.
        origin = self.headers.get("Origin")
        if origin:
            parts = urlsplit(origin)
            if parts.scheme not in ("http", "https") or not parts.netloc or parts.netloc not in (
                    self.headers.get("Host"), self.headers.get("X-Forwarded-Host")):
                raise RequestError("请求来源与飞牛访问地址不一致，请重新打开应用后重试。", 403)

    def do_GET(self):
        try:
            path = self.path_in_app()
            archive = self.server.archive
            if not path:
                self.send(302, b"", headers={"Location": self.server.prefix + "/"})
            elif path == "/api/state":
                self.send(200, dict(archive.state(), csrf_token=self.csrf_token()))
            elif path == "/api/verification-issues":
                with archive.verification_issues() as stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", 'attachment; filename="verification-issues.jsonl"')
                    self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    shutil.copyfileobj(stream, self.wfile)
            elif path == "/api/browse":
                query = parse_qs(urlsplit(self.path).query)
                value = query.get("path", [""])[0]
                if len(value) > 4096 or any(ord(c) < 32 for c in value):
                    raise RequestError("目录路径无效。")
                self.send(200, archive.browse(value))
            else:
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/style.css": ("style.css", "text/css; charset=utf-8"),
                          "/inter.woff2": ("inter.woff2", "font/woff2")}
                if path == "/icon.png":
                    # Installed: target/ui/images; source tree: package/app/ui/images.
                    for icon in (BASE.parent / "ui/images/icon_64.png",
                                 BASE.parent / "package/app/ui/images/icon_64.png"):
                        if icon.is_file():
                            self.send_asset(icon, "image/png")
                            return
                    raise RequestError("页面不存在。", 404)
                if path not in assets:
                    raise RequestError("页面不存在。", 404)
                filename, mime = assets[path]
                self.send_asset(BASE / "static" / filename, mime)
        except RequestError as exc:
            self.send(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return  # The browser closed the page or cancelled a report download.

    def do_POST(self):
        try:
            path = self.path_in_app()
            if self.headers.get("X-Requested-With") != "XMLHttpRequest" or self.headers.get_content_type() != "application/json":
                raise RequestError("请求格式无效。", 403)
            if self.headers.get("Sec-Fetch-Site") in ("same-site", "cross-site"):
                raise RequestError("请从飞牛应用页面提交请求。", 403)
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 16384:
                raise RequestError("请求大小无效。", 413)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise RequestError("请求格式无效。")
            self.validate_write(data)
            archive = self.server.archive
            result = {}
            if path == "/api/accounts/save":
                result = {"id": archive.save_account(data)}
            elif path == "/api/accounts/delete":
                archive.delete_account(data)
            elif path == "/api/start":
                archive.start(data.get("id"), "manual")
            elif path == "/api/reports":
                result = archive.reports(data)
            elif path == "/api/verify":
                archive.start_verification(data)
            elif path == "/api/verify/cancel":
                archive.cancel_verification()
            elif path == "/api/cancel":
                archive.stop()
            elif path == "/api/pause":
                archive.pause(data)
            elif path == "/api/resume":
                archive.resume_paused()
            elif path == "/api/input":
                archive.submit(data)
            elif path == "/api/notify/save":
                archive.save_notify(data)
            elif path == "/api/notify/test":
                archive.test_notify()
            else:
                raise RequestError("接口不存在。", 404)
            self.send(200, result)
        except RequestError as exc:
            self.send(exc.status, {"error": str(exc), **({"code": exc.code} if exc.code else {})})
        except (ValueError, RecursionError):
            self.send(400, {"error": "请求格式无效。"})
        except OSError:
            self.send(500, {"error": "无法写入应用数据，请检查磁盘空间及权限。"})


def read_roots():
    """Folders fnOS lets this app write: the declared share first, then grants."""
    listing = os.environ.get("ICLOUD_ROOTS_FILE")
    if listing and Path(listing).is_file():
        lines = Path(listing).read_text(encoding="utf-8").splitlines()
        return (lines[0] if lines else ""), [line for line in lines[1:] if line]
    roots = [item for item in os.environ.get("ICLOUD_ALLOWED_ROOTS", "").split(":") if item]
    return os.environ.get("ICLOUD_DEFAULT_DIR", ""), roots


def main():
    # The gateway and this app own the entire local transport. There is no TCP listener.
    try:
        os.setsid()  # no controlling terminal for icloudpd's getpass; one session to stop
    except PermissionError:
        pass  # already a session leader when started by the lifecycle script
    state = Path(os.environ["ICLOUD_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    with (state / "service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sock = Path(os.environ["ICLOUD_SOCKET"])
        if sock.exists():
            if not stat.S_ISSOCK(sock.lstat().st_mode):
                raise RuntimeError("应用通信路径被其他文件占用。")
            sock.unlink()
        default_dir, roots = read_roots()
        with Gateway(str(sock), Handler, bind_and_activate=False) as server:
            # New destination subfolders inherit the share ACL. Private state
            # lives under the separate 0700 directory established above.
            os.umask(0o022)
            server.prefix = os.environ.get("ICLOUD_PREFIX", "/app/icloud-photos").rstrip("/")
            server.csrf_key = secrets.token_bytes(32)
            server.archive = Archive(
                state, os.environ["ICLOUDPD_BIN"], roots, default_dir,
                version=os.environ.get("ICLOUD_APP_VERSION", ""),
                startup_grace=float(os.environ.get("ICLOUD_STARTUP_GRACE", "60")),
                resume_delay=float(os.environ.get("ICLOUD_RESUME_DELAY", "120")),
                attended_window=float(os.environ.get("ICLOUD_ATTENDED_WINDOW", "20")))
            quitting = threading.Event()
            reload_requested = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: quitting.set())
            signal.signal(signal.SIGINT, lambda *_: quitting.set())
            signal.signal(signal.SIGHUP, lambda *_: reload_requested.set())
            server.timeout = 0.5
            bound = False
            try:
                # Publish the gateway only after we can read/migrate the account state.
                server.server_bind()
                bound = True
                sock.chmod(0o600)
                server.server_activate()
                while not quitting.is_set():
                    server.handle_request()
                    if reload_requested.is_set():
                        reload_requested.clear()
                        server.archive.reload_roots(*read_roots())
            finally:
                server.archive.close()
                if bound:
                    sock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
