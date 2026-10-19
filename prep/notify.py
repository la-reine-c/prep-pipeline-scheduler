"""Alerting. Silence is the enemy: a dead session must be loud, but only once.

Channels are additive - configure any or all. A problem that persists across
runs is alerted once and then stays quiet for as long as it lasts, because an
hourly run against a broken session would otherwise mean an identical alert
every hour, and you would start ignoring them. When a later run finds it
fixed, the Telegram alert is edited to say so, and the other channels get a
short "fixed" message - so the last word on any problem is always the truth.
"""
from __future__ import annotations

import json
import logging
import shutil
import smtplib
import subprocess
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)

# Telegram rejects a sendMessage body over 4096 characters outright, so a busy
# week's reschedule report would fail to send entirely rather than arrive
# clipped. Truncating keeps the alert - the detail is in the log and the export.
TELEGRAM_LIMIT = 4096


@dataclass
class EmailConfig:
    host: str = "smtp.gmail.com"
    port: int = 587
    username: str = ""
    password: str = ""
    to: str = ""
    sender: str = ""


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class NotifyConfig:
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    email: EmailConfig = field(default_factory=EmailConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    imessage_to: str = ""            # macOS only; ignored elsewhere
    state_path: Path = Path("~/prep/alerts.json")


# ------------------------------------------------------------ channels

def _send_ntfy(cfg: NotifyConfig, title: str, body: str, priority: str) -> bool:
    if not cfg.ntfy_topic:
        return False
    try:
        import requests
        requests.post(
            f"{cfg.ntfy_server.rstrip('/')}/{cfg.ntfy_topic}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=15,
        )
        return True
    except Exception as exc:
        log.error("ntfy failed: %s", exc)
        return False


def _send_email(cfg: NotifyConfig, title: str, body: str) -> bool:
    email = cfg.email
    if not (email.to and email.username and email.password):
        return False

    message = EmailMessage()
    message["Subject"] = title
    message["From"] = email.sender or email.username
    message["To"] = email.to
    message.set_content(body)

    try:
        with smtplib.SMTP(email.host, email.port, timeout=30) as server:
            server.starttls()
            server.login(email.username, email.password)
            server.send_message(message)
        return True
    except Exception as exc:
        log.error("email failed: %s", exc)
        return False


def _telegram_call(cfg: NotifyConfig, method: str, payload: dict) -> dict | None:
    """One Bot API call. The decoded result, or None on any failure."""
    try:
        import requests
        response = requests.post(
            f"https://api.telegram.org/bot{cfg.telegram.bot_token}/{method}",
            json={"chat_id": cfg.telegram.chat_id, **payload},
            timeout=20,
        )
        if response.status_code != 200:
            log.error("telegram %s failed: %s %s", method, response.status_code,
                      response.text[:160])
            return None
        try:
            return response.json().get("result") or {}
        except Exception:
            return {}
    except Exception as exc:
        log.error("telegram %s failed: %s", method, exc)
        return None


def _send_telegram(cfg: NotifyConfig, title: str, body: str) -> int | None:
    """Push to a Telegram bot. Returns the message id, so it can be edited.

    The practical phone alert on Linux: free, instant, no carrier gateway, and
    it survives you changing number. UK carriers have largely killed off
    email-to-SMS, and a paid SMS service is not worth it for a few alerts a
    term.
    """
    telegram = cfg.telegram
    if not (telegram.bot_token and telegram.chat_id):
        return None

    text = f"{title}\n\n{body}"
    if len(text) > TELEGRAM_LIMIT:
        keep = TELEGRAM_LIMIT - 40
        text = text[:keep].rsplit("\n", 1)[0] + "\n\n[...truncated]"

    result = _telegram_call(cfg, "sendMessage", {"text": text})
    if result is None:
        return None
    # 0 still means "sent", just with no id to edit later.
    return int(result.get("message_id") or 0) if isinstance(result, dict) else 0


def _edit_telegram(cfg: NotifyConfig, message_id: int, text: str) -> bool:
    if not (cfg.telegram.bot_token and cfg.telegram.chat_id and message_id):
        return False
    return _telegram_call(cfg, "editMessageText",
                          {"message_id": message_id, "text": text}) is not None


def _send_imessage(cfg: NotifyConfig, title: str, body: str) -> bool:
    """Send via the Messages app on macOS.

    Free, and it reaches your phone as iMessage - or as a real SMS if Text
    Message Forwarding is on. The first run raises a permission prompt on the
    Mac, which must be accepted once, in person.
    """
    if not cfg.imessage_to:
        return False
    if not shutil.which("osascript"):
        return False

    target = cfg.imessage_to.replace('"', "")
    text = f"{title}\n{body}".replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        send "{text}" to participant "{target}" of svc
    end tell
    '''
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error("imessage failed: %s", result.stderr.strip()[:200])
            return False
        return True
    except Exception as exc:
        log.error("imessage failed: %s", exc)
        return False


# ------------------------------------------------------------ dedup

def _load_state(path: Path) -> dict:
    try:
        state = json.loads(path.expanduser().read_text())
    except Exception:
        return {}
    if not isinstance(state, dict):
        return {}
    # Older versions stored only the time each alert was sent.
    return {key: value if isinstance(value, dict) else {"sent": value}
            for key, value in state.items()}


def _save_state(path: Path, state: dict) -> None:
    path = path.expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log.warning("could not record alert state: %s", exc)


def should_send(cfg: NotifyConfig, key: str) -> bool:
    """True unless this same problem has already been alerted and not fixed."""
    if not key:
        return True
    return key not in _load_state(cfg.state_path)


def record_sent(cfg: NotifyConfig, key: str, title: str = "",
                telegram_id: int | None = None, now: float | None = None) -> None:
    if not key:
        return
    state = _load_state(cfg.state_path)
    state[key] = {"sent": now if now is not None else time.time(),
                  "title": title, "telegram_id": telegram_id or None}
    _save_state(cfg.state_path, state)


def clear(cfg: NotifyConfig, key: str, now: float | None = None) -> None:
    """The problem is fixed: say so, and let its next occurrence alert again.

    Does nothing - no network at all - unless that problem was alerted.
    """
    if not key:
        return
    state = _load_state(cfg.state_path)
    entry = state.pop(key, None)
    if entry is None:
        return
    _save_state(cfg.state_path, state)

    title = entry.get("title") or key.replace("_", " ")
    when = time.strftime("%a %d %b %H:%M", time.localtime(now or time.time()))
    fixed = f"Fixed: {title}"
    body = f"Sorted itself out by the run at {when}. Nothing to do."

    # Telegram: change the original alert in place, so the chat holds one
    # message per problem and it ends in the right state. If it cannot be
    # edited (too old, deleted, or sent before ids were kept), send instead.
    if not _edit_telegram(cfg, entry.get("telegram_id") or 0, f"{fixed}\n\n{body}"):
        _send_telegram(cfg, fixed, body)
    _send_ntfy(cfg, fixed, body, "low")
    _send_email(cfg, fixed, body)
    _send_imessage(cfg, fixed, body)


# ------------------------------------------------------------ entry point

def push(cfg: NotifyConfig, title: str, body: str, priority: str = "default",
         dedupe_key: str = "") -> bool:
    """Send on every configured channel. Returns True if any succeeded.

    Pass dedupe_key for a recurring condition (a dead session): it is sent
    once, and again only after clear() has reported it fixed. Leave it empty
    for one-off news, such as a successful reschedule, which should always go.
    """
    if dedupe_key and not should_send(cfg, dedupe_key):
        log.info("suppressing repeat alert '%s' (already sent, not yet fixed)",
                 dedupe_key)
        return False

    telegram_id = _send_telegram(cfg, title, body)
    results = [
        _send_ntfy(cfg, title, body, priority),
        _send_email(cfg, title, body),
        telegram_id is not None,
        _send_imessage(cfg, title, body),
    ]

    if not any(results):
        # Nothing configured, or everything failed. Either way it must be
        # visible in the log rather than vanishing.
        log.warning("[notify - NOT DELIVERED] %s: %s", title, body)
        return False

    if dedupe_key:
        record_sent(cfg, dedupe_key, title, telegram_id)
    return True
