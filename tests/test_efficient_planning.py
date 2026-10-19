"""Packing prep efficiently, and moving as little as possible to do it.

Four things, each found in a real week's plan:

  - Back-to-back blocks of one prep were separate sittings with a break and
    a flow buffer between them, so an evening fitted three blocks, not four.
  - A prep spread one block a day could strand blocks when other prep had
    already filled some of those days, though the rest had room.
  - A prep due tomorrow could find tonight taken by one due next week.
  - Any change on the dashboard re-planned the whole fortnight, moving
    bookings it had nothing to do with.
"""
from __future__ import annotations

import time as clock
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import config as config_module
from prep import pipeline, scheduler
from prep.config import DayWindow, SchedulingConfig
from prep.scheduler import FakeCalendar, Interval
from prep.store import Change, Store
from prep.web import create_app

TZ = ZoneInfo("Europe/London")
MONDAY = datetime(2026, 10, 19, 16, 0, tzinfo=TZ)


def sched(**kw) -> SchedulingConfig:
    base = SchedulingConfig(
        block_minutes=30, flow_buffer_minutes=5, gap_minutes=10,
        max_daily_minutes=120, max_sitting_minutes=60, due_time=time(8, 30),
        windows={d: DayWindow(time(17, 0), time(21, 30)) for d in range(7)})
    for key, value in kw.items():
        setattr(base, key, value)
    return base


# ------------------------------------------------------------ the scheduler

def test_back_to_back_blocks_are_one_sitting():
    cfg = sched()
    due = MONDAY.replace(hour=0) + timedelta(days=1, hours=8, minutes=30)
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 60, None, due, [],
                              MONDAY, TZ)
    assert [(s.start.time(), s.minutes) for s in sessions] == [(time(17, 0), 60)]


def test_a_full_evening_holds_four_blocks_not_three():
    """17:00-19:25 is 145 minutes. As separate sittings 30 + 10 + 35 + 10 +
    35 leaves too little for a fourth; as sittings of up to an hour it fits.
    (The daily limit is raised so it is the packing being tested.)"""
    cfg = sched(windows={0: DayWindow(time(17, 0), time(19, 25))},
                max_daily_minutes=150)
    due = datetime(2026, 10, 20, 8, 30, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 120, None, due, [],
                              MONDAY, TZ)
    worked = sum(scheduler.work_minutes(cfg, s.minutes) for s in sessions)
    assert worked == 120
    assert all(s.start.date() == MONDAY.date() for s in sessions)


def test_sittings_are_capped_and_never_overlap():
    cfg = sched(max_sitting_minutes=60, max_daily_minutes=240)
    due = datetime(2026, 10, 20, 8, 30, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 120, None, due, [],
                              MONDAY, TZ)
    assert all(s.work <= 60 for s in sessions)
    for earlier, later in zip(sessions, sessions[1:]):
        assert later.start >= earlier.end + timedelta(minutes=cfg.prep_gap_minutes)


def test_one_block_sittings_are_still_possible():
    cfg = sched(max_sitting_minutes=30)
    due = datetime(2026, 10, 20, 8, 30, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 60, None, due, [],
                              MONDAY, TZ)
    assert [s.minutes for s in sessions] == [30, 35]


def test_room_left_on_some_days_is_used_when_others_are_full():
    """Four blocks over four evenings is one a day - unless two of those
    evenings are already at the daily limit, when the other two take two."""
    cfg = sched()
    due = datetime(2026, 10, 23, 8, 30, tzinfo=TZ)            # Friday
    load = {date(2026, 10, 19): 120, date(2026, 10, 20): 120}
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 120, None, due, [],
                              MONDAY, TZ, daily_load=load)
    worked = sum(scheduler.work_minutes(cfg, s.minutes) for s in sessions)
    assert worked == 120
    assert {s.start.date() for s in sessions} == {date(2026, 10, 21),
                                                  date(2026, 10, 22)}


def test_the_daily_limit_counts_work_not_buffers():
    """Two one-hour sittings are 120 minutes of work, though the second
    carries a flow buffer: the limit is not tripped by the buffer."""
    cfg = sched()
    due = datetime(2026, 10, 20, 8, 30, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 120, None, due, [],
                              MONDAY, TZ)
    assert sum(s.work for s in sessions) == 120
    assert sum(s.minutes for s in sessions) == 125
    assert all(s.start.date() == MONDAY.date() for s in sessions)


def test_the_daily_limit_still_holds_across_prep():
    cfg = sched()
    due = datetime(2026, 10, 20, 8, 30, tzinfo=TZ)
    load = {date(2026, 10, 19): 90}
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 60, None, due, [],
                              MONDAY, TZ, daily_load=load)
    assert sum(s.work for s in sessions) == 30
    assert load[date(2026, 10, 19)] == 120


def test_prep_sittings_sit_closer_than_diary_events():
    """Another prep's sitting gets a short break after it; a lesson gets
    the full breathing room."""
    cfg = sched(prep_gap_minutes=5, gap_minutes=10)
    due = datetime(2026, 10, 20, 8, 30, tzinfo=TZ)
    other = [Interval(MONDAY.replace(hour=17), MONDAY.replace(hour=18))]
    after_prep = scheduler.plan(cfg, "a", "Essay", "T", 30, None, due, [],
                                MONDAY, TZ, prep_busy=other)
    after_lesson = scheduler.plan(cfg, "a", "Essay", "T", 30, None, due, other,
                                  MONDAY, TZ)
    assert after_prep[0].start.time() == time(18, 5)
    assert after_lesson[0].start.time() == time(18, 10)


def test_an_estimate_is_booked_to_the_minute_not_to_the_block():
    cfg = sched()
    due = datetime(2026, 10, 20, 8, 30, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 79, None, due, [],
                              MONDAY, TZ)
    assert sum(s.work for s in sessions) == 79


def test_a_tiny_remainder_is_rounded_up_to_the_shortest_sitting():
    cfg = sched(min_block_minutes=15)
    assert scheduler.target_minutes(cfg, 65) == 75
    assert scheduler.target_minutes(cfg, 79) == 79
    assert scheduler.target_minutes(cfg, 120) == 120


def test_the_end_of_an_evening_is_used():
    """20 minutes left before the window closes: worth a short sitting,
    where a whole block would not fit."""
    cfg = sched(windows={0: DayWindow(time(17, 0), time(17, 20))},
                max_sitting_minutes=30)
    due = datetime(2026, 10, 20, 8, 30, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 60, None, due, [],
                              MONDAY, TZ)
    assert [(s.start.time(), s.work) for s in sessions] == [(time(17, 0), 20)]

    # Taking all 20 of a 30-minute prep would strand 10 minutes, too short
    # to sit down for: it takes 15 and leaves 15.
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 30, None, due, [],
                              MONDAY, TZ)
    assert [s.work for s in sessions] == [15]


def test_no_sitting_is_left_shorter_than_the_minimum():
    cfg = sched(min_block_minutes=15)
    due = datetime(2026, 10, 23, 8, 30, tzinfo=TZ)
    for minutes in (30, 45, 61, 79, 95, 120):
        sessions = scheduler.plan(cfg, "a", "Essay", "T", minutes, None, due,
                                  [], MONDAY, TZ)
        assert all(s.work >= 15 for s in sessions), (minutes, sessions)
        assert sum(s.work for s in sessions) == scheduler.target_minutes(cfg, minutes)


def test_a_plan_made_at_an_odd_minute_starts_on_a_round_one():
    cfg = sched()
    odd = MONDAY.replace(hour=19, minute=16, second=37)
    due = datetime(2026, 10, 23, 8, 30, tzinfo=TZ)
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 90, None, due, [], odd, TZ)
    assert sessions[0].start == MONDAY.replace(hour=19, minute=20)
    assert all(s.start.minute % 5 == 0 and s.start.second == 0 for s in sessions)
    assert all(s.work % 15 == 0 for s in sessions)


def test_older_bookings_without_a_recorded_buffer_still_count():
    cfg = sched()
    row = {"minutes": 35, "buffer_minutes": None}

    class Row(dict):
        def keys(self):
            return super().keys()
    assert scheduler.row_work(cfg, Row(row)) == 30
    assert scheduler.row_work(cfg, Row(minutes=25, buffer_minutes=5)) == 20


# ------------------------------------------------------------ the pipeline

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
  windows:
    monday:    {{start: "17:00", end: "21:30"}}
    tuesday:   {{start: "17:00", end: "21:30"}}
    wednesday: {{start: "17:00", end: "21:30"}}
    thursday:  {{start: "17:00", end: "21:30"}}
    friday:    {{start: "17:00", end: "21:30"}}
    saturday:  {{start: "17:00", end: "21:30"}}
    sunday:    {{start: "17:00", end: "21:30"}}
web:
  token: "t0ken"
""")
    return config_module.load(tmp_path / "config.yaml")


def _task(store: Store, title: str, due: datetime, minutes: int) -> Change:
    task_id = store.add_task(title, due.isoformat(), minutes=minutes)
    row = store.assignment(task_id)
    return Change("new", task_id, row["label"], "", row["due_at"])


def _booked(store: Store, change: Change) -> int:
    return sum(int(r["minutes"]) for r in store.sessions_for(change.assignment_id))


def test_an_urgent_prep_takes_the_evening_from_one_due_later(cfg):
    store = Store(cfg.db_path)
    calendar = FakeCalendar()
    later = _task(store, "Due Friday", datetime(2026, 10, 23, 8, 30, tzinfo=TZ), 120)
    pipeline.reschedule(cfg, store, calendar, [later], now=MONDAY)
    tonight = [r for r in store.sessions_for(later.assignment_id)
               if r["start_local"].startswith("2026-10-19")]
    assert tonight, "the setup needs the later prep to have taken Monday"

    # Monday is the only evening before this one is due, and the daily
    # limit leaves room for it only if the later prep gives Monday up.
    urgent = _task(store, "Due Tuesday", datetime(2026, 10, 20, 8, 30, tzinfo=TZ), 120)
    outcomes = pipeline.reschedule(cfg, store, calendar, [urgent], now=MONDAY)

    assert _booked(store, urgent) >= 120
    assert _booked(store, later) >= 120, "the prep that moved was not booked again"
    assert all(not r["start_local"].startswith("2026-10-19")
               for r in store.sessions_for(later.assignment_id))
    assert {o.change.assignment_id for o in outcomes} == {urgent.assignment_id,
                                                          later.assignment_id}
    uids = {r["uid"] for r in store.all_sessions()}
    assert uids == set(calendar.events)
    store.close()


def test_nothing_is_moved_when_moving_it_would_not_help(cfg):
    store = Store(cfg.db_path)
    calendar = FakeCalendar()
    later = _task(store, "Due Friday", datetime(2026, 10, 23, 8, 30, tzinfo=TZ), 60)
    pipeline.reschedule(cfg, store, calendar, [later], now=MONDAY)
    before = {r["uid"] for r in store.sessions_for(later.assignment_id)}

    # Already past the only evening it could use: nothing to gain.
    hopeless = _task(store, "Due now", MONDAY + timedelta(minutes=30), 60)
    pipeline.reschedule(cfg, store, calendar, [hopeless], now=MONDAY)

    assert {r["uid"] for r in store.sessions_for(later.assignment_id)} == before
    store.close()


def test_changes_are_placed_soonest_deadline_first(cfg):
    store = Store(cfg.db_path)
    calendar = FakeCalendar()
    later = _task(store, "Due Friday", datetime(2026, 10, 23, 8, 30, tzinfo=TZ), 120)
    soon = _task(store, "Due Tuesday", datetime(2026, 10, 20, 8, 30, tzinfo=TZ), 90)
    outcomes = pipeline.reschedule(cfg, store, calendar, [later, soon], now=MONDAY)
    assert _booked(store, soon) >= 90
    assert [o.change.assignment_id for o in outcomes] == [
        soon.assignment_id, later.assignment_id], "placed in the wrong order"
    store.close()


def _three_preps(cfg):
    store = Store(cfg.db_path)
    calendar = FakeCalendar()
    changes = [_task(store, f"Prep {n}",
                     datetime(2026, 10, 22 + n, 8, 30, tzinfo=TZ), 60)
               for n in range(3)]
    pipeline.reschedule(cfg, store, calendar, changes, now=MONDAY)
    return store, calendar, changes


def test_a_change_that_touches_no_prep_moves_nothing(cfg):
    store, calendar, _ = _three_preps(cfg)
    before = {r["uid"]: r["start_local"] for r in store.all_sessions()}
    used = {datetime.fromisoformat(s).date() for s in before.values()}
    free_day = next(d for d in (date(2026, 10, 19) + timedelta(days=n)
                                for n in range(7)) if d not in used)
    store.add_blackout("17:00", "21:30", on_date=free_day.isoformat())

    outcomes = pipeline.replan_affected(cfg, store, calendar, now=MONDAY)

    assert not [o for o in outcomes if o.sessions]
    assert {r["uid"]: r["start_local"] for r in store.all_sessions()} == before
    store.close()


def test_a_change_moves_only_the_prep_it_clashes_with(cfg):
    store, calendar, changes = _three_preps(cfg)
    before = {r["uid"]: (r["assignment_id"], r["start_local"])
              for r in store.all_sessions()}
    hit_uid, (hit_prep, hit_start) = next(iter(before.items()))
    store.add_blackout("17:00", "21:30", on_date=hit_start[:10])

    outcomes = pipeline.replan_affected(cfg, store, calendar, now=MONDAY)

    moved = {o.change.assignment_id for o in outcomes if o.sessions}
    assert hit_prep in moved
    for uid, (aid, start) in before.items():
        if aid not in moved:
            assert uid in calendar.events, "an untouched booking was moved"
    assert all(not r["start_local"].startswith(hit_start[:10])
               for r in store.all_sessions())
    assert {r["uid"] for r in store.all_sessions()} == set(calendar.events)
    store.close()


def test_prep_short_of_time_is_topped_up_without_moving_what_it_has(cfg):
    store = Store(cfg.db_path)
    calendar = FakeCalendar()
    # Only Monday before it is due, and most of Monday is blocked.
    store.add_blackout("18:00", "21:30", on_date="2026-10-19")
    prep = _task(store, "Due Tuesday", datetime(2026, 10, 20, 8, 30, tzinfo=TZ), 120)
    pipeline.reschedule(cfg, store, calendar, [prep], now=MONDAY)
    kept = {r["uid"] for r in store.sessions_for(prep.assignment_id)}
    assert 0 < _booked(store, prep) < 120

    blackout_id = store.blackouts()[0]["id"]
    store.delete_blackout(blackout_id)
    outcomes = pipeline.replan_affected(cfg, store, calendar, now=MONDAY)

    assert kept <= {r["uid"] for r in store.sessions_for(prep.assignment_id)}
    worked = sum(scheduler.work_minutes(cfg.scheduling, int(r["minutes"]))
                 for r in store.sessions_for(prep.assignment_id))
    assert worked > 30
    assert [o.change.kind for o in outcomes if o.sessions] == ["topup"]
    store.close()


# ------------------------------------------------------------ the dashboard

def _wait(planner, seconds: float = 10.0) -> dict:
    give_up = clock.time() + seconds
    while clock.time() < give_up:
        state = planner.status()
        if not state["running"]:
            return state
        clock.sleep(0.05)
    raise AssertionError("the background plan never finished")


def test_the_dashboard_answers_before_the_plan_is_done(cfg):
    store, calendar, _ = _three_preps(cfg)
    store.close()
    release = __import__("threading").Event()

    def slow_calendar():
        release.wait(10)
        return calendar

    app = create_app(cfg, calendar_factory=slow_calendar, background=True)
    client = app.test_client()
    started = clock.time()
    response = client.post("/blackout", data={
        "weekday": "0", "start_time": "00:00", "end_time": "23:59",
        "token": "t0ken"})
    assert clock.time() - started < 2, "the page waited for the plan"
    assert response.status_code == 302
    assert "background" in response.headers["Location"]

    page = client.get("/?token=t0ken").get_data(as_text=True)
    assert "/planning" in page, "the page does not watch the plan"
    assert client.get("/planning?token=t0ken").get_json()["running"]

    release.set()
    state = _wait(app.config["PLANNER"])
    assert state["last"]["notice"].startswith("Rescheduled")
    store = Store(cfg.db_path)
    assert all(datetime.fromisoformat(r["start_local"]).weekday() != 0
               for r in store.all_sessions())
    assert {r["uid"] for r in store.all_sessions()} == set(calendar.events)
    store.close()


def test_a_task_is_scheduled_in_the_background(cfg):
    calendar = FakeCalendar()
    app = create_app(cfg, calendar_factory=lambda: calendar, background=True)
    client = app.test_client()
    due = (datetime.now(TZ) + timedelta(days=3)).date().isoformat()
    response = client.post("/task", data={"title": "Read chapter 4",
                                          "due_date": due, "minutes": "30",
                                          "token": "t0ken"})
    assert response.status_code == 302
    state = _wait(app.config["PLANNER"])
    assert "Read chapter 4" in state["last"]["notice"]
    assert len(calendar.events) == 1


def test_a_failed_background_plan_says_so_and_the_next_one_runs(cfg):
    def broken():
        raise RuntimeError("iCloud said no")

    app = create_app(cfg, calendar_factory=broken, background=True)
    client = app.test_client()
    client.post("/blackout", data={"weekday": "0", "start_time": "17:00",
                                   "end_time": "19:00", "token": "t0ken"})
    state = _wait(app.config["PLANNER"])
    assert "nothing was moved" in state["last"]["notice"]

    client.post("/blackout", data={"weekday": "1", "start_time": "17:00",
                                   "end_time": "19:00", "token": "t0ken"})
    assert _wait(app.config["PLANNER"])["last"]["notice"]


def test_the_planning_status_needs_the_token(cfg):
    app = create_app(cfg, calendar_factory=FakeCalendar, background=True)
    assert app.test_client().get("/planning").status_code == 403


# ------------------------------------------------------------ the hourly run

def test_the_hourly_run_tops_up_prep_and_stays_quiet_when_it_cannot(
        cfg, tmp_path, monkeypatch, capsys):
    """The hour may have freed time for prep that is short: the run tops it
    up. When it frees none, the run says nothing - the shortfall is already
    on the dashboard, and an alert every hour would soon be ignored."""
    import json
    from prep import notify, run

    now = datetime.now(TZ)
    store = Store(cfg.db_path)
    prep = _task(store, "Short", now + timedelta(days=2), 120)
    store.record_estimate(prep.assignment_id, 120, "corrected")
    store.close()

    payload = tmp_path / "payloads.json"
    payload.write_text(json.dumps([{"url": "assignments",
                                    "body": {"value": []}}]))
    # Every evening blocked: nothing can be booked yet.
    blocked = FakeCalendar(busy_intervals=[
        Interval(now - timedelta(days=1), now + timedelta(days=30))])
    calendar = blocked
    monkeypatch.setattr(run, "CalDAVCalendar", lambda cfg, tz: calendar)
    monkeypatch.setattr(run.config_module, "load", lambda path: cfg)
    pushed = []
    monkeypatch.setattr(notify, "push", lambda *a, **k: pushed.append(a))
    args = ["--config", "x", "--payload-file", str(payload)]

    assert run.main(args) == 0
    assert "no changes" in capsys.readouterr().out
    assert not pushed, "a shortfall nothing could fix was alerted"

    # The evenings come free: the next run books it.
    calendar = FakeCalendar()
    assert run.main(args) == 0
    assert "TOP UP" in capsys.readouterr().out
    store = Store(cfg.db_path)
    assert store.sessions_for(prep.assignment_id)
    store.close()
