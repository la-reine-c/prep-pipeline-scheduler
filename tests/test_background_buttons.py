"""Every button answers at once; the slow part happens in the background.

The calendar here is held up on purpose: if a button waited for it, the
request would not come back until the test let it go.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from prep import background
from prep import config as config_module
from prep.scheduler import FakeCalendar
from prep.store import Store
from prep.web import create_app


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


class HeldCalendar:
    """A calendar factory that blocks until released."""

    def __init__(self):
        self.release = threading.Event()
        self.calendar = FakeCalendar()

    def __call__(self):
        assert self.release.wait(10), "the test never released the calendar"
        return self.calendar


def _wait_until_idle(client, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = client.get("/planning?token=t0ken").get_json()
        if not state["running"]:
            return state
        time.sleep(0.05)
    raise AssertionError("background work never finished")


def _task_form(**extra):
    from datetime import date, timedelta
    form = {"token": "t0ken", "title": "Essay", "minutes": "60",
            "due_date": (date.today() + timedelta(days=5)).isoformat()}
    form.update(extra)
    return form


def test_adding_a_task_answers_before_the_calendar_does(cfg):
    held = HeldCalendar()
    client = create_app(cfg, calendar_factory=held, background=True).test_client()

    started = time.time()
    response = client.post("/task", data=_task_form())
    assert response.status_code == 302
    assert time.time() - started < 2, "the button waited for the calendar"

    # The page says it is working on it, and polls.
    page = client.get("/?token=t0ken").get_data(as_text=True)
    assert "Scheduling Essay" in page and "/planning" in page

    held.release.set()
    state = _wait_until_idle(client)
    assert state["last"]["what"] == "Scheduling Essay"
    assert held.calendar.events, "the task was never booked"

    # Once done, the page shows what happened instead of "working".
    page = client.get("/?token=t0ken&notice=" + state["last"]["notice"]).get_data(as_text=True)
    assert 'id="working"' not in page


def test_a_blackout_is_saved_at_once_and_replanned_later(cfg):
    held = HeldCalendar()
    client = create_app(cfg, calendar_factory=held, background=True).test_client()
    response = client.post("/blackout", data={
        "token": "t0ken", "weekday": "0", "start_time": "19:00",
        "end_time": "21:00", "label": "Orchestra"})
    assert response.status_code == 302
    store = Store(cfg.db_path)
    assert [b["label"] for b in store.blackouts()] == ["Orchestra"]
    store.close()
    held.release.set()
    _wait_until_idle(client)


def test_a_bad_form_is_still_refused_straight_away(cfg):
    client = create_app(cfg, calendar_factory=HeldCalendar(),
                        background=True).test_client()
    response = client.post("/blackout", data={
        "token": "t0ken", "weekday": "0", "start_time": "21:00",
        "end_time": "19:00"})
    assert response.status_code == 400


def test_a_failed_job_says_so_and_the_next_one_runs(cfg):
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("iCloud is down")
        return FakeCalendar()

    client = create_app(cfg, calendar_factory=factory, background=True).test_client()
    client.post("/task", data=_task_form(title="First"))
    state = _wait_until_idle(client)
    assert "could not be reached" in state["last"]["notice"]

    client.post("/task", data=_task_form(title="Second"))
    state = _wait_until_idle(client)
    assert state["last"]["what"] == "Scheduling Second"


def test_the_status_needs_the_token(cfg):
    client = create_app(cfg, calendar_factory=FakeCalendar,
                        background=True).test_client()
    assert client.get("/planning").status_code == 403


def test_a_job_whose_process_died_is_not_shown_as_running(tmp_path):
    planner = background.Planner(tmp_path / "status.json")
    planner._update(lambda state: state["running"].__setitem__(
        "gone", {"what": "Re-planning", "since": time.time(), "pid": 2 ** 22 + 7}))
    planner._update(lambda state: state["running"].__setitem__(
        "here", {"what": "Updating", "since": time.time(), "pid": os.getpid()}))
    assert planner.status()["running"] == ["Updating"]
