"""Tests for the three phases. These stay offline: a saved dump, a fake calendar.

Phase A is itself a test harness, so these are tests of the harness - that it
notices what it claims to notice, and that it refuses to report success when it
has not actually checked anything.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prep import phases


@pytest.fixture
def site(tmp_path: Path):
    (tmp_path / "dumps").mkdir()
    (tmp_path / "dumps" / "payloads-20260925T120000Z.json").write_text(json.dumps([{
        "url": "https://teams.microsoft.com/api/assignments/v1/list",
        "body": {"value": [
            {"id": "a" * 36, "classId": "maths-cls", "displayName": "AAA Prep 5",
             "assignedDateTime": "2026-09-24T09:00:00Z",
             "dueDateTime": "2099-10-02T07:00:00Z",
             "createdBy": {"user": {"id": "t-aaa"}}},
            {"id": "c" * 36, "classId": "latin-cls", "displayName": "Latin vocab",
             "dueDateTime": "2020-04-29T08:00:00Z",
             "createdBy": {"user": {"id": "t-lat"}}},
            {"id": "e" * 36, "classId": "mystery-cls", "displayName": "Unnamed",
             "assignedDateTime": "2026-09-24T09:00:00Z",
             "dueDateTime": "2099-10-03T08:00:00Z"},
        ]}}]))
    (tmp_path / "teachers.json").write_text(json.dumps(
        {"class_map": {"maths-cls": "Archer"}, "title_codes": {},
         "people_map": {}, "archived": {}}))
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/dumps
export_path: {tmp_path}/p.txt
learned_map_path: {tmp_path}/teachers.json
timezone: Europe/London
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
web: {{token: ""}}
scheduling:
  windows:
    monday:    {{start: "17:00", end: "21:30"}}
    tuesday:   {{start: "17:00", end: "21:30"}}
    wednesday: {{start: "17:00", end: "21:30"}}
    thursday:  {{start: "17:00", end: "21:30"}}
    friday:    {{start: "17:00", end: "21:00"}}
    saturday:  {{start: "09:30", end: "18:00"}}
    sunday:    {{start: "09:30", end: "20:00"}}
""")
    return tmp_path


def run_c(site: Path, monkeypatch, calendar=None, extra=()):
    """Phase C with the calendar swapped for a fake."""
    from prep.scheduler import FakeCalendar
    made = calendar if calendar is not None else FakeCalendar()
    monkeypatch.setattr(phases, "CalDAVCalendar", lambda cfg, tz: made)
    code = phases.main(["C", "--config", str(site / "config.yaml"),
                        "--no-refresh", *extra])
    return code, made


# ------------------------------------------------------------------ phase A

def test_phase_a_needs_no_config_credentials_or_network(capsys):
    """The property that makes Phase A worth running first."""
    code = phases.main(["A", "--no-unit-tests", "--quiet"])
    out = capsys.readouterr().out
    assert code == 0, out[-3000:]
    assert "no credentials, no network" in out


def test_phase_a_proves_a_shared_class_splits_by_creator(capsys):
    phases.main(["A", "--no-unit-tests", "--quiet"])
    out = capsys.readouterr().out
    assert "[ ok ] A shared class separated into two teachers" in out


def test_phase_a_checks_the_120_minute_cap_is_per_prep(capsys):
    phases.main(["A", "--no-unit-tests", "--quiet"])
    out = capsys.readouterr().out
    assert "[ ok ] A week's prep caps at 120 minutes" in out
    assert "[ ok ] The cap is per prep, not per week" in out


def test_phase_a_shows_the_outlier_being_rejected(capsys):
    phases.main(["A", "--no-unit-tests", "--quiet"])
    out = capsys.readouterr().out
    assert "[ ok ] The freak evening was damped, not averaged in" in out
    assert "damped" in out


def test_phase_a_fails_loudly_if_an_assertion_breaks(monkeypatch, capsys):
    """A harness that cannot fail is not testing anything.

    The archived class is removed from the learned map, so the prep from a
    finished class should be scheduled - which is the thing Phase A exists to
    catch.
    """
    real_build = phases.dummy.build

    def without_the_archive(*args, **kwargs):
        world = real_build(*args, **kwargs)
        world.learned["archived"] = {}
        return world

    monkeypatch.setattr(phases.dummy, "build", without_the_archive)
    code = phases.main(["A", "--no-unit-tests", "--quiet"])
    out = capsys.readouterr().out
    assert code == 1, "Phase A passed with the archive check disabled"
    assert "[FAIL] The archived class was not scheduled" in out


def test_phase_a_writes_only_to_its_own_scratch_directory(capsys):
    phases.main(["A", "--no-unit-tests", "--quiet"])
    out = capsys.readouterr().out
    scratch = [line for line in out.splitlines() if "scratch directory" in line]
    assert scratch, "Phase A did not say where it worked"
    assert "/prep-phase-a-" in scratch[0]


# ------------------------------------------------------------------ phase C

def test_phase_c_runs_offline_against_a_saved_dump(site, monkeypatch, capsys):
    code, _ = run_c(site, monkeypatch)
    out = capsys.readouterr().out
    assert "Assignments parsed" in out
    assert "3 prep" in out
    assert code in (0, 1)


def test_unresolved_teachers_are_surfaced(site, monkeypatch, capsys):
    run_c(site, monkeypatch)
    out = capsys.readouterr().out
    assert "unresolved" in out
    assert "mystery-cls" in out
    assert "prep.setup" in out, "no instruction on how to fix it"


def test_a_dormant_class_is_offered_for_archiving(site, monkeypatch, capsys):
    run_c(site, monkeypatch)
    out = capsys.readouterr().out
    assert "latin-cls" in out
    assert "no prep for" in out
    assert "--archive" in out, "no instruction on how to confirm it"


def test_a_dry_run_writes_nothing(site, monkeypatch, capsys):
    from prep.scheduler import FakeCalendar
    calendar = FakeCalendar()
    run_c(site, monkeypatch, calendar=calendar)
    assert calendar.events == {}, "a check without --write touched the calendar"
    assert "dry run" in capsys.readouterr().out


def test_write_actually_schedules(site, monkeypatch, capsys):
    from prep.scheduler import FakeCalendar
    calendar = FakeCalendar()
    run_c(site, monkeypatch, calendar=calendar, extra=("--write",))
    assert calendar.events, "--write scheduled nothing"


def test_an_unreachable_calendar_stops_rather_than_planning_blind(
        site, monkeypatch, capsys):
    """Planning against no diary would ignore every lesson you have."""
    def explode(cfg, tz):
        raise ConnectionError("caldav unreachable")
    monkeypatch.setattr(phases, "CalDAVCalendar", explode)

    code = phases.main(["C", "--config", str(site / "config.yaml"), "--no-refresh"])
    out = capsys.readouterr().out
    assert code == 1
    assert "Skipping the scheduling check" in out
    assert "Everything found a slot" not in out, \
        "reported a schedule built with no busy time"


def test_missing_dump_fails_clearly(tmp_path: Path, capsys):
    (tmp_path / "dumps").mkdir()
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/dumps
export_path: {tmp_path}/p.txt
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling: {{windows: {{monday: {{start: "17:00", end: "21:30"}}}}}}
""")
    code = phases.main(["C", "--config", str(tmp_path / "config.yaml"),
                        "--no-refresh"])
    assert code == 1
    assert "no dumps found" in capsys.readouterr().out


def test_a_stale_saved_capture_is_flagged(site, monkeypatch, capsys):
    import os
    dump = site / "dumps" / "payloads-20260925T120000Z.json"
    old = datetime.now(timezone.utc).timestamp() - 3 * 86400
    os.utime(dump, (old, old))
    run_c(site, monkeypatch)
    out = capsys.readouterr().out
    assert "[warn] Using saved capture" in out, "3-day-old capture passed silently"


def test_phase_c_checks_the_dashboard_boots(site, monkeypatch, capsys):
    run_c(site, monkeypatch)
    out = capsys.readouterr().out
    assert "Dashboard renders" in out


def test_phase_c_cross_checks_calendar_against_the_database(site, monkeypatch,
                                                            capsys):
    run_c(site, monkeypatch)
    out = capsys.readouterr().out
    assert "[ ok ] Calendar and scheduler agree" in out
    assert "[ ok ] Database recorded every booking" in out


def test_phase_c_tests_the_lock_not_the_cookie(site, monkeypatch, capsys):
    """The dashboard hands a cookie to whoever gets in with the token.

    Checking the lock with the same client that just unlocked it would prove
    only that cookies work.
    """
    path = site / "config.yaml"
    path.write_text(path.read_text().replace('web: {token: ""}',
                                             'web: {token: "s3cret"}'))
    run_c(site, monkeypatch)
    out = capsys.readouterr().out
    assert "[ ok ] Dashboard refuses a missing token" in out


def test_a_first_run_does_not_print_every_historical_prep(site, monkeypatch,
                                                          capsys):
    """A year of finished prep would bury the lines that matter."""
    dump = site / "dumps" / "payloads-20260925T120000Z.json"
    body = json.loads(dump.read_text())
    body[0]["body"]["value"] = [
        {"id": f"{index:036d}", "classId": "maths-cls",
         "displayName": f"AAA Prep {index}",
         "assignedDateTime": "2020-09-24T09:00:00Z",
         "dueDateTime": "2020-10-02T07:00:00Z",
         "createdBy": {"user": {"id": "t-aaa"}}}
        for index in range(60)
    ]
    dump.write_text(json.dumps(body))
    run_c(site, monkeypatch)
    out = capsys.readouterr().out
    assert "and 45 more new" in out
    assert out.count("NEW Archer") <= 15


# ------------------------------------------------------------------ phase B

def run_b(site: Path, monkeypatch, extra=()):
    from prep.scheduler import FakeCalendar
    monkeypatch.setattr(phases, "CalDAVCalendar",
                        lambda cfg, tz: FakeCalendar())
    code = phases.main(["B", "--config", str(site / "config.yaml"),
                        "--no-refresh", *extra])
    return code


def test_phase_b_lists_what_is_currently_due(site, monkeypatch, capsys):
    run_b(site, monkeypatch)
    out = capsys.readouterr().out
    assert "your current prep" in out
    assert "AAA Prep 5" in out, "the prep still to come was not listed"
    assert "Latin vocab" not in out, "a prep that is already past was listed"


def test_phase_b_does_not_claim_a_session_it_never_tested(site, monkeypatch,
                                                          capsys):
    """Reading a file proves nothing about the credentials."""
    run_b(site, monkeypatch)
    out = capsys.readouterr().out
    assert "[warn] Teams session alive - NOT TESTED" in out
    assert "[ ok ] Teams session alive" not in out


def test_a_missing_browser_is_caught_before_the_capture(site, monkeypatch,
                                                        capsys):
    """"Failed to launch" is a wall of text. Say so up front instead."""
    path = site / "config.yaml"
    path.write_text(path.read_text().replace(
        "profile_dir: " + str(site) + "/prof",
        "profile_dir: " + str(site) + "/prof, chromium_path: /nope/chromium"))
    run_b(site, monkeypatch)
    out = capsys.readouterr().out
    assert "[FAIL] Chromium available - /nope/chromium is not there" in out
    assert "chromium_path in your config points at nothing" in out


def test_a_launch_failure_keeps_the_path_it_looked_in(site, monkeypatch,
                                                      capsys):
    """The path is the whole diagnosis, and it is not in the first 80 chars."""
    def explode(cfg, dump=False, notes=None):
        raise RuntimeError(
            "BrowserType.launch_persistent_context: Failed to launch chromium "
            "because executable doesn't exist at\n"
            "/home/user/.cache/ms-playwright/chromium-1140/chrome-linux/chrome")

    monkeypatch.setattr(phases, "capture", explode)
    from prep.scheduler import FakeCalendar
    monkeypatch.setattr(phases, "CalDAVCalendar", lambda cfg, tz: FakeCalendar())
    phases.main(["B", "--config", str(site / "config.yaml")])
    out = capsys.readouterr().out
    assert "ms-playwright" in out, "the path was truncated away again"
    assert "playwright install --with-deps chromium" in out, "no way forward offered"
    # Ubuntu's apt has no chromium package; that must not be the first advice.
    assert out.index("playwright install") < out.index("apt install -y chromium")


def test_phase_b_leaves_the_database_alone(site, monkeypatch):
    """Phase B is a credentials test. It must not schedule or record anything."""
    from prep.store import Store
    run_b(site, monkeypatch)
    store = Store(site / "p.db")
    assert store.all_sessions() == []
    assert store.upcoming() == [], "Phase B wrote assignments to the database"
    store.close()


# ------------------------------------------------------------------ the CLI

def test_b_and_c_refuse_to_run_without_a_config(capsys):
    assert phases.main(["B"]) == 2
    assert "need --config" in capsys.readouterr().out


def test_all_stops_at_the_first_failing_phase(site, monkeypatch, capsys):
    """If the logic is broken there is no point testing the credentials."""
    later_ran = []
    monkeypatch.setattr(phases, "phase_a", lambda args: _failed("A"))
    monkeypatch.setattr(phases, "phase_b",
                        lambda args, cfg: later_ran.append("B") or _passed("B"))
    monkeypatch.setattr(phases, "phase_c",
                        lambda args, cfg: later_ran.append("C") or _passed("C"))

    code = phases.main(["all", "--config", str(site / "config.yaml")])
    out = capsys.readouterr().out
    assert code == 1
    assert later_ran == [], f"ran {later_ran} after phase A failed"
    assert "Stopping: phase A failed" in out


def test_all_continues_past_a_warning(site, monkeypatch, capsys):
    """A warning is something to look at, not a reason to stop testing."""
    ran = []
    monkeypatch.setattr(phases, "phase_a", lambda args: _warned("A"))
    monkeypatch.setattr(phases, "phase_b",
                        lambda args, cfg: ran.append("B") or _passed("B"))
    monkeypatch.setattr(phases, "phase_c",
                        lambda args, cfg: ran.append("C") or _passed("C"))

    code = phases.main(["all", "--config", str(site / "config.yaml")])
    assert code == 0
    assert ran == ["B", "C"]


def _passed(name: str) -> phases.Report:
    report = phases.Report(name)
    report.add(phases.OK, "pretend success")
    return report


def _warned(name: str) -> phases.Report:
    report = phases.Report(name)
    report.add(phases.WARN, "pretend warning")
    return report


def _failed(name: str) -> phases.Report:
    report = phases.Report(name)
    report.add(phases.FAIL, "pretend failure")
    return report


def test_the_unit_suite_needs_no_network_or_credentials():
    """The property that makes pytest worth running at all."""
    import subprocess, sys
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only"],
        capture_output=True, text=True, cwd=root,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})
    assert result.returncode == 0, "collection needs environment it should not"


# ------------------------------------------------------- secret handling

def test_a_literal_account_password_in_the_config_is_a_warning(site, monkeypatch,
                                                               capsys):
    """Flagged, because it ends up in a backup, a screenshot, or a repository -
    but a warning, so a test machine with a private config can still run B."""
    run_b(site, monkeypatch)
    out = capsys.readouterr().out
    assert "[warn] No account secret is written into the config" in out
    assert "[FAIL] No account secret" not in out
    assert "calendar.password" in out


def test_every_check_has_a_stable_number(capsys):
    """'B.11' must mean the same check on every run, whatever failed first."""
    import ast
    source = (Path(phases.__file__)).read_text()
    names = {}
    for fn in ast.parse(source).body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("add", "check")
                    and getattr(node.func.value, "id", "") == "report"):
                arg = node.args[1]
                text = (arg.value if isinstance(arg, ast.Constant)
                        else "".join(v.value if isinstance(v, ast.Constant) else "x"
                                     for v in arg.values))
                names[text] = fn.name
    numbered = {phases._bare(n) for checks in phases.CHECKS.values() for n in checks}
    missing = [n for n in names if phases._bare(n) not in numbered]
    assert not missing, f"add these to phases.CHECKS: {missing}"
    assert phases.check_id("B", "Feed 'socs' readable") != "B.?"

    phases.main(["--list-checks"])
    assert "B.9   No account secret is written into the config" in capsys.readouterr().out


def test_a_literal_app_token_is_only_a_warning(site, monkeypatch, capsys):
    """Proportion matters: a check that fails the run over a self-generated
    token in a gitignored file is one you learn to ignore."""
    path = site / "config.yaml"
    path.write_text(path.read_text()
        .replace('web: {token: ""}', 'web: {token: "a-self-generated-token"}')
        .replace("calendar: {caldav_url: x, username: u, password: p, "
                 "prep_calendar: Prep}",
                 "calendar:\n"
                 "  caldav_url: x\n"
                 "  username: env:IU\n"
                 "  password: env:IP\n"
                 "  prep_calendar: Prep"))
    monkeypatch.setenv("IU", "a@b.c")
    monkeypatch.setenv("IP", "x")
    run_b(site, monkeypatch)
    out = capsys.readouterr().out
    assert "[warn] No secret is written into the config" in out
    assert "[FAIL] No account secret" not in out


def test_env_secrets_are_flagged_as_improvable(site, monkeypatch, capsys):
    path = site / "config.yaml"
    path.write_text(path.read_text().replace(
        "calendar: {caldav_url: x, username: u, password: p, prep_calendar: Prep}",
        "calendar:\n"
        "  caldav_url: x\n"
        "  username: env:ICLOUD_USER\n"
        "  password: env:ICLOUD_PW\n"
        "  prep_calendar: Prep"))
    monkeypatch.setenv("ICLOUD_USER", "someone@icloud.com")
    monkeypatch.setenv("ICLOUD_PW", "abcd-efgh")
    run_b(site, monkeypatch)
    out = capsys.readouterr().out
    assert "in plaintext .env" in out
    assert "setup-credentials.sh" in out, "no way forward offered"


def test_credentials_are_reported_as_sound(site, monkeypatch, capsys, tmp_path):
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "ICLOUD_USER").write_text("someone@icloud.com")
    (creds / "ICLOUD_PW").write_text("abcd-efgh")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))

    path = site / "config.yaml"
    path.write_text(path.read_text().replace(
        "calendar: {caldav_url: x, username: u, password: p, prep_calendar: Prep}",
        "calendar:\n"
        "  caldav_url: x\n"
        "  username: cred:ICLOUD_USER\n"
        "  password: cred:ICLOUD_PW\n"
        "  prep_calendar: Prep"))
    run_b(site, monkeypatch)
    out = capsys.readouterr().out
    assert "[ ok ] Secrets come from systemd credentials" in out
    assert "none in .env" in out


def test_phase_c_routes_the_days_when_travel_is_set_up(site, monkeypatch, capsys):
    from prep.scheduler import DiaryEvent, FakeCalendar
    from zoneinfo import ZoneInfo

    with open(site / "config.yaml", "a") as handle:
        handle.write(f"""
travel:
  home: {{postcode: "EC1A 1BB"}}
  school: {{name: Example School, postcode: "SW1A 0AA",
           calendars: [Timetable], work_until: "19:00"}}
  times: {{home-school: 75}}
  lookup_postcodes: false
  cache_path: {site}/places.json
""")
    tz = ZoneInfo("Europe/London")
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    lessons = DiaryEvent(datetime.combine(tomorrow, datetime.min.time(), tzinfo=tz)
                         .replace(hour=9),
                         datetime.combine(tomorrow, datetime.min.time(), tzinfo=tz)
                         .replace(hour=15, minute=55),
                         "Lessons", "Y08", "Timetable")
    piano = DiaryEvent(lessons.start.replace(hour=18), lessons.start.replace(hour=19),
                       "Piano", "", "Personal")
    run_c(site, monkeypatch, calendar=FakeCalendar(diary=[lessons, piano]))
    out = capsys.readouterr().out
    assert "--- travel ---" in out
    assert "C.33  [ ok ] Home and school found" in out
    assert "75 min apart" in out
    assert "C.34  [warn] Every outside commitment has a place" in out
    assert "Piano (no location)" in out
    assert "C.36  [ ok ] Nothing booked in transit" in out
