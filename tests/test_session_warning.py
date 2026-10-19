"""The early warning on a normal run.

The point of it is timing: an alert that arrives while the session still
works turns an outage into an errand. An alert that arrives on every run is
one you learn to ignore, which is the same as no alert at all.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from prep import run, session


class Recorder:
    def __init__(self):
        self.pushed = []
        self.cleared = []

    def push(self, cfg, title, body, priority="default", dedupe_key=""):
        self.pushed.append((title, dedupe_key))
        return True

    def clear(self, cfg, key):
        self.cleared.append(key)


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(
        teams=SimpleNamespace(profile_dir=tmp_path / "profile"),
        notify=SimpleNamespace(),
    )


def health(days=None, persistent=True, signed_in=True):
    cookies = []
    if signed_in:
        name = "ESTSAUTHPERSISTENT" if persistent else "ESTSAUTH"
        expires = (datetime.now(timezone.utc) + timedelta(days=days)
                   if days is not None else None)
        cookies.append(session.Cookie(name, "login.microsoftonline.com", expires))
    return session.SessionHealth(found=True, total=len(cookies),
                                 microsoft=cookies)


def run_with(monkeypatch, cfg, state) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(run.notify, "push", recorder.push)
    monkeypatch.setattr(run.notify, "clear", recorder.clear)
    monkeypatch.setattr(session, "read", lambda profile: state)
    run._warn_if_session_is_running_out(cfg)
    return recorder


def test_a_healthy_session_says_nothing(monkeypatch, cfg):
    recorder = run_with(monkeypatch, cfg, health(days=45))
    assert recorder.pushed == [], "alerted about a session with 45 days left"


def test_an_expiring_session_warns_while_it_still_works(monkeypatch, cfg):
    recorder = run_with(monkeypatch, cfg, health(days=4))
    assert len(recorder.pushed) == 1
    title, key = recorder.pushed[0]
    assert "within 4 days" in title
    assert key == "session_expiring", "no dedupe key - this would alert hourly"


def test_a_session_only_cookie_is_the_louder_problem(monkeypatch, cfg):
    recorder = run_with(monkeypatch, cfg, health(days=None, persistent=False))
    assert len(recorder.pushed) == 1
    assert "will not survive" in recorder.pushed[0][0]


def test_a_recovered_session_clears_the_old_alert(monkeypatch, cfg):
    """Otherwise you never hear it is fixed, and the next problem stays silent."""
    recorder = run_with(monkeypatch, cfg, health(days=60))
    assert "session_expiring" in recorder.cleared
    assert "session_not_persistent" in recorder.cleared


def test_a_profile_that_cannot_be_read_never_breaks_the_run(monkeypatch, cfg):
    def explode(profile):
        raise OSError("profile locked")
    recorder = Recorder()
    monkeypatch.setattr(run.notify, "push", recorder.push)
    monkeypatch.setattr(run.notify, "clear", recorder.clear)
    monkeypatch.setattr(session, "read", explode)
    run._warn_if_session_is_running_out(cfg)     # must not raise
    assert recorder.pushed == []


def test_no_sign_in_at_all_is_left_to_the_capture_to_report(monkeypatch, cfg):
    """A profile with no session fails the capture loudly already."""
    recorder = run_with(monkeypatch, cfg, health(signed_in=False))
    assert recorder.pushed == []
