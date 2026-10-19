"""Changing a constraint on the dashboard re-plans everything, without doubles.

The property that matters: after any number of replans, the diary holds
exactly the bookings the database holds - one event per session, none left
behind, none booked twice - and none of them sits in a blacked-out evening.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import config as config_module
from prep import pipeline
from prep.scheduler import FakeCalendar, PlannedSession
from prep.store import Change, Store
from prep.web import create_app

TZ = ZoneInfo("Europe/London")


@pytest.fixture
def site(tmp_path: Path):
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
    friday:    {{start: "17:00", end: "21:00"}}
    saturday:  {{start: "09:30", end: "18:00"}}
    sunday:    {{start: "09:30", end: "20:00"}}
web:
  token: "t0ken"
""")
    cfg = config_module.load(tmp_path / "config.yaml")
    now = datetime.now(TZ)
    store = Store(cfg.db_path)
    calendar = FakeCalendar()
    changes = []
    for n, days in enumerate((4, 6, 9)):
        task_id = store.add_task(f"Essay {n}", (now + timedelta(days=days)).isoformat(),
                                 minutes=90, teacher=f"T{n}")
        row = store.assignment(task_id)
        changes.append(Change("new", task_id, row["label"], row["teacher"] or "",
                              row["due_at"]))
    # Planned from tomorrow, so no booking is already under way when a test
    # replans at the real time a moment later.
    tomorrow = datetime.combine(now.date() + timedelta(days=1),
                                datetime.min.time(), tzinfo=TZ)
    pipeline.reschedule(cfg, store, calendar, changes, now=tomorrow)
    store.close()
    return cfg, calendar


def _client(cfg, calendar):
    return create_app(cfg, calendar_factory=lambda: calendar).test_client()


def _assert_diary_matches_database(cfg, calendar):
    store = Store(cfg.db_path)
    uids = [row["uid"] for row in store.all_sessions()]
    store.close()
    assert len(uids) == len(set(uids))
    assert set(uids) == set(calendar.events), \
        "the diary and the database disagree - a duplicate or a lost booking"


def test_a_blackout_moves_bookings_out_of_that_day(site):
    cfg, calendar = site
    _assert_diary_matches_database(cfg, calendar)
    busiest = max({s.start.weekday() for s in calendar.events.values()},
                  key=lambda d: sum(s.start.weekday() == d
                                    for s in calendar.events.values()))

    response = _client(cfg, calendar).post("/blackout", data={
        "weekday": str(busiest), "start_time": "00:00", "end_time": "23:59",
        "label": "Away", "token": "t0ken"})

    assert response.status_code == 302
    assert "Rescheduled" in response.headers["Location"]
    assert calendar.events, "everything was removed and nothing rebooked"
    assert all(s.start.weekday() != busiest for s in calendar.events.values())
    _assert_diary_matches_database(cfg, calendar)


def test_removing_the_blackout_replans_again_without_doubling(site):
    cfg, calendar = site
    client = _client(cfg, calendar)
    before = len(calendar.events)
    client.post("/blackout", data={"weekday": "0", "start_time": "17:00",
                                   "end_time": "21:30", "token": "t0ken"})
    store = Store(cfg.db_path)
    blackout_id = store.blackouts()[0]["id"]
    store.close()
    client.post(f"/blackout/{blackout_id}/delete", data={"token": "t0ken"})

    _assert_diary_matches_database(cfg, calendar)
    assert len(calendar.events) == before


def test_replanning_twice_books_each_prep_once(site):
    cfg, calendar = site
    store = Store(cfg.db_path)
    pipeline.replan_all(cfg, store, calendar)
    pipeline.replan_all(cfg, store, calendar)
    per_prep = {}
    for session in calendar.events.values():
        per_prep[session.assignment_id] = per_prep.get(session.assignment_id, 0) + 1
    store.close()
    assert sorted(per_prep.values()) == [3, 3, 3], "90 minutes is three blocks each"
    _assert_diary_matches_database(cfg, calendar)


def test_a_correction_replans_from_the_dashboard(site):
    cfg, calendar = site
    store = Store(cfg.db_path)
    target = store.all_sessions()[0]["assignment_id"]
    store.close()

    _client(cfg, calendar).post("/override", data={
        "assignment_id": target, "minutes": "30", "token": "t0ken"})

    booked = [s for s in calendar.events.values() if s.assignment_id == target]
    assert len(booked) == 1, "a 30-minute correction is one block"
    _assert_diary_matches_database(cfg, calendar)


def test_time_already_worked_is_kept_and_counted(site):
    cfg, calendar = site
    store = Store(cfg.db_path)
    target = store.all_sessions()[0]["assignment_id"]
    for row in store.sessions_for(target):
        calendar.delete_by_uid([row["uid"]])
    store.clear_sessions(target)
    now = datetime.now(TZ)
    earlier = PlannedSession(target, "x", "", now - timedelta(hours=3),
                             now - timedelta(hours=2, minutes=30))
    uid = calendar.create(earlier)
    store.record_session(uid, target, earlier.start.isoformat(),
                         earlier.end.isoformat(), 30)

    pipeline.replan_all(cfg, store, calendar)

    assert uid in calendar.events, "a session in the past was removed"
    booked = [s for s in calendar.events.values() if s.assignment_id == target]
    store.close()
    assert len(booked) == 3, "past time was not counted towards the prep"


def test_a_booking_that_will_not_delete_is_not_doubled(site):
    cfg, calendar = site
    stuck = next(iter(calendar.events))
    stuck_prep = calendar.events[stuck].assignment_id
    real_delete = calendar.delete_by_uid

    def refuses_one(uids):
        real_delete([u for u in uids if u != stuck])
        return [u for u in uids if u == stuck]
    calendar.delete_by_uid = refuses_one

    store = Store(cfg.db_path)
    pipeline.replan_all(cfg, store, calendar)
    store.close()

    assert stuck in calendar.events
    booked = [s for s in calendar.events.values() if s.assignment_id == stuck_prep]
    assert len(booked) == 3, "the stuck booking was doubled or dropped"
    _assert_diary_matches_database(cfg, calendar)


def test_a_stray_copy_in_the_diary_is_removed(site):
    cfg, calendar = site
    original = next(iter(calendar.events.values()))
    stray = calendar.create(original)                    # same prep, same slot
    stranger = calendar.create(PlannedSession(
        "someone-elses-prep", "x", "", original.start, original.end))

    store = Store(cfg.db_path)
    pipeline.replan_all(cfg, store, calendar)
    store.close()

    assert stray not in calendar.events
    assert stranger in calendar.events, "a booking we do not own was deleted"


def test_an_unreadable_timetable_moves_nothing(site, monkeypatch):
    cfg, calendar = site
    cfg.feeds = {"socs": "https://example.invalid/feed.ics"}
    monkeypatch.setattr(pipeline.feeds_module, "load_all",
                        lambda *a, **k: ([], ["socs: unreachable"]))
    before = dict(calendar.events)

    response = _client(cfg, calendar).post("/blackout", data={
        "weekday": "0", "start_time": "17:00", "end_time": "21:30",
        "token": "t0ken"})

    assert "nothing+was+moved" in response.headers["Location"]
    assert calendar.events == before
    _assert_diary_matches_database(cfg, calendar)


def test_caldav_deletes_without_asking_the_server_to_filter_by_uid():
    """iCloud answers a UID-filtered REPORT with 412 Precondition Failed, so
    event_by_uid can never be how a booking is found. That one refusal left
    every replaced booking in the diary."""
    from icalendar import Calendar as ICalendar, Event as IEvent
    from prep.scheduler import CalDAVCalendar

    class Stored:
        def __init__(self, uid, prep):
            cal = ICalendar()
            event = IEvent()
            event.add("uid", uid)
            cal.add_component(event)
            self.icalendar_instance, self.uid, self.prep = cal, uid, prep

        def delete(self):
            self.prep.events.remove(self)

    class ICloudLike:
        def __init__(self, uids):
            self.events = [Stored(uid, self) for uid in uids]

        def event_by_uid(self, uid):
            raise RuntimeError("412 Precondition Failed")

        def search(self, **kwargs):
            return list(self.events)

    backend = object.__new__(CalDAVCalendar)
    backend.tz = TZ
    backend.prep = ICloudLike(["a@prep-pi", "b@prep-pi", "keep@prep-pi"])

    failed = backend.delete_by_uid(["a@prep-pi", "b@prep-pi", "gone@prep-pi"])

    assert failed == []
    assert [e.uid for e in backend.prep.events] == ["keep@prep-pi"]
