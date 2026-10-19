"""Your own tasks, added on the dashboard, and the offline demo that shows them."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import config as config_module
from prep import pipeline
from prep.scheduler import FakeCalendar, Interval
from prep.scraper import load_payload_file, parse
from prep.store import Store
from prep.web import create_app

TZ = ZoneInfo("Europe/London")
TEAMS_ID = "1" * 36


@pytest.fixture
def site(tmp_path: Path):
    (tmp_path / "payloads.json").write_text(json.dumps([{
        "url": "assignments", "body": {"value": [
            {"id": TEAMS_ID, "classId": "phys", "displayName": "Kinematics sheet",
             "assignedDateTime": "2026-09-21T09:00:00Z",
             "dueDateTime": "2026-09-25T08:30:00Z"},
        ]}}]))
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/d
export_path: {tmp_path}/p.txt
learned_map_path: {tmp_path}/teachers.json
timezone: Europe/London
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling:
  skip_past_due: false
  windows:
    monday:    {{start: "17:00", end: "21:30"}}
    tuesday:   {{start: "17:00", end: "21:30"}}
    wednesday: {{start: "17:00", end: "21:30"}}
    thursday:  {{start: "17:00", end: "21:30"}}
    friday:    {{start: "17:00", end: "21:00"}}
    saturday:  {{start: "09:30", end: "18:00"}}
    sunday:    {{start: "09:30", end: "20:00"}}
  teacher_keywords: {{Dalton: ["kinematics"]}}
web:
  token: "t0ken"
""")
    cfg = config_module.load(tmp_path / "config.yaml")
    store = Store(cfg.db_path)
    store.upsert(parse(load_payload_file(tmp_path / "payloads.json")),
                 pipeline.teacher_matcher(cfg))
    store.close()
    return cfg


def future(days: int) -> str:
    return (datetime.now(TZ) + timedelta(days=days)).date().isoformat()


def app_with(cfg, calendar):
    return create_app(cfg, calendar_factory=lambda: calendar).test_client()


def add(client, **fields):
    data = {"title": "EPQ reading", "due_date": future(6), "token": "t0ken"}
    data.update(fields)
    return client.post("/task", data=data)


def only_task(cfg):
    store = Store(cfg.db_path)
    try:
        rows = [r for r in store.upcoming() if r["manual"]]
        assert len(rows) == 1
        return dict(rows[0])
    finally:
        store.close()


# ---------------------------------------------------------------- adding

def test_a_task_is_saved_and_booked_straight_away(site):
    calendar = FakeCalendar()
    response = add(app_with(site, calendar), minutes="90")
    assert response.status_code == 302
    assert "notice" not in response.headers["Location"]

    task = only_task(site)
    assert task["title"] == "EPQ reading"
    assert task["assignment_id"].startswith("task-")
    # The minutes you typed are what gets booked, like a correction.
    assert task["override_minutes"] == 90
    assert task["scheduled_minutes"] >= 90
    assert calendar.events


def test_a_task_is_booked_around_the_diary(site):
    # Every evening for the next fortnight is taken except Saturdays.
    busy = []
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(15):
        day = start + timedelta(days=offset)
        if day.weekday() != 5:
            busy.append(Interval(day, day + timedelta(days=1)))
    calendar = FakeCalendar(busy)
    add(app_with(site, calendar), minutes="60", due_date=future(14))
    assert calendar.events
    assert all(s.start.weekday() == 5 for s in calendar.events.values())


def test_without_minutes_the_task_is_estimated(site):
    add(app_with(site, FakeCalendar()))
    task = only_task(site)
    assert task["override_minutes"] is None
    assert task["estimate_minutes"] and task["estimate_source"]


def test_start_date_holds_the_task_back(site):
    calendar = FakeCalendar()
    add(app_with(site, calendar), start_date=future(3), due_date=future(8),
        minutes="30")
    first = min(s.start for s in calendar.events.values())
    assert first.date().isoformat() >= future(3)


def test_the_task_shows_on_the_dashboard_and_not_as_needing_a_teacher(site):
    client = app_with(site, FakeCalendar())
    add(client)
    body = client.get("/?token=t0ken").data.decode()
    assert "EPQ reading" in body
    assert "your task" in body
    assert "Add a task" in body
    # Having no teacher is deliberate for your own task, not a gap to fix.
    assert "Needs a teacher" not in body


@pytest.mark.parametrize("fields", [
    {"title": "   "},
    {"due_date": ""},
    {"due_date": "not-a-date"},
    {"minutes": "0"},
    {"minutes": "lots"},
    {"start_date": future(9), "due_date": future(4)},
])
def test_nonsense_is_refused_and_nothing_is_saved(site, fields):
    response = add(app_with(site, FakeCalendar()), **fields)
    assert response.status_code == 400
    store = Store(site.db_path)
    assert not [r for r in store.upcoming() if r["manual"]]
    store.close()


def test_adding_a_task_needs_the_token(site):
    client = app_with(site, FakeCalendar())
    response = client.post("/task", data={"title": "x", "due_date": future(3)})
    assert response.status_code == 403


# ---------------------------------------------------------------- failure

class Unreachable(FakeCalendar):
    def busy(self, start, end):
        raise ConnectionError("caldav.icloud.com did not answer")

    def delete_by_uid(self, uids):
        raise ConnectionError("caldav.icloud.com did not answer")


def test_a_task_added_while_icloud_is_down_is_kept_for_the_next_run(site):
    response = add(app_with(site, Unreachable()))
    assert response.status_code == 302
    assert "notice=" in response.headers["Location"]

    store = Store(site.db_path)
    pending = store.unplanned_tasks()
    assert [r["title"] for r in pending] == ["EPQ reading"]

    # The next run treats it as new work and places it.
    from prep.store import Change
    row = pending[0]
    outcomes = pipeline.reschedule(
        site, store, FakeCalendar(),
        [Change("new", row["assignment_id"], row["label"], "", row["due_at"])])
    assert outcomes[0].sessions
    assert store.unplanned_tasks() == []
    store.close()


def test_a_task_that_found_no_slot_is_not_retried_every_run(site):
    start = datetime.now(TZ) - timedelta(days=1)
    calendar = FakeCalendar([Interval(start, start + timedelta(days=30))])
    response = add(app_with(site, calendar))
    assert "notice=" in response.headers["Location"]
    store = Store(site.db_path)
    assert store.unplanned_tasks() == []     # or every run would re-report it
    store.close()


def test_the_notice_is_shown(site):
    client = app_with(site, Unreachable())
    location = add(client).headers["Location"]
    body = client.get(location + "&token=t0ken").data.decode()
    assert "not scheduled yet" in body


# ---------------------------------------------------------------- removing

def test_removing_a_task_takes_its_blocks_out_of_the_calendar(site):
    calendar = FakeCalendar()
    client = app_with(site, calendar)
    add(client, minutes="90")
    task_id = only_task(site)["assignment_id"]
    assert calendar.events

    response = client.post(f"/task/{task_id}/delete", data={"token": "t0ken"})
    assert response.status_code == 302
    assert calendar.events == {}
    store = Store(site.db_path)
    assert store.assignment(task_id) is None
    assert store.sessions_for(task_id) == []
    store.close()


def test_a_task_is_not_removed_when_its_blocks_cannot_be(site):
    calendar = FakeCalendar()
    add(app_with(site, calendar), minutes="60")
    task_id = only_task(site)["assignment_id"]

    response = app_with(site, Unreachable()).post(
        f"/task/{task_id}/delete", data={"token": "t0ken"})
    assert "notice=" in response.headers["Location"]
    store = Store(site.db_path)
    assert store.assignment(task_id) is not None
    assert store.sessions_for(task_id)
    store.close()


def test_teams_prep_cannot_be_removed_from_the_dashboard(site):
    response = app_with(site, FakeCalendar()).post(
        f"/task/{TEAMS_ID}/delete", data={"token": "t0ken"})
    assert response.status_code == 404
    store = Store(site.db_path)
    assert store.assignment(TEAMS_ID) is not None
    with pytest.raises(ValueError):
        store.delete_task(TEAMS_ID)
    store.close()


def test_rescheduling_a_task_replaces_its_blocks(site):
    calendar = FakeCalendar()
    client = app_with(site, calendar)
    add(client, minutes="60")
    task_id = only_task(site)["assignment_id"]
    before = set(calendar.events)

    client.post("/reschedule", data={"assignment_id": task_id, "token": "t0ken"})
    assert calendar.events
    assert not before & set(calendar.events)


# ---------------------------------------------------------------- estimates

def test_a_personal_task_stays_out_of_every_average(site):
    store = Store(site.db_path)
    store.record_timing(TEAMS_ID, 40)
    task_id = store.add_task("Mock revision",
                             (datetime.now(TZ) + timedelta(days=3)).isoformat())
    store.record_timing(task_id, 400)
    # One long evening on your own project must not move the prep estimate.
    assert store.global_mean() == 40
    assert "" not in store.teacher_stats()
    store.close()


def test_a_task_a_teacher_set_counts_towards_that_teacher(site):
    store = Store(site.db_path)
    due = datetime.now(TZ) + timedelta(days=3)
    task_id = store.add_task("Extra sheet", due.isoformat(), teacher="Dalton")
    store.record_timing(task_id, 50)
    assert store.teacher_stats()["Dalton"] == (1, 50.0)
    assert store.global_mean() == 50
    assert store.known_teachers() == ["Dalton"]
    store.close()


def test_a_task_date_without_a_timezone_is_refused(site):
    store = Store(site.db_path)
    with pytest.raises(ValueError):
        store.add_task("x", "2026-10-10T17:00:00")
    store.close()


def test_an_old_database_gains_the_task_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE assignments (assignment_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL, class_ref TEXT, teacher TEXT, label TEXT,
                    assigned_at TEXT, due_at TEXT, id_synthetic INTEGER DEFAULT 0,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)""")
    conn.execute("INSERT INTO assignments VALUES ('a', 't', '', 'X', 't', NULL, "
                 "NULL, 0, 'now', 'now')")
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.assignment("a")["manual"] == 0
    store.close()


# ---------------------------------------------------------------- the demo

def test_the_demo_serves_a_populated_dashboard_offline(tmp_path):
    from prep import demo

    cfg, calendar = demo.build(tmp_path)
    assert cfg.web.token == ""
    assert calendar.events, "the demo should open with prep already scheduled"

    client = create_app(cfg, calendar_factory=lambda: calendar).test_client()
    page = client.get("/")
    assert page.status_code == 200
    body = page.data.decode()
    assert "Add a task" in body
    assert "Archer" in body            # the invented teachers are offered
    assert 'type="password"' not in body

    # The dummy fortnight is already full to the daily cap, so the task is
    # due in week three, where there is room.
    before = len(calendar.events)
    response = client.post("/task", data={"title": "Demo task",
                                          "due_date": future(20),
                                          "minutes": "60"})
    assert "notice" not in response.headers["Location"]
    assert len(calendar.events) > before
    # Booked around the dummy lessons, never over them.
    for session in calendar.events.values():
        assert not any(session.start < b.end and session.end > b.start
                       for b in calendar._busy)


def test_the_demo_refuses_to_overwrite_a_kept_database(tmp_path):
    from prep import demo

    (tmp_path / "prep.sqlite3").write_text("mine")
    assert demo.main(["--keep", str(tmp_path)]) == 1
    assert (tmp_path / "prep.sqlite3").read_text() == "mine"


def test_the_dashboard_lists_what_is_coming_not_the_oldest_history(tmp_path):
    """A year of captured history used to push every upcoming prep - and any
    task just added - off the end of a fifty-row list."""
    store = Store(tmp_path / "p.db")
    now = datetime(2026, 9, 30, 12, 0, tzinfo=TZ)
    for weeks in range(60):
        store.add_task(f"old {weeks}", (now - timedelta(weeks=weeks + 1)).isoformat())
    soon = store.add_task("soon", (now + timedelta(days=1)).isoformat())
    later = store.add_task("later", (now + timedelta(days=9)).isoformat())

    rows = store.upcoming(limit=50, now=now)
    ids = [r["assignment_id"] for r in rows]
    assert ids[:2] == [soon, later]
    assert rows[2]["title"] == "old 0"         # then the most recent past
    # Offsets differ between Teams ("Z") and your tasks ("+01:00"); the order
    # must follow the instant, not the text.
    store.add_task("utc", "2026-10-01T09:00:00Z")          # 10:00 London
    store.add_task("bst", "2026-10-01T09:30:00+01:00")     # 09:30 London
    titles = [r["title"] for r in store.upcoming(now=now)]
    assert titles.index("bst") < titles.index("utc")
    store.close()
