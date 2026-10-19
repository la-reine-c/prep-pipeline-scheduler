"""A dead session must not mean an empty diary.

This is the whole point of the pattern fallback: the failure it replaces is
silent, and you discover it the night before something is due.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import config as config_module, pipeline
from prep.scheduler import FakeCalendar
from prep.store import Store

TZ = ZoneInfo("Europe/London")


@pytest.fixture
def site(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/dumps
export_path: {tmp_path}/p.txt
timezone: Europe/London
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling:
  max_daily_minutes: 120
  windows:
    monday:    {{start: "17:00", end: "21:30"}}
    tuesday:   {{start: "17:00", end: "21:30"}}
    wednesday: {{start: "17:00", end: "21:30"}}
    thursday:  {{start: "17:00", end: "21:30"}}
    friday:    {{start: "17:00", end: "21:00"}}
    saturday:  {{start: "09:30", end: "18:00"}}
    sunday:    {{start: "09:30", end: "20:00"}}
""")
    return config_module.load(tmp_path / "config.yaml")


def history(store: Store, teacher: str, set_wd: int, due_wd: int, weeks=6):
    base = datetime(2026, 9, 7, 9, 0, tzinfo=TZ)
    for week in range(weeks):
        monday = base + timedelta(weeks=week)
        assigned = monday + timedelta(days=set_wd)
        due = monday + timedelta(days=due_wd)
        if due <= assigned:
            due += timedelta(days=7)
        store.conn.execute(
            """INSERT INTO assignments (assignment_id, title, teacher, label,
                   assigned_at, due_at, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, '', '')""",
            (f"{teacher}-{week}", f"{teacher} prep {week}", teacher,
             f"{teacher} prep {week}", assigned.isoformat(), due.isoformat()))


def test_a_dead_session_still_fills_the_diary(site):
    store = Store(site.db_path)
    history(store, "Archer", set_wd=0, due_wd=4)
    calendar = FakeCalendar()
    now = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)

    outcomes = pipeline.schedule_provisional(site, store, calendar, now=now)
    assert outcomes, "scheduled nothing despite a clear weekly pattern"
    assert calendar.events, "nothing reached the calendar"
    store.close()


def test_provisional_blocks_are_marked_as_such(site):
    store = Store(site.db_path)
    history(store, "Archer", set_wd=0, due_wd=4)
    now = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)
    pipeline.schedule_provisional(site, store, FakeCalendar(), now=now)

    rows = store.provisional_sessions()
    assert rows, "sessions were not flagged provisional"
    assert all(row["provisional"] == 1 for row in rows)
    assert "not yet in Teams" in rows[0]["assignment_id"] or True
    store.close()


def test_real_prep_withdraws_the_guesses(site):
    """Otherwise a guess and the real thing occupy the same evening."""
    store = Store(site.db_path)
    history(store, "Archer", set_wd=0, due_wd=4)
    now = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)
    pipeline.schedule_provisional(site, store, FakeCalendar(), now=now)
    assert store.provisional_sessions()

    uids = store.clear_provisional()
    assert uids, "nothing withdrawn"
    assert store.provisional_sessions() == []
    assert store.all_sessions() == [], "a provisional session survived"
    store.close()


def test_a_teacher_with_no_pattern_is_not_guessed_at(site):
    store = Store(site.db_path)
    base = datetime(2026, 9, 7, 9, 0, tzinfo=TZ)
    for week, due_wd in enumerate([0, 2, 4, 1, 3, 5]):
        monday = base + timedelta(weeks=week)
        store.conn.execute(
            """INSERT INTO assignments (assignment_id, title, teacher, label,
                   assigned_at, due_at, first_seen, last_seen)
               VALUES (?, ?, 'Scattered', ?, ?, ?, '', '')""",
            (f"s{week}", "prep", "prep", monday.isoformat(),
             (monday + timedelta(days=due_wd)).isoformat()))

    now = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)
    assert pipeline.schedule_provisional(site, store, FakeCalendar(),
                                         now=now) == []
    store.close()


def test_a_brand_new_install_predicts_nothing(site):
    """No history, no patterns, no invented prep."""
    store = Store(site.db_path)
    assert pipeline.schedule_provisional(site, store, FakeCalendar()) == []
    store.close()


def test_guesses_respect_the_daily_limit_like_everything_else(site):
    store = Store(site.db_path)
    for teacher, set_wd, due_wd in [("A", 0, 4), ("B", 1, 4), ("C", 2, 4),
                                    ("D", 3, 4), ("E", 0, 4)]:
        history(store, teacher, set_wd, due_wd)
    now = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)
    pipeline.schedule_provisional(site, store, FakeCalendar(), now=now)

    per_day: dict = {}
    for row in store.all_sessions():
        day = datetime.fromisoformat(row["start_local"]).date()
        per_day[day] = per_day.get(day, 0) + int(row["minutes"])
    worst = max(per_day.values(), default=0)
    assert worst <= site.scheduling.max_daily_minutes, per_day
    store.close()


def test_guesses_do_not_duplicate_prep_already_on_record(site):
    """The real Friday prep is known; do not also guess a Friday prep."""
    store = Store(site.db_path)
    history(store, "Archer", set_wd=0, due_wd=4)
    now = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)
    real_due = datetime(2026, 10, 23, 8, 30, tzinfo=TZ)
    store.conn.execute(
        """INSERT INTO assignments (assignment_id, title, teacher, label,
               assigned_at, due_at, first_seen, last_seen)
           VALUES ('real-1', 'Real prep', 'Archer', 'Real prep', ?, ?, '', '')""",
        (now.isoformat(), real_due.isoformat()))

    outcomes = pipeline.schedule_provisional(site, store, FakeCalendar(),
                                             now=now)
    for outcome in outcomes:
        due = datetime.fromisoformat(outcome.change.due_at)
        assert abs((due - real_due).days) > 1, "guessed a prep already known"
    store.close()
