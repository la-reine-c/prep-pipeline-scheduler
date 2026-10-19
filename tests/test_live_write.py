"""Book the prep Teams shows right now into the TEST calendar. Live, opt-in.

Skipped unless PREP_LIVE=1, so the offline suite stays offline:

    make live-write        # this test, then the dashboard on the same data

Why not just `make check-write`? Phase C only books prep that is new or
changed since the last run, and a dry run records its "dry-run-N" bookings in
the real database. After a few dry runs the database believes everything is
already booked, so a write books nothing. This test runs against its own
database in PREP_STATE_DIR (default ~/prep/live-test), wiped each run, so
every current prep is booked. Your real database and export are left alone.

It then proves the dashboard works on that data: the page renders against
the real calendar, and a blackout added through it re-plans everything in
iCloud with the diary still matching the database exactly - no doubles, no
strays. The blackout is removed again (another replan) before it finishes.

Steps, so a failure can be named:
    L.1  refuses anything but a test calendar; clears last run's bookings
    L.2  Phase C with --write (its own checks are C.1 ... C.36)
    L.3  bookings exist, are real, and the diary matches the database
    L.4  the dashboard renders against the real calendar
    L.5  a blackout added on the dashboard re-plans iCloud, no duplicates
    L.6  removing it re-plans again, no duplicates
    L.7  with travel set up: no booking is in transit or away from home
         and school, judged against your real calendars and timetable

It refuses to run unless prep_calendar has "test" in its name, and it removes
the bookings it made last time before booking again.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("PREP_LIVE") != "1",
    reason="live: signs into Teams and writes to iCloud. Set PREP_LIVE=1.")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("PREP_CONFIG", ROOT / "config.yaml"))
STATE = Path(os.environ.get("PREP_STATE_DIR", "~/prep/live-test")).expanduser()

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday"]


def _diary_matches_database(cfg, tz) -> tuple[set, set]:
    """Future bookings in the calendar and in the database, for comparing."""
    from prep.scheduler import CalDAVCalendar
    from prep.store import Store

    now = datetime.now(tz)
    store = Store(cfg.db_path)
    known = {row[0] for row in store.conn.execute(
        "SELECT assignment_id FROM assignments")}
    in_db = {row["uid"] for row in store.all_sessions()
             if datetime.fromisoformat(row["start_local"]) >= now}
    store.close()
    in_diary = {uid for uid, aid, start in CalDAVCalendar(cfg, tz).pipeline_events(
                    now, now + timedelta(days=60))
                if aid in known and start >= now}
    return in_diary, in_db


def test_current_prep_is_written_and_the_dashboard_replans_it():
    from prep import config as config_module
    from prep import phases
    from prep.scheduler import CalDAVCalendar
    from prep.store import Store
    from prep.web import create_app

    cfg = config_module.load(str(CONFIG))
    name = cfg.calendar.prep_calendar
    assert "test" in name.lower(), (
        f"L.1 prep_calendar is '{name}'. This test only writes to a calendar with "
        f"'test' in its name, so it can never fill your real diary.")

    # Its own state: every current prep is new, and nothing real is touched.
    STATE.mkdir(parents=True, exist_ok=True)
    for stale in STATE.glob("prep.sqlite3*"):
        stale.unlink()
    cfg.db_path = STATE / "prep.sqlite3"
    cfg.export_path = STATE / "prep.txt"

    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    backend = CalDAVCalendar(cfg, tz)
    stale = [uid for uid, _, _ in backend.pipeline_events(
                 now - timedelta(hours=1), now + timedelta(days=60))]
    if stale:
        backend.delete_by_uid(stale)
    print(f"\nL.1 [ ok ] cleared {len(stale)} earlier booking(s) from '{name}'")

    # ------------------------------------------------- book current prep
    args = argparse.Namespace(write=True, no_refresh=False, config=str(CONFIG))
    report = phases.phase_c(args, cfg)
    phases.finish([report])
    assert report.worst() != phases.FAIL, "L.2 Phase C failed - see the C.n lines above"

    store = Store(cfg.db_path)
    booked = [row for row in store.all_sessions()
              if datetime.fromisoformat(row["start_local"]) >= datetime.now(tz)]
    store.close()
    assert booked, "L.3 nothing was booked - is any prep due in the next 3 weeks?"
    assert not any(row["uid"].startswith("dry-run-") for row in booked), \
        "L.3 bookings were recorded as a dry run, so nothing was written"
    in_diary, in_db = _diary_matches_database(cfg, tz)
    assert in_diary == in_db, (f"L.3 after booking: {len(in_diary - in_db)} stray, "
                               f"{len(in_db - in_diary)} missing")
    print(f"\nL.3 [ ok ] {len(in_db)} session(s) booked on '{name}', "
          f"diary matches database")

    # ------------------------------------------------- the dashboard
    client = create_app(cfg).test_client()       # the real calendar
    token = cfg.web.token or ""
    page = client.get(f"{cfg.web.url_prefix}/?token={token}")
    assert page.status_code == 200, f"L.4 dashboard returned HTTP {page.status_code}"
    print("L.4 [ ok ] dashboard renders against the real calendar")

    first = datetime.fromisoformat(booked[0]["start_local"])
    response = client.post(f"{cfg.web.url_prefix}/blackout", data={
        "weekday": str(first.weekday()), "start_time": "00:00",
        "end_time": "23:59", "label": "live test", "token": token})
    assert response.status_code == 302, "L.5 the blackout was not accepted"
    notice = response.headers.get("Location", "")
    assert "nothing+was+moved" not in notice, f"L.5 {notice}"

    store = Store(cfg.db_path)
    moved = [row for row in store.all_sessions()
             if datetime.fromisoformat(row["start_local"]) >= datetime.now(tz)]
    blackout_id = store.blackouts()[0]["id"]
    store.close()
    assert all(datetime.fromisoformat(r["start_local"]).weekday() != first.weekday()
               for r in moved), "L.5 a booking was left in the blacked-out day"
    in_diary, in_db = _diary_matches_database(cfg, tz)
    assert in_diary == in_db, (f"L.5 after the blackout: {len(in_diary - in_db)} "
                               f"duplicate(s), {len(in_db - in_diary)} missing")
    print(f"L.5 [ ok ] blacked out {DAYS[first.weekday()]}s: {len(moved)} session(s) "
          f"re-planned, diary matches database")

    client.post(f"{cfg.web.url_prefix}/blackout/{blackout_id}/delete",
                data={"token": token})
    in_diary, in_db = _diary_matches_database(cfg, tz)
    assert in_diary == in_db, "L.6 after removing the blackout the diary disagrees"
    print(f"L.6 [ ok ] removed it again: {len(in_db)} session(s), diary matches database")

    # ------------------------------------------------- travel
    if cfg.travel is None:
        print("L.7 [skip] no travel section in the config")
        return
    from prep import pipeline
    now = datetime.now(tz)
    store = Store(cfg.db_path)
    _, routing = pipeline.diary_context(cfg, store, CalDAVCalendar(cfg, tz), now,
                                        now + timedelta(days=21), tz)
    stranded = []
    for row in store.all_sessions():
        start = datetime.fromisoformat(row["start_local"])
        end = datetime.fromisoformat(row["end_local"])
        day = start.astimezone(tz).date()
        if start >= now and day in routing.days and not any(
                w.start <= start and end <= w.end for w in routing.days[day].windows):
            stranded.append(f"{start:%a %d %b %H:%M} {row['label'][:30]}")
    store.close()
    for day, plan in sorted(routing.days.items())[:7]:
        for line in plan.lines:
            print(f"      {day:%a %d %b}  {line[:90]}")
    assert not stranded, f"L.7 booked while travelling or away: {stranded[:4]}"
    print(f"L.7 [ ok ] {len(routing.days)} day(s) routed, nothing booked in transit")
