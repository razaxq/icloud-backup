"""Run the bundled Linux icloudpd 1.33.0 as a one-way photo archive."""

from __future__ import annotations

from contextlib import closing
import codecs
import hashlib
import json
import os
import re
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .strict_cli import PROTOCOL
from . import catalog, retention, space


class SyncError(Exception):
    """A fatal, actionable error that is safe to show to the operator."""


class SyncCancelled(SyncError):
    """A pending or running transfer was stopped by the operator."""


# Do not request --force-size: adjusted/alternative are absent on most assets.
# With original requested explicitly, icloudpd skips those missing variants.
MEDIA_OPTIONS = (
    "--size", "original", "--size", "alternative", "--size", "adjusted",
    "--live-photo-size", "original",
    "--align-raw", "as-is", "--live-photo-mov-filename-policy", "original",
    "--file-match-policy", "name-id7", "--keep-unicode-in-filenames",
    "--folder-structure", "{:%Y/%m}", "--xmp-sidecar",
)

# The strict adapter counts verified, already-local assets, once per complete
# asset including its original Live Photo companion.
INCREMENTAL_UNTIL_FOUND = 100

INTEGRITY_REASONS = {
    "inventory": "图库记录与归档清单不一致",
    "archived-resource": "同一版本缺少先前记录的原件或配套文件，或其云端描述已发生变化",
    "raw": "缺少 RAW 或替代原件",
    "auxiliary": "附属资源未知或缺少下载信息",
    "conflicting-resource": "同一资源有不同的云端内容，需要核对",
    "adjusted": "缺少编辑成片",
    "edit": "缺少完整的编辑数据或成片",
    "live": "实况照片缺少原版配套视频",
    "original": "缺少照片或视频原件",
    "signature": "iCloud 返回的文件大小或校验值无法验证",
    "download": "媒体下载未完成",
    "mismatch": "下载内容与 iCloud 的文件大小或校验值不符",
    "metadata": "元数据文件写入失败",
    "local-path": "本地媒体路径不可用",
    "io": "读写文件失败",
    "record": "iCloud 返回了无法识别的媒体记录",
}

_LOG = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (INFO|ERROR|CRITICAL|WARNING|DEBUG)\s+(.+)$")
_SHARED = re.compile(r"SharedSync-[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}")
_SMS_DEVICE = re.compile(r"^([a-z]): (.{1,40})$")
_TWO_FACTOR = re.compile(
    r"Please enter two-factor authentication code"
    r"(?: or device index \([a-z](?:\.\.[a-z])?\) to send SMS with a code"
    r"| that you received over SMS)?:"
)


def session_path(session_dir, domain, username):
    """Private cookie directory for one account; stable across app versions."""
    account_id = hashlib.sha256((domain + "\0" + username.casefold()).encode()).hexdigest()
    return (Path(session_dir) / account_id).resolve()


@dataclass
class SyncResult:
    downloaded: int = 0
    full_scan: bool = True
    warnings: list = field(default_factory=list)  # finished with caveats
    failures: list = field(default_factory=list)  # a library could not be completed
    review: str = ""

    @staticmethod
    def note(bucket, text):
        if text and text not in bucket:
            bucket.append(text)


@dataclass
class _Run:
    zone: str = "PrimarySync"
    downloads: int = 0
    libraries: set = field(default_factory=set)
    sms_devices: list = field(default_factory=list)
    finished: bool = False
    warning: str = ""
    reason: str = ""  # fatal for the whole job: login, region or web-access problems
    denied: bool = False
    checked: bool = False
    missing_hidden_album: bool = False
    issues: dict = field(default_factory=dict)

    def line(self, line, on_event):
        # Only emit app-authored messages. Exceptions can contain signed download
        # URLs, account identifiers or an entire response body.
        if line.startswith(PROTOCOL):
            marker = line[len(PROTOCOL):]
            if item := re.fullmatch(r"item ([01]) ([0-9a-f]{64})", marker):
                on_event("item", (item[2], item[1] == "1"))
                return
            if low := re.fullmatch(r"space ([0-9]{1,20}) ([0-9]{1,20})", marker):
                raise space.LowSpace(int(low[1]), max(space.RESERVE, int(low[2])))
            if marker == "checked":
                self.checked = True
                return
            if marker == "removed":
                on_event("removed", "iCloud 已确认删除一个已备份项目。")
                return
            if marker == "cleanup-complete":
                self.finished = True
                return
            try:
                reason, name = json.loads(marker)
                if reason not in INTEGRITY_REASONS or not isinstance(name, str):
                    raise ValueError("unexpected issue")
            except (TypeError, ValueError):
                reason, name = "record", ""
            self.issues[reason] = self.issues.get(reason, 0) + 1
            if self.issues[reason] <= 3:
                safe = "".join(c if c.isprintable() else "_" for c in name[:255])
                on_event("warning", INTEGRITY_REASONS[reason] + (f"（{safe}）。" if safe else "。"))
            return
        if line == "PrimarySync" or _SHARED.fullmatch(line):
            self.libraries.add(line)
            return
        device = _SMS_DEVICE.fullmatch(line)
        if device:
            self.sms_devices.append(device.groups())
            return
        log = _LOG.fullmatch(line)
        if not log:
            if line == "KeyError: 'Hidden'":
                self.missing_hidden_album = True
            elif line.startswith("KeyError:") or line.startswith("icloudpd has saved the photo record"):
                self.warning = "iCloud 返回了无法识别的媒体记录，相应项目已跳过。"
            return
        level, message = log.groups()
        lower = message.lower()
        if level == "INFO" and message.startswith("Downloaded "):
            self.downloads += 1
            on_event("download", "已写入一个媒体文件。")
            return
        if level == "INFO" and message.startswith("All ") and message.endswith(" have been downloaded"):
            self.finished = True
        elif level == "INFO" and message.startswith("Downloading "):
            count = re.match(r"Downloading (\d+) ", message)
            if count:
                on_event("info", f"正在检查 {count.group(1)} 个图库项目。")
            elif message.startswith("Downloading ??? "):
                on_event("info", "正在检查最近的图库项目。")
        elif level == "INFO" and message.startswith("Found ") and "consecutive previously downloaded" in message:
            on_event("info", "已遇到连续的已归档项目，快速检查结束。")
        elif "access_denied" in lower or "access denied" in lower:
            self.denied = True
            self.reason = "iCloud 拒绝了照片网页访问。请检查账户的网页访问设置。"
        elif lower.startswith("icloud listed only "):
            self.warning = "iCloud 返回的项目数少于图库报告的数量，需要重新完整扫描。"
            on_event("warning", self.warning)
        elif lower.startswith("skipping asset ") and "master record" in lower:
            self.warning = "iCloud 未返回部分项目的主记录，需要重新完整扫描。"
            if missing := re.match(r"Skipping asset ([^\s:]+):", message, re.I):
                on_event("item", (catalog.token([self.zone, missing[1]]), False))
        elif lower in ("unknown album: hidden", "album not found: hidden"):
            self.missing_hidden_album = True
        elif "domain parameter" in lower:
            self.reason = "Apple 要求使用另一服务地区。请核对账户的 iCloud 服务地区。"
        elif "failed to send two-" in lower:
            self.reason = "Apple 未能发送验证码，请稍后重试。"
        elif "verify two-" in lower:
            self.reason = "Apple 未接受验证码。请重试并输入受信任设备上最新的验证码。"
        elif "failed to log" in lower or "invalid email" in lower or "srp authentication" in lower:
            self.reason = "Apple 登录失败，请核对账户、密码和所选服务地区。"
        elif "web access to icloud data is disabled" in lower:
            self.reason = "请在 Apple 设备的 iCloud 设置中打开“在网页上访问 iCloud 数据”。"
        elif "timed out waiting for the icloud web access approval" in lower:
            self.reason = "Apple 设备上的网页访问批准已超时，请重新开始下载。"
        elif "has not finished indexing" in lower:
            self.reason = "Apple 正在为图库建立索引，请稍后再下载。"
        elif "security keys instead of codes" in lower:
            raise SyncError("此 Apple 账户使用实体安全密钥登录，当前应用只支持验证码。")
        elif (lower == "session error, re-authenticating..." or
              (lower.startswith("error downloading ") and ", retrying after " in lower)):
            # The strict adapter independently verifies the final transfer result.
            return
        elif level in ("ERROR", "CRITICAL") or message.startswith("Could not write XMP sidecar"):
            if not self.warning:
                self.warning = "下载器报告部分项目或元数据未能完整读取或写入。请检查网络、剩余空间和目录权限。"
                on_event("warning", self.warning)


def _check_cancel(cancel):
    if cancel.is_set():
        raise SyncCancelled("已停止同步。")


def _answer(text, username, prompt, run):
    # Both input() and getpass() print these prompts without a trailing newline.
    # Only accept a full standalone upstream prompt, never a substring of a log.
    text = text.rstrip(" \r")
    if text == f"iCloud Password for {username}:":
        value = prompt("password", "输入 Apple 账户密码以继续登录。")
        valid = isinstance(value, str) and 0 < len(value) <= 2048
    elif _TWO_FACTOR.fullmatch(text):
        letters = [letter for letter, _ in run.sms_devices] if "or device index" in text else []
        if letters:
            options = "、".join(f"{letter}（{number}）" for letter, number in run.sms_devices)
            message = ("请允许 Apple 设备上的登录请求，并输入显示的 6 位验证码。"
                       f"收不到时，可输入号码前的字母改用短信接收：{options}。")
        elif "over SMS" in text:
            message = "请输入短信收到的 6 位验证码。"
        else:
            message = "请允许 Apple 设备上的登录请求，并输入显示的 6 位验证码。"
        run.sms_devices = []
        value = prompt("code", message)
        if isinstance(value, str):
            value = value.strip().lower()
        valid = isinstance(value, str) and (bool(re.fullmatch(r"[0-9]{6}", value)) or value in letters)
    elif text == "Please choose an option: [0]:":
        # Legacy two-step verification offers a numbered device list; upstream
        # defaults to its first trusted device and then asks for the code.
        return "0"
    elif text == "Please enter two-step authentication code:":
        value = prompt("code", "请输入 Apple 发送到第一个受信任设备的验证码。")
        if isinstance(value, str):
            value = value.strip()
        valid = isinstance(value, str) and bool(re.fullmatch(r"[0-9]{4,6}", value))
    else:
        return None
    if not valid or any(char in value for char in "\r\n\0"):
        raise SyncError("输入的登录信息格式不正确，请重新开始下载。")
    return value


# Keep icloudpd in the service's session so the lifecycle script can stop every
# remaining process, but in its own process group so a cancellation can stop
# PyInstaller's bootloader and its child without signalling the service.
_GROUP = {"process_group": 0} if sys.version_info >= (3, 11) else {"preexec_fn": os.setpgrp}


def _stop(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        except ProcessLookupError:
            process.wait()


def _invoke(binary, args, account, cwd, prompt, on_event, cancel, monitor):
    _check_cancel(cancel)
    result = _Run(zone=args[args.index("--library") + 1] if "--library" in args else "PrimarySync")
    try:
        process = subprocess.Popen(
            [str(binary), *args], cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            umask=0o022,  # NAS folder ACLs govern access; cookies stay under 0700.
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
            **_GROUP,
        )
    except OSError as exc:
        raise SyncError("无法启动内置下载器，请检查安装包和 NAS 的处理器架构。") from exc
    try:
        pending = ""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                _check_cancel(cancel)
                monitor.poll()
                if not selector.select(timeout=0.4):
                    continue
                chunk = os.read(process.stdout.fileno(), 8192)
                pending += decoder.decode(chunk, final=not chunk)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    result.line(line.strip(), on_event)
                if not chunk:
                    result.line(pending.strip(), on_event)
                    break
                answer = _answer(pending, account, prompt, result)
                if answer is not None:
                    _check_cancel(cancel)
                    process.stdin.write((answer + "\n").encode("utf-8"))
                    del answer
                    pending = ""
                # Never buffer an unbounded diagnostic line.
                if len(pending) > 131072:
                    pending = pending[-4096:]
        while process.poll() is None:
            _check_cancel(cancel)
            cancel.wait(0.1)
        _check_cancel(cancel)
        return process.returncode, result
    finally:
        _stop(process)
        process.stdin.close()
        process.stdout.close()


def _short_library(name):
    return name if len(name) <= 22 else name[:22] + "…"


def run_sync(*, binary, username, domain, destination, session_dir,
             prompt, on_event, cancel, include_shared=True, full_scan=True,
             skip_phases=frozenset(), on_phase=None, cleanup=None):
    """Archive one account and return a SyncResult.

    Raises SyncError only when the whole job cannot continue (login, region,
    web access). A library that fails is recorded and the remaining libraries
    are still scanned. The caller serializes runs and provides a cancel-aware,
    blocking prompt(kind, message). All on_event(kind, message) text and every
    message in the result are suitable for display.

    Library phases named in skip_phases ("<library>" and
    "<library>/Hidden") were completed by an earlier, paused or interrupted
    attempt and are skipped; on_phase(name) reports each phase completed now.
    """
    if (domain not in ("cn", "com") or not username or username.startswith("-")
            or len(username) > 320 or any(ord(c) < 32 for c in username)):
        raise SyncError("Apple 账户或 iCloud 服务地区格式不正确。")
    target = Path(destination)
    binary = Path(binary).resolve()
    cookies = session_path(session_dir, domain, username)
    monitor = space.SpaceMonitor(target, on_event)
    try:
        cookies.mkdir(parents=True, exist_ok=True, mode=0o700)
        cookies.chmod(0o700)
        if not target.is_absolute() or not target.is_dir():
            raise SyncError("保存目录不存在，请检查 NAS 目录授权。")
    except OSError as exc:
        raise SyncError("无法访问保存目录或私有会话目录，请检查空间和权限。") from exc

    def check_job():
        _check_cancel(cancel)
        monitor.poll()

    check_job()
    base = ["--no-progress-bar", "--log-level", "info", "--domain", domain,
            "--password-provider", "console", "--mfa-provider", "console",
            "--username", username, "--cookie-directory", str(cookies)]
    rule = retention.policy(**(cleanup or {}))
    preview = rule["mode"] != "off"
    if preview:
        full_scan = True
    scan = [] if full_scan else ["--until-found", str(INCREMENTAL_UNTIL_FOUND)]
    outcome = SyncResult(full_scan=full_scan)
    use_adp = False

    # An unrecognized Apple record can make upstream write a diagnostic JSON
    # containing signed URLs to its working directory. Remove it after each job.
    with tempfile.TemporaryDirectory(prefix="job-", dir=cookies) as private_workdir:
        inventory = str(Path(private_workdir) / "cleanup.sqlite")

        audit = full_scan
        checkpoint = cookies / "archive-review.sqlite"
        resumed = False
        done = set()

        def run(options, label, listing=False, optional_hidden=False, phase=None, prefix=()):
            """Run one phase; return its _Run, or None when it failed or was skipped."""
            nonlocal use_adp
            if phase in skip_phases and (not prefix or resumed):
                on_event("info", f"{label}已在上次完成，跳过。")
                return None
            on_event("info", f"正在{'查找' if listing else '扫描'}{label}。")

            def invoke():
                extra = ["--adp-web-access"] if use_adp else []
                return _invoke(binary, [*prefix, *base, *extra, *options], username, private_workdir,
                               prompt, on_event, cancel, monitor)

            code, result = invoke()
            outcome.downloaded += result.downloads
            if result.denied and not use_adp:
                use_adp = True
                on_event("info", "正在请求 iCloud 网页访问。如果 Apple 发出通知，请在受信任设备上批准。")
                code, result = invoke()
                outcome.downloaded += result.downloads
            if result.reason:
                raise SyncError(result.reason)
            issues = [f"{INTEGRITY_REASONS[name]}（{count} 项）" for name, count in result.issues.items()]
            if result.warning:
                issues.append(result.warning)
            if optional_hidden and result.missing_hidden_album and not issues:
                outcome.note(outcome.warnings, f"{label}未能完成：该共享图库未提供隐藏相簿。")
                return None
            if code or issues or not result.checked or (not listing and not result.finished):
                detail = "；".join(issues) if issues else ("下载器异常退出。" if code else "未收到原件校验完成的确认。")
                outcome.note(outcome.failures, f"{label}未能完成：{detail}")
                on_event("warning", f"{label}未能完成，继续处理其他图库。")
                return None
            if phase:
                done.add(phase)
                if audit and not outcome.failures and not outcome.warnings:
                    retention.save_checkpoint(inventory, checkpoint, target, include_shared, done)
                if on_phase:
                    on_phase(phase)
            return result

        def download_library(name, folder, label, shared):
            if folder.resolve() != folder:
                outcome.note(outcome.failures, f"{label}未能完成：图库保存路径已变为符号链接。")
                return
            args = ["--library", name, "--directory", str(folder), *MEDIA_OPTIONS, *scan]
            record = ("--fn-archive-record", inventory) if audit else ()
            run(args, label, phase=name, prefix=record)
            # Some shared zones may not expose a hidden smart album; that is a
            # caveat rather than a failed archive.
            run(args + ["--album", "Hidden"], f"{label}的隐藏项目", optional_hidden=shared,
                phase=name + "/Hidden", prefix=record)

        if audit:
            resumed = not preview and retention.resume_inventory(checkpoint, inventory, target, include_shared, skip_phases)
            if resumed:
                done.update(skip_phases)
            else:
                checkpoint.unlink(missing_ok=True)
                retention.create_inventory(inventory, target, [("PrimarySync", target)], check=check_job)
        download_library("PrimarySync", target, "个人图库", False)
        if include_shared:
            listed = run(["--list-libraries"], "共享图库", listing=True)
            if listed is not None:
                if "PrimarySync" not in listed.libraries:
                    outcome.note(outcome.failures, "未能确认账户的图库列表是否完整。")
                for library in sorted(listed.libraries - {"PrimarySync"}):
                    shared_folder = target / "共享图库" / library
                    if audit:
                        with closing(sqlite3.connect(inventory)) as db, db:
                            retention.index_legacy(db, target, library, shared_folder, check=check_job)
                    download_library(library, shared_folder, f"共享图库（{_short_library(library)}）", True)
        if audit:
            complete = not outcome.failures and not outcome.warnings
            space.check(target)
            try:
                verified, incomplete, unassigned, selected = retention.publish_review(
                    inventory, target, cookies.name, rule, complete, recheck=resumed, check=check_job)
            except (ValueError, KeyError, OSError, retention.IntegrityError) as exc:
                if isinstance(exc, OSError):
                    space.check(target)
                raise SyncError("归档核对报告未完成，请检查原件、目录权限和磁盘空间。") from exc
            outcome.review = (f"已生成完整档案 {verified} 份、不完整档案 {incomplete} 份，"
                              f"旧文件归属待确认 {unassigned} 份。")
            if incomplete:
                outcome.note(outcome.failures, f"完整扫描仍有 {incomplete} 个项目未通过原件校验。")
            if preview and complete and not incomplete:
                outcome.review += f" 策略预览选中 {selected} 项，云端原件全部保留。"
            on_event("info", outcome.review)
            checkpoint.unlink(missing_ok=True)
    return outcome
