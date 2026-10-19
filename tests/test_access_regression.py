"""Guards on the things that decide whether Teams lets us in at all.

These exist because a change made for an unrelated reason (a rendering fix, a
tidy-up, a new domain) silently altered how Teams saw the browser, and a
working setup started asking for school approval. Anything here changing
should be a decision, not a side effect.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from prep import config as config_module, scraper


class Teams:
    disable_gpu = True
    browser_args: list = []
    no_sandbox = None


def test_the_sandbox_stays_on_for_a_normal_user(monkeypatch):
    """--no-sandbox is a real reduction in isolation and Chromium says so in a
    banner. It belongs only where it is the difference between running and not."""
    monkeypatch.setattr(scraper.os, "geteuid", lambda: 1000)
    assert "--no-sandbox" not in scraper.browser_args(Teams())


def test_root_still_gets_it_because_chromium_will_not_start_without_it(monkeypatch):
    monkeypatch.setattr(scraper.os, "geteuid", lambda: 0)
    assert "--no-sandbox" in scraper.browser_args(Teams())


def test_it_can_be_forced_either_way_from_config(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper.os, "geteuid", lambda: 0)

    class Never(Teams):
        no_sandbox = False

    class Always(Teams):
        no_sandbox = True

    assert "--no-sandbox" not in scraper.browser_args(Never())
    monkeypatch.setattr(scraper.os, "geteuid", lambda: 1000)
    assert "--no-sandbox" in scraper.browser_args(Always())


def test_no_flag_changes_the_user_agent_or_identity():
    """Spoofing what Teams sees is off limits, and an accident here would be
    indistinguishable from doing it on purpose."""
    monkeypatched = Teams()
    monkeypatched.browser_args = []
    for flag in scraper.browser_args(monkeypatched):
        lowered = flag.lower()
        for forbidden in ("user-agent", "useragent", "disable-blink-features",
                          "disable-web-security", "ignore-certificate"):
            assert forbidden not in lowered, f"identity- or safety-affecting flag: {flag}"


def test_the_shipped_config_points_at_a_real_teams_domain():
    """Either domain is fine - a migrated tenant redirects the old one to the
    new, so the config cannot hold you on the old origin. What must not happen
    is the URL drifting to something that is not Teams at all."""
    import yaml
    root = Path(__file__).resolve().parent.parent
    data = yaml.safe_load((root / "config.example.yaml").read_text())
    url = data["teams"]["assignments_url"]
    assert ("teams.microsoft.com" in url or "teams.cloud.microsoft" in url), url
    assert url.startswith("https://"), url


def test_the_software_render_flags_do_not_touch_networking_or_auth():
    """A rendering fix must not change what the tenant sees."""
    for flag in scraper.SOFTWARE_RENDER_ARGS:
        assert "gpu" in flag or "Compositor" in flag, flag
