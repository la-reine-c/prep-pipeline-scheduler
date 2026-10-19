"""Three phases of testing, each answering a different question.

    Phase A   Is the logic right?
              Dummy data, past assignments, no credentials, no network, no
              browser. Runs on a laptop on a train. If A fails, the code is
              wrong - nothing about your account or your wifi can be blamed.

    Phase B   Do the credentials work, and what does Teams say right now?
              Signs in, captures, and prints your current prep. Touches
              nothing else. If B fails, the code may be fine and a password,
              a session or a feed key has expired.

    Phase C   Do the parts work together on real data?
              Fresh capture -> parse -> teachers -> archiving -> timetable ->
              estimate -> schedule -> store -> export -> dashboard, checked
              end to end and against each other. A dry run unless you say
              --write.

Usage:

    python3 -m prep.phases A
    python3 -m prep.phases B --config config.yaml
    python3 -m prep.phases C --config config.yaml
    python3 -m prep.phases all --config config.yaml

B and C capture fresh prep from Teams every time, so a test always runs
against what Teams says now rather than yesterday's dump. `--no-refresh`
reuses the most recent dump when you are offline or deliberately re-examining
a capture.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as config_module
from . import dummy, estimator, identify, pipeline
from .scheduler import (CalDAVCalendar, CalendarAuthFailed,
                        CalendarUnreachable, DryRunCalendar, FakeCalendar,
                        Interval)
from .scraper import SessionExpired, capture, load_payload_file, parse
from .store import Store

log = logging.getLogger("prep.phases")

OK, WARN, FAIL = "ok", "warn", "FAIL"
MARK = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}

ROOT = Path(__file__).resolve().parent.parent

# Every check, numbered, so "B.11" means the same thing on every run whatever
# failed before it. Numbers come from this list, not from the order a run
# happens to reach them in. Add new checks at the END of their phase so the
# numbers people already know do not move. `--list-checks` prints it.
# A quoted part of a name ("Feed 'socs' readable") is ignored for matching.
CHECKS = {
    "A": [
        "Every dummy assignment parsed",
        "Identity came from the id, not a fallback key",
        "Dates survived parsing",
        "Creator id resolved every teacher",
        "A shared class separated into two teachers",
        "A prep with no creator was left unresolved",
        "A class silent for months was flagged",
        "Flagged as a question, not archived on its own",
        "An active class was not flagged",
        "The planted duplicate mapping was caught",
        "A week's prep caps at 120 minutes",
        "An overnight prep is 30 minutes",
        "A well-timed teacher is tracked, not guessed",
        "The freak evening was damped, not averaged in",
        "Booked time allows for the spread",
        "Even a tracked estimate respects the cap",
        "The cap is per prep, not per week",
        "Prep was scheduled",
        "The archived class was not scheduled",
        "Prep whose deadline has gone was skipped",
        "Every prep got all the time it needs",
        "Nothing was booked over a lesson or rehearsal",
        "No evening was overloaded",
        "Every session finishes before its deadline",
        "A time you cannot work stayed free",
        "Teacher patterns were learned",
        "A dead session still fills the diary",
        "Guessed blocks avoid lessons too",
        "Real prep withdraws the guesses",
        "The database agrees with what was booked",
        "A second run finds nothing new",
        "The export was written",
        "Unit tests",
    ],
    "B": [
        # configuration
        "Config file read",
        "Calendar credentials present",
        "Timetable feed configured",
        "Browser profile exists",
        "playwright installed",
        "Chromium available",
        "Third-party cookies allowed",
        "Session will last",
        "No account secret is written into the config",
        "No secret is written into the config",
        "Secrets come from systemd credentials",
        # Microsoft Teams
        "Capture succeeded",
        "A saved capture exists",
        "Using saved capture",
        "Teams session alive",
        "Reached the Assignments view",
        "Assignment list seen to the end",
        "Assignment data had stopped arriving",
        "Assignments parsed",
        "Every prep has its own id",
        "Ids came from Teams, not a fallback",
        "Teams sends who set each prep",
        # your current prep
        "Prep still to come",
        "Every prep has a teacher",
        # iCloud calendar
        "Calendar sign-in accepted",
        "Calendar reachable",
        "Busy time found in the next week",
        # timetable feed
        "Feed readable",
        "Feed has this week",
        # alerting
        "A way to reach you is configured",
        "Test alert delivered",
    ],
    "C": [
        # capture
        "Capture succeeded",
        "A saved capture exists",
        "Using saved capture",
        "Teams session alive",
        "Capture",
        "Reached the Assignments view",
        "Assignment list seen to the end",
        "Assignment data had stopped arriving",
        "Assignments parsed",
        "Still to come",
        # what changed
        "New prep since last run",
        "Due dates that moved",
        # who set what
        "Every live prep has a teacher",
        "No class looks finished",
        "Archived classes excluded",
        "No teacher mapped twice",
        # scheduling
        "Calendar sign-in accepted",
        "Calendar reachable",
        "Timetable feed readable",
        "Scheduling ran",
        "Everything found a slot",
        "Every prep got all the time it needs",
        # do the parts agree
        "Calendar and scheduler agree",
        "Database recorded every booking",
        "No evening exceeds the daily limit",
        "Export written",
        # dashboard
        "Dashboard renders",
        "Dashboard refuses a missing token",
        "Dashboard is protected",
        "Dashboard deployment is sound",
        # alerting
        "Report is a sane size",
        "A way to reach you is configured",
        # travel (added later, so numbered after everything above)
        "Home and school found",
        "Every outside commitment has a place",
        "Every journey can be made in time",
        "Nothing booked in transit",
    ],
}


def _bare(name: str) -> str:
    return re.sub(r"\s*'[^']*'", "", name).strip().lower()


def check_id(phase: str, name: str) -> str:
    """'B.11' for a known check; 'B.?' for one missing from CHECKS."""
    for number, known in enumerate(CHECKS.get(phase, []), 1):
        if _bare(known) == _bare(name):
            return f"{phase}.{number}"
    return f"{phase}.?"


def list_checks() -> None:
    for phase, names in CHECKS.items():
        print(f"Phase {phase}")
        for number, name in enumerate(names, 1):
            print(f"  {phase}.{number:<3} {name}")
        print()


class Report:
    """What was checked, what it said, and whether you can trust the result.

    Every line is printed as it happens - a phase that dies halfway still
    leaves you the part that worked.
    """

    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.lines: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.lines.append((status, name, detail))
        print(f"{check_id(self.phase, name):<5} {MARK[status]} {name}"
              + (f" - {detail}" if detail else ""), flush=True)

    def check(self, condition: bool, name: str, detail: str = "",
              soft: bool = False) -> bool:
        """Assert something, but keep going so you see every failure at once."""
        self.add(OK if condition else (WARN if soft else FAIL), name, detail)
        return bool(condition)

    def note(self, text: str) -> None:
        print(f"             {text}", flush=True)

    def worst(self) -> str:
        if any(s == FAIL for s, _, _ in self.lines):
            return FAIL
        if any(s == WARN for s, _, _ in self.lines):
            return WARN
        return OK


def heading(text: str) -> None:
    print(f"\n--- {text} ---", flush=True)


def finish(reports: list[Report]) -> int:
    print("\n" + "=" * 68)
    lines = [(r.phase, *line) for r in reports for line in r.lines]
    width = min(46, max((len(name) for _, _, name, _ in lines), default=10))
    for phase, status, name, detail in lines:
        print(f"  {check_id(phase, name):<5} {MARK[status]} "
              f"{name[:width]:<{width}}  {detail[:26]}")
    print("-" * 68)

    worst = max((r.worst() for r in reports),
                key=lambda s: {OK: 0, WARN: 1, FAIL: 2}[s])
    failed = [f"{r.phase}" for r in reports if r.worst() == FAIL]
    if worst == FAIL:
        print(f"  PHASE {', '.join(failed)} FAILED - see the [FAIL] lines above.")
        return 1
    if worst == WARN:
        print("  Passed, with some things wanting your attention.")
        return 0
    print("  All phases passed.")
    return 0


# ===========================================================  PHASE A
# Dummy data, past assignments, no credentials, no network, no browser.

def phase_a(args) -> Report:
    report = Report("A")
    print("=" * 68)
    print("PHASE A - dry run on dummy data (no credentials, no network)")
    print("=" * 68)

    now = datetime.now(timezone.utc)
    world = dummy.build(now)
    temp = Path(tempfile.mkdtemp(prefix="prep-phase-a-"))
    config_path = dummy.write_site(temp, world)
    cfg = config_module.load(config_path)
    tz = ZoneInfo(cfg.timezone)

    report.note(f"scratch directory {temp}")
    report.note(f"{len(dummy.TEACHERS)} invented teachers across "
                f"{len({t.class_id for t in dummy.TEACHERS})} shared classes, "
                f"{dummy.WEEKS_BACK} weeks of history")

    # ------------------------------------------------------- parsing
    heading("parsing")
    assignments = parse(world.payloads)
    report.check(len(assignments) == world.assignment_count,
                 "Every dummy assignment parsed",
                 f"{len(assignments)} of {world.assignment_count}")

    synthetic = [a for a in assignments if a.id_is_synthetic]
    report.check(not synthetic, "Identity came from the id, not a fallback key",
                 f"{len(synthetic)} synthetic" if synthetic else "all real ids")

    dated = [a for a in assignments if a.due_at and a.assigned_at]
    report.check(len(dated) >= len(assignments) - 1,
                 "Dates survived parsing", f"{len(dated)} with both dates")

    # ------------------------------------------------------- teachers
    heading("who set what")
    resolve = pipeline.teacher_matcher(cfg)
    resolved = {a.assignment_id: resolve(a) for a in assignments}

    wrong = []
    for teacher in dummy.TEACHERS:
        for assignment_id in world.ids_by_teacher[teacher.name]:
            if resolved.get(assignment_id) != teacher.name:
                wrong.append((teacher.name, resolved.get(assignment_id)))
    report.check(not wrong, "Creator id resolved every teacher",
                 f"{len(wrong)} wrong" if wrong else
                 f"{len(dummy.TEACHERS)} teachers, no mistakes")
    for expected, got in wrong[:4]:
        report.note(f"expected {expected}, got {got or '(nothing)'}")

    # The point of the creator id: two teachers, one class GUID. If the class
    # map were doing the work, both would come back as the same person.
    shared = [t for t in dummy.TEACHERS if t.class_id == "cls-maths"]
    names = {resolved[world.ids_by_teacher[t.name][0]] for t in shared}
    report.check(len(names) == len(shared),
                 "A shared class separated into two teachers",
                 f"cls-maths -> {', '.join(sorted(names))}")

    unresolved = [a for a in assignments
                  if not resolved[a.assignment_id]
                  and a.class_id not in cfg.scheduling.archived_classes]
    report.check(any(a.class_id == dummy.UNKNOWN_CLASS for a in unresolved),
                 "A prep with no creator was left unresolved",
                 "not guessed at, which would poison a teacher's history")

    # ------------------------------------------------------- archiving
    heading("archiving")
    by_class: dict[str, list] = defaultdict(list)
    for item in assignments:
        by_class[item.class_id or "(none)"].append(item)

    dormant = identify.summarise_class(by_class[dummy.DORMANT_CLASS], now=now)
    report.check(dormant["likely_archived"],
                 "A class silent for months was flagged",
                 dormant["archive_basis"])
    report.check(not dormant["stated_archived"],
                 "Flagged as a question, not archived on its own",
                 "waits for you to confirm")

    live_class = identify.summarise_class(by_class["cls-maths"], now=now)
    report.check(not live_class["likely_archived"],
                 "An active class was not flagged",
                 f"{live_class['current']} still to come")

    clashes = _duplicate_teachers(world.learned)
    report.check(bool(clashes), "The planted duplicate mapping was caught",
                 ", ".join(sorted(clashes)) or "none found")

    # ------------------------------------------------------- estimates
    heading("how long each prep takes")
    store = Store(cfg.db_path)
    changes = store.upsert(assignments, resolve)
    for assignment_id, minutes in world.timings:
        store.record_timing(assignment_id, minutes)

    stats = store.teacher_stats()
    history = store.teacher_timings()
    mu = store.global_mean()

    # The rule: 30 minutes per day between set and due, capped at
    # 120 per prep. Foster has never been timed, so the pattern is all there is.
    foster = next(t for t in dummy.TEACHERS if not t.logs)
    week_long = estimator.estimate(
        cfg.estimator, foster.name,
        now - timedelta(days=7), now + timedelta(days=1), {}, None)
    report.check(week_long.minutes == 120,
                 "A week's prep caps at 120 minutes",
                 f"{week_long.minutes} min ({week_long.source})")

    overnight = estimator.estimate(
        cfg.estimator, foster.name, now, now + timedelta(days=1), {}, None)
    report.check(overnight.minutes == 30,
                 "An overnight prep is 30 minutes",
                 f"{overnight.minutes} min")

    # Anomaly rejection. Dalton's history contains one 300-minute evening
    # among an hour's worth of work. A plain average would be dragged up by
    # roughly 30 minutes; the filter should barely move.
    dalton = next(t for t in dummy.TEACHERS if t.anomaly)
    logged = world.logged_by_teacher[dalton.name]
    plain_mean = sum(logged) / len(logged)
    clean = [m for m in logged if m != dalton.anomaly]
    clean_mean = sum(clean) / len(clean)
    tracked = estimator.estimate(
        cfg.estimator, dalton.name, now - timedelta(days=7),
        now + timedelta(days=1), stats, mu, history=history)

    report.check(tracked.source == "tracked",
                 "A well-timed teacher is tracked, not guessed",
                 tracked.detail)
    report.check(tracked.central < plain_mean,
                 "The freak evening was damped, not averaged in",
                 f"centre {tracked.central} min vs mean {plain_mean:.0f} "
                 f"(clean {clean_mean:.0f})")
    report.check(tracked.minutes >= tracked.central,
                 "Booked time allows for the spread",
                 f"books {tracked.minutes} around a centre of {tracked.central}")
    report.check(tracked.minutes <= cfg.estimator.cap_minutes,
                 "Even a tracked estimate respects the cap",
                 f"{tracked.minutes} <= {cfg.estimator.cap_minutes}")

    # The cap is 120 per prep, not 120 a week. Six teachers setting a week's
    # work must come to six estimates, not one shared budget.
    week = [estimator.estimate(cfg.estimator, t.name, now - timedelta(days=7),
                               now + timedelta(days=1), stats, mu,
                               history=history)
            for t in dummy.TEACHERS]
    report.check(all(e.minutes <= cfg.estimator.cap_minutes for e in week)
                 and sum(e.minutes for e in week) > cfg.estimator.cap_minutes,
                 "The cap is per prep, not per week",
                 f"{len(week)} preps totalling {sum(e.minutes for e in week)} min, "
                 f"none over {cfg.estimator.cap_minutes}")

    # ------------------------------------------------------- scheduling
    heading("scheduling around a full diary")
    store.add_blackout("19:00", "20:00", weekday=3, label="dummy blackout")
    calendar = FakeCalendar(world.busy)
    outcomes = pipeline.reschedule(cfg, store, calendar, changes, horizon_days=21)

    placed = [o for o in outcomes if o.sessions]
    skipped_past = [o for o in outcomes if "past due" in o.problem]
    skipped_archived = [o for o in outcomes if "archived" in o.problem]
    no_slot = [o for o in outcomes if "NO FREE SLOT" in o.problem]

    report.check(bool(placed), "Prep was scheduled",
                 f"{len(placed)} placed, {len(skipped_past)} past due, "
                 f"{len(skipped_archived)} archived")
    report.check(bool(skipped_archived) or not any(
        a.class_id == dummy.ARCHIVED_CLASS for a in assignments),
        "The archived class was not scheduled",
        f"{len(skipped_archived)} skipped")
    report.check(bool(skipped_past), "Prep whose deadline has gone was skipped",
                 f"{len(skipped_past)} skipped")

    short = _short_placements(placed)
    report.check(not short, "Every prep got all the time it needs",
                 f"{len(short)} placed short" if short else
                 f"{len(placed)} prep, none cut off", soft=True)
    for outcome, got, wanted in short[:3]:
        report.note(f"{outcome.change.label[:40]}: {got} of {wanted} min - "
                    f"the diary ran out before the deadline")

    sessions = [s for o in placed for s in o.sessions]
    busy_merged = _merge(world.busy)
    clashing = [s for s in sessions
                if any(s.start < b.end and s.end > b.start for b in busy_merged)]
    report.check(not clashing, "Nothing was booked over a lesson or rehearsal",
                 f"{len(clashing)} clash(es)" if clashing else
                 f"{len(sessions)} sessions, all clear")

    per_day: dict = defaultdict(int)
    for session in sessions:
        per_day[session.start.date()] += session.minutes
    overloaded = {day: total for day, total in per_day.items()
                  if total > cfg.scheduling.max_daily_minutes}
    report.check(not overloaded, "No evening was overloaded",
                 f"worst day {max(per_day.values()) if per_day else 0} min, "
                 f"limit {cfg.scheduling.max_daily_minutes}")
    for day, total in sorted(overloaded.items())[:3]:
        report.note(f"{day} has {total} minutes booked")

    late = []
    for outcome in placed:
        due = world.due_by_id.get(outcome.change.assignment_id)
        if due and outcome.sessions[-1].end > due.astimezone(tz):
            late.append(outcome.change.label)
    report.check(not late, "Every session finishes before its deadline",
                 f"{len(late)} late" if late else "no overruns")

    blackout_hits = [s for s in sessions
                     if s.start.weekday() == 3 and s.start.hour == 19]
    report.check(not blackout_hits, "A time you cannot work stayed free",
                 "Thursday 19:00 respected")

    # ------------------------------------------------------- fallback
    heading("when Teams is unreachable")
    from . import patterns as patterns_module
    learned = patterns_module.learn(store, tz)
    report.check(len(learned) >= 3, "Teacher patterns were learned",
                 f"{len(learned)} of {len(dummy.TEACHERS)} teachers regular")
    for pattern in list(learned.values())[:3]:
        report.note(pattern.describe())

    # A second store holding only FINISHED prep - which is exactly the state
    # you are in when the session dies before this week's prep is captured.
    # Running the fallback against the main store would prove nothing: it
    # already knows every upcoming deadline, so every prediction is correctly
    # discarded as a duplicate.
    fallback_store = Store(temp / "fallback.sqlite3")
    past = [a for a in assignments
            if a.due_at and a.due_at < now.isoformat()]
    fallback_store.upsert(past, resolve)

    fallback_calendar = FakeCalendar(world.busy)
    predicted = pipeline.schedule_provisional(
        cfg, fallback_store, fallback_calendar, now=now.astimezone(tz))
    booked = [o for o in predicted if o.sessions]
    report.check(bool(booked), "A dead session still fills the diary",
                 f"{len(booked)} provisional block(s) from patterns")
    report.note("this is what happens when the Teams session expires: "
                "prep keeps being scheduled from what your teachers always do")

    guessed = [s for o in booked for s in o.sessions]
    clashing = [s for s in guessed
                if any(s.start < b.end and s.end > b.start
                       for b in _merge(world.busy))]
    report.check(not clashing, "Guessed blocks avoid lessons too",
                 f"{len(guessed)} sessions, all clear")

    withdrawn = fallback_store.clear_provisional()
    report.check(len(withdrawn) > 0 and not fallback_store.provisional_sessions(),
                 "Real prep withdraws the guesses",
                 f"{len(withdrawn)} withdrawn on the next real capture")
    fallback_store.close()

    # ------------------------------------------------------- consistency
    heading("state")
    stored = store.all_sessions()
    report.check(len(stored) == len(sessions),
                 "The database agrees with what was booked",
                 f"{len(stored)} rows, {len(sessions)} sessions")

    again = store.upsert(assignments, resolve)
    report.check(not again, "A second run finds nothing new",
                 f"{len(again)} spurious change(s)" if again else "idempotent")

    store.write_export(cfg.export_path)
    report.check(cfg.export_path.exists() and cfg.export_path.stat().st_size > 0,
                 "The export was written", cfg.export_path.name)
    store.close()

    if not args.quiet:
        heading("what it would have booked")
        _show_outcomes(outcomes)

    # ------------------------------------------------------- unit suite
    if not args.no_unit_tests:
        heading("offline unit suite")
        result = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                                capture_output=True, text=True, cwd=ROOT)
        tail = (result.stdout or result.stderr).strip().splitlines()
        report.check(result.returncode == 0, "Unit tests",
                     tail[-1][:60] if tail else "")
        if result.returncode != 0:
            print("\n".join(tail[-25:]))

    return report


def _merge(intervals: list[Interval]) -> list[Interval]:
    from .scheduler import merge
    return merge(list(intervals))


def _short_placements(placed) -> list[tuple]:
    """Prep that found *some* time but not all of it.

    Worth its own line: it looks like a success in a count of what was
    scheduled, and it is the quiet way a deadline gets missed. One block of
    slack is allowed, because a session carries a few minutes of flow buffer.
    """
    short = []
    for outcome in placed:
        booked = sum(session.minutes for session in outcome.sessions)
        if booked + 30 < outcome.minutes:
            short.append((outcome, booked, outcome.minutes))
    return short


def _show_outcomes(outcomes, limit: int = 20) -> None:
    """Show what was placed; count what was not.

    A first run pulls in a year of finished prep, so printing every "past due,
    skipped" line buries the handful of lines that actually matter.
    """
    placed = [o for o in outcomes if o.sessions]
    for outcome in placed[:limit]:
        print(f"       {outcome.describe()}")
    if len(placed) > limit:
        print(f"       ... and {len(placed) - limit} more scheduled")

    reasons: dict[str, int] = defaultdict(int)
    for outcome in outcomes:
        if not outcome.sessions and outcome.problem:
            reasons[outcome.problem] += 1
    for problem, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"       {count:>4} not scheduled: {problem}")
        if "NO FREE SLOT" in problem:
            for outcome in outcomes:
                if outcome.problem == problem:
                    print(f"            {outcome.change.describe()[:70]}")


def _duplicate_teachers(learned: dict) -> dict[str, list[str]]:
    """The same teacher mapped in two places. Case-insensitive, deliberately."""
    seen: dict[str, list[str]] = {}
    for bucket in ("class_map", "title_codes", "people_map"):
        for key, teacher in (learned.get(bucket) or {}).items():
            seen.setdefault(str(teacher).strip().lower(), []).append(f"{bucket}:{key}")
    return {teacher: where for teacher, where in seen.items() if len(where) > 1}


# ===========================================================  PHASE B
# Credentials, and what Teams says right now.

def phase_b(args, cfg) -> Report:
    report = Report("B")
    print("=" * 68)
    print("PHASE B - credentials, and your current prep")
    print("=" * 68)
    tz = ZoneInfo(cfg.timezone)

    # ------------------------------------------------------- config
    heading("configuration")
    report.check(True, "Config file read", str(args.config))
    report.check(bool(cfg.calendar.username and cfg.calendar.password),
                 "Calendar credentials present",
                 f"user {cfg.calendar.username[:3]}***"
                 if cfg.calendar.username else "missing")
    report.check(bool(cfg.feeds), "Timetable feed configured",
                 f"{len(cfg.feeds)} feed(s): {', '.join(cfg.feeds)}"
                 if cfg.feeds else "none - lessons will not be seen", soft=True)
    report.check(cfg.teams.profile_dir.exists(), "Browser profile exists",
                 str(cfg.teams.profile_dir))
    if _check_playwright(cfg, report):
        _check_browser(cfg, report)
    _check_third_party_cookies(cfg, report, args.config)
    _check_session(cfg, report, args.config)
    _check_secret_handling(args.config, report)

    # ------------------------------------------------------- teams
    heading("Microsoft Teams")
    notes: dict = {}
    payloads, source, live = _get_payloads(cfg, report,
                                           refresh=not args.no_refresh,
                                           notes=notes)
    if payloads is None:
        report.note("Nothing else in this phase can run without a capture.")
        return report
    if live:
        report.add(OK, "Teams session alive", source)
    else:
        # Reading a file proves nothing about the session, and this is the
        # phase whose entire job is to prove the credentials work.
        report.add(WARN, "Teams session alive", "NOT TESTED - read from disk")
        report.note("--no-refresh skipped the sign-in, which is what Phase B "
                    "is for. Rerun without it to test the session.")

    _check_capture_quality(report, notes, live)

    assignments = parse(payloads)
    report.check(bool(assignments), "Assignments parsed",
                 f"{len(assignments)} prep in the capture")
    if not assignments:
        report.note("The session loaded but no assignments came back. Either "
                    "url_hints is wrong or the page did not finish loading.")
        return report

    ids = {a.assignment_id for a in assignments}
    report.check(len(ids) == len(assignments), "Every prep has its own id",
                 f"{len(ids)} unique of {len(assignments)}")
    synthetic = [a for a in assignments if a.id_is_synthetic]
    report.check(not synthetic, "Ids came from Teams, not a fallback",
                 f"{len(synthetic)} synthetic - a moved due date would "
                 f"duplicate these" if synthetic else "all real",
                 soft=bool(synthetic))

    creators = sum(1 for a in assignments if a.creator_id or a.creator_name)
    report.check(creators > 0, "Teams sends who set each prep",
                 f"{creators} of {len(assignments)} carry a creator",
                 soft=creators == 0)
    if creators == 0:
        report.note("Without a creator, teachers who share a class cannot be "
                    "told apart. Run python3 -m prep.inspect for the detail.")

    # ------------------------------------------------------- the listing
    heading("your current prep")
    _print_current(cfg, assignments, tz, report, args.config)

    # ------------------------------------------------------- calendar
    heading("iCloud calendar")
    try:
        backend = CalDAVCalendar(cfg, tz)
        report.add(OK, "Calendar reachable",
                   f"writes to '{cfg.calendar.prep_calendar}'")
        others = sorted(name for name in backend.calendars
                        if name != cfg.calendar.prep_calendar)
        report.note(f"read as busy: {', '.join(others) if others else '(none)'}")
        now = datetime.now(tz)
        busy = backend.busy(now, now + timedelta(days=7))
        report.check(bool(busy), "Busy time found in the next week",
                     f"{len(busy)} block(s)", soft=True)
        if not busy:
            report.note("An empty diary usually means your timetable is a "
                        "subscribed calendar, which CalDAV cannot see. That is "
                        "what the ICS feed below is for.")
    except Exception as exc:
        _calendar_failure(report, exc, args.config)

    # ------------------------------------------------------- feeds
    if cfg.feeds:
        heading("timetable feed")
        from . import feeds as feeds_module
        now = datetime.now(tz)
        for name, url in cfg.feeds.items():
            try:
                events, problems = feeds_module.load_all(
                    {name: url}, now, now + timedelta(days=7), tz,
                    cache_dir=cfg.feed_cache_dir)
                if problems:
                    report.add(FAIL, f"Feed '{name}' readable", problems[0][:60])
                else:
                    report.check(bool(events), f"Feed '{name}' has this week",
                                 f"{len(events)} events", soft=True)
            except Exception as exc:
                report.add(FAIL, f"Feed '{name}' readable", str(exc)[:60])

    # ------------------------------------------------------- alerts
    heading("alerting")
    channels = []
    if cfg.notify.ntfy_topic:
        channels.append("ntfy")
    if cfg.notify.email.to and cfg.notify.email.username:
        channels.append("email")
    if cfg.notify.telegram.bot_token and cfg.notify.telegram.chat_id:
        channels.append("telegram")
    report.check(bool(channels), "A way to reach you is configured",
                 ", ".join(channels) if channels else
                 "none - a dead session would fail silently", soft=True)
    if args.test_alerts and channels:
        from . import notify
        sent = notify.push(cfg.notify, "Prep: Phase B test",
                           "Phase B reached your alerts. This is what a "
                           "sign-in failure will look like.", priority="high")
        report.check(sent, "Test alert delivered", ", ".join(channels))

    return report


def _check_playwright(cfg, report: Report) -> bool:
    """Is the browser automation library importable by THIS interpreter?

    Worth its own line because installing it into one interpreter and running
    another is an easy mistake, and the resulting ImportError arrives much
    later, from inside a capture, looking like a browser problem.
    """
    from .scraper import PlaywrightMissing, require_playwright

    try:
        require_playwright(cfg.teams.chromium_path)
    except PlaywrightMissing as exc:
        report.add(FAIL, "playwright installed", "not for this interpreter")
        for line in str(exc).splitlines()[1:]:
            if line.strip():
                report.note(line.strip())
        return False
    report.check(True, "playwright installed", sys.executable)
    return True


def _check_browser(cfg, report: Report) -> bool:
    """Is there a Chromium to drive, and where is it?

    Worth its own check rather than letting the capture fail: "failed to
    launch" arrives as a wall of Playwright text with the one useful part -
    the path it looked in - buried in the middle of it.
    """
    import os

    configured = cfg.teams.chromium_path
    if configured:
        usable = os.path.isfile(configured) and os.access(configured, os.X_OK)
        report.check(usable, "Chromium available",
                     configured if usable else f"{configured} is not there")
        if not usable:
            report.note("chromium_path in your config points at nothing. "
                        "Either fix it, or remove the line to use "
                        "Playwright's own build.")
        return usable

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
    except Exception as exc:
        report.add(FAIL, "Chromium available", f"playwright unusable: {exc}"[:70])
        _browser_advice(report)
        return False

    usable = bool(path) and os.path.isfile(path)
    report.check(usable, "Chromium available",
                 f"Playwright's own build" if usable else "not downloaded")
    if not usable:
        report.note(f"Playwright expects one at {path}")
        _browser_advice(report)
    return usable


def _check_secret_handling(config_path: str, report: Report) -> None:
    """Where each secret comes from, read from the raw config.

    Three sources, worst first:
      literal   written into config.yaml - it will end up in a backup, a
                screenshot, or a repository
      env:      a .env file, readable by every process running as you
      cred:     systemd decrypts it into a private tmpfs for one unit

    The YAML is parsed rather than scanned line by line, because both block
    and inline mappings are valid and a line scanner silently misses
    `calendar: {password: hunter2}`.
    """
    import yaml

    # Split by what the secret actually grants, so the verdict is
    # proportionate. A check that fails the run over a self-generated token in
    # a gitignored file is a check you learn to ignore - and then it is not
    # there for the one that matters.
    #
    # ACCOUNT: gets someone into something of yours. Feed URLs count - they
    # carry a personal key, and the URL alone is your whole timetable.
    ACCOUNT_KEYS = {"password", "app_password", "bot_token", "secret"}
    # LOCAL: worth separating, but self-generated and scoped to this app.
    LOCAL_KEYS = {"token", "chat_id", "username"}

    try:
        data = yaml.safe_load(Path(config_path).read_text()) or {}
    except Exception:
        return

    found: dict[str, list[str]] = {"account": [], "local": [],
                                   "env": [], "cred": []}

    def classify(where: str, value, account: bool) -> None:
        if not isinstance(value, str) or not value:
            return
        if value.startswith("cred:"):
            found["cred"].append(where)
        elif value.startswith("env:"):
            found["env"].append(where)
        else:
            found["account" if account else "local"].append(where)

    def walk(node, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else str(key)
                name = str(key).lower()
                if path == "feeds" or name in ACCOUNT_KEYS:
                    classify(here, value, account=True)
                elif name in LOCAL_KEYS:
                    classify(here, value, account=False)
                else:
                    walk(value, here)

    walk(data)

    account, local = found["account"], found["local"]
    env_based, cred_based = found["env"], found["cred"]

    if account:
        # A warning, not a failure: on a test machine a password in a
        # private config.yaml is a deliberate choice, and failing the phase
        # over it would stop the checks after it from running in `all`.
        report.add(WARN, "No account secret is written into the config",
                   f"{len(account)} literal: {', '.join(sorted(account)[:2])}")
        report.note("A password or feed key in config.yaml ends up in a "
                    "backup, a screenshot or a repository.")
        report.note("Move them: sudo ./deploy/setup-credentials.sh")
        return
    if local:
        report.add(WARN, "No secret is written into the config",
                   f"{len(local)} literal: {', '.join(sorted(local)[:2])}")
        report.note("Self-generated and scoped to this app, so not urgent - "
                    "but cred: keeps it out of backups too.")
        return

    if cred_based and not env_based:
        report.add(OK, "Secrets come from systemd credentials",
                   f"{len(cred_based)} encrypted, none in .env")
    elif cred_based:
        report.add(WARN, "Secrets come from systemd credentials",
                   f"{len(cred_based)} encrypted, {len(env_based)} still in .env")
        report.note("finish the move with deploy/setup-credentials.sh")
    elif env_based:
        report.add(WARN, "Secrets come from systemd credentials",
                   f"{len(env_based)} in plaintext .env")
        report.note("encrypt them: sudo ./deploy/setup-credentials.sh")


def _calendar_failure(report: Report, exc: Exception,
                      config_path: str = "config.yaml") -> None:
    """Say which way the calendar failed, because the answers differ wildly.

    These used to share one line and one piece of advice - "you need an
    app-specific password" - which is right for exactly one of them. Told
    that when the real fault was the network, you go and generate a new
    password, it fails the same way, and you conclude the whole approach is
    broken.
    """
    if isinstance(exc, CalendarAuthFailed):
        report.add(FAIL, "Calendar sign-in accepted", str(exc)[:70])
        report.note("iCloud answered, and said no. Three things do this:")
        report.note("  1. your ordinary Apple password - it will not work over "
                    "CalDAV. It must be an app-specific one from "
                    "appleid.apple.com (Sign-In and Security).")
        report.note("  2. stray whitespace or quotes around the value. Check "
                    "with: python3 -m prep.icloud --config " + config_path)
        report.note("  3. an app-specific password generated for a different "
                    "Apple ID, or revoked when the account password changed.")
        return

    if isinstance(exc, CalendarUnreachable):
        report.add(FAIL, "Calendar reachable", str(exc)[:70])
        report.note("iCloud never answered, so this says NOTHING about your "
                    "password - do not go and generate another one.")
        report.note("It was retried and still failed. Check the network, then: "
                    "python3 -m prep.icloud --config " + config_path)
        return

    # "calendar 'Prep' not found" and anything else: print it in full rather
    # than truncating, because the useful part is usually at the end.
    text = str(exc).strip()
    report.add(FAIL, "Calendar reachable", text.splitlines()[0][:70])
    for line in text.splitlines()[1:6]:
        if line.strip():
            report.note(line.strip()[:110])
    if "not found" in text:
        report.note("Create it in the Calendar app on your phone or Mac, with "
                    "exactly that name, and let it sync before rerunning.")


def _check_capture_quality(report: Report, notes: dict, live: bool) -> None:
    """Did the capture reach the right view, and see all of it?

    Both questions exist because of the same failure: a capture that comes
    back with *some* of your prep, no error anywhere, and a number you would
    have to already know is wrong. The payloads themselves cannot answer
    either one, so the capture reports what it did on the way.
    """
    if not live:
        return                      # read from a file; it did nothing on the way

    route = notes.get("route")
    if route == "never reached it":
        report.add(FAIL, "Reached the Assignments view", route)
        report.note("Teams has no URL for this view, so getting there is a "
                    "click. Nothing in the rail, its overflow, or the frames "
                    "was named like Assignments.")
        report.note("A screenshot of what the browser saw is in your dump "
                    "directory. If your school renamed the app, put its name "
                    "in assignment_labels under teams:.")
    elif route:
        report.add(OK, "Reached the Assignments view", route)

    settled = notes.get("settled")
    if settled is None:
        return

    # Teams virtualises the list: what was never scrolled into view was never
    # requested, so this is the difference between "all your prep" and "the
    # first screenful of it", with nothing to tell them apart afterwards.
    if settled.at_bottom is True:
        report.add(OK, "Assignment list seen to the end",
                   f"{settled.scrolls} scroll(s), it would go no further")
    elif settled.at_bottom is False:
        report.add(WARN, "Assignment list seen to the end",
                   f"still scrolling after {settled.scrolls} pass(es)")
        report.note("The list could still be scrolled when the capture "
                    "stopped, so prep below that point was never requested. "
                    "Raise max_settle_ms, and scroll_passes with it.")
    else:
        report.add(WARN, "Assignment list seen to the end",
                   "could not tell - the page would not report its scrolling",
                   )

    if settled.still_arriving:
        report.add(WARN, "Assignment data had stopped arriving",
                   f"gave up at {settled.waited_ms / 1000:.0f}s with data "
                   f"still coming")
        report.note("This count is probably short. Raise max_settle_ms, and "
                    "use python3 -m prep.diagnose to watch it happen.")
    else:
        report.add(OK, "Assignment data had stopped arriving",
                   f"quiet for the last {settled.waited_ms / 1000:.0f}s of "
                   f"the capture")


def _check_third_party_cookies(cfg, report: Report,
                               config_path: str = "config.yaml") -> None:
    """Can Teams renew its own access without a person?

    Its own check because the failure it causes arrives days later and looks
    like something else entirely: the sign-in works, prep appears, and then
    every run starts reporting a dead session while the cookie sitting in the
    profile is still perfectly valid. Signing in again buys a few more days,
    which is exactly the pattern that hides the cause.
    """
    from . import cookies

    state = cookies.prepare(cfg.teams.profile_dir,
                            cfg.teams.third_party_cookies)

    if state.allowed is None:
        report.add(WARN, "Third-party cookies allowed", state.describe())
        report.note(f"Nothing to read until you have signed in: "
                    f"python3 -m prep.login --config {config_path}")
        return
    if state.allowed:
        report.add(OK, "Third-party cookies allowed", state.describe())
        return

    report.add(FAIL, "Third-party cookies allowed", state.describe())
    for line in cookies.advice(state):
        if line:                      # the report has no room for blank lines
            report.note(line)


def _check_session(cfg, report: Report,
                   config_path: str = "config.yaml") -> None:
    """How long the saved session has left, before it is needed.

    A capture that works today says nothing about tomorrow. This is the
    difference between re-authenticating at a time you choose and finding out
    because a week of prep went unscheduled.
    """
    from . import session as session_module

    health = session_module.read(cfg.teams.profile_dir)
    days = health.days_left

    if not health.found or not health.auth_cookies:
        status = WARN
    elif not health.stay_signed_in:
        status = FAIL          # every headless run will start logged out
    elif days is not None and days < 0:
        status = FAIL
    elif days is not None and days < session_module.WARN_DAYS:
        status = WARN
    else:
        status = OK

    report.add(status, "Session will last", health.describe())
    if status == FAIL and not health.stay_signed_in and health.auth_cookies:
        report.note("Sign in again and tick the box: "
                    f"python3 -m prep.login --config {config_path}")


def _browser_advice(report: Report) -> None:
    from .scraper import BROWSER_ADVICE
    for line in BROWSER_ADVICE:
        report.note(line)


def _print_current(cfg, assignments, tz, report: Report,
                   config_path: str = "config.yaml") -> None:
    """The listing: what is due, who set it, and how long it should take."""
    resolve = pipeline.teacher_matcher(cfg)
    now = datetime.now(timezone.utc)
    archived = cfg.scheduling.archived_classes

    # Read what has been learned about timings, but write nothing: Phase B is
    # a credentials test and must not change the database.
    store = Store(cfg.db_path)
    stats, history, mu = (store.teacher_stats(), store.teacher_timings(),
                          store.global_mean())
    store.close()

    from .scheduler import parse_dt
    rows = []
    for item in assignments:
        due = parse_dt(item.due_at, tz)
        if due is None or due <= now.astimezone(tz):
            continue
        if item.class_id and item.class_id.lower() in archived:
            continue
        teacher = resolve(item)
        est = estimator.estimate(cfg.estimator, teacher,
                                 parse_dt(item.assigned_at, tz), due,
                                 stats, mu, history=history)
        rows.append((due, teacher, item.title, est))

    if not rows:
        report.add(WARN, "Prep still to come", "nothing due - is that right?")
        return

    report.add(OK, "Prep still to come", f"{len(rows)} piece(s)")
    print(f"\n       {'due':<17} {'in':>5}  {'teacher':<12} "
          f"{'est':>5}  {'basis':<9} title")
    print("       " + "-" * 74)
    for due, teacher, title, est in sorted(rows):
        days = (due - now.astimezone(tz)).days
        left = "today" if days == 0 else f"{days}d"
        print(f"       {due:%a %d %b %H:%M}  {left:>5}  "
              f"{(teacher or '?'):<12} {est.minutes:>4}m  "
              f"{est.source:<9} {title[:34]}")

    missing = [t for _, who, t, _ in rows if not who]
    if missing:
        report.add(WARN, "Every prep has a teacher",
                   f"{len(missing)} unidentified")
        report.note(f"fix with: python3 -m prep.setup --config {config_path}"
                    f" --set-teachers")


# ===========================================================  PHASE C
# Everything, on real data, end to end.

def phase_c(args, cfg) -> Report:
    report = Report("C")
    print("=" * 68)
    print("PHASE C - full integration on live data"
          + ("" if args.write else " (dry run)"))
    print("=" * 68)
    tz = ZoneInfo(cfg.timezone)

    # ------------------------------------------------------- capture
    heading("capture")
    notes: dict = {}
    payloads, source, live = _get_payloads(cfg, report,
                                           refresh=not args.no_refresh,
                                           notes=notes)
    if payloads is None:
        return report
    report.add(OK, "Capture", source)
    # A short capture does not fail here, it schedules a short week - which
    # is the whole reason this phase cross-checks rather than trusting.
    _check_capture_quality(report, notes, live)

    assignments = parse(payloads)
    if not assignments:
        report.add(FAIL, "Assignments parsed", "0 found")
        return report
    report.add(OK, "Assignments parsed", f"{len(assignments)} prep")

    now = datetime.now(timezone.utc)
    upcoming = [a for a in assignments if a.due_at and a.due_at > now.isoformat()]
    report.add(OK, "Still to come", f"{len(upcoming)} of {len(assignments)}")

    # ------------------------------------------------------- diff
    heading("what changed")
    store = Store(cfg.db_path)
    if not args.write:
        # Dry, so nothing it sees may stick. Recording the prep here would
        # make the first real run think it was already known and book none
        # of it - and the pretend sessions would sit in the database as if
        # they were real.
        real, store = store, store.scratch_copy()
        real.close()
    resolve = pipeline.teacher_matcher(cfg)
    changes = store.upsert(assignments, resolve)

    new = [c for c in changes if c.kind == "new"]
    moved = [c for c in changes if c.kind == "due_changed"]
    report.add(OK, "New prep since last run", str(len(new)))
    report.add(WARN if moved else OK, "Due dates that moved", str(len(moved)))
    # A moved deadline always gets a line: it is the event this whole system
    # exists to react to. New prep is capped, because the first run imports a
    # year of finished work and 200 lines would bury everything else.
    for change in moved:
        report.note(change.describe())
    for change in new[:15]:
        report.note(change.describe())
    if len(new) > 15:
        report.note(f"... and {len(new) - 15} more new (a first run imports "
                    f"your whole history)")

    # ------------------------------------------------------- coverage
    heading("who set what")
    archived = cfg.scheduling.archived_classes
    live = [a for a in assignments
            if not (a.class_id and a.class_id.lower() in archived)]
    unresolved = [a for a in live if not resolve(a)]

    if unresolved:
        classes = {a.class_id for a in unresolved}
        report.add(WARN, "Every live prep has a teacher",
                   f"{len(unresolved)} unresolved across {len(classes)} class(es)")
        for class_id in sorted(classes):
            examples = [a.title[:44] for a in unresolved
                        if a.class_id == class_id][:2]
            report.note(f"{class_id[:36]}: {'; '.join(examples)}")
        report.note("fix: python3 -m prep.setup --config <config> "
                    "--set-teachers")
    else:
        report.add(OK, "Every live prep has a teacher")

    # ------------------------------------------------------- archiving
    by_class: dict[str, list] = defaultdict(list)
    for item in assignments:
        by_class[item.class_id or "(none)"].append(item)

    candidates = []
    for class_id, items in by_class.items():
        if class_id == "(none)" or class_id.lower() in archived:
            continue
        summary = identify.summarise_class(items, now=now)
        if summary["likely_archived"]:
            candidates.append((class_id, summary))

    if candidates:
        report.add(WARN, "No class looks finished", f"{len(candidates)} candidate(s)")
        for class_id, summary in candidates:
            report.note(f"{class_id[:36]}: {summary['archive_basis']}")
        report.note("confirm with: python3 -m prep.setup --config <config> "
                    "--archive <class-id>")
    else:
        report.add(OK, "No class looks finished")

    if archived:
        skipped = sum(1 for a in assignments
                      if a.class_id and a.class_id.lower() in archived)
        report.add(OK, "Archived classes excluded",
                   f"{len(archived)} class(es), {skipped} prep ignored")

    clashes = _duplicate_teachers(identify.load_learned(cfg.learned_map_path))
    if clashes:
        report.add(WARN, "No teacher mapped twice", f"{len(clashes)} to check")
        for teacher, where in clashes.items():
            report.note(f"{teacher}: {', '.join(w[:28] for w in where)}")
    else:
        report.add(OK, "No teacher mapped twice")

    # ------------------------------------------------------- scheduling
    heading("scheduling")
    try:
        backend = CalDAVCalendar(cfg, tz)
        calendar = backend if args.write else DryRunCalendar(backend)
        report.add(OK, "Calendar reachable", cfg.calendar.prep_calendar)
    except Exception as exc:
        # Without the calendar there is no busy time, so any schedule produced
        # here would be planned against an empty diary. Reporting it as a
        # success would be worse than not reporting it at all.
        _calendar_failure(report, exc, args.config)
        report.note("Skipping the scheduling check: with no diary to read, "
                    "any plan")
        report.note("would ignore every lesson and commitment you have.")
        store.write_export(cfg.export_path)
        store.close()
        return report

    try:
        outcomes = pipeline.reschedule(cfg, store, calendar, changes)
    except pipeline.FeedsUnavailable as exc:
        report.add(FAIL, "Timetable feed readable", str(exc)[:90])
        report.note("Scheduling was abandoned on purpose: booking prep without "
                    "the timetable would put it straight through lessons.")
        store.close()
        return report
    except Exception as exc:
        report.add(FAIL, "Scheduling ran", str(exc)[:90])
        store.close()
        return report

    placed = [o for o in outcomes if o.sessions]
    blocked = [o for o in outcomes if o.problem and "NO FREE SLOT" in o.problem]
    report.add(WARN if blocked else OK, "Everything found a slot",
               f"{len(placed)} scheduled, {len(blocked)} could not fit")
    _show_outcomes(outcomes)

    short = _short_placements(placed)
    report.check(not short, "Every prep got all the time it needs",
                 f"{len(short)} placed short" if short else
                 f"{len(placed)} prep, none cut off", soft=True)
    for outcome, got, wanted in short[:4]:
        report.note(f"{outcome.change.label[:40]}: {got} of {wanted} min "
                    f"before the deadline")

    # ------------------------------------------------------- travel
    if cfg.travel:
        heading("travel")
        _check_travel(cfg, store, calendar, tz, report)

    # ------------------------------------------------------- agreement
    heading("do the parts agree")
    sessions = [s for o in placed for s in o.sessions]
    if not args.write and isinstance(calendar, DryRunCalendar):
        report.check(len(calendar.would_create) == len(sessions),
                     "Calendar and scheduler agree",
                     f"{len(calendar.would_create)} events for "
                     f"{len(sessions)} sessions")

    stored_ids = {row["assignment_id"] for row in store.all_sessions()}
    planned_ids = {o.change.assignment_id for o in placed}
    report.check(planned_ids <= stored_ids, "Database recorded every booking",
                 f"{len(planned_ids & stored_ids)} of {len(planned_ids)}")

    per_day: dict = defaultdict(int)
    for row in store.all_sessions():
        start = datetime.fromisoformat(row["start_local"])
        if start > datetime.now(tz):
            per_day[start.date()] += int(row["minutes"])
    worst = max(per_day.values(), default=0)
    report.check(worst <= cfg.scheduling.max_daily_minutes,
                 "No evening exceeds the daily limit",
                 f"worst {worst} min, limit {cfg.scheduling.max_daily_minutes}")

    store.write_export(cfg.export_path)
    report.check(cfg.export_path.exists(), "Export written", str(cfg.export_path))

    # ------------------------------------------------------- dashboard
    heading("dashboard")
    try:
        from .web import create_app
        app = create_app(cfg, calendar_factory=lambda: FakeCalendar())
        client = app.test_client()
        token = f"?token={cfg.web.token}" if cfg.web.token else ""
        response = client.get(f"{cfg.web.url_prefix}/{token}")
        report.check(response.status_code == 200, "Dashboard renders",
                     f"HTTP {response.status_code}, "
                     f"{len(response.data) // 1024} KB")
        if cfg.web.token:
            # A fresh client on purpose. The first request above was handed a
            # cookie, and reusing that client would test the cookie rather
            # than the lock.
            forbidden = app.test_client().get(f"{cfg.web.url_prefix}/")
            report.check(forbidden.status_code == 403,
                         "Dashboard refuses a missing token",
                         f"HTTP {forbidden.status_code}")
        else:
            report.add(WARN, "Dashboard is protected",
                       "no token set - anyone on the network can post to it")

        from .web import token_warning
        weak = token_warning(cfg)
        report.check(not weak, "Dashboard deployment is sound",
                     weak or f"token set, bound to {cfg.web.host}",
                     soft=True)
        if weak:
            report.note("nginx + gunicorn config: deploy/nginx-prep.conf")
    except Exception as exc:
        report.add(FAIL, "Dashboard renders", str(exc)[:70])

    # ------------------------------------------------------- alerting
    heading("alerting")
    from . import notify
    body = "\n".join(o.describe() for o in outcomes) or "no changes"
    report.check(len(body) < 100_000, "Report is a sane size",
                 f"{len(body)} characters")
    if len(body) > notify.TELEGRAM_LIMIT:
        report.note(f"longer than Telegram's {notify.TELEGRAM_LIMIT}-character "
                    "limit, so it will be truncated at a line boundary")
    channels = [name for name, on in (
        ("ntfy", cfg.notify.ntfy_topic),
        ("email", cfg.notify.email.to and cfg.notify.email.username),
        ("telegram", cfg.notify.telegram.bot_token and cfg.notify.telegram.chat_id),
    ) if on]
    report.check(bool(channels), "A way to reach you is configured",
                 ", ".join(channels) if channels else "none", soft=True)

    store.close()
    if not args.write:
        print("\n  (dry run - nothing was written to iCloud or the database. "
              "Add --write to apply.)")
    return report


def _check_travel(cfg, store, calendar, tz, report: Report) -> None:
    """Where you will be each day, and that no prep was booked on the way."""
    from . import travel

    places = travel.Places(cfg.travel)
    missing = [p.name for p in (places.home, places.school)
               if not p.postcode or (cfg.travel.lookup_postcodes
                                     and places.geocoder.lookup(p.postcode) is None)]
    report.check(not missing, "Home and school found",
                 ", ".join(missing) + " not found" if missing else
                 f"{places.home.postcode}, {places.school.postcode}, "
                 f"{places.minutes(places.home, places.school)} min apart",
                 soft=True)

    now = datetime.now(tz)
    try:
        _, routing = pipeline.diary_context(cfg, store, calendar, now,
                                            now + timedelta(days=21), tz, places)
    except Exception as exc:
        report.add(FAIL, "Every journey can be made in time", str(exc)[:70])
        return

    report.check(not routing.unplaced, "Every outside commitment has a place",
                 f"{len(routing.unplaced)} without one" if routing.unplaced
                 else "all placed", soft=True)
    for line in routing.unplaced[:6]:
        report.note(line)
    if routing.unplaced:
        report.note("put a postcode in its Location, or add the place under "
                    "travel.places in config.yaml")

    report.check(not routing.problems, "Every journey can be made in time",
                 f"{len(routing.problems)} cannot" if routing.problems
                 else f"{len(routing.days)} day(s) routed", soft=True)
    for line in routing.problems[:6]:
        report.note(line)

    for day, plan in sorted(routing.days.items())[:7]:
        for line in plan.lines:
            report.note(f"{day:%a %d %b}  {line[:96]}")

    stranded = []
    for row in store.all_sessions():
        start = datetime.fromisoformat(row["start_local"])
        end = datetime.fromisoformat(row["end_local"])
        if start < now or row["provisional"]:
            continue
        day = start.astimezone(tz).date()
        if day in routing.days and not any(
                w.start <= start and end <= w.end for w in routing.days[day].windows):
            stranded.append(f"{start:%a %d %b %H:%M} {row['label'][:40]}")
    report.check(not stranded, "Nothing booked in transit",
                 f"{len(stranded)} booked away from home and school"
                 if stranded else "every session is at home or school")
    for line in stranded[:6]:
        report.note(line)


# ===========================================================  shared

def _get_payloads(cfg, report: Report, refresh: bool, notes: dict | None = None):
    """Fresh from Teams by default, so a test always sees today's prep.

    Returns (payloads, description, live). `live` is False when the data came
    off the disk, and it matters: nothing read from a file can tell you the
    session still works, so the caller must not claim that it does.
    """
    if refresh:
        try:
            payloads = capture(cfg, dump=True, notes=notes)
            return payloads, f"fresh capture, {len(payloads)} payloads", True
        except SessionExpired as exc:
            report.add(FAIL, "Teams session alive", str(exc)[:80])
            report.note("Sign in again on this machine, tick 'Stay signed in', "
                        "then rerun.")
            return None, "", False
        except Exception as exc:
            text = str(exc).strip()
            report.add(FAIL, "Capture succeeded", text.splitlines()[0][:70])
            # The first 80 characters of a Playwright launch failure are the
            # part you already knew. The path it looked in - the only useful
            # part - is further along, so print the whole thing.
            for line in text.splitlines()[1:8]:
                if line.strip():
                    report.note(line.strip()[:110])
            if "executable doesn't exist" in text or "Failed to launch" in text:
                _browser_advice(report)
            return None, "", False

    dump = _newest_dump(cfg.dump_dir)
    if dump is None:
        report.add(FAIL, "A saved capture exists", "no dumps found")
        return None, "", False
    payloads = load_payload_file(dump)
    age = (datetime.now(timezone.utc).timestamp() - dump.stat().st_mtime) / 3600
    report.add(WARN if age > 24 else OK, "Using saved capture",
               f"{dump.name}, {age:.0f} hours old")
    return payloads, f"{dump.name}, {age:.0f} hours old", False


def _newest_dump(dump_dir: Path) -> Path | None:
    dumps = sorted(Path(dump_dir).glob("payloads-*.json"))
    return dumps[-1] if dumps else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test the prep pipeline in phases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="A  dummy data, no credentials   B  credentials and current prep"
               "   C  everything together")
    parser.add_argument("phase", nargs="?",
                        choices=["A", "B", "C", "a", "b", "c", "all"],
                        help="which phase to run")
    parser.add_argument("--list-checks", action="store_true",
                        help="print every check with its number (B.11 etc)")
    parser.add_argument("--config", help="required for B and C")
    parser.add_argument("--no-refresh", action="store_true",
                        help="B and C: reuse the last dump instead of scraping")
    parser.add_argument("--write", action="store_true",
                        help="C: actually write to the calendar")
    parser.add_argument("--no-unit-tests", action="store_true",
                        help="A: skip the offline pytest suite")
    parser.add_argument("--test-alerts", action="store_true",
                        help="B: actually send a test alert")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_checks:
        list_checks()
        return 0
    if not args.phase:
        parser.error("say which phase: A, B, C or all")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    wanted = args.phase.upper()
    order = ["A", "B", "C"] if wanted == "ALL" else [wanted]

    if any(p in ("B", "C") for p in order) and not args.config:
        print("Phase B and C need --config. Phase A does not:\n"
              "    python3 -m prep.phases A")
        return 2

    cfg = None
    if args.config:
        try:
            cfg = config_module.load(args.config)
        except Exception as exc:
            print(f"[FAIL] Config file read - {exc}")
            return 1

    reports: list[Report] = []
    for phase in order:
        if phase == "A":
            reports.append(phase_a(args))
        elif phase == "B":
            reports.append(phase_b(args, cfg))
        else:
            reports.append(phase_c(args, cfg))
        # A failed phase makes the next one meaningless: B cannot be judged if
        # the logic is broken, and C cannot run if the credentials are dead.
        if reports[-1].worst() == FAIL and len(order) > 1:
            print(f"\n  Stopping: phase {phase} failed, so a later phase would "
                  f"only repeat the same failure.")
            break

    return finish(reports)


if __name__ == "__main__":
    sys.exit(main())
