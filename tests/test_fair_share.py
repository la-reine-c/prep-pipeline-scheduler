"""When there is not enough time for everything, prep due together shares it.

Found in a real week: four preps due by Thursday morning, each guessed at two
hours, and about four hours free. Planned one at a time, earliest deadline
first, the first two took every evening and the other two got nothing.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import config as config_module
from prep import pipeline, scheduler
from prep.config import DayWindow, SchedulingConfig
from prep.scheduler import FakeCalendar, Job
from prep.store import Change, Store

TZ = ZoneInfo("Europe/London")
MONDAY = datetime(2026, 10, 19, 12, 0, tzinfo=TZ)
TUESDAY_MORNING = datetime(2026, 10, 20, 8, 30, tzinfo=TZ)


def one_evening() -> SchedulingConfig:
    # Monday 17:00-19:30 is all there is before Tuesday morning.
    return SchedulingConfig(
        block_minutes=30, max_daily_minutes=1440, flow_buffer_minutes=5,
        gap_minutes=10, windows={0: DayWindow(time(17, 0), time(19, 30))})


def test_shares_are_equal_unless_someone_needs_less():
    assert scheduler._fair_shares([4, 4, 4], 6) == [2, 2, 2]
    assert scheduler._fair_shares([4, 4, 4], 7) == [3, 2, 2]   # earliest first
    assert scheduler._fair_shares([1, 4, 4], 7) == [1, 3, 3]
    assert scheduler._fair_shares([2, 2], 9) == [2, 2]


def test_prep_due_together_shares_an_evening():
    jobs = [Job(f"p{n}", f"Prep {n}", "", 120, None,
                TUESDAY_MORNING + timedelta(minutes=n)) for n in range(3)]
    plans = scheduler.plan_fairly(one_evening(), jobs, [], MONDAY, TZ)
    counts = [len(p) for p in plans]
    assert min(counts) >= 1, counts
    assert max(counts) - min(counts) <= 1, counts
    every = sorted((s for p in plans for s in p), key=lambda s: s.start)
    for a, b in zip(every, every[1:]):
        assert b.start >= a.end, "sessions overlap"


def test_plenty_of_time_changes_nothing():
    cfg = one_evening()
    job = Job("p", "Prep", "", 60, None, TUESDAY_MORNING)
    alone = scheduler.plan(cfg, "p", "Prep", "", 60, None, TUESDAY_MORNING,
                           [], MONDAY, TZ)
    [shared] = scheduler.plan_fairly(cfg, [job], [], MONDAY, TZ)
    assert [(s.start, s.end) for s in shared] == [(s.start, s.end) for s in alone]


def test_later_prep_does_not_count_against_prep_due_sooner():
    # Due next week, it has other evenings; prep due tomorrow keeps Monday.
    cfg = one_evening()
    cfg.windows = {d: DayWindow(time(17, 0), time(19, 30)) for d in range(7)}
    soon = Job("soon", "Soon", "", 60, None, TUESDAY_MORNING)
    later = Job("later", "Later", "", 60, None, TUESDAY_MORNING + timedelta(days=6))
    plans = scheduler.plan_fairly(cfg, [later, soon], [], MONDAY, TZ)
    assert sum(s.minutes for s in plans[1]) >= 60


@pytest.fixture
def cfg(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/d
export_path: {tmp_path}/p.txt
timezone: Europe/London
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling:
  max_daily_minutes: 1440
  windows:
    monday: {{start: "17:00", end: "19:30"}}
""")
    return config_module.load(tmp_path / "config.yaml")


def _task(store: Store, title: str, minutes: int) -> Change:
    task_id = store.add_task(title, TUESDAY_MORNING.isoformat(), minutes=minutes)
    row = store.assignment(task_id)
    return Change("new", task_id, row["label"], "", row["due_at"])


def test_new_prep_shares_time_already_booked_for_other_prep(cfg):
    store = Store(cfg.db_path)
    calendar = FakeCalendar()
    first = _task(store, "First", 120)
    pipeline.reschedule(cfg, store, calendar, [first], now=MONDAY)
    assert len(store.sessions_for(first.assignment_id)) >= 3

    # Arrives later, due just as soon: the evening is split between them.
    second = _task(store, "Second", 120)
    pipeline.reschedule(cfg, store, calendar, [second], now=MONDAY)
    a = len(store.sessions_for(first.assignment_id))
    b = len(store.sessions_for(second.assignment_id))
    assert a >= 1 and b >= 1 and abs(a - b) <= 1, (a, b)
    # Nothing left in the calendar that the database has forgotten.
    booked = {r["uid"] for r in store.all_sessions()}
    assert booked == set(calendar.events)
