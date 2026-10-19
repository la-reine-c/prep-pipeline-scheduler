"""Tests for the ICS feed reader, using a synthetic SOCS-shaped timetable."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import feeds
from prep.feeds import FeedError

TZ = ZoneInfo("Europe/London")

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//SOCS//Calendar//EN
CALSCALE:GREGORIAN
BEGIN:VTIMEZONE
TZID:Europe/London
BEGIN:DAYLIGHT
TZOFFSETFROM:+0000
TZOFFSETTO:+0100
TZNAME:BST
DTSTART:19700329T010000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0100
TZOFFSETTO:+0000
TZNAME:GMT
DTSTART:19701025T020000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:lesson-physics@socs
DTSTART;TZID=Europe/London:20260921T090000
DTEND;TZID=Europe/London:20260921T100000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=30
SUMMARY:Physics - Dalton
LOCATION:Science Block
END:VEVENT
BEGIN:VEVENT
UID:cross-country@socs
DTSTART;TZID=Europe/London:20260924T140000
DTEND;TZID=Europe/London:20260924T160000
SUMMARY:6th Form Cross Country
END:VEVENT
BEGIN:VEVENT
UID:term-dates@socs
DTSTART;VALUE=DATE:20260901
DTEND;VALUE=DATE:20261218
SUMMARY:Michaelmas Term
END:VEVENT
BEGIN:VEVENT
UID:cancelled-lesson@socs
DTSTART;TZID=Europe/London:20260922T110000
DTEND;TZID=Europe/London:20260922T120000
SUMMARY:Chemistry - CANCELLED
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:optional-notice@socs
DTSTART;TZID=Europe/London:20260923T160000
DTEND;TZID=Europe/London:20260923T170000
SUMMARY:Optional drop-in
TRANSP:TRANSPARENT
END:VEVENT
BEGIN:VEVENT
UID:floating-time@socs
DTSTART:20260925T183000
DTEND:20260925T200000
SUMMARY:Organ practice
END:VEVENT
END:VCALENDAR
"""

WINDOW_START = datetime(2026, 9, 21, 0, 0, tzinfo=TZ)
WINDOW_END = datetime(2026, 9, 28, 0, 0, tzinfo=TZ)


def parsed():
    return feeds.parse(ICS, WINDOW_START, WINDOW_END, TZ, source="socs")


def test_recurring_lessons_expand():
    """A timetable is mostly RRULEs - one VEVENT must become many lessons."""
    physics = [e for e in parsed() if e.summary.startswith("Physics")]
    assert len(physics) == 3                      # Mon, Wed, Fri in this window
    assert {e.start.strftime("%a") for e in physics} == {"Mon", "Wed", "Fri"}
    assert all(e.minutes == 60 for e in physics)


def test_times_are_local_and_bst_correct():
    physics = [e for e in parsed() if e.summary.startswith("Physics")][0]
    assert physics.start.hour == 9                # 09:00 BST, not 10:00
    assert physics.start.utcoffset() == timedelta(hours=1)


def test_all_day_term_marker_is_ignored():
    """Blocking the whole term as busy would leave nowhere to put prep."""
    assert not [e for e in parsed() if "Michaelmas" in e.summary]


def test_all_day_can_be_included_when_asked():
    events = feeds.parse(ICS, WINDOW_START, WINDOW_END, TZ, skip_all_day=False)
    assert [e for e in events if "Michaelmas" in e.summary]


def test_cancelled_and_transparent_events_are_free_time():
    summaries = " ".join(e.summary for e in parsed())
    assert "CANCELLED" not in summaries
    assert "Optional drop-in" not in summaries


def test_floating_time_is_treated_as_local():
    organ = [e for e in parsed() if e.summary == "Organ practice"]
    assert len(organ) == 1
    assert organ[0].start.hour == 18 and organ[0].start.minute == 30


def test_one_off_event_survives():
    xc = [e for e in parsed() if "Cross Country" in e.summary]
    assert len(xc) == 1 and xc[0].minutes == 120


def test_events_are_sorted():
    events = parsed()
    assert events == sorted(events, key=lambda e: e.start)


def test_window_outside_the_data_returns_nothing():
    events = feeds.parse(ICS, datetime(2027, 6, 1, tzinfo=TZ),
                         datetime(2027, 6, 8, tzinfo=TZ), TZ)
    assert events == []


def test_garbage_raises_rather_than_returning_empty():
    """Silently returning no events would let prep be booked over lessons."""
    with pytest.raises(FeedError):
        feeds.parse(b"this is not an ics file at all", WINDOW_START, WINDOW_END, TZ)


def test_fetch_rejects_a_non_ics_response(monkeypatch):
    class FakeResponse:
        content = b"<html><body>Sign in to continue</body></html>"
        def raise_for_status(self): pass

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())
    with pytest.raises(FeedError) as excinfo:
        feeds.fetch("https://example.invalid/feed.ics")
    assert "not an iCalendar" in str(excinfo.value)


def test_fetch_falls_back_to_cache(tmp_path: Path, monkeypatch):
    cache = tmp_path / "socs.ics"
    cache.write_bytes(ICS)

    import requests
    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(requests, "get", boom)

    data = feeds.fetch("https://example.invalid/feed.ics", cache=cache)
    assert b"BEGIN:VCALENDAR" in data


def test_fetch_without_cache_raises(monkeypatch):
    import requests
    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(requests, "get", boom)

    with pytest.raises(FeedError):
        feeds.fetch("https://example.invalid/feed.ics")


def test_load_all_reports_problems_rather_than_hiding_them(monkeypatch):
    import requests
    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(requests, "get", boom)

    events, problems = feeds.load_all({"socs": "https://example.invalid/f.ics"},
                                      WINDOW_START, WINDOW_END, TZ)
    assert events == []
    assert problems and "socs" in problems[0]
