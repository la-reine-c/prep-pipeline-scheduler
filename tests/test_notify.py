"""Tests for alerting. The dedup behaviour is the part that decides whether
you keep reading these alerts or start ignoring them."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from prep import notify
from prep.notify import EmailConfig, NotifyConfig


def cfg(tmp_path: Path, **kw) -> NotifyConfig:
    base = NotifyConfig(state_path=tmp_path / "alerts.json")
    for key, value in kw.items():
        setattr(base, key, value)
    return base


def test_email_is_sent_with_the_right_fields(tmp_path: Path, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"], sent["port"] = host, port
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent["tls"] = True
        def login(self, user, password): sent["user"] = user
        def send_message(self, message):
            sent["to"] = message["To"]
            sent["subject"] = message["Subject"]
            sent["body"] = message.get_content()

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    config = cfg(tmp_path, email=EmailConfig(
        username="me@gmail.com", password="app-pw", to="me@gmail.com"))

    assert notify.push(config, "Prep: sign-in needed", "session died") is True
    assert sent["to"] == "me@gmail.com"
    assert sent["subject"] == "Prep: sign-in needed"
    assert "session died" in sent["body"]
    assert sent["tls"] is True


def test_a_persistent_failure_alerts_once_not_every_run(tmp_path: Path, monkeypatch):
    """Hourly runs must not mean an identical email every hour."""
    calls = []
    monkeypatch.setattr(notify, "_send_email",
                        lambda c, t, b: (calls.append(t), True)[1])
    config = cfg(tmp_path, email=EmailConfig(
        username="u", password="p", to="me@example.com"))

    for _ in range(5):
        notify.push(config, "Prep: sign-in needed", "dead",
                    dedupe_key="session_expired")

    assert len(calls) == 1, f"alerted {len(calls)} times for one problem"


def test_the_alert_does_not_come_back_while_the_problem_lasts(tmp_path: Path,
                                                             monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_send_email",
                        lambda c, t, b: (calls.append(t), True)[1])
    config = cfg(tmp_path, email=EmailConfig(
        username="u", password="p", to="me@example.com"))

    # Sent three days ago and never fixed.
    notify.record_sent(config, "session_expired", "sign-in needed",
                       now=time.time() - 72 * 3600)
    notify.push(config, "sign-in needed", "still dead", dedupe_key="session_expired")
    assert calls == []


def test_a_fix_edits_the_telegram_alert_in_place(tmp_path: Path, monkeypatch):
    posts = []

    class FakeResponse:
        status_code = 200
        text = "ok"
        def __init__(self, result): self._result = result
        def json(self): return {"ok": True, "result": self._result}

    def fake_post(url, json=None, timeout=None):
        posts.append((url.rsplit("/", 1)[1], json))
        return FakeResponse({"message_id": 77})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    from prep.notify import TelegramConfig
    config = cfg(tmp_path, telegram=TelegramConfig(bot_token="1:A", chat_id="9"))
    notify.push(config, "Prep: Teams sign-in needed", "dead",
                dedupe_key="session_expired")
    notify.clear(config, "session_expired")

    assert [method for method, _ in posts] == ["sendMessage", "editMessageText"]
    edit = posts[1][1]
    assert edit["message_id"] == 77
    assert edit["text"].startswith("Fixed: Prep: Teams sign-in needed")


def test_a_fix_is_announced_on_channels_that_cannot_edit(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_send_email",
                        lambda c, t, b: (calls.append(t), True)[1])
    config = cfg(tmp_path, email=EmailConfig(
        username="u", password="p", to="me@example.com"))

    notify.push(config, "Prep: calendar error", "down", dedupe_key="calendar_failed")
    notify.clear(config, "calendar_failed")
    assert calls == ["Prep: calendar error", "Fixed: Prep: calendar error"]


def test_clearing_a_problem_that_was_never_alerted_sends_nothing(tmp_path: Path,
                                                                monkeypatch):
    """Every good run clears every key - that must stay silent and offline."""
    calls = []
    monkeypatch.setattr(notify, "_send_email",
                        lambda c, t, b: (calls.append(t), True)[1])
    monkeypatch.setattr(notify, "_send_telegram",
                        lambda *a: (calls.append("telegram"), 1)[1])
    config = cfg(tmp_path, email=EmailConfig(
        username="u", password="p", to="me@example.com"))
    notify.clear(config, "session_expired")
    assert calls == []


def test_an_old_style_state_file_still_works(tmp_path: Path, monkeypatch):
    """alerts.json from before used to hold a bare timestamp per problem."""
    (tmp_path / "alerts.json").write_text('{"session_expired": 1700000000.0}')
    calls = []
    monkeypatch.setattr(notify, "_send_email",
                        lambda c, t, b: (calls.append(t), True)[1])
    config = cfg(tmp_path, email=EmailConfig(
        username="u", password="p", to="me@example.com"))

    assert notify.should_send(config, "session_expired") is False
    notify.clear(config, "session_expired")
    assert calls == ["Fixed: session expired"]
    assert notify.should_send(config, "session_expired") is True


def test_recovery_makes_the_next_failure_loud_again(tmp_path: Path, monkeypatch):
    """After a good run, a fresh failure must alert immediately."""
    calls = []
    monkeypatch.setattr(notify, "_send_email",
                        lambda c, t, b: (calls.append(t), True)[1])
    config = cfg(tmp_path, email=EmailConfig(
        username="u", password="p", to="me@example.com"))

    notify.push(config, "sign-in needed", "dead", dedupe_key="session_expired")
    notify.clear(config, "session_expired")          # a run succeeded
    notify.push(config, "sign-in needed", "dead again", dedupe_key="session_expired")
    assert calls == ["sign-in needed", "Fixed: sign-in needed", "sign-in needed"]


def test_routine_news_is_never_suppressed(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_send_email",
                        lambda c, t, b: (calls.append(t), True)[1])
    config = cfg(tmp_path, email=EmailConfig(
        username="u", password="p", to="me@example.com"))

    for _ in range(3):
        notify.push(config, "Prep rescheduled", "3 sessions")   # no dedupe_key
    assert len(calls) == 3


def test_every_channel_is_tried(tmp_path: Path, monkeypatch):
    tried = []
    monkeypatch.setattr(notify, "_send_ntfy",
                        lambda *a: (tried.append("ntfy"), True)[1])
    monkeypatch.setattr(notify, "_send_email",
                        lambda *a: (tried.append("email"), False)[1])
    monkeypatch.setattr(notify, "_send_imessage",
                        lambda *a: (tried.append("imessage"), True)[1])

    config = cfg(tmp_path)
    assert notify.push(config, "t", "b") is True
    assert tried == ["ntfy", "email", "imessage"]


def test_total_failure_is_reported_not_swallowed(tmp_path: Path, monkeypatch, caplog):
    monkeypatch.setattr(notify, "_send_ntfy", lambda *a: False)
    monkeypatch.setattr(notify, "_send_email", lambda *a: False)
    monkeypatch.setattr(notify, "_send_imessage", lambda *a: False)

    config = cfg(tmp_path)
    with caplog.at_level("WARNING"):
        assert notify.push(config, "important", "body") is False
    assert "NOT DELIVERED" in caplog.text


def test_undelivered_alert_is_not_marked_as_sent(tmp_path: Path, monkeypatch):
    """A failed send must not silence the retry on the next run."""
    monkeypatch.setattr(notify, "_send_ntfy", lambda *a: False)
    monkeypatch.setattr(notify, "_send_email", lambda *a: False)
    monkeypatch.setattr(notify, "_send_imessage", lambda *a: False)

    config = cfg(tmp_path)
    notify.push(config, "t", "b", dedupe_key="session_expired")
    assert notify.should_send(config, "session_expired") is True


def test_a_broken_state_file_does_not_stop_alerts(tmp_path: Path, monkeypatch):
    (tmp_path / "alerts.json").write_text("{ this is not json")
    calls = []
    monkeypatch.setattr(notify, "_send_email",
                        lambda c, t, b: (calls.append(t), True)[1])
    config = cfg(tmp_path, email=EmailConfig(
        username="u", password="p", to="me@example.com"))
    notify.push(config, "t", "b", dedupe_key="session_expired")
    assert len(calls) == 1


def test_imessage_is_skipped_off_macos(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    config = cfg(tmp_path, imessage_to="+447700900000")
    assert notify._send_imessage(config, "t", "b") is False


def test_telegram_posts_to_the_bot_api(tmp_path: Path, monkeypatch):
    sent = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

    def fake_post(url, json=None, timeout=None):
        sent["url"] = url
        sent["payload"] = json
        return FakeResponse()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    from prep.notify import TelegramConfig
    config = cfg(tmp_path, telegram=TelegramConfig(bot_token="123:ABC",
                                                   chat_id="4242"))
    assert notify.push(config, "Prep: sign-in needed", "session died") is True
    assert "bot123:ABC/sendMessage" in sent["url"]
    assert sent["payload"]["chat_id"] == "4242"
    assert "session died" in sent["payload"]["text"]


def test_telegram_api_error_is_a_failure_not_a_success(tmp_path: Path, monkeypatch):
    class FakeResponse:
        status_code = 401
        text = "Unauthorized"

    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse())

    from prep.notify import TelegramConfig
    config = cfg(tmp_path, telegram=TelegramConfig(bot_token="bad", chat_id="1"))
    assert notify._send_telegram(config, "t", "b") is None
    # And a bad token must not silence the next attempt.
    notify.push(config, "t", "b", dedupe_key="session_expired")
    assert notify.should_send(config, "session_expired") is True


def test_telegram_unconfigured_is_simply_skipped(tmp_path: Path):
    assert notify._send_telegram(cfg(tmp_path), "t", "b") is None
