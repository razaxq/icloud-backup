"""Outbound notifications: generic webhook, Bark, ntfy, Telegram and SMTP mail.

Every message raised as NotifyError is safe to show; none of them contain the
configured URLs or tokens, which often carry credentials themselves.
"""
import datetime as dt
import json
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

APP_TITLE = "iCloud 照片归档"
CHANNELS = ("none", "webhook", "bark", "ntfy", "telegram", "smtp")
EVENTS = ("auth", "error", "partial", "success")
SECURITY = ("ssl", "starttls", "none")
TIMEOUT = 12

DEFAULTS = dict(
    channel="none",
    events=dict(auth=True, error=True, partial=False, success=False),
    webhook_url="", bark_url="", ntfy_url="", ntfy_token="",
    telegram_token="", telegram_chat="",
    smtp_host="", smtp_port=465, smtp_security="ssl", smtp_user="", smtp_password="",
    smtp_from="", smtp_to="",
)
SECRETS = ("webhook_url", "bark_url", "ntfy_url", "ntfy_token", "telegram_token", "smtp_password")
REQUIRED = {
    "webhook": ("webhook_url",), "bark": ("bark_url",), "ntfy": ("ntfy_url",),
    "telegram": ("telegram_token", "telegram_chat"), "smtp": ("smtp_host", "smtp_to"),
}
CHANNEL_FIELDS = {
    "none": (), "webhook": ("webhook_url",), "bark": ("bark_url",),
    "ntfy": ("ntfy_url", "ntfy_token"), "telegram": ("telegram_token", "telegram_chat"),
    "smtp": ("smtp_host", "smtp_user", "smtp_password", "smtp_from", "smtp_to"),
}
_EMAIL = re.compile(r"[^@\s<>,;\"]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_TELEGRAM_TOKEN = re.compile(r"[0-9]{5,16}:[A-Za-z0-9_-]{30,64}")
_TELEGRAM_CHAT = re.compile(r"-?[0-9]{1,20}|@[A-Za-z0-9_]{5,32}")
_NTFY_PATH = re.compile(r"(?:/[^/\s]+)*/[A-Za-z0-9_-]{1,64}")


class NotifyError(Exception):
    """A display-safe delivery or configuration problem."""


def _clean(value, limit):
    value = value.strip() if isinstance(value, str) else ""
    if len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("通知设置含有过长内容或无效字符。")
    return value


def _url(value, *, ntfy=False):
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or " " in value:
        raise ValueError("通知地址需要以 http:// 或 https:// 开头。")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("通知地址的端口无效。") from None
    if port == 0:
        raise ValueError("通知地址的端口无效。")
    if ntfy and not _NTFY_PATH.fullmatch(parts.path.rstrip("/") or "/"):
        raise ValueError("ntfy 地址需要包含主题，例如 https://ntfy.sh/my-photos。")
    return value


def _recipients(value):
    items = [item.strip() for item in re.split(r"[,;，；\s]+", value) if item.strip()]
    if not items or len(items) > 5 or not all(_EMAIL.fullmatch(item) for item in items):
        raise ValueError("请填写 1 到 5 个有效的收件邮箱，用逗号分隔。")
    return ", ".join(items)


def normalize(raw):
    """Fill defaults for a stored configuration without rejecting it."""
    config = json.loads(json.dumps(DEFAULTS))
    if not isinstance(raw, dict):
        return config
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if key == "events":
            value = value if isinstance(value, dict) else {}
            config["events"] = {name: bool(value.get(name, default[name])) for name in EVENTS}
        elif isinstance(default, int):
            config[key] = value if type(value) is int else default
        elif isinstance(value, str):
            config[key] = value
    if config["channel"] not in CHANNELS:
        config["channel"] = "none"
    if config["smtp_security"] not in SECURITY:
        config["smtp_security"] = "ssl"
    return config


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}" if parts.hostname else ""


def _ntfy_server(url):
    # Tokens belong to the publishing server, including its reverse-proxy prefix.
    parts = urlsplit(url)
    return (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
            parts.path.rstrip("/").rpartition("/")[0])


def public(config):
    """The configuration as shown to the page: secrets reduced to set/unset."""
    shown = {key: value for key, value in config.items() if key not in SECRETS}
    shown["secrets"] = {key: bool(config.get(key)) for key in SECRETS}
    shown["previews"] = {key: _origin(config[key]) for key in ("webhook_url", "bark_url", "ntfy_url")
                         if config.get(key)}
    return shown


def merge(current, data):
    """Update only the selected channel; reuse credentials within their original service."""
    if not isinstance(data, dict):
        raise ValueError("通知设置格式无效。")
    config = normalize(current)
    channel = data.get("channel")
    if channel not in CHANNELS:
        raise ValueError("请选择有效的通知方式。")
    config["channel"] = channel
    events = data.get("events")
    if not isinstance(events, dict):
        raise ValueError("请选择需要通知的事件。")
    config["events"] = {name: events.get(name) is True for name in EVENTS}

    clear = data.get("clear", [])
    if not isinstance(clear, list) or not all(item in SECRETS for item in clear):
        raise ValueError("通知设置格式无效。")
    if channel == "none":
        for key in SECRETS:
            config[key] = ""
        return config
    old = config.copy()
    for key in CHANNEL_FIELDS[channel]:
        if key in SECRETS:
            value = _clean(data.get(key), 2048)
            if value or key in clear:
                config[key] = value
        elif key in data:
            config[key] = _clean(data[key], 1024 if key == "smtp_to" else 320)

    if channel in ("ntfy", "bark", "webhook"):
        key = channel + "_url"
        config[key] = _url(config[key], ntfy=channel == "ntfy")
        if channel == "ntfy" and config[key] and old[key] and not _clean(data.get("ntfy_token"), 2048):
            try:
                same_server = _ntfy_server(old[key]) == _ntfy_server(config[key])
            except ValueError:  # an invalid endpoint saved by an older version
                same_server = False
            if not same_server:
                config["ntfy_token"] = ""
    elif channel == "telegram":
        if config["telegram_token"] and not _TELEGRAM_TOKEN.fullmatch(config["telegram_token"]):
            raise ValueError("Telegram 机器人令牌格式不正确。")
        if config["telegram_chat"] and not _TELEGRAM_CHAT.fullmatch(config["telegram_chat"]):
            raise ValueError("Telegram 会话 ID 应为数字或以 @ 开头的频道名。")
    elif channel == "smtp":
        port = data.get("smtp_port", config["smtp_port"])
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError("邮件服务器端口无效。")
        config["smtp_port"] = port
        security = data.get("smtp_security", config["smtp_security"])
        if security not in SECURITY:
            raise ValueError("邮件加密方式无效。")
        config["smtp_security"] = security
        if config["smtp_host"] and not _HOST.fullmatch(config["smtp_host"]):
            raise ValueError("邮件服务器地址无效。")
        if config["smtp_from"] and not _EMAIL.fullmatch(config["smtp_from"]):
            raise ValueError("发件邮箱格式不正确。")
        if config["smtp_to"]:
            config["smtp_to"] = _recipients(config["smtp_to"])
        identity = ("smtp_port", "smtp_security", "smtp_user")
        changed = (old["smtp_host"].lower() != config["smtp_host"].lower()
                   or any(old[k] != config[k] for k in identity))
        if changed and not _clean(data.get("smtp_password"), 2048):
            config["smtp_password"] = ""
        if config["smtp_user"] and not config["smtp_password"]:
            raise ValueError("请为当前邮件服务器和用户名填写密码或授权码。匿名发送时请同时清空用户名。")

    missing = [key for key in REQUIRED.get(channel, ()) if not config[key]]
    if missing:
        raise ValueError("请填写所选通知方式的必填项。")
    if channel == "smtp" and not (config["smtp_from"] or _EMAIL.fullmatch(config["smtp_user"])):
        raise ValueError("请填写发件邮箱。")
    return config


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The configured endpoint receives the authenticated POST directly.
        return None


def _post(url, payload, headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", "User-Agent": "fn-icloud-photos", **(headers or {})})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=TIMEOUT) as response:
            response.read(65536)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise NotifyError(f"通知服务拒绝了请求（HTTP {exc.code}），请检查令牌或地址。") from None
        raise NotifyError(f"通知服务返回 HTTP {exc.code}。") from None
    except (URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        kind = "连接超时" if "timed out" in str(reason).lower() else "无法连接"
        raise NotifyError(f"{kind}通知服务，请检查 NAS 的网络和通知地址。") from None


def _ntfy(config, title, body, event):
    parts = urlsplit(config["ntfy_url"])
    path = parts.path.rstrip("/")
    topic = path.rsplit("/", 1)[-1]
    root = urlunsplit((parts.scheme, parts.netloc, path[: -len(topic)] or "/", "", ""))
    headers = {"Authorization": "Bearer " + config["ntfy_token"]} if config["ntfy_token"] else {}
    _post(root, {"topic": topic, "title": title, "message": body, "tags": ["camera"],
                 "priority": 4 if event in ("auth", "error") else 3}, headers)


def _smtp(config, title, body):
    sender = config["smtp_from"] or config["smtp_user"]
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = sender
    message["To"] = config["smtp_to"]
    message.set_content(body)
    context = ssl.create_default_context()
    try:
        if config["smtp_security"] == "ssl":
            server = smtplib.SMTP_SSL(config["smtp_host"], config["smtp_port"], timeout=TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=TIMEOUT)
        with server:
            if config["smtp_security"] == "starttls":
                server.starttls(context=context)
            if config["smtp_user"]:
                server.login(config["smtp_user"], config["smtp_password"])
            server.send_message(message)
    except smtplib.SMTPAuthenticationError:
        raise NotifyError("邮件服务器拒绝了用户名或密码。") from None
    except smtplib.SMTPRecipientsRefused:
        raise NotifyError("邮件服务器拒绝了收件地址。") from None
    except ssl.SSLError:
        raise NotifyError("与邮件服务器建立加密连接失败，请检查端口和加密方式。") from None
    except smtplib.SMTPException:
        raise NotifyError("邮件服务器拒绝发送这封通知。") from None
    except OSError:
        raise NotifyError("无法连接邮件服务器，请检查地址、端口和 NAS 的网络。") from None


def send(config, title, body, event):
    channel = config.get("channel")
    if channel == "webhook":
        _post(config["webhook_url"], {
            "app": "icloud-photos", "event": event, "title": title, "message": body,
            "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
    elif channel == "bark":
        _post(config["bark_url"], {"title": title, "body": body, "group": APP_TITLE})
    elif channel == "ntfy":
        _ntfy(config, title, body, event)
    elif channel == "telegram":
        _post(f"https://api.telegram.org/bot{config['telegram_token']}/sendMessage",
              {"chat_id": config["telegram_chat"], "text": f"{title}\n{body}",
               "disable_web_page_preview": True})
    elif channel == "smtp":
        _smtp(config, title, body)
    else:
        raise NotifyError("尚未设置通知方式。")


def mask_account(account):
    """Keep enough of an Apple account to recognise it in a third-party push."""
    if "@" in account:
        name, _, domain = account.partition("@")
        return (name[:2] + "***" if len(name) > 2 else name[:1] + "***") + "@" + domain
    return account[:3] + "****" + account[-4:] if len(account) > 7 else account[:1] + "***"
