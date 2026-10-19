"""What Phase B and Phase C say about a capture that came back short.

The payloads cannot answer either question these check. A capture that never
left chat and a capture that stopped a screenful into a long list both return
plausible JSON, parse cleanly, and produce a number you would have to already
know is wrong. So the capture reports what it did on the way, and the phases
read that report rather than inferring it from the result.
"""
from __future__ import annotations

import pytest

from prep import scraper
from prep.phases import FAIL, OK, WARN, Report, _check_capture_quality


def _statuses(report: Report) -> dict[str, str]:
    return {name: status for status, name, _detail in report.lines}


def _settled(**overrides) -> scraper.Settled:
    fields = dict(waited_ms=12000, payloads=3, scrolls=5,
                  at_bottom=True, still_arriving=False)
    fields.update(overrides)
    return scraper.Settled(**fields)


def test_a_fully_scrolled_capture_passes_quietly():
    report = Report("B")
    _check_capture_quality(report, {
        "route": "clicked through to it (treeitem)",
        "settled": _settled(),
    }, live=True)

    statuses = _statuses(report)
    assert statuses["Reached the Assignments view"] == OK
    assert statuses["Assignment list seen to the end"] == OK
    assert statuses["Assignment data had stopped arriving"] == OK


def test_a_list_still_scrolling_is_flagged(capsys):
    """The failure this exists for: prep below the fold was never requested,
    and nothing about the result looks wrong."""
    report = Report("B")
    _check_capture_quality(report, {
        "route": "clicked through to it (treeitem)",
        "settled": _settled(at_bottom=False, scrolls=8),
    }, live=True)

    assert _statuses(report)["Assignment list seen to the end"] == WARN
    printed = capsys.readouterr().out
    assert "never requested" in printed, "no explanation of what is missing"
    assert "max_settle_ms" in printed, "no way out offered"


def test_never_reaching_assignments_fails_rather_than_warns():
    """Capturing the chat page is not a degraded success. It is nothing."""
    report = Report("B")
    _check_capture_quality(report, {"route": "never reached it"}, live=True)
    assert _statuses(report)["Reached the Assignments view"] == FAIL


def test_it_says_what_to_do_when_the_app_was_renamed(capsys):
    report = Report("B")
    _check_capture_quality(report, {"route": "never reached it"}, live=True)
    assert "assignment_labels" in capsys.readouterr().out, \
        "no route out for a school that renamed the app"


def test_how_it_got_there_is_reported_not_just_that_it_did():
    """Which of the three routes worked is the whole diagnosis when the count
    is wrong: clicking means the deep link was ignored, as it usually is."""
    report = Report("B")
    _check_capture_quality(report, {
        "route": "reloaded, then clicked (button in the More menu)",
        "settled": _settled(),
    }, live=True)
    detail = [d for s, n, d in report.lines if n == "Reached the Assignments view"][0]
    assert "More menu" in detail


def test_data_still_arriving_at_the_deadline_is_flagged():
    report = Report("B")
    _check_capture_quality(report, {
        "route": "opened straight onto the assignment data",
        "settled": _settled(waited_ms=60000, still_arriving=True),
    }, live=True)
    assert _statuses(report)["Assignment data had stopped arriving"] == WARN


def test_a_page_that_cannot_report_its_scrolling_is_not_called_complete():
    """"Could not tell" must not read as "saw everything" - that is how a
    short capture gets a clean bill of health."""
    report = Report("B")
    _check_capture_quality(report, {
        "route": "opened straight onto the assignment data",
        "settled": _settled(at_bottom=None),
    }, live=True)
    assert _statuses(report)["Assignment list seen to the end"] == WARN


def test_nothing_is_claimed_about_a_capture_read_from_disk():
    """--no-refresh opened no browser, so it did nothing on the way and has
    nothing to report. Saying otherwise would be inventing evidence."""
    report = Report("B")
    _check_capture_quality(report, {"route": "clicked through to it (treeitem)",
                                    "settled": _settled()}, live=False)
    assert not report.lines, "claimed something about a capture off the disk"


def test_an_empty_report_does_not_crash_the_phase():
    """A capture that raised before it learned anything still has to print."""
    report = Report("B")
    _check_capture_quality(report, {}, live=True)
    assert not report.lines


# ------------------------------------------- the record the capture fills in

class _Frame:
    def __init__(self, moves): self.moves = list(moves)
    def evaluate(self, script, arg=None):
        return self.moves.pop(0) if self.moves else False


class _Page:
    def __init__(self, frames):
        self.frames = frames
        self.waited = 0
        self.payloads: list = []

    def wait_for_timeout(self, ms):
        self.waited += ms


class _Teams:
    settle_ms = 2000
    quiet_ms = 2000
    max_settle_ms = 20000
    scroll_passes = 2
    assignment_labels: list = []


def test_settle_reports_reaching_the_end_of_the_list():
    page = _Page([_Frame([True, False])])
    settled = scraper.settle(page, page.payloads, _Teams())
    assert settled.at_bottom is True
    assert settled.still_arriving is False
    assert settled.scrolls >= 1


def test_settle_reports_a_list_it_never_got_to_the_bottom_of():
    """Always moving, so the clock runs out first - and the record has to say
    so rather than reporting a tidy finish."""
    page = _Page([_Frame([True] * 100)])
    settled = scraper.settle(page, page.payloads, _Teams())
    assert settled.at_bottom is False
    assert settled.waited_ms >= _Teams.max_settle_ms


def test_settle_writes_its_record_into_the_notes_the_caller_passed():
    notes: dict = {}
    page = _Page([_Frame([False])])
    scraper.settle(page, page.payloads, _Teams(), notes=notes)
    assert notes["settled"].at_bottom is True
