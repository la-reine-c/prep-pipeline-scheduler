"""Teams web is moving to teams.cloud.microsoft.

An automatic redirect completes 30 September 2026, the old URL keeps
redirecting, and admins can defer it only until 31 December 2026. It is a
domain change, not a functional one — but two things in here match on
hostnames, and "cloud.microsoft" has no .com, so nothing matched it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from prep import scraper, session

NEW = "https://teams.cloud.microsoft/v2/#/edu/assignments"
OLD = "https://teams.microsoft.com/v2/#/edu/assignments"


def test_the_new_teams_domain_is_not_mistaken_for_a_sign_in_page():
    """The dangerous version of this fix: a broad pattern that matches the
    app itself would report every healthy capture as an expired session."""
    assert not any(host in NEW for host in scraper.LOGIN_HOSTS)
    assert not any(host in OLD for host in scraper.LOGIN_HOSTS)


def test_a_real_sign_in_redirect_is_still_caught():
    for url in ("https://login.microsoftonline.com/common/oauth2/authorize",
                "https://login.live.com/oauth20_authorize.srf"):
        assert any(host in url for host in scraper.LOGIN_HOSTS), url


def test_cookies_on_the_new_domain_are_recognised(tmp_path: Path):
    """Without this a working session reads as 'never signed in' the day the
    redirect reaches you."""
    import sqlite3
    path = tmp_path / "Default"
    path.mkdir(parents=True)
    conn = sqlite3.connect(path / "Cookies")
    conn.execute("CREATE TABLE cookies (name TEXT, host_key TEXT, "
                 "expires_utc INTEGER, value BLOB)")
    expiry = datetime.now(timezone.utc) + timedelta(days=40)
    conn.execute("INSERT INTO cookies VALUES (?, ?, ?, ?)",
                 ("ESTSAUTHPERSISTENT", "teams.cloud.microsoft",
                  int((expiry.timestamp() + session.WINDOWS_EPOCH_OFFSET)
                      * 1_000_000), b"x"))
    conn.commit()
    conn.close()

    health = session.read(tmp_path)
    assert health.auth_cookies, "a cookie on the new domain was ignored"
    assert health.stay_signed_in


def test_url_hints_are_path_based_so_the_domain_does_not_matter():
    """The assignment service could move host and these would still match."""
    hints = ["assignment", "edu/api", "assignmentservice"]
    for host in ("teams.microsoft.com", "teams.cloud.microsoft"):
        url = f"https://{host}/api/v1/assignments/list"
        assert scraper._looks_like_assignment_url(url, hints), url
