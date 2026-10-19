"""Third-party cookies, which decide whether Teams can renew its own access.

The failure these guard against is the nastiest kind in this system: it does
not happen when you make the mistake. Sign in with third-party cookies
blocked and everything works - the window renders, prep is captured, the
session cookie is written. Days later every run starts reporting a dead
session, signing in again fixes it for a few more days, and nothing anywhere
mentions cookies.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from prep import cookies, scraper


def _prefs(profile: Path) -> dict:
    return json.loads((profile / "Default" / "Preferences").read_text())


def test_a_fresh_profile_is_set_to_allow_them(tmp_path: Path):
    cookies.allow(tmp_path)
    profile = _prefs(tmp_path)["profile"]
    assert profile["cookie_controls_mode"] == 0
    assert profile["block_third_party_cookies"] is False


def test_a_profile_that_blocks_them_is_corrected(tmp_path: Path):
    """The actual bug: Chromium was set to block them, so renewal failed."""
    path = tmp_path / "Default"
    path.mkdir()
    (path / "Preferences").write_text(json.dumps(
        {"profile": {"cookie_controls_mode": 2}}))

    assert cookies.read_state(tmp_path).allowed is False
    assert cookies.allow(tmp_path), "nothing was changed"
    assert cookies.read_state(tmp_path).allowed is True


def test_nothing_else_in_the_profile_is_disturbed(tmp_path: Path):
    """This file holds the whole browser's settings. Rewriting it wholesale
    would cost far more than it fixes."""
    path = tmp_path / "Default"
    path.mkdir()
    (path / "Preferences").write_text(json.dumps(
        {"profile": {"exit_type": "Normal", "name": "Person 1"},
         "browser": {"window_placement": {"maximized": True}}}))

    cookies.allow(tmp_path)
    after = _prefs(tmp_path)
    assert after["profile"]["name"] == "Person 1"
    assert after["profile"]["exit_type"] == "Normal"
    assert after["browser"]["window_placement"]["maximized"] is True


def test_it_says_when_there_was_nothing_to_do(tmp_path: Path):
    """Called before every launch, so a no-op must not rewrite the file."""
    cookies.allow(tmp_path)
    assert cookies.allow(tmp_path) == ""


def test_an_unreadable_preferences_file_does_not_lose_the_setting(tmp_path: Path):
    """A half-written Preferences file is not a wrong setting - it is a
    profile Chromium resets, which costs the sign-in this is protecting."""
    path = tmp_path / "Default"
    path.mkdir()
    (path / "Preferences").write_text("{ this is not json")

    cookies.allow(tmp_path)
    assert _prefs(tmp_path)["profile"]["cookie_controls_mode"] == 0


def test_no_profile_yet_is_not_reported_as_blocked(tmp_path: Path):
    """"Not yet known" and "blocked" call for completely different advice."""
    state = cookies.read_state(tmp_path / "nothing-here")
    assert state.allowed is None
    assert "blocked" not in state.describe().lower()


def test_blocking_outright_counts_as_blocked(tmp_path: Path):
    path = tmp_path / "Default"
    path.mkdir()
    (path / "Preferences").write_text(json.dumps(
        {"profile": {"default_content_setting_values": {"cookies": 2}}}))
    assert cookies.read_state(tmp_path).allowed is False


def test_a_managed_policy_is_not_papered_over(tmp_path: Path, monkeypatch):
    """A policy beats the profile. Writing a preference that will be ignored,
    and then reporting success, is worse than saying so."""
    policy = tmp_path / "policies"
    policy.mkdir()
    (policy / "cookies.json").write_text(json.dumps(
        {"BlockThirdPartyCookies": True}))
    monkeypatch.setattr(cookies, "POLICY_DIRS", (str(policy),))

    profile = tmp_path / "profile"
    state = cookies.prepare(profile, enabled=True)
    assert state.allowed is False
    assert state.enforced
    assert not (profile / "Default" / "Preferences").exists(), \
        "wrote a preference a policy will ignore"
    assert any("policy" in line for line in cookies.advice(state))


def test_a_policy_that_allows_them_is_read_as_allowed(tmp_path: Path, monkeypatch):
    policy = tmp_path / "policies"
    policy.mkdir()
    (policy / "cookies.json").write_text(json.dumps(
        {"BlockThirdPartyCookies": False}))
    monkeypatch.setattr(cookies, "POLICY_DIRS", (str(policy),))
    assert cookies.read_state(tmp_path).allowed is True


def test_turning_it_off_writes_nothing(tmp_path: Path):
    """The switch exists to reproduce the failure. It must actually do that."""
    cookies.prepare(tmp_path, enabled=False)
    assert not (tmp_path / "Default" / "Preferences").exists()


# -------------------------------------------------------- the launch flags

class Teams:
    disable_gpu = True
    browser_args: list = []
    no_sandbox = None
    third_party_cookies = True


def test_the_cookie_flag_does_not_replace_the_rendering_one(monkeypatch):
    """Chromium keeps only the LAST --disable-features it is given. Appending
    a second one would silently undo software rendering - a blank window
    caused by a change about cookies."""
    monkeypatch.setattr(scraper.os, "geteuid", lambda: 1000)
    args = scraper.browser_args(Teams())

    features = [a for a in args if a.startswith("--disable-features=")]
    assert len(features) == 1, f"more than one would lose all but the last: {features}"
    assert "VizDisplayCompositor" in features[0]
    assert "TrackingProtection3pcd" in features[0]


def test_a_config_flag_is_merged_rather_than_dropped(monkeypatch):
    monkeypatch.setattr(scraper.os, "geteuid", lambda: 1000)

    class WithExtra(Teams):
        browser_args = ["--disable-features=SomethingElse", "--use-gl=angle"]

    args = scraper.browser_args(WithExtra())
    features = [a for a in args if a.startswith("--disable-features=")]
    assert len(features) == 1
    for expected in ("VizDisplayCompositor", "TrackingProtection3pcd",
                     "SomethingElse"):
        assert expected in features[0], expected
    assert "--use-gl=angle" in args, "an unrelated flag was eaten by the merge"


def test_nothing_is_deduplicated_into_nothing(monkeypatch):
    monkeypatch.setattr(scraper.os, "geteuid", lambda: 1000)

    class Duplicated(Teams):
        browser_args = ["--disable-features=TrackingProtection3pcd"]

    args = scraper.browser_args(Duplicated())
    features = [a for a in args if a.startswith("--disable-features=")][0]
    assert features.count("TrackingProtection3pcd") == 1


def test_turning_it_off_removes_the_flag_too(monkeypatch):
    monkeypatch.setattr(scraper.os, "geteuid", lambda: 1000)

    class Without(Teams):
        third_party_cookies = False

    args = scraper.browser_args(Without())
    assert not any("TrackingProtection" in a for a in args)
    assert any("VizDisplayCompositor" in a for a in args), "lost the render fix"


def test_the_cookie_flag_changes_no_identity_and_weakens_no_protection():
    """Allowing a cookie is a setting. Spoofing what Teams sees is not, and
    an accident here would be indistinguishable from doing it on purpose."""
    for flag in scraper.browser_args(Teams()):
        lowered = flag.lower()
        for forbidden in ("user-agent", "useragent", "disable-web-security",
                          "disable-blink-features", "ignore-certificate",
                          "allow-running-insecure"):
            assert forbidden not in lowered, flag


def test_the_capture_asks_before_it_launches():
    """Chromium writes its own preferences back out when it exits, so a write
    made while it is running is undone."""
    import inspect
    source = inspect.getsource(scraper.capture)
    assert "cookies.prepare" in source
    assert source.index("check_profile_lock") < source.index("cookies.prepare") \
        < source.index("launch_persistent_context")
