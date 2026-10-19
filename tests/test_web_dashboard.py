"""Dashboard behaviour: corrections, blackouts, and confidence display."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta
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


@pytest.fixture(autouse=True)
def _keep_fixture_prep_on_the_page(monkeypatch):
    # The fixtures are prep due on fixed September dates, read against the
    # real clock. Past-due prep leaves the dashboard after a week, so without
    # this they would vanish from the page as the calendar moves on.
    monkeypatch.setattr("prep.store.PAST_DUE_DAYS", 100000)


@pytest.fixture
def site(tmp_path: Path):
    (tmp_path / "payloads.json").write_text(json.dumps([{
        "url": "assignments", "body": {"value": [
            {"id": "1" * 36, "classId": "phys", "displayName": "Kinematics sheet",
             "assignedDateTime": "2026-09-21T09:00:00Z",
             "dueDateTime": "2026-09-25T08:30:00Z"},
            {"id": "2" * 36, "classId": "chem", "displayName": "Titration writeup",
             "assignedDateTime": "2026-09-21T09:00:00Z",
             "dueDateTime": "2026-09-24T08:30:00Z"},
        ]}}]))
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/d
export_path: {tmp_path}/p.txt
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
  teacher_keywords: {{Dalton: ["kinematics"], Garner: ["titration"]}}
web:
  token: "t0ken"
""")
    cfg = config_module.load(tmp_path / "config.yaml")
    store = Store(cfg.db_path)
    changes = store.upsert(parse(load_payload_file(tmp_path / "payloads.json")),
                           pipeline.teacher_matcher(cfg))
    pipeline.reschedule(cfg, store, FakeCalendar(), changes,
                        now=datetime(2026, 9, 21, 16, 0, tzinfo=TZ))
    store.close()
    return cfg


def client_for(cfg):
    return create_app(cfg, calendar_factory=lambda: FakeCalendar()).test_client()


def test_page_renders_with_confidence_words_not_just_colour(site):
    page = client_for(site).get("/?token=t0ken")
    assert page.status_code == 200
    body = page.data.decode()
    # A status colour must never carry meaning alone.
    assert "guess only" in body or "no timings yet" in body
    assert "Kinematics sheet" in body
    assert "Titration writeup" in body


def test_estimate_and_source_are_recorded_for_display(site):
    store = Store(site.db_path)
    row = store.assignment("1" * 36)
    assert row["estimate_minutes"] and row["estimate_source"]
    store.close()


def test_correcting_a_prediction_changes_the_schedule(site):
    client = client_for(site)
    posted = client.post("/override", data={"assignment_id": "1" * 36,
                                            "minutes": "45", "token": "t0ken"})
    assert posted.status_code == 302

    store = Store(site.db_path)
    assert store.assignment("1" * 36)["override_minutes"] == 45

    # The correction must actually drive the next reschedule.
    from prep.store import Change
    row = store.assignment("1" * 36)
    change = Change("due_changed", "1" * 36, row["label"], row["teacher"],
                    row["due_at"], row["due_at"])
    calendar = FakeCalendar()
    outcomes = pipeline.reschedule(site, store, calendar, [change],
                                   now=datetime(2026, 9, 21, 16, 0, tzinfo=TZ))
    assert outcomes[0].minutes == 45
    assert outcomes[0].estimate_source == "corrected"
    store.close()


def test_clearing_a_correction_restores_the_prediction(site):
    client = client_for(site)
    client.post("/override", data={"assignment_id": "1" * 36,
                                   "minutes": "45", "token": "t0ken"})
    client.post("/override", data={"assignment_id": "1" * 36,
                                   "minutes": "", "token": "t0ken"})
    store = Store(site.db_path)
    assert store.assignment("1" * 36)["override_minutes"] is None
    store.close()


def test_a_silly_correction_is_refused(site):
    client = client_for(site)
    assert client.post("/override", data={"assignment_id": "1" * 36,
                                          "minutes": "0",
                                          "token": "t0ken"}).status_code == 400
    assert client.post("/override", data={"assignment_id": "1" * 36,
                                          "minutes": "5000",
                                          "token": "t0ken"}).status_code == 400


def test_blackout_blocks_that_time_in_the_schedule(site):
    client = client_for(site)
    added = client.post("/blackout", data={
        "weekday": "0", "start_time": "17:00", "end_time": "21:30",
        "label": "Brass band", "token": "t0ken"})
    assert added.status_code == 302

    store = Store(site.db_path)
    from prep.store import Change
    rows = [store.assignment("1" * 36), store.assignment("2" * 36)]
    changes = [Change("due_changed", r["assignment_id"], r["label"],
                      r["teacher"] or "", r["due_at"], r["due_at"]) for r in rows]
    calendar = FakeCalendar()
    pipeline.reschedule(site, store, calendar, changes,
                        now=datetime(2026, 9, 21, 16, 0, tzinfo=TZ))

    monday = datetime(2026, 9, 21).date()
    for session in calendar.events.values():
        if session.start.date() == monday:
            assert not (session.start.time() < time(21, 30)
                        and session.end.time() > time(17, 0)), \
                "prep scheduled during a blacked-out evening"
    store.close()


def test_a_one_off_blackout_only_applies_to_its_date(site):
    store = Store(site.db_path)
    store.add_blackout("17:00", "21:30", on_date="2026-09-22", label="Concert")
    intervals = pipeline.blackout_intervals(
        store, datetime(2026, 9, 21, tzinfo=TZ), datetime(2026, 9, 28, tzinfo=TZ), TZ)
    assert len(intervals) == 1
    assert intervals[0].start.date().isoformat() == "2026-09-22"
    store.close()


def test_a_weekly_blackout_repeats(site):
    store = Store(site.db_path)
    store.add_blackout("19:15", "21:45", weekday=4, label="Brass band")
    intervals = pipeline.blackout_intervals(
        store, datetime(2026, 9, 21, tzinfo=TZ), datetime(2026, 10, 12, tzinfo=TZ), TZ)
    assert len(intervals) == 3                   # three Fridays in range
    assert all(i.start.weekday() == 4 for i in intervals)
    store.close()


def test_backwards_blackout_is_refused(site):
    store = Store(site.db_path)
    with pytest.raises(ValueError):
        store.add_blackout("21:00", "19:00", weekday=0)
    with pytest.raises(ValueError):
        store.add_blackout("19:00", "21:00")     # neither weekday nor date
    store.close()


def test_blackouts_can_be_removed(site):
    store = Store(site.db_path)
    blackout_id = store.add_blackout("19:00", "21:00", weekday=2, label="x")
    store.close()

    client = client_for(site)
    assert client.post(f"/blackout/{blackout_id}/delete",
                       data={"token": "t0ken"}).status_code == 302
    store = Store(site.db_path)
    assert store.blackouts() == []
    store.close()


def test_malformed_blackout_row_is_skipped_not_fatal(site):
    """A hand-edited database must not take the whole run down."""
    store = Store(site.db_path)
    store.conn.execute(
        "INSERT INTO blackouts (weekday, on_date, start_time, end_time, label, "
        "created_at) VALUES (0, NULL, 'not-a-time', '21:00', 'bad', '2026-01-01')")
    intervals = pipeline.blackout_intervals(
        store, datetime(2026, 9, 21, tzinfo=TZ), datetime(2026, 9, 24, tzinfo=TZ), TZ)
    assert intervals == []
    store.close()


def test_dashboard_still_works_under_a_prefix(site):
    site.web.url_prefix = "/prep"
    client = client_for(site)
    assert client.get("/?token=t0ken").status_code == 404
    page = client.get("/prep/?token=t0ken")
    assert page.status_code == 200
    # Forms must post to the prefixed path, or every button 404s.
    assert 'action="/prep/log"' in page.data.decode()


def test_confidence_updates_as_soon_as_times_are_logged(site):
    """The stored source is a snapshot; the badge must reflect what is known now."""
    from prep import estimator

    store = Store(site.db_path)
    assert estimator.confidence("Dalton", {}, None) == "pattern"
    assert estimator.confidence("Dalton", {}, 45.0) == "global"
    assert estimator.confidence("Dalton", {"Dalton": (2, 40.0)}, 45.0) == "shrunk"
    assert estimator.confidence("Dalton", {"Dalton": (6, 40.0)}, 45.0) == "measured"
    assert estimator.confidence("Dalton", {"Dalton": (6, 40.0)}, 45.0,
                                overridden=True) == "corrected"
    store.close()


def test_badge_changes_on_the_rendered_page_after_logging(site):
    client = client_for(site)
    before = client.get("/?token=t0ken").data.decode()
    assert "guess only" in before or "no timings yet" in before

    store = Store(site.db_path)
    for minutes in (40, 45, 38, 42, 44, 41):
        store.record_timing("1" * 36, minutes)
    store.close()

    after = client.get("/?token=t0ken").data.decode()
    assert "measured" in after, "logging six times did not update the badge"


def test_calibration_compares_prediction_against_reality(site):
    """The chart must plot each teacher's own prediction, not a shared average."""
    store = Store(site.db_path)
    store.record_estimate("1" * 36, 120, "pattern")
    store.record_estimate("2" * 36, 90, "pattern")
    store.record_timing("1" * 36, 40)
    store.record_timing("2" * 36, 85)

    rows = {r["teacher"]: r for r in store.calibration()}
    assert rows["Dalton"]["mean_predicted"] == 120
    assert rows["Dalton"]["mean_actual"] == 40
    assert rows["Garner"]["mean_predicted"] == 90
    assert rows["Garner"]["mean_actual"] == 85
    # Two teachers must not share a predicted value.
    assert rows["Dalton"]["mean_predicted"] != rows["Garner"]["mean_predicted"]
    store.close()


def test_calibration_uses_the_correction_when_one_exists(site):
    store = Store(site.db_path)
    store.record_estimate("1" * 36, 120, "pattern")
    store.set_override("1" * 36, 45)
    store.record_timing("1" * 36, 47)
    row = {r["teacher"]: r for r in store.calibration()}["Dalton"]
    assert row["mean_predicted"] == 45, "the chart ignored your correction"
    store.close()


def test_unlogged_preps_are_excluded_from_calibration(site):
    store = Store(site.db_path)
    store.record_estimate("1" * 36, 120, "pattern")
    store.record_estimate("2" * 36, 90, "pattern")
    store.record_timing("1" * 36, 40)
    assert len(store.calibration()) == 1
    store.close()


# ------------------------------------------------- Teams session panel

def test_the_dashboard_never_asks_for_credentials(site):
    """A page on your own box that asks for your school password is a
    phishing page you have trained yourself to trust. There must never be
    one, so this is asserted rather than left to good intentions."""
    page = client_for(site).get("/?token=t0ken").get_data(as_text=True).lower()
    assert 'type="password"' not in page
    assert "type='password'" not in page
    assert "<input" not in page.split("teams sign-in")[-1][:800], \
        "the sign-in panel grew an input field"


def test_an_expiring_session_is_surfaced(tmp_path, monkeypatch):
    """You can reach the dashboard from your phone. That is where you should
    find out, while there is still time to act."""
    from datetime import datetime, timedelta, timezone
    from prep import session as session_module, web

    def expiring(profile):
        return session_module.SessionHealth(
            found=True, total=1,
            microsoft=[session_module.Cookie(
                "ESTSAUTHPERSISTENT", "login.microsoftonline.com",
                datetime.now(timezone.utc) + timedelta(days=3))])

    monkeypatch.setattr(session_module, "read", expiring)
    cfg = _config(tmp_path)
    page = web.create_app(cfg).test_client().get("/").get_data(as_text=True)
    assert "Teams sign-in" in page
    assert "3 days left" in page
    assert "prep.login" in page, "no way forward offered"


def test_a_healthy_session_is_not_nagged_about(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from prep import session as session_module, web

    monkeypatch.setattr(session_module, "read", lambda profile:
        session_module.SessionHealth(
            found=True, total=1,
            microsoft=[session_module.Cookie(
                "ESTSAUTHPERSISTENT", "login.microsoftonline.com",
                datetime.now(timezone.utc) + timedelta(days=50))]))
    cfg = _config(tmp_path)
    page = web.create_app(cfg).test_client().get("/").get_data(as_text=True)
    assert "Teams sign-in" not in page


def test_an_unreadable_profile_does_not_break_the_page(tmp_path, monkeypatch):
    from prep import session as session_module, web

    def explode(profile):
        raise OSError("profile gone")
    monkeypatch.setattr(session_module, "read", explode)
    cfg = _config(tmp_path)
    response = web.create_app(cfg).test_client().get("/")
    assert response.status_code == 200


def _config(tmp_path):
    from prep import config as config_module
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/dumps
export_path: {tmp_path}/p.txt
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/profile}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
web: {{token: ""}}
scheduling: {{windows: {{monday: {{start: "17:00", end: "21:30"}}}}}}
""")
    return config_module.load(tmp_path / "config.yaml")


# ------------------------------------------------- deployment soundness

def _cfg_with(tmp_path, host="127.0.0.1", token=""):
    from prep import config as config_module
    (tmp_path / "c.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/dumps
export_path: {tmp_path}/p.txt
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/profile}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
web: {{host: "{host}", token: "{token}"}}
scheduling: {{windows: {{monday: {{start: "17:00", end: "21:30"}}}}}}
""")
    return config_module.load(tmp_path / "c.yaml")


def test_no_token_on_loopback_is_fine(tmp_path):
    from prep.web import token_warning
    assert token_warning(_cfg_with(tmp_path)) == ""


def test_no_token_bound_publicly_is_called_out(tmp_path):
    from prep.web import token_warning
    warning = token_warning(_cfg_with(tmp_path, host="0.0.0.0"))
    assert "no token set" in warning
    assert "0.0.0.0" in warning


def test_a_short_token_is_called_out(tmp_path):
    """The token is the whole security model, so its length is not cosmetic."""
    from prep.web import token_warning
    warning = token_warning(_cfg_with(tmp_path, host="0.0.0.0", token="hunter2"))
    assert "7 characters" in warning
    assert "secrets.token_urlsafe" in warning, "no way to generate a good one"


def test_a_strong_token_passes(tmp_path):
    from prep.web import token_warning
    import secrets
    assert token_warning(_cfg_with(tmp_path, host="0.0.0.0",
                                   token=secrets.token_urlsafe(32))) == ""


def test_the_gunicorn_factory_needs_to_be_told_where_the_config_is(monkeypatch):
    from prep import web
    monkeypatch.delenv("PREP_CONFIG", raising=False)
    with pytest.raises(RuntimeError, match="PREP_CONFIG"):
        web.from_env()


def test_the_gunicorn_factory_builds_an_app(tmp_path, monkeypatch):
    from prep import web
    _cfg_with(tmp_path)
    monkeypatch.setenv("PREP_CONFIG", str(tmp_path / "c.yaml"))
    assert web.from_env().test_client().get("/").status_code == 200


def test_task_with_no_free_slot_shows_in_backlog(site):
    """A saved task that could not be booked must still be visible."""
    store = Store(site.db_path)
    # Due in the past relative to the real clock: never schedulable.
    task_id = store.add_task("UCAS statement draft", "2026-09-22T17:00:00+01:00")
    store.close()

    body = client_for(site).get("/?token=t0ken").data.decode()
    assert "<h2>Backlog</h2>" in body
    backlog = body[body.index("<h2>Backlog</h2>"):body.index("<h2>Upcoming prep</h2>")]
    upcoming = body[body.index("<h2>Upcoming prep</h2>"):]
    assert "UCAS statement draft" in backlog
    assert "UCAS statement draft" not in upcoming          # not listed twice
    assert f"/task/{task_id}/delete" in backlog             # still removable
    assert 'name="token" value="t0ken"' in backlog


def test_no_backlog_section_when_everything_is_booked(site):
    body = client_for(site).get("/?token=t0ken").data.decode()
    assert "<h2>Backlog</h2>" not in body


def test_dates_read_day_month_year_in_local_time(site):
    body = client_for(site).get("/?token=t0ken").data.decode()
    # Stored as 2026-09-25T08:30:00Z; London is on BST, so 09:30.
    assert "25/09/2026 09:30" in body
    assert "2026-09-25" not in body


def test_past_days_leave_the_scheduled_sessions(site):
    store = Store(site.db_path)
    store.conn.execute("DELETE FROM sessions")
    today = datetime.now(TZ).replace(hour=19, minute=0, second=0, microsecond=0)
    for uid, when in (("old", today - timedelta(days=1)), ("now", today),
                      ("next", today + timedelta(days=1))):
        store.record_session(uid, "1" * 36, when.isoformat(),
                             (when + timedelta(minutes=30)).isoformat(), 30)
    store.conn.commit()
    store.close()
    body = client_for(site).get("/?token=t0ken").data.decode()
    diary = body[body.index("Scheduled sessions"):]
    assert (today - timedelta(days=1)).strftime("%d/%m/%Y") not in diary
    assert today.strftime("%d/%m/%Y") in diary
    assert (today + timedelta(days=1)).strftime("%d/%m/%Y") in diary


def test_scheduled_hours_count_only_what_is_still_to_come(site):
    store = Store(site.db_path)
    store.conn.execute("DELETE FROM sessions")
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    # Finished an hour ago, then 90 minutes still ahead.
    store.record_session("done", "1" * 36, (now - timedelta(hours=2)).isoformat(),
                         (now - timedelta(hours=1)).isoformat(), 60)
    store.record_session("ahead", "1" * 36, (now + timedelta(hours=1)).isoformat(),
                         (now + timedelta(hours=2, minutes=30)).isoformat(), 90)
    store.conn.commit()
    store.close()
    body = client_for(site).get("/?token=t0ken").data.decode()
    tile = body[:body.index('<div class="k">scheduled</div>')]
    assert tile.rsplit('<div class="n">', 1)[1].startswith("1.5")
