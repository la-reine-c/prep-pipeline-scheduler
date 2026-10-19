"""The sign-in helper. No browser here - the browser parts are monkeypatched."""
from __future__ import annotations

from pathlib import Path

import pytest

from prep import login


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/dumps
export_path: {tmp_path}/p.txt
teams:
  assignments_url: https://teams.microsoft.com/assignments
  url_hints: [assignment]
  profile_dir: {tmp_path}/profile
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling: {{windows: {{monday: {{start: "17:00", end: "21:30"}}}}}}
""")
    return path


def test_a_half_finished_sign_in_is_reported_as_failure(config, monkeypatch,
                                                        capsys):
    """Closing the window on a login page means it did not work."""
    monkeypatch.setattr(login, "open_window",
                        lambda cfg: (0, "https://login.microsoftonline.com/x"))
    code = login.main(["--config", str(config)])
    out = capsys.readouterr().out
    assert code == 1
    assert "sign-in did not finish" in out


def test_it_verifies_headless_rather_than_trusting_the_window(config,
                                                             monkeypatch,
                                                             capsys):
    """A window you watched proves nothing about an unattended run."""
    checked = {}

    def fake_capture(cfg, dump=False):
        checked["headless"] = cfg.teams.headless
        return [{"url": "x/assignments", "body": {"value": [
            {"id": "a" * 36, "displayName": "Prep", "classId": "c",
             "dueDateTime": "2099-01-01T08:00:00Z"}]}}]

    monkeypatch.setattr(login, "open_window",
                        lambda cfg: (3, "https://teams.microsoft.com/x"))
    monkeypatch.setattr(login, "capture", fake_capture)

    code = login.main(["--config", str(config)])
    out = capsys.readouterr().out
    assert code == 0
    assert checked["headless"] is True, "verified with a visible browser"
    assert "headless run sees 1 prep" in out


def test_a_session_that_does_not_survive_says_why(config, monkeypatch, capsys):
    def expired(cfg, dump=False):
        raise login.SessionExpired("redirected to sign-in")

    monkeypatch.setattr(login, "open_window",
                        lambda cfg: (3, "https://teams.microsoft.com/x"))
    monkeypatch.setattr(login, "capture", expired)

    code = login.main(["--config", str(config)])
    out = capsys.readouterr().out
    assert code == 1
    assert "did NOT survive" in out
    assert "Stay signed in" in out, "no cause offered for the usual cause"


def test_an_unticked_box_is_caught_even_though_the_capture_worked(
        config, monkeypatch, capsys):
    """The capture succeeds either way. That is exactly why it is missed."""
    from prep import session

    monkeypatch.setattr(login, "open_window",
                        lambda cfg: (3, "https://teams.microsoft.com/x"))
    monkeypatch.setattr(login, "capture", lambda cfg, dump=False: [
        {"url": "x/assignments", "body": {"value": [
            {"id": "a" * 36, "displayName": "Prep", "classId": "c",
             "dueDateTime": "2099-01-01T08:00:00Z"}]}}])
    monkeypatch.setattr(login.session, "read", lambda profile: session.SessionHealth(
        found=True, total=2,
        microsoft=[session.Cookie("ESTSAUTH", "login.microsoftonline.com", None)]))

    code = login.main(["--config", str(config)])
    out = capsys.readouterr().out
    assert code == 1, "a session-only sign-in was reported as working"
    assert "dies when the browser closes" in out


def test_no_verify_skips_the_headless_check(config, monkeypatch):
    def must_not_run(cfg, dump=False):
        raise AssertionError("--no-verify still ran a capture")

    monkeypatch.setattr(login, "open_window",
                        lambda cfg: (3, "https://teams.microsoft.com/x"))
    monkeypatch.setattr(login, "capture", must_not_run)
    assert login.main(["--config", str(config), "--no-verify"]) == 0


# ------------------------------------------------- browser flags

def test_signing_in_and_scraping_use_identical_flags(config):
    """Different flags would let you sign in successfully in a window that
    renders, then have every headless capture fail.

    The sign-in is allowed exactly one deviation - the render level it walks
    down when a window opens blank - and it prints the config lines that make
    that choice permanent, so the two do not silently stay apart.
    """
    import inspect
    from prep import scraper

    assert "browser_args(teams)" in inspect.getsource(scraper.capture)
    signing_in = inspect.getsource(login.open_window)
    assert "browser_args(teams, render=level)" in signing_in
    assert "render_advice" in signing_in, \
        "a window fixed by a flag that is never written down is fixed once"


def test_software_rendering_is_on_by_default(config, monkeypatch):
    from prep import config as config_module, scraper
    args = scraper.browser_args(config_module.load(config).teams)
    assert "--disable-gpu" in args
    assert "--disable-gpu-compositing" in args

    # As root, and only as root. Asserting it unconditionally made this test
    # depend on who ran it, which is why it failed for everyone but root.
    monkeypatch.setattr(scraper.os, "geteuid", lambda: 0)
    as_root = scraper.browser_args(config_module.load(config).teams)
    assert "--no-sandbox" in as_root, "needed when running as root on a headless box"


def test_gpu_can_be_turned_back_on(tmp_path):
    from prep import config as config_module, scraper
    path = tmp_path / "c.yaml"
    path.write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/d
export_path: {tmp_path}/p.txt
teams:
  assignments_url: x
  url_hints: [a]
  profile_dir: {tmp_path}/profile
  disable_gpu: false
  browser_args: ["--window-size=1280,900"]
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling: {{windows: {{monday: {{start: "17:00", end: "21:30"}}}}}}
""")
    args = scraper.browser_args(config_module.load(path).teams)
    assert "--disable-gpu" not in args
    assert "--window-size=1280,900" in args, "extra args were dropped"


def test_the_software_flags_are_ones_every_chromium_understands():
    """A flag a newer Chromium renamed would turn a rendering bug into a
    browser that will not start at all."""
    from prep import scraper
    for flag in scraper.SOFTWARE_RENDER_ARGS:
        assert flag.startswith("--disable"), flag


# ----------------------------------------- the playwright install message

class _BlockPlaywright:
    """Hide playwright from an import, however it is installed.

    find_spec, not the old find_module/load_module pair: those were removed
    in Python 3.12, so on anything current the blocker silently did nothing
    and these tests passed by never testing anything.
    """

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] != "playwright":
            return None
        raise ImportError(f"No module named '{name}'")


@pytest.fixture
def no_playwright(monkeypatch):
    """An interpreter that has never seen playwright.

    Already-imported modules are dropped as well: a meta path finder is only
    consulted on a miss, so leaving playwright in sys.modules would let the
    import sail past the blocker.
    """
    import sys
    blocker = _BlockPlaywright()
    monkeypatch.setattr(sys, "meta_path", [blocker] + sys.meta_path)
    for name in [n for n in sys.modules if n.split(".")[0] == "playwright"]:
        monkeypatch.delitem(sys.modules, name)
    return blocker


def test_a_missing_playwright_says_how_to_install_it(no_playwright):
    """This fires while someone is following a setup guide. "No module named
    'playwright'" is accurate and tells them nothing."""
    from prep.scraper import PlaywrightMissing, require_playwright
    with pytest.raises(PlaywrightMissing) as caught:
        require_playwright()
    message = str(caught.value)
    assert "pip install playwright" in message
    assert "--break-system-packages" in message, "no route for a failed venv"


def test_it_names_the_interpreter_that_came_up_short(no_playwright):
    """Installing into one interpreter and running another is easy to do."""
    import sys
    from prep.scraper import PlaywrightMissing, require_playwright
    with pytest.raises(PlaywrightMissing, match=sys.executable.replace("+", r"\+")):
        require_playwright()


def test_with_a_system_chromium_it_does_not_suggest_downloading_one(no_playwright):
    from prep.scraper import PlaywrightMissing, require_playwright
    with pytest.raises(PlaywrightMissing) as caught:
        require_playwright("/usr/bin/chromium")
    message = str(caught.value)
    assert "playwright install chromium" not in message, \
        "told him to download a browser he already has"
    assert "already there" in message


def test_login_prints_the_advice_rather_than_a_traceback(config, no_playwright,
                                                         capsys):
    code = login.main(["--config", str(config)])
    out = capsys.readouterr().out
    assert code == 1
    assert "pip install playwright" in out
    assert "Traceback" not in out
