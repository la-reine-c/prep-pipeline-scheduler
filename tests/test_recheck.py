"""The five-minute recheck re-plans only when something has changed.

Four triggers - finished, missed, timing, unplanned - and the property that
matters as much as any of them: when none has happened, nothing moves and
the calendar is never even opened.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import config as config_module
from prep import pipeline, recheck
from prep.scheduler import FakeCalendar
from prep.store import Change, Store
from prep.web import create_app

TZ = ZoneInfo("Europe/London")
MONDAY = datetime(2026, 10, 12, 0, 0, tzinfo=TZ)


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    return (MONDAY + timedelta(days=day_offset)).replace(hour=hour, minute=minute)


@pytest.fixture
def cfg(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/d
export_path: {tmp_path}/p.txt
learned_map_path: {tmp_path}/teachers.json
timezone: Europe/London
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling:
  block_minutes: 30
  max_daily_minutes: 120
  confirm_grace_hours: 24
  windows:
    monday:    {{start: "17:00", end: "21:30"}}
    tuesday:   {{start: "17:00", end: "21:30"}}
    wednesday: {{start: "17:00", end: "21:30"}}
    thursday:  {{start: "17:00", end: "21:30"}}
    friday:    {{start: "17:00", end: "21:00"}}
    saturday:  {{start: "09:30", end: "18:00"}}
    sunday:    {{start: "09:30", end: "20:00"}}
web:
  token: "t0ken"
""")
    return config_module.load(tmp_path / "config.yaml")


def _book(cfg, calendar, title="Essay", due=None, minutes=90, teacher="Smith"):
    """A task booked from Monday morning. Returns its id."""
    store = Store(cfg.db_path)
    task_id = store.add_task(title, (due or at(4, 17)).isoformat(),
                             minutes=minutes, teacher=teacher)
    row = store.assignment(task_id)
    pipeline.reschedule(cfg, store, calendar,
                        [Change("new", task_id, row["label"], teacher,
                                row["due_at"])], now=MONDAY)
    store.close()
    return task_id


class Counting:
    """A calendar factory that remembers whether it was ever called."""

    def __init__(self, calendar):
        self.calendar = calendar
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.calendar


def _recheck(cfg, calendar, now):
    factory = Counting(calendar)
    store = Store(cfg.db_path)
    try:
        result = recheck.run(cfg, store, factory, now=now)
        sessions = store.all_sessions()
    finally:
        store.close()
    return result, factory, sessions


def _assert_diary_matches_database(cfg, calendar):
    store = Store(cfg.db_path)
    owned = {row["uid"] for row in store.all_sessions()}
    store.close()
    assert set(calendar.events) <= owned, "a booking the database lost track of"


# ----------------------------------------------------------- nothing changed

def test_a_quiet_recheck_never_opens_the_calendar(cfg):
    calendar = FakeCalendar()
    _book(cfg, calendar)
    before = dict(calendar.events)

    result, factory, _ = _recheck(cfg, calendar, at(0, 12))

    assert not result.findings
    assert factory.calls == 0
    assert calendar.events == before


def test_a_session_inside_its_grace_period_counts_as_done(cfg):
    calendar = FakeCalendar()
    _book(cfg, calendar)
    first = min(calendar.events.values(), key=lambda s: s.start)
    before = dict(calendar.events)

    # Ended, not confirmed, but less than 24 hours ago.
    result, factory, sessions = _recheck(cfg, calendar,
                                         first.end + timedelta(hours=23))

    assert not result.findings
    assert factory.calls == 0
    assert calendar.events == before
    assert all((row["status"] or "") == "" for row in sessions)


# ----------------------------------------------------------- missed

def test_an_unconfirmed_session_is_rebooked_after_24_hours(cfg):
    calendar = FakeCalendar()
    task_id = _book(cfg, calendar, minutes=90)
    first = min(calendar.events.values(), key=lambda s: s.start)
    when = first.end + timedelta(hours=24, minutes=5)

    result, factory, sessions = _recheck(cfg, calendar, when)

    assert task_id in result.findings.missed
    assert factory.calls == 1
    by_uid = {row["uid"]: row for row in sessions}
    assert by_uid[_uid_of(calendar, first, sessions)]["status"] == "skipped"

    # The missed half hour is booked again: three blocks still count.
    counted = [row for row in sessions
               if (row["status"] or "") not in ("missed", "skipped")]
    assert len(counted) == 3
    assert all(datetime.fromisoformat(row["start_local"]) >= when
               for row in counted if (row["status"] or "") == ""
               and datetime.fromisoformat(row["end_local"]) > when)
    _assert_diary_matches_database(cfg, calendar)

    # Settled: the next recheck has nothing to do.
    again, factory, _ = _recheck(cfg, calendar, when + timedelta(minutes=5))
    assert not again.findings
    assert factory.calls == 0


def _uid_of(calendar, session, rows):
    for row in rows:
        if datetime.fromisoformat(row["start_local"]) == session.start:
            return row["uid"]
    raise AssertionError("session not in the database")


def _ended_session(cfg, calendar, minutes=60):
    """A task due in six days with one session that finished an hour ago,
    booked in the calendar like any other."""
    from prep.scheduler import PlannedSession

    now = datetime.now(TZ)
    store = Store(cfg.db_path)
    task_id = store.add_task("Essay", (now + timedelta(days=6)).isoformat(),
                             minutes=minutes, teacher="Smith")
    store.record_estimate(task_id, minutes, "corrected")
    start, end = now - timedelta(hours=1, minutes=30), now - timedelta(hours=1)
    uid = calendar.create(PlannedSession(task_id, "Essay", "Smith", start, end))
    store.record_session(uid, task_id, start.isoformat(), end.isoformat(), 30)
    store.close()
    return task_id, uid


def test_pressing_missed_on_the_dashboard_rebooks_at_once(cfg):
    calendar = FakeCalendar()
    task_id, uid = _ended_session(cfg, calendar, minutes=30)

    client = create_app(cfg, calendar_factory=lambda: calendar).test_client()
    response = client.post(f"/session/{uid}/missed", data={"token": "t0ken"})

    assert response.status_code == 302
    store = Store(cfg.db_path)
    rows = store.sessions_for(task_id)
    store.close()
    assert {row["uid"]: row["status"] for row in rows}[uid] == "skipped"
    assert len(rows) == 2, "the missed half hour was not booked again"
    _assert_diary_matches_database(cfg, calendar)


def test_a_session_still_to_come_cannot_be_marked(cfg):
    calendar = FakeCalendar()
    _book(cfg, calendar, due=datetime.now(TZ) + timedelta(days=6))
    store = Store(cfg.db_path)
    future = [row["uid"] for row in store.all_sessions()
              if datetime.fromisoformat(row["start_local"]) > datetime.now(TZ)]
    store.close()

    client = create_app(cfg, calendar_factory=lambda: calendar).test_client()
    response = client.post(f"/session/{future[0]}/missed",
                           data={"token": "t0ken"})

    assert response.status_code == 400


def test_pressing_done_stops_it_ever_counting_as_missed(cfg):
    calendar = FakeCalendar()
    _book(cfg, calendar)
    first = min(calendar.events.values(), key=lambda s: s.start)
    store = Store(cfg.db_path)
    uid = _uid_of(calendar, first, store.all_sessions())
    store.set_session_status(uid, "done")
    store.close()

    result, factory, _ = _recheck(cfg, calendar,
                                  first.end + timedelta(hours=30))

    # Only later sessions can have run out of grace by now; this one is safe.
    assert uid not in [u for uids in result.findings.missed.values()
                       for u in uids]


def test_missed_prep_that_is_already_due_is_settled_without_the_calendar(cfg):
    calendar = FakeCalendar()
    _book(cfg, calendar, due=at(1, 23))
    when = at(3, 12)                       # due date passed, nothing confirmed

    result, factory, sessions = _recheck(cfg, calendar, when)

    assert factory.calls == 0
    assert not result.findings.missed
    assert {row["status"] for row in sessions} == {"skipped"}


def test_a_missed_session_waits_when_the_timetable_is_down(cfg):
    calendar = FakeCalendar()
    _book(cfg, calendar)
    first = min(calendar.events.values(), key=lambda s: s.start)
    when = first.end + timedelta(hours=25)

    def broken():
        raise pipeline.FeedsUnavailable("socs: timed out")

    store = Store(cfg.db_path)
    result = recheck.run(cfg, store, broken, now=when)
    statuses = {row["status"] for row in store.all_sessions()}
    store.close()

    assert result.error
    assert "missed" in statuses, "a failed recheck must leave it to retry"


# ----------------------------------------------------------- finished

def test_logging_a_time_takes_the_rest_of_the_prep_out_of_the_diary(cfg):
    calendar = FakeCalendar()
    task_id = _book(cfg, calendar)
    first = min(calendar.events.values(), key=lambda s: s.start)
    store = Store(cfg.db_path)
    store.record_timing(task_id, 40)
    store.close()

    result, factory, sessions = _recheck(cfg, calendar,
                                         first.end + timedelta(minutes=5))

    assert task_id in result.findings.finished
    assert result.removed >= 1
    left = [row for row in sessions if row["assignment_id"] == task_id]
    assert len(left) == 1 and left[0]["status"] == "done"
    assert len(calendar.events) == 1
    _assert_diary_matches_database(cfg, calendar)


def test_logging_on_the_dashboard_clears_it_at_once(cfg):
    calendar = FakeCalendar()
    task_id = _book(cfg, calendar, due=datetime.now(TZ) + timedelta(days=6))

    client = create_app(cfg, calendar_factory=lambda: calendar).test_client()
    response = client.post("/log", data={"token": "t0ken",
                                         "assignment_id": task_id,
                                         "minutes": "45"})

    assert response.status_code == 302
    now = datetime.now(TZ)
    assert not [s for s in calendar.events.values() if s.start >= now]


# ----------------------------------------------------------- timing

def test_a_corrected_estimate_rebooks_to_the_new_length(cfg):
    calendar = FakeCalendar()
    task_id = _book(cfg, calendar, minutes=90)
    assert len(calendar.events) == 3
    store = Store(cfg.db_path)
    store.set_override(task_id, 30)
    store.close()

    result, _, sessions = _recheck(cfg, calendar, MONDAY + timedelta(hours=1))

    assert result.findings.timing == {task_id: (90, 30)}
    assert len(sessions) == 1 and len(calendar.events) == 1


def test_a_change_inside_one_block_moves_nothing(cfg):
    calendar = FakeCalendar()
    task_id = _book(cfg, calendar, minutes=90)
    before = dict(calendar.events)
    store = Store(cfg.db_path)
    store.set_override(task_id, 85)          # still three blocks
    store.close()

    result, factory, _ = _recheck(cfg, calendar, MONDAY + timedelta(hours=1))

    assert not result.findings
    assert factory.calls == 0
    assert calendar.events == before


# ----------------------------------------------------------- unplanned

def test_prep_saved_while_the_feed_was_down_is_booked(cfg):
    calendar = FakeCalendar()
    store = Store(cfg.db_path)
    task_id = store.add_task("Essay", at(4, 17).isoformat(), minutes=60)
    store.close()

    result, _, sessions = _recheck(cfg, calendar, MONDAY)

    assert result.findings.unplanned == [task_id]
    assert len(sessions) == 2 and len(calendar.events) == 2


def test_prep_that_found_no_slot_is_not_retried_every_five_minutes(cfg):
    calendar = FakeCalendar()
    # Due before any evening window: planned, estimated, nothing fits.
    _book(cfg, calendar, due=at(0, 16))
    assert not calendar.events

    result, factory, _ = _recheck(cfg, calendar, MONDAY + timedelta(hours=1))

    assert not result.findings
    assert factory.calls == 0


# ----------------------------------------------------------- the database

def test_an_old_database_does_not_call_its_whole_history_missed(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE assignments (assignment_id TEXT PRIMARY KEY, title TEXT,
            class_ref TEXT, teacher TEXT, label TEXT, assigned_at TEXT,
            due_at TEXT, id_synthetic INTEGER, first_seen TEXT, last_seen TEXT,
            estimate_minutes INTEGER, estimate_source TEXT);
        CREATE TABLE sessions (uid TEXT PRIMARY KEY, assignment_id TEXT,
            start_local TEXT, end_local TEXT, minutes INTEGER, created_at TEXT,
            provisional INTEGER DEFAULT 0);
    """)
    conn.execute("INSERT INTO assignments VALUES ('a', 'Essay', '', 'S', "
                 "'Essay', '2026-01-01', '2026-01-09', 0, 'x', 'x', 60, 'p')")
    conn.execute("INSERT INTO sessions VALUES ('old', 'a', "
                 "'2026-01-05T17:00:00+00:00', '2026-01-05T17:30:00+00:00', "
                 "30, 'x', 0)")
    conn.execute("INSERT INTO sessions VALUES ('future', 'a', "
                 "'2099-01-05T17:00:00+00:00', '2099-01-05T17:30:00+00:00', "
                 "30, 'x', 0)")
    conn.commit()
    conn.close()

    store = Store(path)
    status = {row["uid"]: row["status"] for row in
              store.conn.execute("SELECT uid, status FROM sessions")}
    store.close()
    assert status == {"old": "done", "future": ""}


def test_pretend_bookings_from_old_dry_runs_are_cleared(tmp_path):
    store = Store(tmp_path / "p.db")
    task_id = store.add_task("Essay", "2099-01-09T17:00:00+00:00", minutes=60)
    store.record_estimate(task_id, 60, "corrected")
    store.record_session("dry-run-1", task_id, "2099-01-05T17:00:00+00:00",
                         "2099-01-05T17:30:00+00:00", 30)
    store.close()

    store = Store(tmp_path / "p.db")
    try:
        assert not store.all_sessions()
        assert [row["assignment_id"] for row in
                store.unplanned(datetime.now(TZ))] == [task_id]
    finally:
        store.close()


def test_a_dry_run_leaves_the_database_exactly_as_it_was(cfg):
    calendar = FakeCalendar()
    _book(cfg, calendar)
    store = Store(cfg.db_path)
    before = [tuple(row) for row in store.conn.execute(
        "SELECT * FROM sessions ORDER BY uid")]
    scratch = store.scratch_copy()
    store.close()

    scratch.add_task("Another", at(5, 17).isoformat(), minutes=30)
    scratch.conn.execute("DELETE FROM sessions")
    scratch.close()

    store = Store(cfg.db_path)
    after = [tuple(row) for row in store.conn.execute(
        "SELECT * FROM sessions ORDER BY uid")]
    tasks = store.conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
    store.close()
    assert after == before and tasks == 1


# ----------------------------------------------------------- the dashboard

def test_the_dashboard_asks_about_sessions_that_have_ended(cfg):
    calendar = FakeCalendar()
    now = datetime.now(TZ)
    store = Store(cfg.db_path)
    task_id = store.add_task("Essay", (now + timedelta(days=3)).isoformat(),
                             minutes=30)
    store.record_session("s1", task_id,
                         (now - timedelta(hours=2)).isoformat(),
                         (now - timedelta(hours=1, minutes=30)).isoformat(), 30)
    store.close()

    page = (create_app(cfg, calendar_factory=lambda: calendar).test_client()
            .get("/?token=t0ken").get_data(as_text=True))

    assert "Did you do these?" in page
    assert "/session/s1/done" in page and "/session/s1/missed" in page


def test_a_dry_run_then_a_real_run_still_books_the_prep(cfg, tmp_path,
                                                         monkeypatch, capsys):
    """The order the deploy guide gives: check dry, then go live. The dry run
    used to record the prep as known, so the real run found nothing new and
    booked none of it."""
    import json
    from prep import run

    now = datetime.now(TZ)
    payload = tmp_path / "payloads.json"
    payload.write_text(json.dumps([{"url": "assignments", "body": {"value": [
        {"id": "a" * 36, "classId": "c", "displayName": "Essay",
         "assignedDateTime": now.astimezone(ZoneInfo("UTC")).isoformat(),
         "dueDateTime": (now + timedelta(days=5)).astimezone(ZoneInfo("UTC"))
                        .isoformat()}]}}]))
    calendar = FakeCalendar()
    monkeypatch.setattr(run, "CalDAVCalendar", lambda cfg, tz: calendar)
    monkeypatch.setattr(run.config_module, "load", lambda path: cfg)
    args = ["--config", "x", "--payload-file", str(payload)]

    assert run.main(args + ["--dry-run"]) == 0
    assert not calendar.events
    store = Store(cfg.db_path)
    assert not store.conn.execute("SELECT * FROM assignments").fetchall()
    store.close()

    assert run.main(args) == 0
    assert calendar.events, "the real run booked nothing after a dry run"
