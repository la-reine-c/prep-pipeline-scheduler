"""Tests. Run with: python -m pytest -q"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import config as config_module
from prep import estimator, pipeline, scheduler
from prep.config import DayWindow, EstimatorConfig, SchedulingConfig
from prep.scheduler import FakeCalendar, Interval
from prep.scraper import RawAssignment, parse
from prep.store import Store

TZ = ZoneInfo("Europe/London")


def sched_cfg(**kw) -> SchedulingConfig:
    base = SchedulingConfig(
        block_minutes=30,
        flow_buffer_minutes=5,
        gap_minutes=10,
        due_time=config_module.parse_time("08:30"),
        windows={d: DayWindow(config_module.parse_time("17:00"),
                              config_module.parse_time("21:30")) for d in range(0, 5)}
        | {5: DayWindow(config_module.parse_time("09:30"), config_module.parse_time("18:00")),
           6: DayWindow(config_module.parse_time("09:30"), config_module.parse_time("20:00"))},
        teacher_keywords={"Dalton": ["dalton", "physics"], "Garner": ["garner"]},
    )
    for key, value in kw.items():
        setattr(base, key, value)
    return base


# ---------------------------------------------------------------- parser

def test_parse_extracts_nested_assignments():
    payloads = [{
        "url": "https://teams.microsoft.com/api/assignments/v1/list",
        "body": {"value": [
            {"id": "a" * 36, "displayName": "Kinematics sheet",
             "className": "Y12 Physics Dalton", "assignedDateTime": "2026-09-14T09:00:00Z",
             "dueDateTime": "2026-09-18T08:30:00Z"},
            {"wrapper": {"assignment": {
                "id": "b" * 36, "title": "Titration writeup",
                "courseName": "Chemistry Garner",
                "assignedDateTime": "2026-09-18T15:00:00Z",
                "dueDateTime": "2026-09-25T08:30:00Z"}}},
        ]},
    }]
    found = parse(payloads)
    assert len(found) == 2
    assert {a.title for a in found} == {"Kinematics sheet", "Titration writeup"}
    assert all(not a.id_is_synthetic for a in found)


def test_parse_ignores_objects_without_due_date():
    payloads = [{"url": "assignments", "body": {"user": {"displayName": "A Student"}}}]
    assert parse(payloads) == []


def test_synthetic_id_is_stable_when_due_date_moves():
    """The identity bug we explicitly designed against."""
    first = parse([{"url": "assignments", "body": {
        "title": "Essay", "className": "Music", "dueDateTime": "2026-09-20T08:30:00Z"}}])
    second = parse([{"url": "assignments", "body": {
        "title": "Essay", "className": "Music", "dueDateTime": "2026-09-27T08:30:00Z"}}])
    assert first[0].id_is_synthetic and second[0].id_is_synthetic
    assert first[0].assignment_id == second[0].assignment_id


# ---------------------------------------------------------------- estimator

def test_pattern_default_is_min_30d_120():
    cfg = EstimatorConfig()
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)
    assert estimator.pattern_minutes(cfg, datetime(2026, 9, 23, 9, 0, tzinfo=TZ), due) == 60
    assert estimator.pattern_minutes(cfg, datetime(2026, 9, 18, 9, 0, tzinfo=TZ), due) == 120
    assert estimator.pattern_minutes(cfg, None, due) == 30


def test_estimate_uses_pattern_without_timings():
    cfg = EstimatorConfig()
    est = estimator.estimate(cfg, "Dalton", datetime(2026, 9, 21, 9, 0, tzinfo=TZ),
                             datetime(2026, 9, 25, 8, 30, tzinfo=TZ), {}, None)
    assert est.source == "pattern" and est.minutes == 120


def test_single_outlier_is_shrunk_towards_the_mean():
    cfg = EstimatorConfig(shrinkage_k=3.0)
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)
    assigned = datetime(2026, 9, 21, 9, 0, tzinfo=TZ)
    est = estimator.estimate(cfg, "Garner", assigned, due, {"Garner": (1, 20.0)}, 80.0)
    # (1*20 + 3*80) / 4 = 65
    assert est.minutes == 65 and est.source == "shrunk"


def test_many_timings_dominate_the_prior():
    cfg = EstimatorConfig(shrinkage_k=3.0)
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)
    est = estimator.estimate(cfg, "Dalton", datetime(2026, 9, 21, 9, 0, tzinfo=TZ), due,
                             {"Dalton": (12, 50.0)}, 80.0)
    assert est.source == "measured"
    assert 50 <= est.minutes <= 57


def test_estimate_never_exceeds_cap():
    cfg = EstimatorConfig(cap_minutes=120)
    est = estimator.estimate(cfg, "Foster", None, datetime(2026, 9, 25, 8, 30, tzinfo=TZ),
                             {"Foster": (10, 400.0)}, 400.0)
    assert est.minutes == 120


# ---------------------------------------------------------------- slot maths

def test_merge_collapses_overlaps():
    base = datetime(2026, 9, 21, 17, 0, tzinfo=TZ)
    merged = scheduler.merge([
        Interval(base, base + timedelta(hours=1)),
        Interval(base + timedelta(minutes=30), base + timedelta(hours=2)),
        Interval(base + timedelta(hours=5), base + timedelta(hours=6)),
    ])
    assert len(merged) == 2
    assert merged[0].minutes == 120


def test_free_slots_respect_working_window_and_busy_time():
    cfg = sched_cfg()
    start = datetime(2026, 9, 21, 12, 0, tzinfo=TZ)     # Monday midday
    end = datetime(2026, 9, 22, 8, 30, tzinfo=TZ)
    busy = [Interval(datetime(2026, 9, 21, 18, 0, tzinfo=TZ),
                     datetime(2026, 9, 21, 19, 30, tzinfo=TZ))]
    slots = scheduler.free_slots(cfg, start, end, busy, TZ)
    assert all(s.start.hour >= 17 for s in slots)
    assert all(s.end.hour <= 21 or (s.end.hour == 21 and s.end.minute <= 30) for s in slots)
    # 17:00-17:50 (10 min gap before the busy block) and 19:40-21:30
    assert len(slots) == 2
    assert slots[0].minutes == 50 and slots[1].minutes == 110


def test_free_slots_skips_a_day_with_no_window():
    cfg = sched_cfg(windows={0: DayWindow(config_module.parse_time("17:00"),
                                          config_module.parse_time("21:30"))})
    slots = scheduler.free_slots(cfg, datetime(2026, 9, 22, 9, 0, tzinfo=TZ),
                                 datetime(2026, 9, 23, 9, 0, tzinfo=TZ), [], TZ)
    assert slots == []


def test_plan_places_enough_blocks_and_spreads_them():
    cfg = sched_cfg()
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)        # Monday
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)        # Friday
    sessions = scheduler.plan(cfg, "id1", "Kinematics", "Dalton", 120,
                              now, due, [], now, TZ)
    assert sum(s.minutes for s in sessions) >= 120
    assert len({s.start.date() for s in sessions}) > 1   # not all crammed into one evening
    assert all(s.end <= datetime(2026, 9, 25, 8, 30, tzinfo=TZ) for s in sessions)


def test_plan_never_overlaps_busy_time():
    cfg = sched_cfg()
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    due = datetime(2026, 9, 23, 8, 30, tzinfo=TZ)
    busy = [Interval(datetime(2026, 9, 21, 17, 0, tzinfo=TZ),
                     datetime(2026, 9, 21, 21, 30, tzinfo=TZ))]
    sessions = scheduler.plan(cfg, "id2", "Essay", "Hughes", 60, now, due, busy, now, TZ)
    for session in sessions:
        for block in busy:
            assert session.end <= block.start or session.start >= block.end


def test_plan_returns_nothing_when_deadline_has_passed():
    cfg = sched_cfg()
    now = datetime(2026, 9, 25, 10, 0, tzinfo=TZ)
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)
    assert scheduler.plan(cfg, "id3", "Late", "Dalton", 60, None, due, [], now, TZ) == []


def test_flow_buffer_applies_only_after_the_first_sitting():
    cfg = sched_cfg()
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    due = datetime(2026, 9, 26, 8, 30, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "id4", "Long", "Garner", 120, now, due, [], now, TZ)
    assert sessions[0].minutes == 30
    assert all(s.minutes == 35 for s in sessions[1:])


# ---------------------------------------------------------------- store

def raw(**kw) -> RawAssignment:
    defaults = dict(assignment_id="x" * 36, title="Sheet 1", class_ref="Physics Dalton",
                    class_id="aaaaaaaa-1111-2222-3333-444444444444",
                    creator_id="", creator_name="",
                    assigned_at="2026-09-21T09:00:00Z", due_at="2026-09-25T08:30:00Z")
    defaults.update(kw)
    return RawAssignment(**defaults)


def test_store_detects_new_then_moved_due_date():
    store = Store(":memory:")
    resolve = lambda item: "Dalton"

    changes = store.upsert([raw()], resolve)
    assert [c.kind for c in changes] == ["new"]

    assert store.upsert([raw()], resolve) == []          # idempotent

    changes = store.upsert([raw(due_at="2026-09-28T08:30:00Z")], resolve)
    assert changes[0].kind == "due_changed"
    assert changes[0].previous_due == "2026-09-25T08:30:00Z"
    store.close()


def test_timings_feed_teacher_stats():
    store = Store(":memory:")
    store.upsert([raw()], lambda item: "Dalton")
    store.record_timing("x" * 36, 50)
    store.record_timing("x" * 36, 70)
    assert store.teacher_stats() == {"Dalton": (2, 60.0)}
    assert store.global_mean() == 60.0
    store.close()


# ---------------------------------------------------------------- end to end

@pytest.fixture
def project(tmp_path: Path) -> Path:
    payloads = [{
        "url": "https://teams.microsoft.com/api/assignments/v1/list",
        "body": {"value": [
            {"id": "1" * 36, "displayName": "Kinematics sheet",
             "className": "Y12 Physics Dalton",
             "assignedDateTime": "2026-09-21T09:00:00Z",
             "dueDateTime": "2026-09-25T08:30:00Z"},
            {"id": "2" * 36, "displayName": "Titration writeup",
             "className": "Y12 Chemistry Garner",
             "assignedDateTime": "2026-09-21T09:00:00Z",
             "dueDateTime": "2026-09-24T08:30:00Z"},
        ]},
    }]
    (tmp_path / "payloads.json").write_text(json.dumps(payloads))

    config = f"""
db_path: {tmp_path}/prep.sqlite3
dump_dir: {tmp_path}/dumps
export_path: {tmp_path}/prep.txt
timezone: Europe/London
teams:
  assignments_url: "https://teams.microsoft.com/v2/#/edu/assignments"
  url_hints: ["assignment"]
  profile_dir: {tmp_path}/profile
calendar:
  caldav_url: "https://caldav.icloud.com/"
  username: "test@example.com"
  password: "test-pass"
  prep_calendar: "Prep"
estimator:
  per_day_minutes: 30
  cap_minutes: 120
  shrinkage_k: 3
scheduling:
  block_minutes: 30
  flow_buffer_minutes: 5
  gap_minutes: 10
  due_time: "08:30"
  windows:
    monday:    {{start: "17:00", end: "21:30"}}
    tuesday:   {{start: "17:00", end: "21:30"}}
    wednesday: {{start: "17:00", end: "21:30"}}
    thursday:  {{start: "17:00", end: "21:30"}}
    friday:    {{start: "17:00", end: "21:00"}}
    saturday:  {{start: "09:30", end: "18:00"}}
    sunday:    {{start: "09:30", end: "20:00"}}
  teacher_keywords:
    Dalton: ["dalton"]
    Garner: ["garner"]
web:
  token: "test-token"
"""
    (tmp_path / "config.yaml").write_text(config)
    return tmp_path


def test_end_to_end_schedules_both_preps(project: Path):
    cfg = config_module.load(project / "config.yaml")
    store = Store(cfg.db_path)
    from prep.scraper import load_payload_file

    assignments = parse(load_payload_file(project / "payloads.json"))
    changes = store.upsert(assignments, pipeline.teacher_matcher(cfg))
    assert len(changes) == 2

    calendar = FakeCalendar(busy_intervals=[
        Interval(datetime(2026, 9, 22, 17, 0, tzinfo=TZ),   # Tuesday rehearsal
                 datetime(2026, 9, 22, 21, 0, tzinfo=TZ)),
    ])
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    outcomes = pipeline.reschedule(cfg, store, calendar, changes, now=now)

    assert all(o.sessions for o in outcomes), [o.describe() for o in outcomes]
    assert len(calendar.events) == sum(len(o.sessions) for o in outcomes)

    # Nothing may land inside the Tuesday rehearsal.
    for session in calendar.events.values():
        assert not (session.start < datetime(2026, 9, 22, 21, 0, tzinfo=TZ)
                    and session.end > datetime(2026, 9, 22, 17, 0, tzinfo=TZ))

    # Two preps must not be scheduled on top of each other.
    ordered = sorted(calendar.events.values(), key=lambda s: s.start)
    for earlier, later in zip(ordered, ordered[1:]):
        assert later.start >= earlier.end

    assert cfg.export_path.exists()
    store.close()


def test_moved_due_date_replaces_old_sessions(project: Path):
    cfg = config_module.load(project / "config.yaml")
    store = Store(cfg.db_path)
    from prep.scraper import load_payload_file

    payload_path = project / "payloads.json"
    assignments = parse(load_payload_file(payload_path))
    changes = store.upsert(assignments, pipeline.teacher_matcher(cfg))

    calendar = FakeCalendar()
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    pipeline.reschedule(cfg, store, calendar, changes, now=now)
    original_uids = {r["uid"] for r in store.sessions_for("1" * 36)}
    assert original_uids

    # Dalton pulls the deadline forward by a day.
    payloads = json.loads(payload_path.read_text())
    payloads[0]["body"]["value"][0]["dueDateTime"] = "2026-09-24T08:30:00Z"
    moved = store.upsert(parse(payloads), pipeline.teacher_matcher(cfg))
    assert moved[0].kind == "due_changed"

    pipeline.reschedule(cfg, store, calendar, moved, now=now)
    new_uids = {r["uid"] for r in store.sessions_for("1" * 36)}

    assert new_uids and new_uids.isdisjoint(original_uids)     # replaced, not duplicated
    assert original_uids.isdisjoint(calendar.events.keys())    # old events deleted
    for session in [calendar.events[uid] for uid in new_uids]:
        assert session.end <= datetime(2026, 9, 24, 8, 30, tzinfo=TZ)
    store.close()


def test_web_dashboard_serves_and_logs_a_timing(project: Path):
    from prep.web import create_app

    cfg = config_module.load(project / "config.yaml")
    store = Store(cfg.db_path)
    from prep.scraper import load_payload_file
    changes = store.upsert(parse(load_payload_file(project / "payloads.json")),
                           pipeline.teacher_matcher(cfg))
    pipeline.reschedule(cfg, store, FakeCalendar(), changes,
                        now=datetime(2026, 9, 21, 16, 0, tzinfo=TZ))
    store.close()

    app = create_app(cfg, calendar_factory=lambda: FakeCalendar())
    client = app.test_client()

    assert client.get("/").status_code == 403                      # token enforced
    page = client.get("/?token=test-token")
    assert page.status_code == 200
    assert b"Kinematics sheet" in page.data

    posted = client.post("/log", data={"assignment_id": "1" * 36, "minutes": "55",
                                       "token": "test-token"})
    assert posted.status_code == 302

    store = Store(cfg.db_path)
    assert store.teacher_stats()["Dalton"] == (1, 55.0)
    store.close()


def test_daily_ceiling_is_shared_across_preps():
    """Three preps must not stack four hours into one evening."""
    cfg = sched_cfg(max_daily_minutes=120)
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    due = datetime(2026, 9, 28, 8, 30, tzinfo=TZ)
    load: dict = {}
    placed = []
    for n in range(3):
        placed += scheduler.plan(cfg, f"id{n}", f"Prep {n}", "Dalton", 120,
                                 now, due, [], now, TZ, daily_load=load)
    per_day: dict = {}
    for session in placed:
        per_day[session.start.date()] = per_day.get(session.start.date(), 0) + session.minutes
    assert per_day, "nothing scheduled"
    assert max(per_day.values()) <= 125   # one block's flow buffer of slack


def test_end_to_end_respects_the_daily_ceiling(project: Path):
    cfg = config_module.load(project / "config.yaml")
    cfg.scheduling.max_daily_minutes = 90
    store = Store(cfg.db_path)
    from prep.scraper import load_payload_file
    changes = store.upsert(parse(load_payload_file(project / "payloads.json")),
                           pipeline.teacher_matcher(cfg))
    calendar = FakeCalendar()
    pipeline.reschedule(cfg, store, calendar, changes,
                        now=datetime(2026, 9, 21, 16, 0, tzinfo=TZ))
    per_day: dict = {}
    for session in calendar.events.values():
        per_day[session.start.date()] = per_day.get(session.start.date(), 0) + session.minutes
    assert max(per_day.values()) <= 95
    store.close()


# ---------------------------------------------------------------- ICS feeds

FEED_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//SOCS//EN
BEGIN:VEVENT
UID:evening-rehearsal@socs
DTSTART;TZID=Europe/London:20260921T170000
DTEND;TZID=Europe/London:20260921T213000
RRULE:FREQ=DAILY;COUNT=6
SUMMARY:Rehearsal - blocks every evening
END:VEVENT
END:VCALENDAR
"""


def _feed_project(project: Path, url: str) -> None:
    """Point the project config at a feed."""
    text = (project / "config.yaml").read_text()
    (project / "config.yaml").write_text(
        text + f'\nfeeds:\n  socs: "{url}"\nfeed_cache_dir: {project}/feed-cache\n')


def test_feed_events_block_scheduling(project: Path, monkeypatch):
    """A timetable event from a subscribed feed must reserve that time."""
    _feed_project(project, "https://example.invalid/socs.ics")

    class FakeResponse:
        content = FEED_ICS
        def raise_for_status(self): pass

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())

    cfg = config_module.load(project / "config.yaml")
    store = Store(cfg.db_path)
    from prep.scraper import load_payload_file
    changes = store.upsert(parse(load_payload_file(project / "payloads.json")),
                           pipeline.teacher_matcher(cfg))

    calendar = FakeCalendar()
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    pipeline.reschedule(cfg, store, calendar, changes, now=now)

    # Every weekday evening in range is taken by the feed, so nothing may land
    # inside 17:00-21:30 on those days.
    for session in calendar.events.values():
        day = session.start.date()
        if datetime(2026, 9, 21).date() <= day <= datetime(2026, 9, 26).date():
            blocked_start = datetime.combine(day, time(17, 0), tzinfo=TZ)
            blocked_end = datetime.combine(day, time(21, 30), tzinfo=TZ)
            assert not (session.start < blocked_end and session.end > blocked_start), \
                f"prep scheduled during a timetabled event: {session.start}"
    store.close()


def test_failed_feed_aborts_rather_than_scheduling_blind(project: Path, monkeypatch):
    """Losing the timetable must stop the run, not produce a confident wrong plan."""
    _feed_project(project, "https://example.invalid/socs.ics")

    import requests
    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(requests, "get", boom)

    cfg = config_module.load(project / "config.yaml")
    store = Store(cfg.db_path)
    from prep.scraper import load_payload_file
    changes = store.upsert(parse(load_payload_file(project / "payloads.json")),
                           pipeline.teacher_matcher(cfg))

    calendar = FakeCalendar()
    with pytest.raises(pipeline.FeedsUnavailable):
        pipeline.reschedule(cfg, store, calendar, changes,
                            now=datetime(2026, 9, 21, 16, 0, tzinfo=TZ))
    assert calendar.events == {}          # nothing written
    store.close()


def test_cached_feed_keeps_the_run_working(project: Path, monkeypatch, tmp_path: Path):
    """A brief network blip should use the cached timetable, not abort."""
    _feed_project(project, "https://example.invalid/socs.ics")
    cfg = config_module.load(project / "config.yaml")
    cfg.feed_cache_dir.mkdir(parents=True, exist_ok=True)
    (cfg.feed_cache_dir / "socs.ics").write_bytes(FEED_ICS)

    import requests
    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(requests, "get", boom)

    store = Store(cfg.db_path)
    from prep.scraper import load_payload_file
    changes = store.upsert(parse(load_payload_file(project / "payloads.json")),
                           pipeline.teacher_matcher(cfg))
    calendar = FakeCalendar()
    outcomes = pipeline.reschedule(cfg, store, calendar, changes,
                                   now=datetime(2026, 9, 21, 16, 0, tzinfo=TZ))
    assert outcomes                       # ran, using the cache
    store.close()


def test_dry_run_reads_real_busy_time_but_writes_nothing(project: Path):
    """A dry run against an empty diary would reassure you about an impossible plan."""
    from prep.scheduler import DryRunCalendar

    cfg = config_module.load(project / "config.yaml")
    store = Store(cfg.db_path)
    from prep.scraper import load_payload_file
    changes = store.upsert(parse(load_payload_file(project / "payloads.json")),
                           pipeline.teacher_matcher(cfg))

    # Monday evening is entirely taken in the "real" calendar.
    real = FakeCalendar(busy_intervals=[
        Interval(datetime(2026, 9, 21, 17, 0, tzinfo=TZ),
                 datetime(2026, 9, 21, 21, 30, tzinfo=TZ))])
    dry = DryRunCalendar(real)

    pipeline.reschedule(cfg, store, dry, changes,
                        now=datetime(2026, 9, 21, 16, 0, tzinfo=TZ))

    assert dry.would_create, "dry run produced no plan at all"
    assert real.events == {}, "dry run wrote to the calendar"

    # The busy time must have been honoured, not ignored.
    for session in dry.would_create:
        assert not (session.start < datetime(2026, 9, 21, 21, 30, tzinfo=TZ)
                    and session.end > datetime(2026, 9, 21, 17, 0, tzinfo=TZ)), \
            "dry run ignored real busy time"
    store.close()


# ------------------------------------------------- teacher resolution (real shapes)

REAL_SHAPE = [{
    "url": "https://teams.microsoft.com/api/assignments/v1/list",
    "body": {"value": [
        # No className anywhere - only the GUID. This is what his tenant sends.
        {"id": "a1" + "0" * 34, "classId": "11111111-2222-3333-4444-555555555555",
         "displayName": "AAA Prep 3 - The Discriminant", "status": "assigned",
         "dueDateTime": "2026-09-21T07:00:00Z"},
        {"id": "a2" + "0" * 34, "classId": "11111111-2222-3333-4444-555555555555",
         "displayName": "M3 CCC Prep 1", "status": "assigned",
         "dueDateTime": "2026-09-23T07:20:00Z"},
        {"id": "a3" + "0" * 34, "classId": "66666666-7777-8888-9999-aaaaaaaaaaaa",
         "displayName": "Core Practical 1", "status": "assigned",
         "dueDateTime": "2026-09-22T22:59:00Z"},
    ]},
}]


def test_class_guid_never_masquerades_as_a_class_name():
    """The GUID in the label made every keyword match score against a GUID."""
    items = parse(REAL_SHAPE)
    assert all(a.class_id for a in items)
    assert all(a.class_ref == "" for a in items)
    for item in items:
        assert "11111111" not in item.label and "66666666" not in item.label


def test_title_codes_separate_teachers_sharing_one_class(project: Path):
    """Three maths teachers post into one class; only the title tells them apart."""
    text = (project / "config.yaml").read_text().replace(
        "  teacher_keywords:",
        "  title_codes: {AAA: Archer, BBB: Brooks, CCC: Carver}\n"
        "  class_map:\n"
        "    66666666-7777-8888-9999-aaaaaaaaaaaa: Garner\n"
        "  teacher_keywords:")
    (project / "config.yaml").write_text(text)

    cfg = config_module.load(project / "config.yaml")
    resolve = pipeline.teacher_matcher(cfg)
    by_title = {a.title: resolve(a) for a in parse(REAL_SHAPE)}

    assert by_title["AAA Prep 3 - The Discriminant"] == "Archer"
    assert by_title["M3 CCC Prep 1"] == "Carver"
    assert by_title["Core Practical 1"] == "Garner"      # resolved by class GUID


def test_unknown_class_stays_blank_rather_than_guessing(project: Path):
    cfg = config_module.load(project / "config.yaml")
    resolve = pipeline.teacher_matcher(cfg)
    unknown = parse([{"url": "x", "body": {
        "id": "b1" + "0" * 34, "classId": "ffffffff-0000-0000-0000-000000000000",
        "displayName": "Mystery task", "dueDateTime": "2026-09-30T08:30:00Z"}}])
    assert resolve(unknown[0]) == ""


def test_past_due_assignments_are_skipped(project: Path):
    """A first sync pulls in a year of history that cannot be scheduled."""
    stale = [{"url": "x", "body": {"value": [
        {"id": "c1" + "0" * 34, "classId": "z", "displayName": "Latin vocab",
         "dueDateTime": "2026-04-29T08:00:00Z"},
        {"id": "c2" + "0" * 34, "classId": "z", "displayName": "Current work",
         "dueDateTime": "2026-09-25T08:30:00Z"},
    ]}}]
    cfg = config_module.load(project / "config.yaml")
    store = Store(cfg.db_path)
    changes = store.upsert(parse(stale), pipeline.teacher_matcher(cfg))

    calendar = FakeCalendar()
    outcomes = pipeline.reschedule(cfg, store, calendar, changes,
                                   now=datetime(2026, 9, 21, 16, 0, tzinfo=TZ))
    problems = {o.change.label: o.problem for o in outcomes}
    assert problems["Latin vocab"] == "past due, skipped"
    assert not problems["Current work"]
    store.close()


def test_real_due_time_is_used_not_the_config_default():
    """A 23:59 deadline must not be truncated to 08:30, losing an evening."""
    cfg = sched_cfg()
    now = datetime(2026, 9, 22, 16, 0, tzinfo=TZ)
    due = datetime(2026, 9, 22, 23, 59, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "id", "Core Practical 1", "Garner", 60,
                              now, due, [], now, TZ)
    assert sessions, "a same-evening deadline should still be schedulable"
    assert all(s.end <= due for s in sessions)


def test_midnight_due_date_falls_back_to_config_time():
    cfg = sched_cfg()
    now = datetime(2026, 9, 21, 16, 0, tzinfo=TZ)
    due = datetime(2026, 9, 23, 0, 0, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "id", "Essay", "Dalton", 60, now, due, [], now, TZ)
    assert all(s.end <= datetime(2026, 9, 23, 8, 30, tzinfo=TZ) for s in sessions)


def test_old_database_is_migrated_not_broken(tmp_path: Path):
    """An existing database predates class_id; it must be upgraded in place."""
    import sqlite3
    old = tmp_path / "old.sqlite3"
    con = sqlite3.connect(old)
    con.executescript("""
        CREATE TABLE assignments (
            assignment_id TEXT PRIMARY KEY, title TEXT NOT NULL, class_ref TEXT,
            teacher TEXT, label TEXT, assigned_at TEXT, due_at TEXT,
            id_synthetic INTEGER DEFAULT 0, first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL);
        CREATE TABLE timings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id TEXT NOT NULL,
            minutes INTEGER NOT NULL, note TEXT, logged_at TEXT NOT NULL);
        INSERT INTO assignments VALUES
            ('old-1','Kept work','Physics','Dalton','Kept work',NULL,
             '2026-09-25T08:30:00Z',0,'2026-09-01','2026-09-01');
        INSERT INTO timings (assignment_id, minutes, note, logged_at)
            VALUES ('old-1', 47, '', '2026-09-01');
    """)
    con.commit(); con.close()

    store = Store(old)
    columns = {r[1] for r in store.conn.execute("PRAGMA table_info(assignments)")}
    assert "class_id" in columns

    # Existing rows and logged timings must survive the upgrade.
    assert store.teacher_stats() == {"Dalton": (1, 47.0)}

    # And inserting new-style rows must now work.
    store.upsert([raw()], lambda item: "Dalton")
    assert store.assignment("x" * 36) is not None
    store.close()


def test_unlogged_teacher_benefits_from_other_teachers_timings():
    """Logging times must pull down estimates everywhere, not one teacher at a time."""
    cfg = EstimatorConfig()
    assigned = datetime(2026, 9, 18, 9, 0, tzinfo=TZ)
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)

    # Nothing logged anywhere: fall back to the pattern.
    cold = estimator.estimate(cfg, "Irving", assigned, due, {}, None)
    assert cold.source == "pattern" and cold.minutes == 120

    # Times logged for OTHER teachers, averaging 40 minutes.
    warm = estimator.estimate(cfg, "Irving", assigned, due,
                              {"Dalton": (4, 40.0)}, 40.0)
    assert warm.source == "global"
    assert warm.minutes == 40


def test_global_fallback_never_inflates_a_short_window():
    """A prep set and due the same day must not be padded out to the average."""
    cfg = EstimatorConfig()
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)
    assigned = datetime(2026, 9, 24, 9, 0, tzinfo=TZ)     # pattern = 30 min
    est = estimator.estimate(cfg, "Irving", assigned, due, {"Dalton": (4, 90.0)}, 90.0)
    assert est.minutes == 30, "a 1-day prep should not inherit a 90 min average"


def test_logged_teacher_still_dominates_the_global_mean():
    cfg = EstimatorConfig(shrinkage_k=3.0)
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)
    assigned = datetime(2026, 9, 18, 9, 0, tzinfo=TZ)
    est = estimator.estimate(cfg, "Garner", assigned, due,
                             {"Garner": (10, 75.0)}, 30.0)
    assert est.source == "measured"
    assert 60 <= est.minutes <= 75


# ---------------------------------------------------------------- identify

def test_naming_one_prep_teaches_the_whole_class(tmp_path: Path):
    from prep import identify

    items = parse(REAL_SHAPE)
    learned = {"class_map": {}, "title_codes": {}}
    message = identify.apply_set(items, learned, "Core Practical", "Garner")

    assert "66666666-7777-8888-9999-aaaaaaaaaaaa" in learned["class_map"]
    assert learned["class_map"]["66666666-7777-8888-9999-aaaaaaaaaaaa"] == "Garner"
    assert "whole class" in message


def test_naming_a_code_maps_the_code_not_the_class(tmp_path: Path):
    from prep import identify

    items = parse(REAL_SHAPE)
    learned = {"class_map": {}, "title_codes": {}}
    identify.apply_set(items, learned, "AAA", "Archer")

    assert learned["title_codes"] == {"AAA": "Archer"}
    assert learned["class_map"] == {}, "a shared class must not be claimed by one code"


def test_ambiguous_title_refuses_to_guess():
    from prep import identify

    items = parse([{"url": "x", "body": {"value": [
        {"id": "d1" + "0" * 34, "classId": "class-one", "displayName": "Prep 1",
         "dueDateTime": "2026-09-25T08:30:00Z"},
        {"id": "d2" + "0" * 34, "classId": "class-two", "displayName": "Prep 2",
         "dueDateTime": "2026-09-26T08:30:00Z"},
    ]}}])
    learned = {"class_map": {}, "title_codes": {}}
    message = identify.apply_set(items, learned, "Prep", "Archer")
    assert "different classes" in message
    assert learned["class_map"] == {}


def test_learned_map_overrides_config(project: Path, tmp_path: Path):
    """What you taught it must win over anything stale in the YAML."""
    learned_file = tmp_path / "teachers.json"
    learned_file.write_text(json.dumps({
        "class_map": {"66666666-7777-8888-9999-aaaaaaaaaaaa": "Garner"},
        "title_codes": {"AAA": "Archer", "CCC": "Carver"},
    }))
    text = (project / "config.yaml").read_text() + f"\nlearned_map_path: {learned_file}\n"
    (project / "config.yaml").write_text(text)

    cfg = config_module.load(project / "config.yaml")
    resolve = pipeline.teacher_matcher(cfg)
    by_title = {a.title: resolve(a) for a in parse(REAL_SHAPE)}

    assert by_title["Core Practical 1"] == "Garner"
    assert by_title["AAA Prep 3 - The Discriminant"] == "Archer"
    assert by_title["M3 CCC Prep 1"] == "Carver"


def test_blank_entries_in_the_learned_map_are_ignored(project: Path, tmp_path: Path):
    """Half-filled templates must not map a class to an empty teacher."""
    learned_file = tmp_path / "teachers.json"
    learned_file.write_text(json.dumps({
        "class_map": {"66666666-7777-8888-9999-aaaaaaaaaaaa": ""},
        "title_codes": {},
    }))
    text = (project / "config.yaml").read_text() + f"\nlearned_map_path: {learned_file}\n"
    (project / "config.yaml").write_text(text)

    cfg = config_module.load(project / "config.yaml")
    assert "66666666-7777-8888-9999-aaaaaaaaaaaa" not in cfg.scheduling.class_map


# ------------------------------------------- shared classes (every class here)

CREATOR_SHAPE = [{
    "url": "https://teams.microsoft.com/api/assignments/v1/list",
    "body": {"value": [
        {"id": "e1" + "0" * 34, "classId": "chem-class",
         "displayName": "Core Practical 1", "dueDateTime": "2026-09-25T08:30:00Z",
         "createdBy": {"user": {"id": "teacher-aaa", "displayName": None}}},
        {"id": "e2" + "0" * 34, "classId": "chem-class",
         "displayName": "MCQ test", "dueDateTime": "2026-09-26T08:30:00Z",
         "createdBy": {"user": {"id": "teacher-bbb", "displayName": None}}},
        {"id": "e3" + "0" * 34, "classId": "music-class",
         "displayName": "Sentence Writing", "dueDateTime": "2026-09-27T08:30:00Z",
         "createdBy": {"user": {"id": "teacher-ccc", "displayName": "A Hughes"}}},
    ]},
}]


def test_creator_is_extracted():
    items = {a.title: a for a in parse(CREATOR_SHAPE)}
    assert items["Core Practical 1"].creator_id == "teacher-aaa"
    assert items["MCQ test"].creator_id == "teacher-bbb"
    assert items["Sentence Writing"].creator_name == "A Hughes"


def test_two_teachers_in_one_class_are_separated(project: Path, tmp_path: Path):
    """Every class here is shared, so class_map alone cannot work."""
    learned = tmp_path / "teachers.json"
    learned.write_text(json.dumps({"people_map": {
        "teacher-aaa": "Garner", "teacher-bbb": "Foster"}}))
    text = (project / "config.yaml").read_text() + f"\nlearned_map_path: {learned}\n"
    (project / "config.yaml").write_text(text)

    cfg = config_module.load(project / "config.yaml")
    resolve = pipeline.teacher_matcher(cfg)
    by_title = {a.title: resolve(a) for a in parse(CREATOR_SHAPE)}

    # Same classId, different teachers - only the creator distinguishes them.
    assert by_title["Core Practical 1"] == "Garner"
    assert by_title["MCQ test"] == "Foster"


def test_creator_beats_a_stale_class_map(project: Path, tmp_path: Path):
    """A class map that assumes one teacher must not override who actually set it."""
    learned = tmp_path / "teachers.json"
    learned.write_text(json.dumps({
        "class_map": {"chem-class": "Garner"},
        "people_map": {"teacher-bbb": "Foster"},
    }))
    text = (project / "config.yaml").read_text() + f"\nlearned_map_path: {learned}\n"
    (project / "config.yaml").write_text(text)

    cfg = config_module.load(project / "config.yaml")
    resolve = pipeline.teacher_matcher(cfg)
    by_title = {a.title: resolve(a) for a in parse(CREATOR_SHAPE)}
    assert by_title["MCQ test"] == "Foster"          # creator wins
    assert by_title["Core Practical 1"] == "Garner"  # unmapped creator falls through


def test_identify_maps_a_person(tmp_path: Path):
    from prep import identify
    items = parse(CREATOR_SHAPE)
    learned = {"class_map": {}, "title_codes": {}, "people_map": {}}
    message = identify.apply_set(items, learned, "who:teacher-aaa", "Garner")
    assert learned["people_map"] == {"teacher-aaa": "Garner"}
    assert "1 preps" in message


def test_dashboard_can_live_under_a_sub_path(project: Path):
    """Needed when another service already owns the root of the host."""
    from prep.web import create_app

    text = (project / "config.yaml").read_text().replace(
        'web:\n  token: "test-token"',
        'web:\n  token: "test-token"\n  url_prefix: "/prep"')
    (project / "config.yaml").write_text(text)

    cfg = config_module.load(project / "config.yaml")
    assert cfg.web.url_prefix == "/prep"

    app = create_app(cfg, calendar_factory=lambda: FakeCalendar())
    client = app.test_client()

    assert client.get("/?token=test-token").status_code == 404      # root not ours
    assert client.get("/prep/?token=test-token").status_code == 200
    assert client.get("/prep/healthz").status_code == 200


def test_no_prefix_still_serves_the_root(project: Path):
    from prep.web import create_app
    cfg = config_module.load(project / "config.yaml")
    assert cfg.web.url_prefix == ""
    client = create_app(cfg, calendar_factory=lambda: FakeCalendar()).test_client()
    assert client.get("/?token=test-token").status_code == 200
