"""Where you are each day decides when you can work.

Prep happens at home or at school, never in transit and never at a lesson.
These tests walk the cases that matter: an ordinary school day, a club after
school, an outside lesson near home and one near school, a journey that
cannot be made, and a lesson added after prep was booked.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import config as config_module
from prep import pipeline, travel
from prep.feeds import FeedEvent, parse
from prep.scheduler import DiaryEvent, FakeCalendar, Interval
from prep.store import Change, Store

TZ = ZoneInfo("Europe/London")
NEAR_HOME = "EC1A 9AA"
NEAR_SCHOOL = "SW1A 1AA"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def cfg(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/d
export_path: {tmp_path}/p.txt
learned_map_path: {tmp_path}/teachers.json
timezone: Europe/London
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep,
           personal_calendar: Personal}}
scheduling:
  block_minutes: 30
  max_daily_minutes: 120
  gap_minutes: 10
  windows:
    monday:    {{start: "17:00", end: "21:30"}}
    tuesday:   {{start: "17:00", end: "21:30"}}
    wednesday: {{start: "17:00", end: "21:30"}}
    thursday:  {{start: "17:00", end: "21:30"}}
    friday:    {{start: "17:00", end: "21:00"}}
    saturday:  {{start: "09:30", end: "18:00"}}
    sunday:    {{start: "09:30", end: "20:00"}}
travel:
  home: {{postcode: "EC1A 1BB"}}
  school:
    name: Example School
    postcode: "SW1A OAA"
    calendars: ["Timetable"]
    feeds: [socs]
    work_until: "19:00"
  places:
    piano: {{name: "Piano", postcode: "{NEAR_HOME}", match: ["mrs smith"]}}
  times:
    home-school: 75
    home-{NEAR_HOME}: 10
    school-{NEAR_HOME}: 70
    home-{NEAR_SCHOOL}: 70
    school-{NEAR_SCHOOL}: 10
  lookup_postcodes: false
  cache_path: {tmp_path}/places.json
""")
    return config_module.load(tmp_path / "config.yaml")


def lessons(day: int, end_hour: int = 15, end_minute: int = 55) -> DiaryEvent:
    return DiaryEvent(at(day, 9), at(day, end_hour, end_minute), "Lessons",
                      "Physics 2", "Timetable ")          # trailing space, as in iCloud


def personal(day: int, hour: int, minute: int, minutes: int,
             where: str, what: str = "Piano") -> DiaryEvent:
    start = at(day, hour, minute)
    return DiaryEvent(start, start + timedelta(minutes=minutes), what, where,
                      "Personal")


def routed(cfg, diary, feed=(), blackouts=()):
    places = travel.Places(cfg.travel)
    stops, unplaced = travel.collect_stops(places, diary, list(feed),
                                           list(blackouts), "Personal")
    busy = [Interval(e.start, e.end) for e in diary]
    busy += [Interval(e.start, e.end) for e in feed]
    busy += [i for i, _, _ in blackouts]
    routing = travel.route(cfg, places, stops, busy, at(12, 0), at(25, 0), TZ)
    routing.unplaced = unplaced
    return routing


def covers(windows, start, end) -> bool:
    return any(w.start <= start and end <= w.end for w in windows)


def overlaps(windows, start, end) -> bool:
    return any(w.start < end and start < w.end for w in windows)


# ------------------------------------------------------------ places

def test_a_postcode_is_normalised_including_the_letter_o_typo():
    assert travel.normalise_postcode("sw1a oaa") == "SW1A 0AA"
    assert travel.normalise_postcode("Mrs Smith, 4 Lane, EC1A9AA") == "EC1A 9AA"
    assert travel.normalise_postcode("Music School room 8") == ""


def test_events_are_placed_by_postcode_name_or_calendar(cfg):
    places = travel.Places(cfg.travel)
    assert places.resolve("Physics 2", "Timetable ").key == "school"
    assert places.resolve("Sports Hall Squash", "socs").key == "school"
    assert places.resolve("Example School, High Street", "Personal").key == "school"
    assert places.resolve("at Mrs Smith", "Personal").key == "piano"
    assert places.resolve("4 Lane EC1A 9AA", "Personal").key == "piano"
    assert places.resolve("home", "Personal").is_home
    assert places.resolve("Somewhere N1 9GU", "Personal").key == "N1 9GU"
    assert places.resolve("Netball court", "Personal") is None


def test_an_away_fixture_in_the_school_feed_is_still_away(cfg):
    """A postcode beats the calendar it came from."""
    places = travel.Places(cfg.travel)
    assert places.resolve("Other College N1 9GU", "socs").key == "N1 9GU"


def test_a_given_journey_time_wins_in_either_direction(cfg):
    places = travel.Places(cfg.travel)
    assert places.minutes(places.home, places.school) == 75
    assert places.minutes(places.school, places.home) == 75
    assert places.minutes(places.school, places.school) == 0


def test_an_unknown_journey_is_estimated_from_a_known_one(cfg):
    """Scaled from home-school, so it reflects how you actually travel."""
    coords = {"EC1A 1BB": [51.000, -0.100], "SW1A 0AA": [50.820, -0.100],
              "SW1A 2AA": [50.910, -0.100]}                   # halfway
    geocoder = travel.Geocoder(None, fetch=lambda pc: coords.get(pc))
    cfg.travel.lookup_postcodes = True
    places = travel.Places(cfg.travel, geocoder)
    halfway = places.resolve("SW1A 2AA")
    minutes = places.minutes(places.home, halfway)
    # 15 fixed + half of the remaining 60 = 45
    assert 40 <= minutes <= 50


def test_with_lookups_off_an_unknown_place_gets_the_default(cfg):
    places = travel.Places(cfg.travel)
    assert places.minutes(places.home, places.resolve("N1 9GU")) == 60


def test_the_postcode_cache_remembers_answers_but_not_failures(tmp_path):
    calls = []

    def fetch(postcode):
        calls.append(postcode)
        if postcode == "ZZ1 1ZZ":
            raise ConnectionError("offline")
        return None if postcode == "XX1 1XX" else [51.0, -0.1]

    geocoder = travel.Geocoder(tmp_path / "places.json", fetch=fetch)
    assert geocoder.lookup("ec1a 1bb") == (51.0, -0.1)
    assert geocoder.lookup("XX1 1XX") is None
    assert geocoder.lookup("ZZ1 1ZZ") is None
    again = travel.Geocoder(tmp_path / "places.json", fetch=fetch)
    again.lookup("EC1A 1BB")
    again.lookup("XX1 1XX")
    again.lookup("ZZ1 1ZZ")
    assert calls.count("EC1A 1BB") == 1
    assert calls.count("XX1 1XX") == 1, "a postcode that does not exist was asked twice"
    assert calls.count("ZZ1 1ZZ") == 2, "a network failure was remembered as an answer"


def test_lookups_off_never_touch_the_network(tmp_path):
    def fetch(postcode):
        raise AssertionError("network used")
    assert travel.Geocoder(None, enabled=False, fetch=fetch).lookup("EC1A 1BB") is None


# ------------------------------------------------------------ routing a day

def test_an_ordinary_school_day_starts_prep_when_you_get_home(cfg):
    routing = routed(cfg, [lessons(13)])
    windows = routing.days[at(13, 0).date()].windows
    assert not overlaps(windows, at(13, 15, 55), at(13, 17, 10)), \
        "prep booked on the journey home"
    assert covers(windows, at(13, 17, 10), at(13, 21, 30))


def test_a_club_after_school_is_worked_around_at_school(cfg):
    """Lessons to 15:55, choir 17:00-18:30: work at school in between, and
    at home only once you are back at 19:45."""
    choir = FeedEvent(at(14, 17), at(14, 18, 30), "Chamber Choir", "socs", "SARH")
    routing = routed(cfg, [lessons(14)], feed=[choir])
    windows = routing.days[at(14, 0).date()].windows
    assert covers(windows, at(14, 16, 5), at(14, 16, 50)), "the hour at school was lost"
    assert not overlaps(windows, at(14, 18, 30), at(14, 19, 45))
    assert covers(windows, at(14, 19, 45), at(14, 21, 30))


def test_a_lesson_near_home_means_going_home_first(cfg):
    routing = routed(cfg, [lessons(13), personal(13, 18, 0, 45, NEAR_HOME)])
    plan = routing.days[at(13, 0).date()]
    assert covers(plan.windows, at(13, 17, 10), at(13, 17, 50))
    assert covers(plan.windows, at(13, 18, 55), at(13, 21, 30))
    assert not overlaps(plan.windows, at(13, 17, 50), at(13, 18, 55)), \
        "prep booked on the way to the lesson, at it, or on the way back"


def test_a_lesson_near_school_means_staying_at_school(cfg):
    """Home and back would take longer than the gap - stay and work."""
    routing = routed(cfg, [lessons(13), personal(13, 18, 0, 45, NEAR_SCHOOL)])
    plan = routing.days[at(13, 0).date()]
    assert covers(plan.windows, at(13, 16, 5), at(13, 17, 50))
    assert not overlaps(plan.windows, at(13, 17, 50), at(13, 19, 55))
    assert covers(plan.windows, at(13, 19, 55), at(13, 21, 30))
    assert any("Example School" in line for line in plan.lines)


def test_staying_at_school_is_chosen_when_home_would_arrive_too_late(cfg):
    """Nothing after school, but the home window shut early: stay and work."""
    cfg.scheduling.windows[0] = type(cfg.scheduling.windows[0])(
        cfg.scheduling.windows[0].start.replace(hour=17),
        cfg.scheduling.windows[0].end.replace(hour=18, minute=0))
    routing = routed(cfg, [lessons(12)])
    windows = routing.days[at(12, 0).date()].windows
    assert covers(windows, at(12, 16, 5), at(12, 17, 55)), \
        "an hour at school beats twenty minutes at home"


def test_an_impossible_journey_is_reported(cfg):
    late_lesson = DiaryEvent(at(13, 9), at(13, 17), "Lessons", "Y08", "Timetable")
    routing = routed(cfg, [late_lesson, personal(13, 17, 30, 45, NEAR_HOME)])
    [problem] = routing.problems
    assert "cannot get from Example School" in problem
    assert "needs 70 min, has 30" in problem


def test_a_day_without_commitments_keeps_the_usual_window(cfg):
    routing = routed(cfg, [lessons(13)])
    assert at(17, 0).date() not in routing.days


def test_a_personal_event_without_a_place_is_flagged_not_guessed(cfg):
    routing = routed(cfg, [lessons(13),
                           DiaryEvent(at(13, 18), at(13, 19), "Piano", "", "Personal")])
    assert routing.unplaced and "(no location)" in routing.unplaced[0]


def test_the_feed_keeps_each_events_location():
    ics = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1
DTSTART:20261014T160000Z
DTEND:20261014T173000Z
SUMMARY:Chamber Choir
LOCATION:SARH
END:VEVENT
END:VCALENDAR
"""
    [found] = parse(ics, at(13, 0), at(16, 0), TZ)
    assert found.location == "SARH"


# ------------------------------------------------------------ the pipeline

def _book(cfg, calendar, now, days=(4, 6), starts=(0, 0)):
    store = Store(cfg.db_path)
    changes = []
    for n, (ahead, begin) in enumerate(zip(days, starts)):
        task_id = store.add_task(f"Essay {n}", (now + timedelta(days=ahead)).isoformat(),
                                 minutes=90, teacher=f"T{n}",
                                 start_at=(now + timedelta(days=begin)).isoformat())
        row = store.assignment(task_id)
        changes.append(Change("new", task_id, row["label"], "", row["due_at"]))
    pipeline.reschedule(cfg, store, calendar, changes, now=now)
    return store


def test_a_located_blackout_is_somewhere_you_travel_to(cfg):
    store = Store(cfg.db_path)
    store.add_blackout("18:00", "18:45", weekday=1, label="Piano",
                       location=NEAR_HOME)
    calendar = FakeCalendar(diary=[lessons(13)])
    busy, routing = pipeline.diary_context(cfg, store, calendar, at(12, 6),
                                           at(19, 6), TZ)
    plan = routing.days[at(13, 0).date()]
    assert not overlaps(plan.windows, at(13, 17, 50), at(13, 18, 55))
    assert covers(plan.windows, at(13, 17, 10), at(13, 17, 50))
    store.close()


def test_a_blackout_without_a_place_is_just_busy(cfg):
    store = Store(cfg.db_path)
    store.add_blackout("19:00", "20:00", weekday=1, label="Supper")
    busy, routing = pipeline.diary_context(cfg, store, FakeCalendar(diary=[]),
                                           at(12, 6), at(19, 6), TZ)
    assert routing.days == {}
    assert any(b.start == at(13, 19) for b in busy)
    store.close()


def test_nothing_is_booked_in_transit(cfg):
    diary = [lessons(d) for d in (12, 13, 14, 15, 16)]
    calendar = FakeCalendar(diary=diary)
    store = _book(cfg, calendar, at(12, 6))
    for session in calendar.events.values():
        if session.start.date() <= at(16, 0).date():
            assert session.start >= session.start.replace(hour=17, minute=10), \
                f"booked at {session.start:%a %H:%M}, before you are home"
    store.close()


def test_adding_a_lesson_moves_only_the_prep_it_clashes_with(cfg):
    diary = [lessons(d) for d in (12, 13, 14, 15, 16)]
    calendar = FakeCalendar(diary=diary)
    now = at(12, 6)
    # The second prep cannot start until Thursday, so a Tuesday lesson can
    # only clash with the first.
    store = _book(cfg, calendar, now, days=(3, 7), starts=(0, 3))
    before = dict(calendar.events)
    hit = next(s for s in before.values() if s.start.date() == at(13, 0).date())
    assert all(s.start.date() >= at(15, 0).date() for s in before.values()
               if s.assignment_id != hit.assignment_id)

    # A piano lesson near school, right where that booking sat. Nothing else
    # changes: the next run notices on its own.
    calendar.diary.append(personal(13, hit.start.hour, hit.start.minute, 45,
                                   NEAR_SCHOOL))
    pipeline.reschedule(cfg, store, calendar, [], now=now)

    lesson = calendar.diary[-1]
    away = (lesson.start - timedelta(minutes=10), lesson.end + timedelta(minutes=70))
    for session in calendar.events.values():
        assert not (session.start < away[1] and away[0] < session.end), \
            f"{session.start:%a %H:%M} is at or travelling to the lesson"
    untouched = [uid for uid, s in before.items()
                 if s.assignment_id != hit.assignment_id]
    assert all(uid in calendar.events for uid in untouched), \
        "prep that did not clash was moved anyway"
    uids = {row["uid"] for row in store.all_sessions()}
    assert uids == set(calendar.events), "diary and database disagree"
    store.close()


def test_without_travel_configured_nothing_changes(cfg):
    cfg.travel = None
    store = Store(cfg.db_path)
    busy, routing = pipeline.diary_context(cfg, store,
                                           FakeCalendar(diary=[lessons(13)]),
                                           at(12, 6), at(19, 6), TZ)
    assert routing is None
    store.close()


def test_the_scheduled_run_moves_prep_when_a_lesson_is_added(cfg, tmp_path,
                                                             monkeypatch, capsys):
    """Nothing new from Teams, but a lesson was added in Personal: the
    regular run must still notice and move the prep it now clashes with."""
    import json
    from prep import run

    now = datetime.now(TZ)
    payload = tmp_path / "payloads.json"
    payload.write_text(json.dumps([{"url": "assignments", "body": {"value": [
        {"id": "a" * 36, "classId": "c", "displayName": "Essay",
         "assignedDateTime": now.astimezone(ZoneInfo("UTC")).isoformat(),
         "dueDateTime": (now + timedelta(days=5)).astimezone(ZoneInfo("UTC"))
                        .isoformat()}]}}]))
    diary = [DiaryEvent(datetime.combine((now + timedelta(days=d)).date(),
                                         datetime.min.time(), tzinfo=TZ)
                        .replace(hour=9),
                        datetime.combine((now + timedelta(days=d)).date(),
                                         datetime.min.time(), tzinfo=TZ)
                        .replace(hour=15, minute=55),
                        "Lessons", "Y08", "Timetable")
             for d in range(1, 8)
             if (now + timedelta(days=d)).weekday() < 5]
    calendar = FakeCalendar(diary=diary)
    monkeypatch.setattr(run, "CalDAVCalendar", lambda cfg, tz: calendar)
    monkeypatch.setattr(run.config_module, "load", lambda path: cfg)
    args = ["--config", "x", "--payload-file", str(payload)]

    assert run.main(args) == 0
    # Not a session already under way: run in an evening window, the first
    # booking starts the moment it is made, and that one rightly stays put.
    hit = min((s for s in calendar.events.values()
               if s.start > datetime.now(TZ) + timedelta(minutes=5)),
              key=lambda s: s.start)

    assert run.main(args) == 0
    assert "no changes" in capsys.readouterr().out, \
        "a run with nothing new moved prep anyway"

    calendar.diary.append(DiaryEvent(hit.start, hit.start + timedelta(minutes=45),
                                     "Piano", NEAR_SCHOOL, "Personal"))
    assert run.main(args) == 0
    assert "REPLAN" in capsys.readouterr().out

    lesson = calendar.diary[-1]
    for session in calendar.events.values():
        assert not (session.start < lesson.end and lesson.start < session.end)
    _assert_nothing_in_transit(cfg, calendar)
    store = Store(cfg.db_path)
    assert {row["uid"] for row in store.all_sessions()} == set(calendar.events)
    store.close()


def _assert_nothing_in_transit(cfg, calendar):
    """Every future booking sits inside a window where you are at home or
    at school - the same test as Phase C's "Nothing booked in transit".

    Not a fixed band around the lesson: how far ahead you must stop depends
    on where you are working, ten minutes from school or seventy from home.
    """
    now = datetime.now(TZ)
    store = Store(cfg.db_path)
    try:
        _, routing = pipeline.diary_context(cfg, store, calendar, now,
                                            now + timedelta(days=21), TZ)
    finally:
        store.close()
    for session in calendar.events.values():
        if session.start < now:
            continue
        plan = routing.days.get(session.start.date())
        if plan is not None:
            assert any(w.start <= session.start and session.end <= w.end
                       for w in plan.windows), \
                f"booked in transit: {session.start:%a %H:%M}"


# ------------------------------------------------------------ the dashboard

def _dashboard(cfg, calendar):
    from prep.web import create_app
    return create_app(cfg, calendar_factory=lambda: calendar).test_client()


def test_the_dashboard_offers_places_when_travel_is_set_up(cfg):
    page = _dashboard(cfg, FakeCalendar()).get("/").get_data(as_text=True)
    assert "Something outside school" in page
    assert 'name="location"' in page
    assert "Personal" in page


def test_the_dashboard_hides_them_without_travel(cfg):
    cfg.travel = None
    page = _dashboard(cfg, FakeCalendar()).get("/").get_data(as_text=True)
    assert "Something outside school" not in page


def test_an_outside_commitment_goes_to_personal_and_moves_prep(cfg):
    from urllib.parse import unquote_plus

    now = datetime.now(TZ)
    calendar = FakeCalendar()
    store = Store(cfg.db_path)
    task_id = store.add_task("Essay", (now + timedelta(days=3)).isoformat(),
                             minutes=60)
    row = store.assignment(task_id)
    pipeline.reschedule(cfg, store, calendar,
                        [Change("new", task_id, row["label"], "", row["due_at"])])
    store.close()
    # Not a session already under way, which rightly stays where it is.
    posted = datetime.now(TZ)
    first = min((s for s in calendar.events.values()
                 if s.start > posted + timedelta(minutes=5)),
                key=lambda s: s.start)

    response = _dashboard(cfg, calendar).post("/outside", data={
        "title": "Piano", "on_date": first.start.date().isoformat(),
        "start_time": first.start.strftime("%H:%M"),
        "end_time": (first.start + timedelta(minutes=45)).strftime("%H:%M"),
        "location": NEAR_SCHOOL})

    assert response.status_code == 302
    notice = unquote_plus(response.headers["Location"])
    assert "Added Piano" in notice
    [added] = [e for e in calendar.diary if e.calendar == "Personal"]
    assert added.location == NEAR_SCHOOL
    for session in calendar.events.values():
        if session.start < posted:
            continue
        assert not (session.start < added.end and added.start < session.end), \
            "prep left at the new lesson"
    _assert_nothing_in_transit(cfg, calendar)


def test_a_blackout_keeps_its_place(cfg):
    client = _dashboard(cfg, FakeCalendar())
    client.post("/blackout", data={"weekday": "1", "start_time": "18:00",
                                   "end_time": "18:45", "label": "Piano",
                                   "location": NEAR_HOME})
    store = Store(cfg.db_path)
    assert store.blackouts()[0]["location"] == NEAR_HOME
    store.close()
    assert NEAR_HOME in client.get("/").get_data(as_text=True)


def test_a_place_that_cannot_be_found_is_said_so(cfg):
    from urllib.parse import unquote_plus

    response = _dashboard(cfg, FakeCalendar()).post("/blackout", data={
        "weekday": "1", "start_time": "18:00", "end_time": "18:45",
        "location": "the netball court"})
    assert "no journey was counted" in unquote_plus(response.headers["Location"])


def test_a_backwards_outside_commitment_is_refused(cfg):
    response = _dashboard(cfg, FakeCalendar()).post("/outside", data={
        "title": "Piano", "on_date": "2026-10-13", "start_time": "19:00",
        "end_time": "18:00", "location": NEAR_HOME})
    assert response.status_code == 400
