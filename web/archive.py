"""Account, schedule, download and notification state behind the gateway.

The gateway in server.py turns requests into calls on Archive. Validation and
permission failures raise RequestError, which carries the user-facing message
and the status code the gateway answers with.

Expects the project root and this directory on sys.path (server.py sets both
up), so that `worker` and `notifier` import.
"""
import collections
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import time
import traceback

from worker.engine import run_sync, session_path, SyncError, SyncCancelled
from worker.retention import policy as cleanup_policy
import notifier


class RequestError(Exception):
    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def timestamp(seconds=None):
    if seconds is None:
        seconds = time.time()
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat(timespec="seconds")


def require_text(data, key, limit=1024):
    value = data.get(key)
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise RequestError("输入为空、过长或含有无效字符，请检查后再试。")
    return value


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


STATE_VERSION = 3
VERIFICATION_VERSION = 2
ACTIVE = {"running", "waiting_password", "waiting_code", "cancelling", "pausing"}
PAUSABLE = {"running", "waiting_password", "waiting_code"}
HELD = ACTIVE | {"paused"}  # the job owns the single download slot
TRIGGERS = ("manual", "schedule", "resume")
PAUSE_MINUTES = (0, 30, 120)
DOWNLOADER = "icloudpd 1.33.0"
SHORT_INTERVALS = (5, 10, 15, 30, 60)
LEGACY_HOURS = (0, 1, 6, 12, 24, 168)
INTERVALS = (0, *SHORT_INTERVALS, 360, 720, 1440, 10080)
FULL_SCAN_EVERY = 7 * 86400
PROGRESS_SAVE_INTERVAL = 15
MAX_ACCOUNTS = 10
MAX_CRASH_RESUMES = 2
PROMPT_TIMEOUT = {"password": 1800, "code": 600}
IDLE_JOB = dict(status="idle", message="", updated_at=None, account_id=None, account="",
                trigger=None, started_at=None, full_scan=True, downloaded=0, removed=0,
                done_phases=[], pause_until=None, pause_due=None)


def clean_resume(raw):
    """Where a paused, interrupted or crashed job continues; None when nothing is pending."""
    if not isinstance(raw, dict):
        return None
    done = raw.get("done")
    downloaded = raw.get("downloaded")
    trigger = raw.get("trigger")
    if trigger == "watch":
        trigger = "schedule"
    return dict(full=raw.get("full") is not False,
                done=[item for item in done if isinstance(item, str)][:200] if isinstance(done, list) else [],
                paused=raw.get("paused") is True,
                trigger=trigger if trigger in TRIGGERS else None,
                started_at=raw.get("started_at") if isinstance(raw.get("started_at"), str) else None,
                downloaded=downloaded if type(downloaded) is int and downloaded >= 0 else 0)


def legacy_interval(raw):
    hours = raw.get("interval_hours")
    return hours * 60 if type(hours) is int and hours in LEGACY_HOURS else None


def migrate_schedule(raw, watch_minutes):
    interval = legacy_interval(raw) or 0
    due = number(raw.get("next_due"))
    if raw.get("watch") is True:
        interval = min(interval, watch_minutes) if interval else watch_minutes
        if not raw.get("resume") and not raw.get("watch_suspended"):
            watch_due = number(raw.get("watch_due"))
            dates = [date for date in (due, watch_due) if date is not None]
            due = min(dates) if dates else time.time() + interval * 60
    return dict(raw, interval_minutes=interval, next_due=due)


class Archive:
    def __init__(self, state_dir, binary, roots, default_dir="", *, version="",
                 startup_grace=60.0, resume_delay=120.0, attended_window=20.0):
        self.state_dir = Path(state_dir).resolve()
        self.binary = binary
        self.version = version
        self.default_dir = str(default_dir or "")
        self.roots = self._clean_roots(roots)
        self.view_roots = None
        self.startup_grace = startup_grace
        self.resume_delay = resume_delay
        self.attended_window = attended_window
        self.lock = threading.Condition(threading.RLock())
        self.logs = collections.deque(maxlen=200)
        self.history = collections.deque(maxlen=50)
        self.accounts = []
        self.notify_config = notifier.normalize(None)
        self.notify_status = None
        self.job = dict(IDLE_JOB)
        self.stop_reason = None
        self.pause_minutes = 0
        self.media_count = 0
        self.next_progress_save = 0.0
        self.last_event = None
        self.thread = None
        self.cancel = threading.Event()
        self.shutdown = False
        self.answer = None
        self.prompt_id = None
        self.prompt_kind = None
        self.last_seen = None
        self.started = time.time()
        self.file = self.state_dir / "archive.json"
        self.sessions = self.state_dir / "sessions"
        if self.file.exists():
            try:
                saved = json.loads(self.file.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("unexpected state layout")
            except (ValueError, UnicodeDecodeError):
                # Keep the unreadable file for inspection instead of refusing to start.
                self.file.replace(self.state_dir / f"archive.json.unreadable-{int(time.time())}")
                self.log("warning", "应用状态文件无法读取，已另存并恢复为初始设置。")
            else:
                self._load(saved)
        self.scheduler = threading.Thread(target=self.schedule_loop, name="scheduler", daemon=True)
        self.scheduler.start()

    # ----- persistence -------------------------------------------------

    @staticmethod
    def _clean_roots(roots):
        clean = []
        for root in roots:
            path = Path(root)
            if path.is_absolute() and path not in clean:
                clean.append(path)
        return clean

    @staticmethod
    def _account(raw):
        raw = raw if isinstance(raw, dict) else {}
        interval = raw.get("interval_minutes")
        last_run = raw.get("last_run")
        try:
            cleanup = cleanup_policy(**raw.get("cleanup", {}))
        except (TypeError, ValueError):
            cleanup = cleanup_policy()
        return dict(
            id=str(raw.get("id") or secrets.token_hex(6)),
            account=str(raw.get("account") or ""),
            domain=raw.get("domain") if raw.get("domain") in ("com", "cn") else "com",
            destination=str(raw.get("destination") or ""),
            interval_minutes=interval if type(interval) is int and interval in INTERVALS else 0,
            include_shared=raw.get("include_shared") is not False,
            cleanup=cleanup,
            next_due=number(raw.get("next_due")),
            last_success=number(raw.get("last_success")),
            last_full_scan=number(raw.get("last_full_scan")),
            needs_full_scan=raw.get("needs_full_scan") is True,
            last_run=last_run if isinstance(last_run, dict) else None,
            crashes=raw.get("crashes") if type(raw.get("crashes")) is int else 0,
            resume=clean_resume(raw.get("resume")),
        )

    def _load(self, saved):
        def listed(key):
            value = saved.get(key)
            return value if isinstance(value, list) else []

        version = saved.get("version")
        single_account = "version" not in saved and isinstance(saved.get("config"), dict)
        if "verification_version" in saved and (type(saved["verification_version"]) is not int or
                                                saved["verification_version"] not in (1, VERIFICATION_VERSION)):
            raise ValueError("原件校验记录版本与当前应用不兼容。原配置已保留。")
        settings = saved.get("settings")
        watch_minutes = settings.get("watch_minutes") if isinstance(settings, dict) else None
        if type(watch_minutes) is not int or watch_minutes not in SHORT_INTERVALS:
            watch_minutes = 10
        if type(version) is int and version in (2, STATE_VERSION):
            for item in listed("accounts"):
                if isinstance(item, dict):
                    if version == 2:
                        item = migrate_schedule(item, watch_minutes)
                    self.accounts.append(self._account(item))
                    if len(self.accounts) == MAX_ACCOUNTS:
                        break
            self.notify_config = notifier.normalize(saved.get("notify"))
            self.history.extend(item for item in listed("history") if isinstance(item, dict))
        elif single_account:
            # Version 0.1 kept a single account under "config".
            old = saved.get("config") if isinstance(saved.get("config"), dict) else {}
            if old.get("account"):
                account = self._account(migrate_schedule(dict(old, next_due=saved.get("next_due")), watch_minutes))
                self.accounts = [account]
        else:
            raise ValueError("归档配置版本与当前应用不兼容。请使用兼容版本打开，原配置已保留。")
        job = saved.get("job") if isinstance(saved.get("job"), dict) else {}
        self.job = dict(IDLE_JOB, **{key: job[key] for key in IDLE_JOB if key in job})
        if self.job["status"] in HELD and self.job["trigger"] == "watch":
            self.job["trigger"] = "schedule"
        if not isinstance(self.job["done_phases"], list):
            self.job["done_phases"] = []
        if self.job["status"] == "paused" and (self.find(self.job.get("account_id")) is None or
                                               not (self.find(self.job["account_id"])["resume"] or {}).get("paused")):
            self.job.update(status="cancelled", message="暂停的任务已失效。", pause_until=None, pause_due=None)
        if not self.job.get("account_id") and self.accounts and single_account:
            self.job.update(account_id=self.accounts[0]["id"], account=self.accounts[0]["account"])
        self.logs.extend(line for line in listed("logs") if isinstance(line, str))
        migrate = single_account or saved.get("verification_version") != VERIFICATION_VERSION
        if migrate:
            # Before 0.3, a successful run could still have missing originals.
            # Keep the audit trail and schedules, but establish a fresh baseline.
            for item in self.accounts:
                item.update(last_success=None, last_full_scan=None, needs_full_scan=True, cleanup=cleanup_policy(), resume=None)
            self.job.update(done_phases=[], full_scan=True)
            if self.job["status"] == "paused":
                self.set_job("cancelled", "归档方式已升级，请重新完整扫描。")
            for entry in [self.job, *self.history, *(item["last_run"] for item in self.accounts)]:
                if entry and entry.get("status") in ("success", "partial"):
                    entry.update(status="unverified", message="旧版记录需要重新核对图库身份和原件。" + str(entry.get("message", "")))
        if self.job["status"] in ACTIVE:
            self._recover_crash()
        if version != STATE_VERSION or migrate or self.job["status"] == "interrupted":
            self.persist()

    def _recover_crash(self):
        """The service stopped without finishing the job (power loss, kill)."""
        account = self.find(self.job.get("account_id"))
        message = "上次任务因服务意外停止而中断。"
        if account:
            account["crashes"] += 1
            if account["crashes"] <= MAX_CRASH_RESUMES:
                account["resume"] = clean_resume(dict(full=bool(self.job.get("full_scan", True)),
                                                      done=self.job.get("done_phases")))
                account["next_due"] = time.time() + self.resume_delay
                message += "几分钟后会自动继续。"
            else:
                account["resume"] = None
                self.schedule_next(account)
                message += "任务已多次在运行中中断，已暂停自动继续。请查看 server.log 后手动开始。"
            downloaded = self.job.get("downloaded")
            message += "时间记为发现中断的时间，下载数量为最后保存的进度，实际数量可能更多。"
            self._finish_record(account, "interrupted", message,
                                downloaded=downloaded if type(downloaded) is int else 0, estimated=True)
        self.set_job("interrupted", message)

    def persist(self):
        data = dict(version=STATE_VERSION, verification_version=VERIFICATION_VERSION,
                    accounts=self.accounts, notify=self.notify_config,
                    job=self.job, logs=list(self.logs), history=list(self.history))
        with tempfile.NamedTemporaryFile(dir=self.state_dir, mode="w", encoding="utf-8", delete=False) as out:
            tmp = Path(out.name)
            try:
                json.dump(data, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        try:
            tmp.replace(self.file)
        finally:
            tmp.unlink(missing_ok=True)

    def try_persist(self):
        try:
            self.persist()
        except OSError:
            self.log("warning", "无法写入应用状态目录，请检查系统盘空间。")

    # ----- helpers -----------------------------------------------------

    def find(self, account_id):
        return next((item for item in self.accounts if item["id"] == account_id), None)

    def set_job(self, status, message, **context):
        self.job = dict(self.job, status=status, message=message, updated_at=timestamp(), **context)

    def log(self, kind, message):
        with self.lock:
            if kind == "download":
                self.media_count += 1
                self.job["downloaded"] = self.media_count
                self.job["updated_at"] = timestamp()
                message = f"本次已写入 {self.media_count} 个媒体文件。"
            elif kind == "removed":
                self.job["removed"] += 1
                self.job["updated_at"] = timestamp()
                message = f"iCloud 已确认删除 {self.job['removed']} 个已备份项目。"
            progress = kind in ("download", "removed")
            if progress and self.last_event == kind and self.logs:
                self.logs.pop()
            self.last_event = kind
            self.logs.append(f"{dt.datetime.now().astimezone():%H:%M:%S}  {message}")
            if progress and time.monotonic() >= self.next_progress_save:
                self.next_progress_save = time.monotonic() + PROGRESS_SAVE_INTERVAL
                self.try_persist()

    def schedule_next(self, account):
        minutes = account["interval_minutes"]
        account["next_due"] = time.time() + 60 * minutes if minutes else None

    def phase_done(self, name):
        with self.lock:
            if name not in self.job["done_phases"]:
                self.job["done_phases"] = [*self.job["done_phases"], name]
            self.try_persist()

    def _finish_record(self, account, status, message, *, finished=None, started=None, downloaded=None, estimated=False):
        finished = finished or timestamp()
        entry = dict(account_id=account["id"], account=account["account"],
                     trigger=self.job.get("trigger") or "schedule",
                     started_at=started or self.job.get("started_at") or finished, finished_at=finished,
                     status=status, downloaded=self.media_count if downloaded is None else downloaded,
                     downloaded_estimate=estimated, removed=self.job.get("removed", 0),
                     full_scan=bool(self.job.get("full_scan", True)), message=message)
        self.history.append(entry)
        account["last_run"] = dict(status=status, message=message, finished_at=finished,
                                   downloaded=entry["downloaded"])

    def attended(self):
        return self.last_seen is not None and time.monotonic() - self.last_seen <= self.attended_window

    def notify(self, event, title, body, *, force=False):
        with self.lock:
            config = json.loads(json.dumps(self.notify_config))
        if config["channel"] == "none" or (not force and not config["events"].get(event)):
            return
        threading.Thread(target=self._deliver, args=(config, title, body, event), daemon=True).start()

    def _deliver(self, config, title, body, event):
        try:
            notifier.send(config, title, body, event)
            status = dict(ok=True, at=timestamp(), message="")
        except notifier.NotifyError as exc:
            status = dict(ok=False, at=timestamp(), message=str(exc))
            self.log("warning", "通知发送失败：" + str(exc))
        except Exception:  # never let a notifier bug take down the service
            traceback.print_exc()
            status = dict(ok=False, at=timestamp(), message="通知发送时出现内部错误。")
        with self.lock:
            self.notify_status = status

    def _roots_snapshot(self):
        # Directory checks can wait for a sleeping disk, so never under the lock.
        return [path for path in list(self.roots) if path.is_dir()]

    def reload_roots(self, default_dir, roots):
        with self.lock:
            self.roots = self._clean_roots(roots)
            self.default_dir = str(default_dir or "")
            self.view_roots = None
            self.log("info", "已更新飞牛授权目录。")

    def _display_roots(self):
        # Polling the dashboard must not stat every NAS volume every 2.5 seconds.
        # Browsing and writes always check the live permissions.
        with self.lock:
            if self.view_roots and self.view_roots[0] > time.monotonic():
                return self.view_roots[1]
            roots = self.roots
        available = [str(path) for path in roots if path.is_dir()]
        with self.lock:
            if self.roots is roots:
                self.view_roots = (time.monotonic() + 60, available)
                return available
            return []

    # ----- views -------------------------------------------------------

    def _public_account(self, account):
        def iso(value):
            return timestamp(value) if value else None
        return dict(id=account["id"], account=account["account"], domain=account["domain"],
                    destination=account["destination"], interval_minutes=account["interval_minutes"],
                    include_shared=account["include_shared"], cleanup=account["cleanup"],
                    next_run=iso(account["next_due"]), last_success=iso(account["last_success"]),
                    last_full_scan=iso(account["last_full_scan"]), last_run=account["last_run"],
                    needs_full_scan=account["needs_full_scan"],
                    resume_pending=bool(account["resume"]) and not account["resume"]["paused"])

    def state(self):
        available = self._display_roots()
        with self.lock:
            self.last_seen = time.monotonic()
            job = dict(self.job, prompt_id=self.prompt_id, prompt_kind=self.prompt_kind)
            job.pop("pause_due", None)
            notify = notifier.public(self.notify_config)
            notify["last"] = self.notify_status
            return dict(auth="ok", version=self.version, downloader=DOWNLOADER, roots=available,
                        default_dir=self.default_dir if self.default_dir in available else "",
                        accounts=[self._public_account(item) for item in self.accounts],
                        job=job, logs=list(self.logs), history=list(reversed(self.history))[:20],
                        notify=notify)

    def destination(self, value):
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise RequestError("请输入授权文件夹内的完整绝对路径。")
        try:
            real = path.resolve()
            roots = [root.resolve(strict=True) for root in self._roots_snapshot()]
            if not any(real.is_relative_to(root) for root in roots):
                raise RequestError("请在飞牛应用设置中授权该文件夹读写权限，或选择已列出的目录。")
            if real.is_relative_to(self.state_dir) or self.state_dir.is_relative_to(real):
                raise RequestError("保存目录与应用私有目录重叠，请另选目录。")
            real.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".icloud-write-", dir=real):
                pass
        except (OSError, RuntimeError) as exc:
            raise RequestError("无法写入该文件夹，请检查磁盘是否在线及应用的读写权限。") from exc
        return real

    def browse(self, value):
        roots = self._roots_snapshot()
        if not value:
            return dict(path="", parent=None, entries=[dict(name=str(root), path=str(root)) for root in roots],
                        truncated=False)
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise RequestError("目录路径无效。")
        try:
            real = path.resolve(strict=True)
            resolved = [(root, root.resolve(strict=True)) for root in roots]
        except (OSError, RuntimeError):
            raise RequestError("目录不存在或暂时无法访问。", 404) from None
        base = next((target for _, target in resolved if real.is_relative_to(target)), None)
        if base is None:
            raise RequestError("该目录不在飞牛为本应用授权的范围内。", 403)
        names, truncated = [], False
        try:
            with os.scandir(real) as entries:
                for index, entry in enumerate(entries):
                    if index >= 5000 or len(names) >= 500:
                        truncated = True
                        break
                    if entry.name.startswith((".", "@", "#")):
                        continue
                    try:
                        if entry.is_dir() and Path(entry.path).resolve().is_relative_to(base):
                            names.append(entry.name)
                    except OSError:
                        continue
        except OSError:
            raise RequestError("无法读取该目录，请检查应用的读取权限。", 403) from None
        names.sort(key=str.casefold)
        return dict(path=str(real), parent=str(real.parent) if real != base else "",
                    entries=[dict(name=name, path=str(real / name)) for name in names], truncated=truncated)

    # ----- accounts and notifications -----------------------------------

    def save_account(self, data):
        account = require_text(data, "account", 320).strip()
        if not account or account.startswith("-") or any(c.isspace() for c in account):
            raise RequestError("请输入有效的 Apple 账户。")
        domain = data.get("domain")
        interval = data.get("interval_minutes", legacy_interval(data))
        include_shared = data.get("include_shared", True)
        if (domain not in ("com", "cn") or type(interval) is not int or interval not in INTERVALS
                or not isinstance(include_shared, bool)):
            raise RequestError("服务地区、自动备份频率或共享图库设置无效。")
        with self.lock:
            roots = self.roots
        destination = str(self.destination(require_text(data, "destination", 4096)))
        account_id = data.get("id")
        stale_session = None
        with self.lock:
            if self.roots is not roots:
                raise RequestError("飞牛已更新目录授权，请重新保存。", 409)
            existing = None
            if account_id is not None:
                existing = self.find(account_id)
                if existing is None:
                    raise RequestError("该账户已被删除，请刷新页面。", 404)
                if self.job["status"] in HELD and self.job.get("account_id") == account_id:
                    raise RequestError("请等该账户的当前任务结束（或取消已暂停的任务）后再修改设置。", 409)
            elif len(self.accounts) >= MAX_ACCOUNTS:
                raise RequestError(f"最多可添加 {MAX_ACCOUNTS} 个 Apple 账户。")
            key = (domain, account.casefold())
            if any(item is not existing and (item["domain"], item["account"].casefold()) == key
                   for item in self.accounts):
                raise RequestError("这个 Apple 账户已经添加过了。")
            identity_changed = existing and (existing["domain"], existing["account"].casefold()) != key
            scope_changed = existing and (identity_changed or existing["destination"] != destination or
                                          existing["include_shared"] != include_shared)
            cleanup = existing["cleanup"] if existing and not scope_changed else cleanup_policy()
            if "cleanup" in data:
                try:
                    cleanup = cleanup_policy(**data["cleanup"])
                except (TypeError, ValueError):
                    raise RequestError("iCloud 清理策略无效，请检查模式、天数或保留数量。") from None
            if cleanup["shared"] and not include_shared:
                raise RequestError("清理共享图库前，请先开启共享图库备份。")
            if ((not existing or scope_changed) and data.get("allow_shared_destination") is not True and
                    any(item is not existing and item["destination"] == destination for item in self.accounts)):
                raise RequestError("另一个账户已经使用这个文件夹。共用目录会混合保存双方的照片和隐藏项目。",
                                   409, "destination_in_use")
            updated = self._account(dict(existing or {}, account=account, domain=domain, destination=destination,
                                         interval_minutes=interval, include_shared=include_shared, cleanup=cleanup))
            if scope_changed:
                updated.update(last_success=None, last_full_scan=None, last_run=None,
                               needs_full_scan=True, resume=None, crashes=0)
            if existing is None or existing["interval_minutes"] != interval or scope_changed:
                if not updated["resume"]:
                    self.schedule_next(updated)
            if identity_changed:
                stale_session = session_path(self.sessions, existing["domain"], existing["account"])
            previous = list(self.accounts)
            if existing:
                self.accounts[self.accounts.index(existing)] = updated
            else:
                self.accounts.append(updated)
            try:
                self.persist()
            except OSError:
                self.accounts = previous
                raise
            self.lock.notify_all()
        if stale_session:
            shutil.rmtree(stale_session, ignore_errors=True)
        return updated["id"]

    def delete_account(self, data):
        with self.lock:
            account = self.find(data.get("id"))
            if account is None:
                raise RequestError("该账户已被删除，请刷新页面。", 404)
            if self.job["status"] in HELD and self.job.get("account_id") == account["id"]:
                raise RequestError("请先取消该账户的当前任务。", 409)
            previous = list(self.accounts)
            self.accounts.remove(account)
            try:
                self.persist()
            except OSError:
                self.accounts = previous
                raise
            self.lock.notify_all()
        # The saved Apple session is a credential; downloaded photos stay untouched.
        shutil.rmtree(session_path(self.sessions, account["domain"], account["account"]), ignore_errors=True)

    def save_notify(self, data):
        with self.lock:
            try:
                updated = notifier.merge(self.notify_config, data)
            except ValueError as exc:
                raise RequestError(str(exc)) from None
            previous, self.notify_config = self.notify_config, updated
            try:
                self.persist()
            except OSError:
                self.notify_config = previous
                raise

    def test_notify(self):
        with self.lock:
            config = json.loads(json.dumps(self.notify_config))
        if config["channel"] == "none":
            raise RequestError("请先选择并保存通知方式。")
        try:
            notifier.send(config, notifier.APP_TITLE + "：测试通知",
                          "这是一条测试通知。任务需要验证或失败时，你会在这里收到提醒。", "test")
        except notifier.NotifyError as exc:
            with self.lock:
                self.notify_status = dict(ok=False, at=timestamp(), message=str(exc))
            raise RequestError(str(exc), 502) from None
        with self.lock:
            self.notify_status = dict(ok=True, at=timestamp(), message="")

    # ----- jobs --------------------------------------------------------

    def _wants_full_scan(self, account, trigger, resume):
        last = account["last_full_scan"]
        return (account["cleanup"]["mode"] != "off" or account["needs_full_scan"]
                or last is None or time.time() - last >= FULL_SCAN_EVERY
                or bool(trigger in ("resume", "continue") and resume and resume["full"]))

    def _still_due(self, account, trigger):
        now = time.time()
        if trigger == "continue":
            return (self.job["status"] == "paused" and self.job.get("account_id") == account["id"]
                    and self.job.get("pause_due") is not None and self.job["pause_due"] <= now)
        return (account["next_due"] is not None and account["next_due"] <= now
                and bool(account["resume"]) == (trigger == "resume"))

    def start(self, account_id, trigger="manual", scheduled_account=None):
        with self.lock:
            if self.shutdown or self.job["status"] in ACTIVE:
                raise RequestError("当前已有任务正在运行或停止。", 409)
            paused = self.job["status"] == "paused"
            if trigger == "continue" and (not paused or self.job.get("account_id") != account_id):
                raise RequestError("没有可以继续的暂停任务。", 409)
            if paused and trigger != "continue":
                raise RequestError("有一个已暂停的任务。请先继续或取消它。", 409, "paused")
            account = self.find(account_id)
            if account is None:
                raise RequestError("请先添加 Apple 账户和保存位置。", 404)
            if scheduled_account is not None and (account is not scheduled_account or not self._still_due(account, trigger)):
                raise RequestError("自动下载计划已变更。", 409)
            roots = self.roots
        destination = self.destination(account["destination"])
        with self.lock:
            if self.shutdown or self.job["status"] in ACTIVE or (self.job["status"] == "paused") != (trigger == "continue"):
                raise RequestError("当前已有任务正在运行或停止。", 409)
            # save_account replaces the dictionary on every committed edit.
            # Do not combine the old validated path with a new account/schedule.
            if (self.find(account_id) is not account or roots is not self.roots or
                    (scheduled_account is not None and not self._still_due(account, trigger))):
                raise RequestError("账户设置、自动下载计划或目录授权已更改，请重新开始。", 409)
            resume, account["resume"] = account["resume"], None
            full = self._wants_full_scan(account, trigger, resume)
            continuing = trigger == "continue"
            shown = (resume.get("trigger") or "manual") if continuing and resume else trigger
            skip = set(resume["done"]) if (resume and trigger in ("resume", "continue")
                                           and resume["full"] == full) else set()
            config = dict(account)
            now = timestamp()
            previous = dict(self.job)
            self.cancel = threading.Event()
            self.stop_reason = None
            if not continuing:
                self.logs.clear()
            self.media_count = (resume or {}).get("downloaded", 0) if continuing else 0
            self.next_progress_save = 0.0
            self.last_event = None
            account["next_due"] = None
            self.job = dict(IDLE_JOB, status="running", message="正在连接 iCloud。",
                            updated_at=now, account_id=account_id, account=account["account"], trigger=shown,
                            started_at=(resume.get("started_at") or now) if continuing and resume else now,
                            full_scan=full, downloaded=self.media_count, removed=previous.get("removed", 0) if continuing else 0,
                            done_phases=sorted(skip))
            label = "完整扫描" if full else "快速检查"
            if continuing:
                self.log("info", f"继续已暂停的{label}：{account['account']}")
            else:
                prefix = {"manual": "", "schedule": "定时", "resume": "继续上次中断的"}[trigger]
                self.log("info", f"开始{prefix}{label}：{account['account']}")
            try:
                self.persist()
                self.thread = threading.Thread(target=self.download, args=(config, destination, full, skip),
                                               name="download", daemon=True)
                self.thread.start()
            except Exception:
                self.thread = None
                message = "无法启动任务，请检查应用状态目录的剩余空间及权限。"
                if continuing:
                    account.update(resume=resume, next_due=config["next_due"])
                    self.job = previous
                else:
                    self.set_job("error", message)
                    account["needs_full_scan"] = True
                    self.schedule_next(account)
                    self._finish_record(account, "error", message)
                self.log("warning", message)
                self.try_persist()
                if not self.attended():
                    self.notify("error", notifier.APP_TITLE + "：任务未能开始", message)
                self.lock.notify_all()
                raise RequestError(message, 503) from None
            self.lock.notify_all()

    def ask(self, kind, message):
        with self.lock:
            if self.cancel.is_set():
                raise SyncCancelled("已取消下载。")
            self.answer = None
            self.prompt_id = secrets.token_hex(12)
            self.prompt_kind = kind
            self.set_job("waiting_password" if kind == "password" else "waiting_code", message)
            deadline = time.monotonic() + PROMPT_TIMEOUT[kind]
            notified = False
            while self.answer is None and not self.cancel.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.lock.wait(timeout=min(remaining, 5))
                if not notified and not self.attended() and self.answer is None:
                    notified = True
                    minutes = PROMPT_TIMEOUT[kind] // 60
                    self.notify("auth", notifier.APP_TITLE + "：需要验证",
                                f"Apple 账户 {notifier.mask_account(self.job['account'])} 需要"
                                f"{'输入密码' if kind == 'password' else '输入验证码'}。"
                                f"请在 {minutes} 分钟内从飞牛桌面打开「{notifier.APP_TITLE}」完成验证。")
            value, self.answer = self.answer, None
            self.prompt_id = self.prompt_kind = None
            if self.cancel.is_set():
                raise SyncCancelled("已取消下载。")
            if value is None:
                raise SyncError("等待 Apple 验证超时。请打开页面重新开始下载并完成验证。")
            self.set_job("running", "已提交验证，正在连接 iCloud。")
            return value

    def submit(self, data):
        with self.lock:
            if self.prompt_id is None or data.get("prompt_id") != self.prompt_id or self.answer is not None:
                raise RequestError("此验证请求已失效，请刷新后重试。", 409)
            self.answer = require_text(data, "value")
            self.lock.notify_all()

    def _halt(self, reason, status, message):
        self.stop_reason = reason
        self.cancel.set()
        self.answer = None
        self.prompt_id = self.prompt_kind = None
        self.set_job(status, message)
        self.lock.notify_all()

    def stop(self):
        with self.lock:
            if self.job["status"] == "paused":
                return self._cancel_paused()
            if self.job["status"] not in ACTIVE:
                raise RequestError("当前没有运行中的任务。", 409)
            self._halt("cancel", "cancelling", "正在停止下载。")

    def pause(self, data):
        minutes = data.get("minutes")
        if type(minutes) is not int or minutes not in PAUSE_MINUTES:
            raise RequestError("暂停时长无效。")
        with self.lock:
            if self.job["status"] not in PAUSABLE:
                raise RequestError("当前没有可以暂停的任务。", 409)
            self.pause_minutes = minutes
            self._halt("pause", "pausing", "正在暂停。已下载的文件和未完成的传输会保留，继续后接着下载。")

    def resume_paused(self):
        with self.lock:
            if self.job["status"] != "paused":
                raise RequestError("没有可以继续的暂停任务。", 409)
            account_id = self.job["account_id"]
        self.start(account_id, "continue")

    def _cancel_paused(self):
        account = self.find(self.job.get("account_id"))
        message = "已取消暂停的任务，已下载的文件保存在原目录。"
        previous_job = self.job
        previous_account = account.copy() if account else None
        previous_history, previous_logs = self.history.copy(), self.logs.copy()
        previous_event = self.last_event
        if account:
            resume, account["resume"] = account["resume"], None
            account["crashes"] = 0
            self.schedule_next(account)
            if resume:
                self._finish_record(account, "cancelled", message, downloaded=resume["downloaded"])
        self.set_job("cancelled", message, pause_until=None, pause_due=None)
        self.log("info", message)
        try:
            self.persist()
        except OSError:
            if account:
                account.update(previous_account)
            self.job, self.history, self.logs = previous_job, previous_history, previous_logs
            self.last_event = previous_event
            raise
        self.lock.notify_all()

    def download(self, config, destination, full, skip=frozenset()):
        warnings, failures = [], []
        outcome = None
        try:
            outcome = run_sync(binary=self.binary, username=config["account"], domain=config["domain"],
                               destination=destination, session_dir=self.sessions, prompt=self.ask,
                               on_event=self.log, cancel=self.cancel,
                               include_shared=config["include_shared"], full_scan=full,
                               skip_phases=frozenset(skip),
                               on_phase=self.phase_done, cleanup=config["cleanup"])
            warnings, failures = outcome.warnings, outcome.failures
            count = f"本次新下载 {self.media_count} 个媒体文件。"
            if failures:
                status = "error"
                message = f"归档失败，{failures[0]} {count}"
            elif warnings:
                status = "partial"
                message = f"扫描完成，有 {len(warnings)} 条提醒。{count}{warnings[0]}"
            else:
                status = "success"
                message = ("完整扫描" if full else "快速检查") + "完成，" + count
            if outcome.review:
                message += " " + outcome.review
        except SyncCancelled:
            status, message = "cancelled", "任务已取消，已下载的文件保存在原目录。"
        except SyncError as exc:
            status, message = "error", str(exc)
        except OSError:
            status, message = "error", "读写或启动下载器失败，请检查应用权限、磁盘空间及安装文件。"
        except Exception:
            traceback.print_exc()
            status, message = "error", "下载器异常退出，请重新运行并检查安装版本。"
        if status == "error" and not failures and self.media_count:
            message += f" 失败前已写入 {self.media_count} 个媒体文件。"
        with self.lock:
            if self.cancel.is_set() and self.stop_reason == "pause":
                status = "paused"
            elif self.shutdown and status in ("cancelled", "error"):
                status, message = "interrupted", "服务停止，任务已中断。服务启动后会自动继续。"
            elif self.cancel.is_set():
                status, message = "cancelled", "任务已取消，已下载的文件保存在原目录。"
            self.answer = self.prompt_id = self.prompt_kind = None
            account = self.find(config["id"])
            if self.job.get("removed") and status != "paused":
                message += f" iCloud 已确认删除 {self.job['removed']} 个已备份项目。"
            if status != "paused":
                for line in failures + warnings:
                    self.log("warning", line)
            if account and status == "paused":
                self._record_pause(account, full)
                self.try_persist()
                self.lock.notify_all()
                return
            if account:
                if status == "error":
                    account["needs_full_scan"] = True
                if status == "interrupted":
                    account["resume"] = dict(full=full, done=list(self.job["done_phases"]), paused=False,
                                             trigger=None, started_at=None, downloaded=0)
                    account["next_due"] = time.time()
                else:
                    account["resume"] = None
                    account["crashes"] = 0
                    self.schedule_next(account)
                    if status in ("success", "partial"):
                        account["last_success"] = time.time()
                        if full:
                            account["last_full_scan"] = time.time()
                            account["needs_full_scan"] = False
                self._finish_record(account, status, message)
            self.set_job(status, message)
            self.log("info", message)
            self.try_persist()
            unattended = not self.attended()
            downloaded = self.media_count
            self.lock.notify_all()
        if status in ("error", "partial", "success") and unattended:
            # File-level diagnostics stay on the NAS. A configured external
            # notification gets the event and count, never private filenames.
            title = {"error": "任务失败", "partial": "完成（有提醒）", "success": "下载完成"}[status]
            summary = f"本次已写入 {downloaded} 个媒体文件。"
            if status != "success":
                summary += "请在飞牛应用中查看任务详情。"
            self.notify(status, f"{notifier.APP_TITLE}：{title}",
                        f"{notifier.mask_account(config['account'])}：{title}。{summary}")

    def _record_pause(self, account, full):
        until = time.time() + self.pause_minutes * 60 if self.pause_minutes else None
        account["resume"] = dict(full=full, done=list(self.job["done_phases"]), paused=True,
                                 trigger=self.job.get("trigger"), started_at=self.job.get("started_at"),
                                 downloaded=self.media_count)
        message = ("已暂停，将在设定时间自动继续。" if until else "已暂停，点击「继续」后接着下载。")
        self.set_job("paused", message, pause_until=timestamp(until) if until else None, pause_due=until)
        self.log("info", message)

    def _record_start_failure(self, account, trigger, message):
        with self.lock:
            account_id = account["id"]
            if (self.find(account_id) is not account or self.job["status"] in HELD
                    or not self._still_due(account, trigger)):
                return
            account["resume"] = None
            account["needs_full_scan"] = True
            self.schedule_next(account)
            now = timestamp()
            self.job = dict(IDLE_JOB, status="error", message=message, updated_at=now, account_id=account_id,
                            account=account["account"], trigger=trigger, started_at=now)
            self.media_count = 0
            self._finish_record(account, "error", message, finished=now, started=now, downloaded=0)
            self.log("warning", f"{account['account']}：{message}")
            self.try_persist()
            label = account["account"]
            unattended = not self.attended()
        if unattended:
            self.notify("error", notifier.APP_TITLE + "：定时任务未能开始",
                        f"{notifier.mask_account(label)}：定时任务未能开始。请在飞牛应用中查看任务详情。")

    def _delay_continue(self, reason):
        """A timed pause could not continue (e.g. the disk is offline): stay paused and retry later."""
        with self.lock:
            if self.job["status"] != "paused":
                return
            due = time.time() + 300
            self.set_job("paused", f"未能自动继续：{reason} 5 分钟后重试。", pause_until=timestamp(due), pause_due=due)
            self.log("warning", self.job["message"])
            self.try_persist()

    def _next_due(self):
        """The earliest due run, honouring a short grace period after start-up.

        A paused job holds the slot: only its own timed continuation can start.
        """
        earliest = self.started + self.startup_grace
        if self.job["status"] == "paused":
            account = self.find(self.job.get("account_id"))
            due = self.job.get("pause_due")
            return (max(due, earliest), account, "continue") if account and due is not None else None
        scheduled = (item for item in self.accounts if item["next_due"] is not None)
        account = min(scheduled, key=lambda item: item["next_due"], default=None)
        if account is None:
            return None
        trigger = "resume" if account["resume"] else "schedule"
        return max(account["next_due"], earliest), account, trigger

    def schedule_loop(self):
        while True:
            with self.lock:
                if self.shutdown:
                    return
                candidate = None if self.job["status"] in ACTIVE else self._next_due()
                if candidate is None:
                    self.lock.wait(timeout=60)
                    continue
                when, account, trigger = candidate
                delay = when - time.time()
                if delay > 0:
                    self.lock.wait(timeout=min(delay, 60))
                    continue
                account_id = account["id"]
            try:
                self.start(account_id, trigger, scheduled_account=account)
            except RequestError as exc:
                if trigger == "continue" and exc.status != 409:
                    self._delay_continue(str(exc))
                elif exc.status != 409:
                    self._record_start_failure(account, trigger, str(exc))
                else:
                    with self.lock:
                        self.lock.wait(timeout=5)
            except Exception:
                traceback.print_exc()
                self._record_start_failure(account, trigger, "定时任务无法启动，请检查应用状态目录和 server.log。")

    def close(self):
        with self.lock:
            self.shutdown = True
            if self.job["status"] in ACTIVE and self.job["status"] != "pausing":
                self.stop()
            self.lock.notify_all()
        if self.thread:
            self.thread.join()
        self.scheduler.join()
