"""Settling, and the profile lock.

Both exist because of the same failure: a capture that silently sees only
some of your prep. Nothing errors, the data looks plausible, and the only
clue is a number you have to already know is wrong.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from prep import scraper


class FakePage:
    """Records waits; can be told to deliver payloads at set times."""

    def __init__(self, arrivals=None, payloads=None):
        self.waited = 0
        self.arrivals = dict(arrivals or {})       # {ms_elapsed: how_many}
        self.payloads = payloads if payloads is not None else []
        self.scrolls = 0
        self.keys = []

    def wait_for_timeout(self, ms):
        self.waited += ms
        for at, count in list(self.arrivals.items()):
            if self.waited >= at:
                self.payloads.extend([{"url": "x", "body": {}}] * count)
                del self.arrivals[at]

    class _Mouse:
        def __init__(self, page): self.page = page
        def wheel(self, x, y): self.page.scrolls += 1

    @property
    def mouse(self):
        return self._Mouse(self)

    class _Keyboard:
        def __init__(self, page): self.page = page
        def press(self, key): self.page.keys.append(key)

    @property
    def keyboard(self):
        return self._Keyboard(self)


class Teams:
    settle_ms = 2000
    quiet_ms = 2000
    max_settle_ms = 30000
    scroll_passes = 4


def test_it_waits_for_data_that_arrives_late():
    """The bug this fixes: a fixed wait that ends before Teams has finished."""
    payloads = []
    page = FakePage(arrivals={1000: 3, 9000: 4}, payloads=payloads)
    scraper.settle(page, payloads, Teams())
    assert len(payloads) == 7, "stopped before the second batch arrived"


def test_it_stops_once_the_page_goes_quiet():
    payloads = []
    page = FakePage(arrivals={500: 5}, payloads=payloads)
    scraper.settle(page, payloads, Teams())
    assert page.waited < 12000, f"kept waiting after the page was done ({page.waited}ms)"


def test_it_always_waits_the_minimum():
    """Some views fetch nothing for a second or two after load."""
    payloads = []
    page = FakePage(payloads=payloads)
    scraper.settle(page, payloads, Teams())
    assert page.waited >= Teams.settle_ms


def test_it_gives_up_eventually():
    """A page that never goes quiet must not hang the scheduled run."""
    payloads = []

    class Chatty(FakePage):
        def wait_for_timeout(self, ms):
            self.waited += ms
            self.payloads.append({"url": "x", "body": {}})

    page = Chatty(payloads=payloads)
    scraper.settle(page, payloads, Teams())
    assert page.waited <= Teams.max_settle_ms + 1000


def test_it_scrolls_to_pull_in_a_virtualised_list():
    """Without this the capture sees one screenful of a long list."""
    payloads = []
    page = FakePage(payloads=payloads)
    scraper.settle(page, payloads, Teams())
    assert page.scrolls > 0, "never scrolled, so a lazy list would stay unloaded"
    assert "End" in page.keys


def test_a_scroll_resets_the_quiet_timer():
    """A scroll can start a fetch; stopping immediately after would miss it."""
    payloads = []
    page = FakePage(arrivals={4500: 2}, payloads=payloads)
    scraper.settle(page, payloads, Teams())
    assert len(payloads) == 2


# ------------------------------------------------------------- the lock

def test_a_stale_lock_is_removed(tmp_path: Path):
    """Left by a crash. Safe to clear, and it blocks everything if you do not."""
    lock = tmp_path / "SingletonLock"
    os.symlink("somehost-999999", lock)          # a pid that cannot be alive
    note = scraper.check_profile_lock(tmp_path)
    assert not lock.is_symlink(), "the stale lock survived"
    assert "stale" in note


def test_a_live_lock_is_refused(tmp_path: Path):
    """Two Chromiums on one profile is the bug, not the workaround."""
    lock = tmp_path / "SingletonLock"
    os.symlink(f"thishost-{os.getpid()}", lock)
    with pytest.raises(scraper.ProfileBusy, match=str(os.getpid())):
        scraper.check_profile_lock(tmp_path)
    assert lock.is_symlink(), "removed a lock held by a running browser"


def test_no_lock_is_not_a_problem(tmp_path: Path):
    assert scraper.check_profile_lock(tmp_path) == ""


def test_a_lock_with_no_readable_target_does_not_crash(tmp_path: Path):
    (tmp_path / "SingletonLock").write_text("not a symlink")
    scraper.check_profile_lock(tmp_path)          # must not raise


# --------------------------------------------- landing on the right view

class RoutingPage(FakePage):
    """A Teams-like SPA that ignores a deep link until told again."""

    def __init__(self, honour_hash_on=None, **kw):
        super().__init__(**kw)
        self.url = "https://teams.cloud.microsoft/v2/#/chat"
        self.honour_hash_on = honour_hash_on or "evaluate"
        self.routed = []

    def evaluate(self, script, arg=None):
        self.routed.append(arg)
        if self.honour_hash_on == "evaluate":
            self.payloads.append({"url": "x/assignments", "body": {}})

    def goto(self, url, **kw):
        self.routed.append(url)
        if self.honour_hash_on == "goto":
            self.payloads.append({"url": "x/assignments", "body": {}})


class RouteTeams(Teams):
    assignments_url = "https://teams.cloud.microsoft/v2/#/edu/assignments"
    nav_timeout_ms = 1000


def test_nothing_happens_when_the_data_already_arrived():
    """The common case must not cost an extra round trip."""
    page = RoutingPage(payloads=[{"url": "x", "body": {}}])
    assert scraper.reach_assignments(page, page.payloads, RouteTeams()) is True
    assert page.routed == [], "re-routed despite already having data"


def test_the_route_is_reapplied_from_inside_the_app():
    """Teams boots at chat and ignores the fragment; clicking is what works,
    and setting location.hash is what clicking does."""
    page = RoutingPage(honour_hash_on="evaluate", payloads=[])
    assert scraper.reach_assignments(page, page.payloads, RouteTeams()) is True
    assert page.routed[0] == "#/edu/assignments"


def test_a_full_reload_is_the_last_resort():
    page = RoutingPage(honour_hash_on="goto", payloads=[])
    assert scraper.reach_assignments(page, page.payloads, RouteTeams()) is True
    assert page.routed[-1] == RouteTeams.assignments_url


def test_it_reports_failure_rather_than_pretending():
    page = RoutingPage(honour_hash_on="never", payloads=[])
    assert scraper.reach_assignments(page, page.payloads, RouteTeams()) is False


# ------------------------------- clicking, because the URL does not change

class ClickablePage(RoutingPage):
    """A page where only a click reaches Assignments, as Teams behaves."""

    def __init__(self, working_role="treeitem", **kw):
        super().__init__(honour_hash_on="never", **kw)
        self.working_role = working_role
        self.clicked = []

    def get_by_role(self, role, name=None):
        return _Locator(self, role, role == self.working_role)

    def get_by_text(self, pattern):
        return _Locator(self, "text", self.working_role == "text")

    def locator(self, selector):
        return _Locator(self, selector, False)

    def screenshot(self, **kw):
        pass


class _Locator:
    def __init__(self, page, how, works):
        self.page, self.how, self.works = page, how, works

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):
        return self.works

    def click(self, timeout=None):
        if not self.works:
            raise RuntimeError("not clickable")
        self.page.clicked.append(self.how)
        self.page.payloads.append({"url": "x/assignments", "body": {}})


def test_a_click_is_what_reaches_assignments():
    """Teams does not put this view in the URL, so no goto can get there."""
    page = ClickablePage(working_role="treeitem", payloads=[])
    assert scraper.reach_assignments(page, page.payloads, RouteTeams()) is True
    assert page.clicked == ["treeitem"]


def test_it_tries_several_handles_because_the_markup_moves():
    page = ClickablePage(working_role="button", payloads=[])
    assert scraper.reach_assignments(page, page.payloads, RouteTeams()) is True
    assert page.clicked == ["button"], "gave up before trying button"


def test_clicking_is_tried_before_anything_else():
    """The URL tricks do not work here, so they must not be tried first."""
    page = ClickablePage(working_role="link", payloads=[])
    scraper.reach_assignments(page, page.payloads, RouteTeams())
    assert page.routed == [], "re-routed by URL before trying a click"


def test_nothing_clickable_is_reported_not_papered_over():
    page = ClickablePage(working_role="nothing", payloads=[])
    assert scraper.reach_assignments(page, page.payloads, RouteTeams()) is False


def test_the_data_still_comes_only_from_json():
    """Clicking is navigation. Reading the DOM for prep would be scraping."""
    import inspect
    source = inspect.getsource(scraper.parse)
    for forbidden in ("query_selector", "inner_text", "text_content",
                      "locator", "get_by_"):
        assert forbidden not in source, f"parse() touches the DOM: {forbidden}"
