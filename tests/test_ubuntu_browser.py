"""Ubuntu ships Chromium only as a snap, and a snap is confined.

Measured on Ubuntu 24.04 with the pipeline's own launch code: a snap with its
profile under /tmp launches and reports success while writing the profile into
its private /tmp, so the sign-in never reaches profile_dir; in a hidden folder
it refuses to start with a misleading "profile in use". Both must be caught
before the browser opens.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from prep import scraper
from prep.scraper import BROWSER_ADVICE, is_snap, snap_profile_problem


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_a_snap_is_recognised_by_its_path():
    assert is_snap("/snap/bin/chromium")
    assert not is_snap("")


def test_ubuntus_chromium_browser_wrapper_counts_as_the_snap(tmp_path):
    # /usr/bin/chromium-browser on Ubuntu is a shell script that runs the snap.
    wrapper = tmp_path / "chromium-browser"
    wrapper.write_text("#!/bin/sh\nexec /snap/bin/chromium \"$@\"\n")
    assert is_snap(str(wrapper))


def test_a_real_binary_is_not_a_snap(tmp_path):
    binary = tmp_path / "chromium"
    binary.write_bytes(b"\x7fELF" + b"\0" * 64 + b"/snap/bin/ in a string table")
    assert not is_snap(str(binary))


def test_a_snap_with_a_plain_profile_in_home_is_fine(home):
    assert snap_profile_problem("/snap/bin/chromium", home / "prep" / "profile") == ""


@pytest.mark.parametrize("where", ["/tmp/prep-profile", "/var/lib/prep/profile"])
def test_a_snap_outside_home_is_refused(home, where):
    problem = snap_profile_problem("/snap/bin/chromium", Path(where))
    assert "cannot write" in problem and "~/prep/profile" in problem


def test_a_snap_in_a_hidden_folder_is_refused(home):
    problem = snap_profile_problem("/snap/bin/chromium",
                                   home / ".config" / "prep" / "profile")
    assert "'.config' is a hidden folder" in problem


def test_playwrights_own_browser_may_live_anywhere(home):
    assert snap_profile_problem("", Path("/tmp/prep-profile")) == ""
    assert snap_profile_problem("", home / ".hidden") == ""


def test_capture_refuses_before_touching_the_profile(home, tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(scraper, "require_playwright", lambda path: None)
    outside = tmp_path.parent / f"{tmp_path.name}-outside" / "profile"
    teams = SimpleNamespace(chromium_path="/snap/bin/chromium", profile_dir=outside)
    with pytest.raises(RuntimeError, match="snap"):
        scraper.capture(SimpleNamespace(teams=teams))
    assert not outside.exists(), "the profile was created before the refusal"


def test_the_advice_never_starts_with_an_apt_package_ubuntu_does_not_have():
    first_command = next(line for line in BROWSER_ADVICE if "install" in line
                         and line.startswith("    "))
    assert "playwright install" in first_command
