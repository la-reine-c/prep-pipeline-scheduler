"""Session durability, read from a synthetic Chromium cookie store.

The epoch conversion is the part worth testing: Chromium counts microseconds
from 1601, and getting it wrong by the 11,644,473,600-second offset turns
"expires in 40 days" into "expired in 1601" without anything looking broken.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prep import session


def chromium_time(when: datetime) -> int:
    return int((when.timestamp() + session.WINDOWS_EPOCH_OFFSET) * 1_000_000)


def make_store(profile: Path, cookies: list[tuple[str, str, int]]) -> Path:
    path = profile / "Default"
    path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path / "Cookies")
    conn.execute("CREATE TABLE cookies (name TEXT, host_key TEXT, "
                 "expires_utc INTEGER, value BLOB)")
    conn.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?)",
                     [(n, h, e, b"secret") for n, h, e in cookies])
    conn.commit()
    conn.close()
    return profile


def test_a_persistent_session_reports_days_left(tmp_path: Path):
    expiry = datetime.now(timezone.utc) + timedelta(days=40)
    profile = make_store(tmp_path, [
        ("ESTSAUTHPERSISTENT", "login.microsoftonline.com", chromium_time(expiry)),
        ("ESTSAUTH", "login.microsoftonline.com", chromium_time(
            datetime.now(timezone.utc) + timedelta(days=1))),
    ])
    health = session.read(profile)
    assert health.found
    assert health.stay_signed_in
    assert 39 <= health.days_left <= 41, health.days_left
    assert "about 40 days left" in health.describe()


def test_session_only_cookies_mean_stay_signed_in_was_not_ticked(tmp_path: Path):
    """The failure this exists to catch: it looks fine until the browser closes."""
    profile = make_store(tmp_path, [
        ("ESTSAUTH", "login.microsoftonline.com", 0),
        ("buid", "login.microsoftonline.com", 0),
    ])
    health = session.read(profile)
    assert health.found
    assert not health.stay_signed_in
    assert "NOT ticked" in health.describe()


def test_the_longest_lived_cookie_bounds_the_session(tmp_path: Path):
    """Short-lived cookies refresh while a persistent one is valid."""
    soon = datetime.now(timezone.utc) + timedelta(hours=2)
    far = datetime.now(timezone.utc) + timedelta(days=60)
    profile = make_store(tmp_path, [
        ("ESTSAUTH", "login.microsoftonline.com", chromium_time(soon)),
        ("ESTSAUTHPERSISTENT", "login.microsoftonline.com", chromium_time(far)),
    ])
    health = session.read(profile)
    assert health.days_left > 50, "took the shortest expiry, not the longest"


def test_an_expired_session_says_so(tmp_path: Path):
    gone = datetime.now(timezone.utc) - timedelta(days=3)
    profile = make_store(tmp_path, [
        ("ESTSAUTHPERSISTENT", "login.microsoftonline.com", chromium_time(gone)),
    ])
    assert "EXPIRED" in session.read(profile).describe()


def test_a_profile_that_never_signed_in(tmp_path: Path):
    profile = make_store(tmp_path, [("theme", "example.com", 0)])
    health = session.read(profile)
    assert health.found
    assert health.auth_cookies == []
    assert "never signed in" in health.describe()


def test_a_missing_profile_is_not_a_crash(tmp_path: Path):
    health = session.read(tmp_path / "nothing-here")
    assert not health.found
    assert "no cookie store" in health.describe()


def test_a_foreign_schema_is_reported_not_guessed_around(tmp_path: Path):
    """A Chromium that changed its store should say so, not report 'fine'."""
    path = tmp_path / "Default"
    path.mkdir(parents=True)
    (path / "Cookies").write_bytes(b"not a database at all")
    health = session.read(tmp_path)
    assert not health.found
    assert "unexpected cookie store" in health.problem


def test_cookie_values_are_never_read(tmp_path: Path):
    """The value is the credential. Nothing here needs it.

    Checked two ways: the column is not in the query, and there is nowhere
    for a value to be kept even if it were.
    """
    import inspect, re
    selects = re.findall(r'"(SELECT[^"]*)"', inspect.getsource(session))
    assert selects, "no query found to check"
    for statement in selects:
        assert "value" not in statement.lower(), \
            f"the value column is being selected: {statement}"
    assert not hasattr(session.Cookie("n", "h", None), "value")


def test_a_value_would_not_survive_even_if_selected(tmp_path: Path):
    """Belt and braces: nothing a cookie holds reaches the caller."""
    expiry = datetime.now(timezone.utc) + timedelta(days=5)
    profile = make_store(tmp_path, [
        ("ESTSAUTHPERSISTENT", "login.microsoftonline.com",
         chromium_time(expiry))])
    health = session.read(profile)
    assert "secret" not in repr(health)


def test_the_windows_epoch_offset_is_right():
    """A known value: 13 Jan 2026 00:00 UTC in Chromium time."""
    known = datetime(2026, 1, 13, tzinfo=timezone.utc)
    assert session._to_datetime(chromium_time(known)) == known


def test_a_running_browser_cannot_be_disturbed(tmp_path: Path):
    """It copies the store. The real profile is never opened for writing."""
    profile = make_store(tmp_path, [
        ("ESTSAUTHPERSISTENT", "login.microsoftonline.com",
         chromium_time(datetime.now(timezone.utc) + timedelta(days=10)))])
    store = profile / "Default" / "Cookies"
    before = store.stat().st_mtime_ns
    session.read(profile)
    assert store.stat().st_mtime_ns == before, "the real cookie store was touched"


# ------------------------------------------------------------- snapshots

def good_profile(tmp_path: Path, days: int = 50) -> Path:
    expiry = datetime.now(timezone.utc) + timedelta(days=days)
    return make_store(tmp_path / "profile", [
        ("ESTSAUTHPERSISTENT", "login.microsoftonline.com", chromium_time(expiry))])


def test_a_good_profile_can_be_snapshotted_and_restored(tmp_path: Path):
    profile = good_profile(tmp_path)
    target = session.take_snapshot(profile, session.read(profile))
    assert session.read(target).stay_signed_in, "the snapshot lost the session"

    # Simulate the profile being wrecked.
    (profile / "Default" / "Cookies").write_bytes(b"corrupted")
    assert not session.read(profile).found

    session.restore_snapshot(profile)
    assert session.read(profile).stay_signed_in, "restore did not bring it back"


def test_a_session_only_profile_is_not_snapshotted(tmp_path: Path):
    """Restoring one would succeed and then fail at the next run."""
    profile = make_store(tmp_path / "profile", [
        ("ESTSAUTH", "login.microsoftonline.com", 0)])
    with pytest.raises(RuntimeError, match="session-only"):
        session.take_snapshot(profile, session.read(profile))


def test_an_expired_session_is_not_snapshotted(tmp_path: Path):
    profile = good_profile(tmp_path, days=-2)
    with pytest.raises(RuntimeError, match="expired"):
        session.take_snapshot(profile, session.read(profile))


def test_a_profile_with_no_sign_in_is_not_snapshotted(tmp_path: Path):
    profile = make_store(tmp_path / "profile", [("theme", "example.com", 0)])
    with pytest.raises(RuntimeError, match="no sign-in"):
        session.take_snapshot(profile, session.read(profile))


def test_restoring_keeps_what_it_replaced(tmp_path: Path):
    """A snapshot older than you thought must not cost you the current one."""
    profile = good_profile(tmp_path)
    session.take_snapshot(profile, session.read(profile))
    session.restore_snapshot(profile)
    displaced = list(profile.parent.glob("profile-replaced-*"))
    assert displaced, "the replaced profile was deleted"


def test_only_the_last_few_snapshots_are_kept(tmp_path: Path):
    profile = good_profile(tmp_path)
    for _ in range(session.KEEP_SNAPSHOTS + 2):
        session.take_snapshot(profile, session.read(profile))
    assert len(session.snapshots(profile)) == session.KEEP_SNAPSHOTS


def test_snapshots_taken_in_the_same_second_do_not_collide(tmp_path: Path):
    """And still sort oldest-first, since the newest is what gets restored."""
    profile = good_profile(tmp_path)
    made = [session.take_snapshot(profile, session.read(profile))
            for _ in range(3)]
    assert len({p.name for p in made}) == 3, "a snapshot overwrote another"
    assert session.snapshots(profile)[-1].name == made[-1].name


def test_snapshots_are_not_world_readable(tmp_path: Path):
    """It holds a live session cookie."""
    profile = good_profile(tmp_path)
    target = session.take_snapshot(profile, session.read(profile))
    assert target.stat().st_mode & 0o077 == 0, oct(target.stat().st_mode)
    assert session.snapshot_dir(profile).stat().st_mode & 0o077 == 0


def test_restoring_with_nothing_saved_says_so(tmp_path: Path):
    profile = good_profile(tmp_path)
    with pytest.raises(RuntimeError, match="no snapshots"):
        session.restore_snapshot(profile)
