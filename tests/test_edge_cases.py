"""Obscure edge cases. Each one here is something that would produce a wrong
schedule or a silent failure rather than a loud error."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import config as config_module
from prep import estimator, feeds, notify, pipeline, scheduler
from prep.config import DayWindow, EstimatorConfig, SchedulingConfig
from prep.scheduler import FakeCalendar, Interval
from prep.scraper import parse
from prep.store import Store

TZ = ZoneInfo("Europe/London")

# BST ends 25 October 2026: 02:00 BST -> 01:00 GMT. 01:30 happens twice.
DST_END = date(2026, 10, 25)


def sched(**kw) -> SchedulingConfig:
    base = SchedulingConfig(
        block_minutes=30, flow_buffer_minutes=5, gap_minutes=10,
        due_time=time(8, 30),
        windows={d: DayWindow(time(17, 0), time(21, 30)) for d in range(5)}
        | {5: DayWindow(time(9, 30), time(18, 0)),
           6: DayWindow(time(9, 30), time(20, 0))},
    )
    for key, value in kw.items():
        setattr(base, key, value)
    return base


# ------------------------------------------------------- clocks going back

def test_sessions_do_not_overlap_across_the_dst_boundary():
    """25 Oct 2026: 01:30 occurs twice. Sessions must still be ordered."""
    cfg = sched(windows={6: DayWindow(time(0, 30), time(6, 0))})
    now = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
    due = datetime(2026, 10, 26, 8, 30, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "id", "Overnight", "Dalton", 120,
                              now, due, [], now, TZ)
    assert sessions
    for earlier, later in zip(sessions, sessions[1:]):
        assert later.start >= earlier.end, "sessions overlap across the clock change"
    for session in sessions:
        assert session.end > session.start


def test_session_length_is_wall_clock_not_absolute_across_dst():
    """A 30-minute block must stay 30 minutes of real elapsed time."""
    cfg = sched(windows={6: DayWindow(time(0, 30), time(6, 0))})
    now = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
    due = datetime(2026, 10, 26, 8, 30, tzinfo=TZ)
    for session in scheduler.plan(cfg, "id", "Overnight", "B", 120,
                                  now, due, [], now, TZ):
        elapsed = (session.end - session.start).total_seconds() / 60
        assert 25 <= elapsed <= 45, f"block became {elapsed} minutes real time"


def test_free_slots_survive_the_dst_day():
    cfg = sched()
    start = datetime(2026, 10, 25, 9, 0, tzinfo=TZ)
    end = datetime(2026, 10, 26, 9, 0, tzinfo=TZ)
    slots = scheduler.free_slots(cfg, start, end, [], TZ)
    assert slots
    for slot in slots:
        assert slot.end > slot.start
        assert slot.minutes > 0


# ------------------------------------------------------- malformed inputs

def test_due_before_assigned_does_not_produce_a_negative_estimate():
    """A data error must not make a prep take negative or zero time."""
    cfg = EstimatorConfig()
    assigned = datetime(2026, 9, 25, 9, 0, tzinfo=TZ)
    due = datetime(2026, 9, 21, 8, 30, tzinfo=TZ)      # before it was set
    minutes = estimator.pattern_minutes(cfg, assigned, due)
    assert minutes > 0
    assert minutes <= cfg.cap_minutes


def test_busy_interval_with_end_before_start_is_survivable():
    """A malformed calendar entry must not swallow the whole evening."""
    cfg = sched()
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    due = datetime(2026, 9, 23, 8, 30, tzinfo=TZ)
    broken = [Interval(datetime(2026, 9, 21, 20, 0, tzinfo=TZ),
                       datetime(2026, 9, 21, 18, 0, tzinfo=TZ))]
    sessions = scheduler.plan(cfg, "id", "Essay", "B", 60, now, due, broken, now, TZ)
    assert sessions, "one malformed busy entry blocked all scheduling"


def test_zero_length_window_places_nothing_rather_than_looping():
    cfg = sched(windows={0: DayWindow(time(17, 0), time(17, 0))})
    now = datetime(2026, 9, 21, 9, 0, tzinfo=TZ)
    due = datetime(2026, 9, 22, 8, 30, tzinfo=TZ)
    assert scheduler.plan(cfg, "id", "x", "B", 60, now, due, [], now, TZ) == []


def test_daily_ceiling_below_one_block_places_nothing_rather_than_looping():
    """max_daily_minutes < block_minutes must terminate, not spin."""
    cfg = sched(max_daily_minutes=10, block_minutes=30)
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)
    assert scheduler.plan(cfg, "id", "x", "B", 120, now, due, [], now, TZ,
                          daily_load={}) == []


def test_block_longer_than_any_window_places_nothing():
    cfg = sched(block_minutes=600)
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)
    assert scheduler.plan(cfg, "id", "x", "B", 600, now, due, [], now, TZ) == []


# ------------------------------------------------------- titles and text

def test_awkward_title_characters_survive_into_an_event():
    """Newlines and semicolons break naive iCalendar generation."""
    from icalendar import Calendar as ICalendar, Event as IEvent
    nasty = "Prep: read pp.1-5; do Q3,Q4\nthen \"review\" — ✅ café"
    cal = ICalendar()
    cal.add("prodid", "-//t//EN")
    cal.add("version", "2.0")
    event = IEvent()
    event.add("uid", "x@y")
    event.add("summary", nasty[:120])
    event.add("dtstart", datetime(2026, 9, 21, 17, 0, tzinfo=TZ))
    event.add("dtend", datetime(2026, 9, 21, 17, 30, tzinfo=TZ))
    cal.add_component(event)
    text = cal.to_ical().decode()

    reparsed = ICalendar.from_ical(text)
    summary = str(reparsed.walk("VEVENT")[0].get("summary"))
    assert "café" in summary and "✅" in summary
    assert "Q3,Q4" in summary


def test_very_long_title_is_truncated_not_rejected():
    long_title = "A" * 5000
    items = parse([{"url": "x", "body": {
        "id": "f" * 36, "displayName": long_title,
        "dueDateTime": "2026-09-25T08:30:00Z"}}])
    assert len(items) == 1
    assert items[0].title == long_title          # stored in full
    assert len(items[0].label) >= 5000


# ------------------------------------------------------- notifications

def test_a_long_report_is_truncated_for_telegram(tmp_path: Path, monkeypatch):
    """Telegram rejects messages over 4096 characters."""
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

    import requests
    def fake_post(url, json=None, timeout=None):
        captured["len"] = len(json["text"])
        return FakeResponse()
    monkeypatch.setattr(requests, "post", fake_post)

    from prep.notify import NotifyConfig, TelegramConfig
    cfg = NotifyConfig(state_path=tmp_path / "a.json",
                       telegram=TelegramConfig(bot_token="t", chat_id="1"))
    huge = "\n".join(f"MOVED Teacher {n}: a prep with a long title" for n in range(400))
    notify.push(cfg, "Prep rescheduled", huge)
    assert captured["len"] <= 4096, f"sent {captured['len']} chars to Telegram"


def test_zero_and_negative_timings_are_rejected(tmp_path: Path):
    """A stray 0 would drag every estimate down."""
    store = Store(tmp_path / "p.db")
    from prep.scraper import RawAssignment
    store.upsert([RawAssignment("a" * 36, "Sheet", "", "cls", "", "",
                                "2026-09-21T09:00:00Z", "2026-09-25T08:30:00Z")],
                 lambda item: "Dalton")
    with pytest.raises(ValueError):
        store.record_timing("a" * 36, 0)
    with pytest.raises(ValueError):
        store.record_timing("a" * 36, -30)
    store.close()


# ------------------------------------------------------- feeds

def test_event_with_duration_instead_of_dtend():
    ics = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//t//EN
BEGIN:VEVENT
UID:dur@socs
DTSTART;TZID=Europe/London:20260921T090000
DURATION:PT45M
SUMMARY:Lesson with DURATION
END:VEVENT
END:VCALENDAR
"""
    events = feeds.parse(ics, datetime(2026, 9, 21, tzinfo=TZ),
                         datetime(2026, 9, 22, tzinfo=TZ), TZ)
    assert len(events) == 1
    assert events[0].minutes == 45, f"got {events[0].minutes} minutes"


def test_feed_with_no_events_is_not_an_error():
    ics = b"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nEND:VCALENDAR\n"
    assert feeds.parse(ics, datetime(2026, 9, 21, tzinfo=TZ),
                       datetime(2026, 9, 28, tzinfo=TZ), TZ) == []


def test_zero_length_feed_event_gets_a_sane_duration():
    ics = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//t//EN
BEGIN:VEVENT
UID:zero@socs
DTSTART;TZID=Europe/London:20260921T090000
DTEND;TZID=Europe/London:20260921T090000
SUMMARY:Instant
END:VEVENT
END:VCALENDAR
"""
    events = feeds.parse(ics, datetime(2026, 9, 21, tzinfo=TZ),
                         datetime(2026, 9, 22, tzinfo=TZ), TZ)
    assert events and events[0].minutes > 0


# ------------------------------------------------- horizon vs deadline

def test_prep_due_beyond_the_busy_horizon_is_not_scheduled_blind(tmp_path: Path):
    """busy time is only fetched for the horizon. Scheduling past it would
    place prep in days whose commitments were never loaded."""
    project = tmp_path
    (project / "payloads.json").write_text(json.dumps([{
        "url": "assignments", "body": {"value": [
            {"id": "h" * 36, "classId": "cls", "displayName": "Distant coursework",
             "assignedDateTime": "2026-09-21T09:00:00Z",
             "dueDateTime": "2026-12-15T08:30:00Z"}]}}]))
    (project / "config.yaml").write_text(f"""
db_path: {project}/p.db
dump_dir: {project}/d
export_path: {project}/p.txt
timezone: Europe/London
teams: {{assignments_url: x, url_hints: [a], profile_dir: {project}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling:
  windows:
    monday:    {{start: "17:00", end: "21:30"}}
    tuesday:   {{start: "17:00", end: "21:30"}}
    wednesday: {{start: "17:00", end: "21:30"}}
    thursday:  {{start: "17:00", end: "21:30"}}
    friday:    {{start: "17:00", end: "21:00"}}
    saturday:  {{start: "09:30", end: "18:00"}}
    sunday:    {{start: "09:30", end: "20:00"}}
""")
    cfg = config_module.load(project / "config.yaml")
    store = Store(cfg.db_path)
    from prep.scraper import load_payload_file
    changes = store.upsert(parse(load_payload_file(project / "payloads.json")),
                           pipeline.teacher_matcher(cfg))

    # Fill every evening inside the horizon, so any block that still gets
    # placed must have spilled past it.
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    packed = [Interval(datetime.combine((now + timedelta(days=n)).date(),
                                        time(0, 0), tzinfo=TZ),
                       datetime.combine((now + timedelta(days=n)).date(),
                                        time(23, 59), tzinfo=TZ))
              for n in range(22)]
    calendar = FakeCalendar(busy_intervals=packed)
    pipeline.reschedule(cfg, store, calendar, changes, now=now, horizon_days=21)

    horizon = now + timedelta(days=21)
    for session in calendar.events.values():
        assert session.start <= horizon, (
            f"scheduled {session.start} beyond the {horizon} busy horizon, "
            "where no commitments were loaded")
    store.close()


def test_a_week_long_feed_event_reports_no_slot_rather_than_crashing():
    """A trip covering the whole window leaves nowhere to put prep."""
    cfg = sched()
    now = datetime(2026, 9, 21, 9, 0, tzinfo=TZ)
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)
    trip = [Interval(datetime(2026, 9, 21, 0, 0, tzinfo=TZ),
                     datetime(2026, 9, 26, 0, 0, tzinfo=TZ))]
    assert scheduler.plan(cfg, "id", "Essay", "B", 120, now, due, trip, now, TZ) == []


# ------------------------------------------------- store lookups

def test_a_title_containing_sql_wildcards_matches_only_itself(tmp_path: Path):
    """A LIKE lookup on '100% recall' must not match everything."""
    store = Store(tmp_path / "p.db")
    from prep.scraper import RawAssignment
    store.upsert([
        RawAssignment("w1" + "0" * 34, "100% recall test", "", "c", "", "",
                      None, "2026-09-25T08:30:00Z"),
        RawAssignment("w2" + "0" * 34, "Unrelated essay", "", "c", "", "",
                      None, "2026-09-26T08:30:00Z"),
    ], lambda item: "")
    rows = store.find_by_label("100% recall")
    assert len(rows) == 1
    assert rows[0]["label"] == "100% recall test"

    # A bare "%" is a literal, so it matches only the row that contains one -
    # not every row, which is what an unescaped LIKE would do.
    wildcard = store.find_by_label("%")
    assert len(wildcard) == 1
    assert wildcard[0]["label"] == "100% recall test"
    assert store.find_by_label("_") == []
    store.close()


def test_synthetic_ids_do_not_collide_across_different_classes():
    items = parse([{"url": "x", "body": {"value": [
        {"title": "Prep 1", "className": "Physics", "due": "2026-09-25T08:30:00Z"},
        {"title": "Prep 1", "className": "Chemistry", "due": "2026-09-25T08:30:00Z"},
    ]}}])
    assert len({a.assignment_id for a in items}) == 2, "two classes collapsed into one"


# ------------------------------------------------- stale feed cache

def test_a_very_stale_feed_cache_is_refused(tmp_path: Path, monkeypatch):
    """A months-old timetable is worse than none: term structure has changed."""
    cache = tmp_path / "socs.ics"
    cache.write_bytes(b"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nEND:VCALENDAR\n")
    import os
    old = datetime.now(timezone.utc).timestamp() - 30 * 24 * 3600
    os.utime(cache, (old, old))

    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))

    with pytest.raises(feeds.FeedError) as excinfo:
        feeds.fetch("https://example.invalid/f.ics", cache=cache)
    assert "stale" in str(excinfo.value).lower()


def test_a_fresh_cache_is_still_used(tmp_path: Path, monkeypatch):
    cache = tmp_path / "socs.ics"
    cache.write_bytes(b"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nEND:VCALENDAR\n")

    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    assert b"VCALENDAR" in feeds.fetch("https://example.invalid/f.ics", cache=cache)


# ------------------------------------------- can a creator split a shared class

def _creator_payload(creator_ids: list[str], class_id: str = "shared-class"):
    return [{"url": "https://teams.microsoft.com/api/assignments/v1/list",
             "body": {"value": [
                 {"id": f"{n}" * 36, "classId": class_id,
                  "displayName": f"Prep {n}",
                  "dueDateTime": f"2026-09-2{n}T08:30:00Z",
                  "createdBy": {"user": {"id": who, "displayName": None}}}
                 for n, who in enumerate(creator_ids, start=1)]}}]


def test_distinct_creators_in_one_class_are_all_extracted():
    """The claim being tested: the creator splits a class that codes cannot."""
    items = parse(_creator_payload(["t-aaa", "t-bbb", "t-ccc"]))
    assert len({a.class_id for a in items}) == 1, "fixture is not a shared class"
    assert {a.creator_id for a in items} == {"t-aaa", "t-bbb", "t-ccc"}


def test_creator_resolves_teachers_a_class_map_would_conflate(tmp_path: Path):
    from prep import pipeline

    learned = tmp_path / "teachers.json"
    learned.write_text(json.dumps({"people_map": {
        "t-aaa": "Archer", "t-bbb": "Brooks", "t-ccc": "Carver"}}))
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/d
export_path: {tmp_path}/p.txt
learned_map_path: {learned}
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling:
  windows: {{monday: {{start: "17:00", end: "21:30"}}}}
  class_map: {{shared-class: Archer}}
""")
    cfg = config_module.load(tmp_path / "config.yaml")
    resolve = pipeline.teacher_matcher(cfg)
    resolved = [resolve(a) for a in parse(_creator_payload(["t-aaa", "t-bbb", "t-ccc"]))]

    # A class map would call all three the same person. The creator must not.
    assert resolved == ["Archer", "Brooks", "Carver"]


def test_creator_name_is_used_when_the_tenant_sends_one():
    items = parse([{"url": "x", "body": {
        "id": "n" * 36, "classId": "c", "displayName": "Essay",
        "dueDateTime": "2026-09-25T08:30:00Z",
        "createdBy": {"user": {"id": "t-1", "displayName": "R Archer"}}}}])
    assert items[0].creator_name == "R Archer"
    assert items[0].creator_id == "t-1"


def test_missing_creator_leaves_the_fields_blank_not_broken():
    items = parse([{"url": "x", "body": {
        "id": "m" * 36, "classId": "c", "displayName": "Essay",
        "dueDateTime": "2026-09-25T08:30:00Z"}}])
    assert items[0].creator_id == "" and items[0].creator_name == ""


def test_odd_creator_shapes_do_not_crash_the_parser():
    """Tenants differ; an unexpected shape must degrade, not explode."""
    shapes = [
        {"createdBy": "just-a-string"},
        {"createdBy": {"id": "flat-1", "displayName": "Flat Shape"}},
        {"createdBy": {"user": None}},
        {"createdBy": {"application": {"id": "app-1"}}},
        {"lastModifiedBy": {"user": {"id": "lm-1"}}},
    ]
    for extra in shapes:
        node = {"id": "z" * 36, "classId": "c", "displayName": "Essay",
                "dueDateTime": "2026-09-25T08:30:00Z", **extra}
        items = parse([{"url": "x", "body": node}])
        assert len(items) == 1
        assert isinstance(items[0].creator_id, str)
        assert isinstance(items[0].creator_name, str)
