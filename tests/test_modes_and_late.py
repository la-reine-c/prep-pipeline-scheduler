"""How you get there, and how late you work.

Two things found in a real week:

  - A lesson 18 km from home was given 75 minutes back, because the only
    journey timed was the bus to school. A lift is not a bus, so car and
    public transport are timed and estimated apart, and each commitment can
    say which it is.
  - Working until 22:00 is possible but should be the last resort: from
    20:00 each later hour is used only when nothing earlier before the
    deadline is free.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from prep import config as config_module
from prep import scheduler, travel
from prep.config import DayWindow, SchedulingConfig
from prep.scheduler import DiaryEvent, FakeCalendar, Interval
from prep.store import Store

TZ = ZoneInfo("Europe/London")
ROOT = Path(__file__).resolve().parents[1]
HOME, SCHOOL, LESSON = "EC1A 1BB", "SW1A 0AA", "E1 6AN"
COORDS = {HOME: [50.993843, -0.117988], SCHOOL: [50.821191, -0.1216],
          LESSON: [50.8395, -0.2205]}


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def cfg(tmp_path: Path):
    (tmp_path / "places.json").write_text(json.dumps(COORDS))
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
  windows:
    monday:    {{start: "17:00", end: "22:00"}}
    tuesday:   {{start: "17:00", end: "22:00"}}
    wednesday: {{start: "17:00", end: "22:00"}}
    thursday:  {{start: "17:00", end: "22:00"}}
    friday:    {{start: "17:00", end: "22:00"}}
    saturday:  {{start: "09:30", end: "22:00"}}
    sunday:    {{start: "09:30", end: "22:00"}}
  late_from: "20:00"
  late_step_minutes: 60
travel:
  home: {{postcode: "{HOME}"}}
  school: {{name: School, postcode: "{SCHOOL}", calendars: ["Timetable"]}}
  times:
    home-school: 75
  car_times:
    "home-{LESSON}": 45
  default_mode: car
  school_mode: public
  lookup_postcodes: false
  cache_path: {tmp_path}/places.json
web:
  token: "t0ken"
""")
    return config_module.load(tmp_path / "config.yaml")


# ------------------------------------------------------------ journeys

def test_a_lift_is_not_the_bus(cfg):
    places = travel.Places(cfg.travel)
    lesson = places.resolve(LESSON)
    assert places.minutes(places.home, lesson, "car") == 45
    assert places.minutes(places.home, lesson, "public") == 75, \
        "public transport is still estimated from the bus to school"
    assert places.minutes(places.home, places.school, "public") == 75


def test_car_estimates_are_calibrated_from_car_journeys(cfg):
    """School to the lesson was never timed. By car it is scaled from the
    45-minute drive, not the 75-minute bus."""
    places = travel.Places(cfg.travel)
    lesson = places.resolve(LESSON)
    by_car = places.minutes(places.school, lesson, "car")
    by_bus = places.minutes(places.school, lesson, "public")
    assert by_car < by_bus
    assert by_car <= 30


def test_with_no_car_journey_timed_a_sensible_default_is_used(cfg):
    cfg.travel.car_times = {}
    places = travel.Places(cfg.travel)
    lesson = places.resolve(LESSON)
    assert places.minutes(places.home, lesson, "car") < 60


def test_which_way_a_journey_goes(cfg):
    places = travel.Places(cfg.travel)
    lesson = places.resolve(LESSON)
    assert places.mode_for(places.home, places.school) == "public"
    assert places.mode_for(places.home, lesson) == "car"
    assert places.mode_for(places.home, lesson, "public") == "public"
    assert places.mode_for(places.home, lesson, "", "car") == "car"


@pytest.mark.parametrize("text, mode", [
    ("Piano (car)", "car"), ("Swimming - lift from Mum", "car"),
    ("Orchestra [bus]", "public"), ("going by train", "public"),
    ("Public transport", "public"), ("Piano", ""), ("Carol service", ""),
])
def test_an_event_can_say_how_you_get_there(text, mode):
    assert travel.mode_from_text(text) == mode


def _home_from(cfg, mode: str) -> datetime:
    """When you are back home after a 17:00-18:00 lesson."""
    places = travel.Places(cfg.travel)
    event = DiaryEvent(at(13, 17), at(13, 18), "Lesson", LESSON, "Personal",
                       travel=mode)
    stops, _ = travel.collect_stops(places, [event], [], [], "Personal")
    routing = travel.route(cfg, places, stops, [Interval(event.start, event.end)],
                           at(13, 0), at(14, 0), TZ)
    return min(w.start for w in routing.days[at(13, 0).date()].windows
               if w.start >= event.end)


def test_a_lift_home_gets_you_working_sooner(cfg):
    assert _home_from(cfg, "car") == at(13, 18, 45)
    assert _home_from(cfg, "public") == at(13, 19, 15)
    assert _home_from(cfg, "") == at(13, 18, 45), "outside commitments default to car"


def test_a_blackout_keeps_how_you_get_there(cfg):
    from prep import pipeline
    store = Store(cfg.db_path)
    store.add_blackout("17:00", "18:00", on_date="2026-10-13", label="Lesson",
                       location=LESSON, travel="public")
    found = pipeline.located_blackouts(store, at(13, 0), at(14, 0), TZ)
    interval, where, label = found[0]
    assert (where, found[0].travel) == (LESSON, "public")
    store.close()


# ------------------------------------------------------------ late evening

def sched(**kw) -> SchedulingConfig:
    base = SchedulingConfig(
        block_minutes=30, flow_buffer_minutes=5, gap_minutes=10,
        max_daily_minutes=120, max_sitting_minutes=60,
        late_from=time(20, 0), late_step_minutes=60, due_time=time(8, 30),
        windows={d: DayWindow(time(17, 0), time(22, 0)) for d in range(7)})
    for key, value in kw.items():
        setattr(base, key, value)
    return base


def test_late_tiers():
    assert scheduler.late_tiers(sched()) == [
        (None, time(20)), (time(20), time(21)), (time(21), time(22)),
        (time(22), time(23)), (time(23), None)]
    assert scheduler.late_tiers(sched(late_from=None)) == [(None, None)]
    assert scheduler.late_tiers(sched(late_step_minutes=0)) == [
        (None, time(20)), (time(20), None)]


def test_an_earlier_evening_later_in_the_week_beats_working_late_tonight():
    """Monday is free only from 19:30. With days to spare, the work goes
    into Tuesday's early evening rather than Monday after 20:00."""
    cfg = sched()
    busy = [Interval(at(12, 17), at(12, 19, 20))]
    due = at(15, 8, 30)
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 60, None, due, busy,
                              at(12, 16), TZ)
    assert all(s.end.time() <= time(20, 0) for s in sessions), \
        [(s.start, s.end) for s in sessions]


def test_late_time_is_used_when_nothing_earlier_is_left():
    cfg = sched()
    busy = [Interval(at(12, 17), at(12, 19, 50))]
    due = at(13, 8, 30)                             # only tonight
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 60, None, due, busy,
                              at(12, 16), TZ)
    assert sum(s.work for s in sessions) == 60
    assert sessions[0].start == at(12, 20)


def test_the_first_late_hour_goes_before_the_second():
    """Two evenings, both free only after 20:00: 20:00-21:00 on each is
    used before 21:00-22:00 on either."""
    cfg = sched()
    busy = [Interval(at(12, 17), at(12, 19, 50)),
            Interval(at(13, 17), at(13, 19, 50))]
    due = at(14, 8, 30)
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 90, None, due, busy,
                              at(12, 16), TZ)
    assert sum(s.work for s in sessions) == 90
    assert all(s.end.time() <= time(21, 0) for s in sessions), \
        [(s.start.time(), s.end.time()) for s in sessions]


def test_without_late_from_nothing_changes():
    cfg = sched(late_from=None)
    busy = [Interval(at(12, 17), at(12, 19, 20))]
    sessions = scheduler.plan(cfg, "a", "Essay", "T", 60, None, at(15, 8, 30),
                              busy, at(12, 16), TZ)
    assert sessions[0].start == at(12, 19, 30)


# ------------------------------------------------------------ config edits

CONFIG = """\
# my settings
scheduling:
  windows:
    monday:    {start: "17:00", end: "21:30"}   # after school
    saturday:  {start: "09:30", end: "18:00"}
  teacher_keywords:
    Archer:    ["archer"]
travel:
  home:
    postcode: "EC1A 1BB"
  places: {}
  times:                    # door to door
    home-school: 75
  default_mode: "public"
web:
  port: 8080
"""


def _apply(tmp_path, changes, text=CONFIG):
    if not (ROOT / "deploy" / "configedit.py").exists():
        pytest.skip("deploy/ ships only in 2-deploy-on-mini")
    path = tmp_path / "config.yaml"
    path.write_text(text)
    plan = tmp_path / "changes.json"
    plan.write_text(json.dumps(changes))
    result = subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "configedit.py"), str(path),
         "apply", str(plan)], capture_output=True, text=True)
    return result, path.read_text()


def test_one_field_of_a_one_line_mapping_is_changed(tmp_path):
    result, text = _apply(tmp_path, [
        {"set": "scheduling.windows.monday.end", "value": "22:00"}])
    assert result.returncode == 0, result.stderr
    assert 'monday:    {start: "17:00", end: "22:00"}   # after school' in text
    assert yaml.safe_load(text)["scheduling"]["windows"]["saturday"]["end"] == "18:00"


def test_missing_settings_are_added_in_their_section(tmp_path):
    result, text = _apply(tmp_path, [
        {"ensure": "scheduling.late_from", "value": "20:00"},
        {"ensure": "travel.car_times.home-E1 6AN", "value": 45},
        {"ensure": "travel.places.piano", "value": {"postcode": "EC1A 9AA"}}])
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load(text)
    assert data["scheduling"]["late_from"] == "20:00"
    assert data["travel"]["car_times"] == {"home-E1 6AN": 45}
    assert data["travel"]["places"] == {"piano": {"postcode": "EC1A 9AA"}}
    assert data["web"]["port"] == 8080
    assert text.startswith("# my settings") and "# door to door" in text, \
        "comments were lost"


def test_a_value_you_set_is_never_overwritten_by_ensure(tmp_path):
    result, text = _apply(tmp_path, [
        {"ensure": "travel.default_mode", "value": "car"}])
    assert "kept" in result.stdout
    assert yaml.safe_load(text)["travel"]["default_mode"] == "public"


def test_applying_twice_changes_nothing(tmp_path):
    changes = [{"set": "scheduling.windows.monday.end", "value": "22:00"},
               {"ensure": "travel.car_times.home-E1 6AN", "value": 45}]
    _, once = _apply(tmp_path, changes)
    result, twice = _apply(tmp_path, changes, text=once)
    assert once == twice
    assert not [line for line in result.stdout.splitlines()
                if line.startswith(("changed", "added"))]


def test_a_setting_for_a_section_you_do_not_have_is_skipped(tmp_path):
    no_travel = CONFIG.split("travel:")[0] + "web:\n  port: 8080\n"
    result, text = _apply(tmp_path, [
        {"set": "travel.default_mode", "value": "car"},
        {"set": "scheduling.windows.monday.end", "value": "22:00"}],
        text=no_travel)
    assert result.returncode == 0
    assert "skipped" in result.stdout
    assert "22:00" in text


def test_the_planning_check_refuses_a_backwards_window(tmp_path):
    data = yaml.safe_load(CONFIG)
    assert config_module.check_planning(data)
    data["scheduling"]["windows"]["monday"]["end"] = "16:00"
    with pytest.raises(RuntimeError):
        config_module.check_planning(data)


def test_the_release_settings_apply_to_the_shipped_config(tmp_path):
    """The real config-changes.json against the example config: every
    change lands, and the result loads."""
    changes = ROOT.parent / "release" / "config-changes.json"
    if not changes.exists():
        pytest.skip("no release folder next to this copy")
    result, text = _apply(tmp_path, json.loads(changes.read_text()),
                          text=(ROOT / "config.example.yaml").read_text())
    assert result.returncode == 0, result.stderr
    assert "skipped" not in result.stdout, result.stdout
    assert config_module.check_planning(yaml.safe_load(text))


# ------------------------------------------------------------ the dashboard

def test_the_dashboard_asks_how_you_get_there(cfg):
    from prep.web import create_app
    calendar = FakeCalendar()
    client = create_app(cfg, calendar_factory=lambda: calendar).test_client()
    page = client.get("/?token=t0ken").get_data(as_text=True)
    assert 'name="travel"' in page
    assert '<option value="car" selected' in page

    client.post("/outside", data={
        "title": "Piano", "on_date": "2026-10-13", "start_time": "17:00",
        "end_time": "18:00", "location": LESSON, "travel": "public",
        "token": "t0ken"})
    assert calendar.diary[-1].travel == "public"

    client.post("/blackout", data={
        "on_date": "2026-10-14", "start_time": "17:00", "end_time": "18:00",
        "label": "Swim", "location": LESSON, "travel": "car", "token": "t0ken"})
    store = Store(cfg.db_path)
    assert store.blackouts()[0]["travel"] == "car"
    store.close()
    page = client.get("/?token=t0ken").get_data(as_text=True)
    assert f"{LESSON} (car)" in page
