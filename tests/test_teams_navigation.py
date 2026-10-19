"""Getting to Assignments, and seeing all of it, on teams.cloud.microsoft.

Two things about the new Teams make a capture silently short rather than
loudly broken:

    the app opens on Teams itself and the address bar never changes, so
    nothing about the URL tells you whether you are looking at your prep;

    the list is virtualised, so prep that has not been scrolled into view has
    never been requested - and a capture that stops scrolling early is short
    by exactly the part you cannot see.

Both produce a plausible number with no error anywhere.
"""
from __future__ import annotations

import pytest

from prep import scraper


# --------------------------------------------------------------- scrolling

class FakeFrame:
    """A frame that reports whether anything actually scrolled."""

    def __init__(self, moves: list[bool] | None = None, broken: bool = False):
        self.moves = list(moves or [])
        self.broken = broken
        self.asked = 0

    def evaluate(self, script, arg=None):
        self.asked += 1
        if self.broken:
            raise RuntimeError("frame detached")
        return self.moves.pop(0) if self.moves else False


class FramedPage:
    """A page whose scrolling happens inside frames, as Teams' does."""

    def __init__(self, frames, arrivals=None, payloads=None):
        self.frames = frames
        self.waited = 0
        self.arrivals = dict(arrivals or {})
        self.payloads = payloads if payloads is not None else []
        self.wheels = 0

    def wait_for_timeout(self, ms):
        self.waited += ms
        for at, count in list(self.arrivals.items()):
            if self.waited >= at:
                self.payloads.extend([{"url": "x", "body": {}}] * count)
                del self.arrivals[at]

    class _Mouse:
        def __init__(self, page): self.page = page
        def move(self, x, y): pass
        def wheel(self, x, y): self.page.wheels += 1

    @property
    def mouse(self):
        return self._Mouse(self)


class Teams:
    settle_ms = 2000
    quiet_ms = 2000
    max_settle_ms = 30000
    scroll_passes = 2
    assignment_labels: list = []


def test_every_frame_is_scrolled_not_just_the_page():
    """The list lives in an iframe. A wheel event goes to whatever is under
    the pointer, which at rest is the rail beside it."""
    outer, inner = FakeFrame([False]), FakeFrame([True])
    assert scraper.scroll_for_more(FramedPage([outer, inner])) is True
    assert outer.asked == 1 and inner.asked == 1


def test_a_list_at_its_end_says_so():
    """"Nothing moved" is what lets the capture stop early with confidence."""
    page = FramedPage([FakeFrame([False]), FakeFrame([False])])
    assert scraper.scroll_for_more(page) is False


def test_a_page_that_cannot_be_asked_admits_it():
    """None, not False: guessing "at the end" would truncate the capture."""
    class Bare:
        class _Mouse:
            def move(self, x, y): pass
            def wheel(self, x, y): pass
        mouse = _Mouse()

        class _Keyboard:
            def press(self, key): pass
        keyboard = _Keyboard()

    assert scraper.scroll_for_more(Bare()) is None


def test_a_detached_frame_does_not_stop_the_others():
    """Frames come and go while Teams re-renders."""
    broken, working = FakeFrame(broken=True), FakeFrame([True])
    assert scraper.scroll_for_more(FramedPage([broken, working])) is True


def test_scrolling_continues_while_the_page_is_still_moving():
    """scroll_passes is a floor, not a ceiling. A long list needs more passes
    than a short one, and nobody knows which they have in advance."""
    frame = FakeFrame([True] * 8 + [False])
    page = FramedPage([frame])
    scraper.settle(page, page.payloads, Teams())
    assert frame.asked > Teams.scroll_passes, \
        f"stopped at the configured {Teams.scroll_passes} passes with more list to see"


def test_it_stops_scrolling_once_the_list_will_not_move():
    """The opposite failure: scrolling a finished list forever, which would
    hold every run open until max_settle_ms."""
    frame = FakeFrame([False])
    page = FramedPage([frame])
    scraper.settle(page, page.payloads, Teams())
    assert page.waited < Teams.max_settle_ms, "never decided the list was done"


def test_data_arriving_after_a_scroll_is_waited_for():
    """The whole point of scrolling: it starts fetches, and stopping before
    they land captures a list one screenful long."""
    frame = FakeFrame([True, True, False])
    page = FramedPage([frame], arrivals={6000: 4})
    scraper.settle(page, page.payloads, Teams())
    assert len(page.payloads) == 4


def test_the_scroll_script_reads_no_assignment_data():
    """Scrolling is navigation. Reading the list would be DOM scraping, which
    this system does not do - the data comes only from JSON responses."""
    for forbidden in ("innerText", "textContent", "innerHTML"):
        assert forbidden not in scraper.SCROLL_JS, forbidden


# ---------------------------------------------------------------- the click

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


class Rail:
    """A left rail where Assignments is only reachable through "More apps"."""

    def __init__(self, hidden_until_more=True):
        self.clicked: list = []
        self.hidden_until_more = hidden_until_more
        self.frames: list = []

    def _available(self) -> bool:
        return not self.hidden_until_more or "more" in self.clicked

    def get_by_role(self, role, name=None):
        if name is not None and name.search("More apps"):
            return _Locator(self, "more", True)
        return _Locator(self, role, role == "treeitem" and self._available())

    def get_by_text(self, pattern):
        return _Locator(self, "text", False)

    def locator(self, selector):
        return _Locator(self, selector, False)

    def wait_for_timeout(self, ms):
        pass


def test_assignments_hidden_behind_more_apps_is_still_found():
    """Not pinned to the rail is not the same as not there, and the two look
    identical from here."""
    page = Rail(hidden_until_more=True)
    how = scraper.click_assignments(page)
    assert how, "gave up while Assignments was one click away in the overflow"
    assert page.clicked == ["more", "treeitem"]
    assert "More" in how, f"said nothing about how it got there: {how}"


def test_the_overflow_is_not_opened_when_it_is_not_needed():
    """A wrong guess at a menu costs a click on the wrong thing."""
    page = Rail(hidden_until_more=False)
    assert scraper.click_assignments(page) == "treeitem"
    assert "more" not in page.clicked


class FrameHost(Rail):
    """Nothing on the page; the tab is inside the edu app's iframe."""

    def __init__(self):
        super().__init__(hidden_until_more=True)
        self.inner = Rail(hidden_until_more=False)
        self.frames = [self, self.inner]

    def get_by_role(self, role, name=None):
        return _Locator(self, role, False)

    def locator(self, selector):
        return _Locator(self, selector, False)


def test_it_looks_inside_the_frames_too():
    """Teams runs the edu app in an iframe, and once you are in it the tabs
    live there rather than on the page around it."""
    page = FrameHost()
    how = scraper.click_assignments(page)
    assert how and "frame" in how, how
    assert page.inner.clicked == ["treeitem"]


# --------------------------------------------------------------- the label

class Renamed:
    assignment_labels = ["Classwork"]


def test_a_renamed_app_can_be_named_in_the_config():
    """A school that calls it Classwork has no Assignments link to find, and
    that failure looks exactly like a dead session."""
    pattern = scraper.label_pattern(Renamed())
    assert pattern.search("Classwork")
    assert not pattern.search("Chat")


def test_the_default_still_matches_what_teams_calls_it():
    for teams in (None, Teams()):
        pattern = scraper.label_pattern(teams)
        assert pattern.search("Assignments")
        assert pattern.search("assignment")


def test_an_empty_label_list_does_not_match_everything():
    """The dangerous version of this: a pattern built from nothing matches
    every element on the page, and the first click lands anywhere."""
    class Empty:
        assignment_labels = ["", "  "]

    pattern = scraper.label_pattern(Empty())
    assert not pattern.search("Chat")
    assert pattern.search("Assignments")


# ------------------------------------------------- the keyboard way in
#
# Teams jumps to the Nth app in the left rail on Ctrl+N. Which number
# Assignments is depends on how the rail is arranged, so the code tries them
# and watches for assignment data rather than asking anyone to remember.
# It matters most headless, where the rail can be collapsed to icons, drawn
# off-screen, or still booting - nothing to click, and the shortcut works.

class RouteTeams(Teams):
    assignments_url = "https://teams.cloud.microsoft/v2/#/edu/assignments"
    nav_timeout_ms = 1000


class KeyboardPage:
    """Nothing clickable; one rail shortcut opens the assignment app."""

    def __init__(self, opens_on=5):
        self.opens_on = opens_on
        self.pressed: list = []
        self.payloads: list = []
        self.frames: list = []
        self.url = "https://teams.cloud.microsoft/v2/#/chat"
        self.routed: list = []

    def wait_for_timeout(self, ms):
        pass

    class _Keyboard:
        def __init__(self, page): self.page = page

        def press(self, key):
            self.page.pressed.append(key)
            if key == f"Control+{self.page.opens_on}":
                self.page.payloads.append({"url": "x/assignments", "body": {}})

    @property
    def keyboard(self):
        return self._Keyboard(self)

    # Nothing here is clickable, which is the case being tested.
    def get_by_role(self, role, name=None):
        return _Locator(self, role, False)

    def get_by_text(self, pattern):
        return _Locator(self, "text", False)

    def locator(self, selector):
        return _Locator(self, selector, False)

    def evaluate(self, script, arg=None):
        self.routed.append(arg)
        return []

    def goto(self, url, **kw):
        self.routed.append(url)

    def screenshot(self, **kw):
        pass


def test_the_rail_shortcut_that_works_is_found():
    page = KeyboardPage(opens_on=5)
    assert scraper.keyboard_to_assignments(page, page.payloads) == "Control+5"


def test_it_stops_at_the_one_that_worked():
    """Carrying on would navigate away from the view it just reached."""
    page = KeyboardPage(opens_on=3)
    scraper.keyboard_to_assignments(page, page.payloads)
    assert "Control+4" not in page.pressed


def test_a_menu_swallowing_the_keystroke_is_dismissed_first():
    page = KeyboardPage(opens_on=2)
    scraper.keyboard_to_assignments(page, page.payloads)
    assert page.pressed[0] == "Escape"


def test_no_shortcut_working_is_reported_not_papered_over():
    page = KeyboardPage(opens_on=None)
    assert scraper.keyboard_to_assignments(page, page.payloads) == ""


def test_the_keyboard_is_tried_when_there_is_nothing_to_click():
    """The headless case: the rail is there, but not as anything hittable."""
    page = KeyboardPage(opens_on=6)
    notes: dict = {}
    assert scraper.reach_assignments(page, page.payloads, RouteTeams(),
                                     notes=notes) is True
    assert "Control+6" in notes["route"]


def test_clicking_still_comes_first():
    """A click is what a person does and it lands somewhere known. The
    shortcuts walk through other views on the way, so they are the fallback."""
    page = Rail(hidden_until_more=False)
    page.pressed = []
    page.payloads = []

    def _press(key):
        page.pressed.append(key)

    page.keyboard = type("K", (), {"press": staticmethod(_press)})()
    scraper.click_assignments(page)
    assert page.clicked == ["treeitem"]
    assert not page.pressed, "used the keyboard despite a click working"


def test_a_page_that_cannot_take_keystrokes_does_not_crash():
    class NoKeyboard:
        payloads: list = []

    assert scraper.keyboard_to_assignments(NoKeyboard(), []) == ""


# ------------------------------------------ saying what was there instead

def test_the_app_bar_labels_are_collected_from_every_frame():
    """"Nothing named Assignments" is useless when the tenant calls it
    Classwork. This is what turns that into a one-line config change."""
    class Frame:
        def __init__(self, names): self.names = names
        def evaluate(self, script): return self.names

    class Page:
        frames = [Frame(["Chat", "Teams"]), Frame(["Classwork", "Chat"])]

    assert scraper.describe_rail(Page()) == ["Chat", "Classwork", "Teams"]


def test_a_frame_that_will_not_answer_does_not_lose_the_others():
    class Good:
        def evaluate(self, script): return ["Calendar"]

    class Bad:
        def evaluate(self, script): raise RuntimeError("detached")

    class Page:
        frames = [Bad(), Good()]

    assert scraper.describe_rail(Page()) == ["Calendar"]


def test_the_labels_are_recorded_when_nothing_was_reached():
    """So the failure carries its own diagnosis rather than needing a rerun."""
    page = KeyboardPage(opens_on=None)

    class Frame:
        @staticmethod
        def evaluate(script):
            return ["Chat", "Classwork"]

    page.frames = [Frame()]
    notes: dict = {}
    assert scraper.reach_assignments(page, page.payloads, RouteTeams(),
                                     notes=notes) is False
    assert notes["route"] == "never reached it"
    assert "Classwork" in notes["rail"]
