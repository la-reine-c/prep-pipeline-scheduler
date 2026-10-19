"""The pipeline: diff -> estimate -> schedule -> export.

Kept separate from run.py so the web app can trigger a reschedule with the
identical code path.
"""
from __future__ import annotations

import fcntl
import logging
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import estimator, feeds as feeds_module, scheduler
from .config import Config
from .scheduler import CalendarBackend, Interval, PlannedSession
from .scraper import RawAssignment
from .store import Change, Store

log = logging.getLogger(__name__)


class FeedsUnavailable(RuntimeError):
    """A subscribed calendar feed could not be read.

    Scheduling is aborted rather than run against a partial view of the diary:
    silently losing the timetable would book prep during lessons.
    """


@dataclass
class Outcome:
    change: Change
    minutes: int
    estimate_source: str
    sessions: list[PlannedSession]
    problem: str = ""
    short: int = 0            # blocks it needed that found no time

    def describe(self) -> str:
        if self.problem:
            return f"{self.change.describe()} -> {self.problem}"
        first = self.sessions[0].start.strftime("%a %d %b %H:%M")
        return (f"{self.change.describe()} -> {len(self.sessions)} sessions, "
                f"{self.minutes} min ({self.estimate_source}), first {first}")


def teacher_matcher(cfg: Config):
    """Resolve a teacher, most reliable signal first.

    1. Who set it (creator id or name). The only signal that works when a
       class is shared between teachers, because it is per assignment.
    2. A teacher code in the title ("AAA Prep 3", "M3 CCC Prep 1").
    3. The class GUID - only correct when a class has ONE teacher.
    4. Keywords in the class name - useless when the tenant sends no name.

    An unresolved teacher is left blank on purpose. A wrong attribution is
    worse than none: it poisons that teacher's timing average and every future
    estimate inherits the error.
    """
    codes = {code.upper(): teacher
             for code, teacher in (cfg.scheduling.title_codes or {}).items()}
    # Class ids and creator ids are opaque strings whose case can vary between
    # captures, so every lookup is folded. Teacher names keep the spelling you
    # gave them; only the matching ignores case.
    class_map = {str(key).lower(): value
                 for key, value in (cfg.scheduling.class_map or {}).items()}
    people = {str(key).lower(): value
              for key, value in (cfg.scheduling.people_map or {}).items()}
    keywords = cfg.scheduling.teacher_keywords

    def resolve(item: RawAssignment) -> str:
        for key in (item.creator_id, item.creator_name):
            if key and key.lower() in people:
                return people[key.lower()]
        if item.creator_name and not people:
            # The tenant already told us the name; trust it rather than
            # demanding it be mapped to itself.
            return item.creator_name

        words = re.findall(r"[A-Za-z]{2,4}", item.title)
        for word in words:
            teacher = codes.get(word.upper())
            if teacher:
                return teacher

        if item.class_id and item.class_id.lower() in class_map:
            return class_map[item.class_id.lower()]

        haystack = f"{item.class_ref} {item.title}".lower()
        for teacher, keyword_list in keywords.items():
            if any(word in haystack for word in keyword_list):
                return teacher
        return ""

    return resolve


def blackout_intervals(store: Store, start: datetime, end: datetime,
                       tz: ZoneInfo) -> list[Interval]:
    """Expand the blackout table into concrete busy intervals.

    A weekly entry repeats on that weekday; a dated one applies once.
    """
    return [interval for interval, _, _ in
            located_blackouts(store, start, end, tz)]


class LocatedBlackout(tuple):
    """(interval, location, label), plus how you get there as `.travel`.

    Still a 3-tuple, so everything that unpacks one is unchanged."""
    travel: str = ""

    def __new__(cls, interval, location, label, travel=""):
        made = super().__new__(cls, (interval, location, label))
        made.travel = travel
        return made


def located_blackouts(store: Store, start: datetime, end: datetime,
                      tz: ZoneInfo) -> list[tuple[Interval, str, str]]:
    """Blackouts as (interval, location, label). A location makes it
    somewhere you travel to, not just time you are busy."""
    from datetime import time as dtime

    def parse_hm(text: str) -> dtime:
        hour, minute = (int(part) for part in str(text).split(":")[:2])
        return dtime(hour, minute)

    rows = store.blackouts()
    if not rows:
        return []

    intervals: list[tuple[Interval, str, str]] = []
    day = start.date()
    while day <= end.date():
        for row in rows:
            if row["on_date"]:
                if str(row["on_date"]) != day.isoformat():
                    continue
            elif row["weekday"] is None or int(row["weekday"]) != day.weekday():
                continue
            try:
                begin = datetime.combine(day, parse_hm(row["start_time"]), tzinfo=tz)
                finish = datetime.combine(day, parse_hm(row["end_time"]), tzinfo=tz)
            except Exception:
                log.warning("skipping malformed blackout %s", dict(row))
                continue
            if finish > begin:
                keys = row.keys()
                intervals.append(LocatedBlackout(
                    Interval(begin, finish),
                    (row["location"] if "location" in keys else "") or "",
                    row["label"] or "",
                    (row["travel"] if "travel" in keys else "") or ""))
        day += timedelta(days=1)
    return intervals


def diary_context(cfg: Config, store: Store, calendar: CalendarBackend,
                  now: datetime, horizon: datetime, tz: ZoneInfo,
                  places=None):
    """Everything that decides when you are free: busy time, and - when
    travel is configured - where you are each day.

    Raises FeedsUnavailable before anything is decided: a schedule built
    without the timetable would put prep straight through lessons.
    """
    from . import travel as travel_module

    busy = list(calendar.busy(now, horizon))

    # Subscribed ICS calendars (the school timetable) never appear over CalDAV,
    # so they are fetched directly. If a feed fails we must NOT quietly carry on
    # with an empty timetable - that would book prep straight through lessons.
    feed_events = []
    if cfg.feeds:
        feed_events, problems = feeds_module.load_all(
            cfg.feeds, now, horizon, tz, cache_dir=cfg.feed_cache_dir)
        if problems:
            raise FeedsUnavailable(
                "refusing to schedule without the full timetable: "
                + "; ".join(problems))
        for event in feed_events:
            busy.append(Interval(event.start, event.end))
        log.info("feeds contributed %d busy events", len(feed_events))

    # Times you have told the dashboard you cannot work.
    blackouts = located_blackouts(store, now, horizon, tz)
    busy.extend(interval for interval, _, _ in blackouts)

    routing = None
    if cfg.travel:
        places = places or travel_module.Places(cfg.travel)
        listing = getattr(calendar, "diary_events", None)
        diary = listing(now, horizon) if listing else []
        stops, unplaced = travel_module.collect_stops(
            places, diary, feed_events, blackouts,
            cfg.calendar.personal_calendar)
        routing = travel_module.route(cfg, places, stops, busy, now, horizon, tz)
        routing.unplaced = unplaced
        for problem in routing.problems:
            log.warning("%s", problem)
    return busy, routing


def _conflicted(cfg: Config, store: Store, busy: list[Interval],
                availability, now: datetime, tz: ZoneInfo) -> set[str]:
    """Prep whose future bookings no longer fit the diary.

    A lesson added in Personal, a new blackout, a journey that now runs
    through the evening: the booking was right when it was made and is not
    any more. Those prep are re-planned; everything else stays put.
    """
    merged = scheduler.merge(list(busy))
    clashing: set[str] = set()
    for row in store.all_sessions():
        if row["provisional"]:
            continue
        start = datetime.fromisoformat(row["start_local"])
        end = datetime.fromisoformat(row["end_local"])
        if start < now:
            continue
        overlaps = any(b.start < end and start < b.end for b in merged)
        windows = scheduler.day_windows(cfg.scheduling, start.astimezone(tz).date(),
                                        tz, availability)
        inside = any(w.start <= start and end <= w.end for w in windows)
        if overlaps or not inside:
            clashing.add(row["assignment_id"])
    return clashing


@contextmanager
def planning_lock(cfg: Config):
    """One plan at a time, across processes.

    The dashboard, the scheduled run and a test can all plan at once. Two
    plans that both read "nothing booked" and then both book is exactly how
    the diary gets every prep twice.
    """
    path = Path(cfg.db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def replan_all(cfg: Config, store: Store, calendar: CalendarBackend,
               now: datetime | None = None,
               horizon_days: int = 21) -> list[Outcome]:
    """Re-plan every prep still to come, e.g. after a blackout changes.

    Sessions already under way or finished stay where they are and count
    towards the prep; everything in the future is taken out and planned
    again around the new constraints. Prep you have logged a time for is
    done and left alone, as are provisional guesses.
    """
    tz = ZoneInfo(cfg.timezone)
    now = now or datetime.now(tz)
    logged = store.logged_ids()
    rows = []
    for row in store.conn.execute("SELECT * FROM assignments"):
        due = scheduler.parse_dt(row["due_at"], tz)
        if (due is None or due <= now
                or row["assignment_id"] in logged
                or row["assignment_id"].startswith("provisional-")):
            continue
        rows.append((due, row))
    # Soonest deadline first, so it gets first pick of the evenings.
    rows.sort(key=lambda pair: pair[0])
    changes = [Change("replan", row["assignment_id"],
                      row["label"] or row["title"], row["teacher"] or "",
                      row["due_at"])
               for _, row in rows]
    return reschedule(cfg, store, calendar, changes, now=now,
                      horizon_days=horizon_days, everything=True)


def _clear_future(store: Store, calendar: CalendarBackend,
                  assignment_ids: list[str], now: datetime) -> None:
    """Take future bookings out of the diary and the database together.

    A booking the calendar would not delete stays in the database, so it is
    still counted as booked and as busy - a replacement is never placed on
    top of a session that is still in the diary.
    """
    future = [row["uid"] for aid in assignment_ids
              for row in store.sessions_for(aid)
              if datetime.fromisoformat(row["start_local"]) >= now
              and not row["provisional"]]
    failed = set(calendar.delete_by_uid(future) or [])
    store.delete_sessions([uid for uid in future if uid not in failed])
    if failed:
        log.warning("%d booking(s) could not be removed and were kept",
                    len(failed))


def _sweep_duplicates(store: Store, calendar: CalendarBackend,
                      start: datetime, end: datetime) -> int:
    """Delete future events for our prep that the database does not own.

    Only events whose prep this database knows about are touched; anything
    else in the calendar - another machine's bookings, your own entries -
    is left alone.
    """
    listing = getattr(calendar, "pipeline_events", None)
    if listing is None:
        return 0
    known_ids = {row[0] for row in store.conn.execute(
        "SELECT assignment_id FROM assignments")}
    owned = {row[0] for row in store.conn.execute("SELECT uid FROM sessions")}
    stray = [uid for uid, aid, when in listing(start, end)
             if aid in known_ids and uid not in owned and when >= start]
    if stray:
        calendar.delete_by_uid(stray)
        log.info("removed %d duplicate booking(s)", len(stray))
    return len(stray)


def estimate_for(cfg: Config, row, teacher: str, assigned: datetime | None,
                 due: datetime, stats, mu, history) -> estimator.Estimate:
    """How long a prep should get, as planning decides it."""
    est = estimator.estimate(cfg.estimator, teacher, assigned, due,
                             stats, mu, history=history)
    # A correction you made by hand always wins over the prediction.
    override = row["override_minutes"] if "override_minutes" in row.keys() else None
    if override:
        est = estimator.Estimate(int(override), "corrected", "set by you")
    return est


def reschedule(cfg: Config, store: Store, calendar: CalendarBackend,
               changes: list[Change], now: datetime | None = None,
               horizon_days: int = 21, everything: bool = False) -> list[Outcome]:
    with planning_lock(cfg):
        outcomes = _reschedule(cfg, store, calendar, changes, now, horizon_days)
    # Prep left short only shares fairly if everything competes for the time,
    # including prep booked on earlier runs - so when earlier bookings hold
    # time it could have had, the whole diary is replanned.
    if everything or not _could_share(cfg, store, outcomes, now):
        return outcomes
    log.info("not enough time for everything; sharing it out again")
    replanned = {o.change.assignment_id: o for o in replan_all(
        cfg, store, calendar, now=now, horizon_days=horizon_days)}
    merged = [replanned.pop(o.change.assignment_id, o) for o in outcomes]
    return merged + list(replanned.values())


def _could_share(cfg: Config, store: Store, outcomes: list[Outcome],
                 now: datetime | None) -> bool:
    """Whether prep left short could take time booked for other prep."""
    tz = ZoneInfo(cfg.timezone)
    now = now or datetime.now(tz)
    planned = {o.change.assignment_id for o in outcomes}
    others = [datetime.fromisoformat(row["start_local"])
              for row in store.all_sessions()
              if row["assignment_id"] not in planned and not row["provisional"]
              and datetime.fromisoformat(row["end_local"]) > now]
    for outcome in outcomes:
        due = scheduler.parse_dt(outcome.change.due_at, tz) if outcome.short else None
        if due is not None and any(start < due for start in others):
            return True
    return False


def _reschedule(cfg: Config, store: Store, calendar: CalendarBackend,
                changes: list[Change], now: datetime | None,
                horizon_days: int) -> list[Outcome]:
    tz = ZoneInfo(cfg.timezone)
    now = now or datetime.now(tz)
    horizon = now + timedelta(days=horizon_days)
    busy, routing = diary_context(cfg, store, calendar, now, horizon, tz)
    availability = routing.availability if routing else None

    # Bookings the diary has moved out from under are re-planned too, so a
    # lesson you add is respected on the next run without touching anything.
    changes = list(changes)
    already = {c.assignment_id for c in changes}
    for assignment_id in sorted(_conflicted(cfg, store, busy, availability,
                                            now, tz) - already):
        row = store.assignment(assignment_id)
        if row is not None:
            changes.append(Change("replan", assignment_id,
                                  row["label"] or row["title"],
                                  row["teacher"] or "", row["due_at"]))

    # Everything that can fail has been read, so only now is anything taken
    # out of the diary. A replan that stopped here would leave prep unbooked.
    replans = [c.assignment_id for c in changes if c.kind == "replan"]
    if replans:
        _clear_future(store, calendar, replans, now)
        _sweep_duplicates(store, calendar, now, now + timedelta(days=60))

    # Existing prep sessions are busy time too, so preps don't collide, and they
    # count towards each day's total prep load.
    daily_load: dict[date, int] = {}
    for row in store.all_sessions():
        start = datetime.fromisoformat(row["start_local"])
        end = datetime.fromisoformat(row["end_local"])
        if end > now:
            busy.append(Interval(start, end))
            daily_load[start.date()] = daily_load.get(start.date(), 0) + int(row["minutes"])
    busy = scheduler.merge(busy)

    stats = store.teacher_stats()
    mu = store.global_mean()
    history = store.teacher_timings()
    outcomes: list[Outcome] = []
    jobs: list[tuple[Change, estimator.Estimate, scheduler.Job]] = []

    for change in changes:
        row = store.assignment(change.assignment_id)
        if row is None:
            continue

        due = scheduler.parse_dt(row["due_at"], tz)
        if due is None:
            outcomes.append(Outcome(change, 0, "-", [],
                                    problem=f"unparseable due date {row['due_at']}"))
            continue
        # A class you have marked finished stops producing work, even if a
        # stale assignment is still in the capture.
        class_id = (row["class_id"] or "") if "class_id" in row.keys() else ""
        if class_id and class_id.lower() in cfg.scheduling.archived_classes:
            outcomes.append(Outcome(change, 0, "-", [],
                                    problem="archived class, skipped"))
            continue

        # Prep whose deadline has passed cannot be scheduled, and a first sync
        # pulls in a whole year of history. Skip it quietly rather than filling
        # the report with impossible work.
        if cfg.scheduling.skip_past_due and due <= now:
            outcomes.append(Outcome(change, 0, "-", [], problem="past due, skipped"))
            continue

        assigned = scheduler.parse_dt(row["assigned_at"], tz)

        # A moved deadline invalidates everything already placed for this prep.
        if change.kind not in ("new", "replan"):
            for old in store.sessions_for(change.assignment_id):
                old_day = datetime.fromisoformat(old["start_local"]).date()
                daily_load[old_day] = max(0, daily_load.get(old_day, 0) - int(old["minutes"]))
            calendar.delete_by_uid(store.clear_sessions(change.assignment_id))

        est = estimate_for(cfg, row, change.teacher, assigned, due,
                           stats, mu, history)
        store.record_estimate(change.assignment_id, est.minutes, est.source)

        # Time already worked, or booked and not removable, counts towards it.
        # A session's length includes its flow buffer, which is not work, so
        # each one counts as at most one block.
        needed = est.minutes
        if change.kind == "replan":
            block = cfg.scheduling.block_minutes
            needed -= sum(min(int(r["minutes"]), block)
                          for r in store.sessions_for(change.assignment_id)
                          if (r["status"] or "") not in ("missed", "skipped"))
            if needed <= 0:
                outcomes.append(Outcome(change, est.minutes, est.source, [],
                                        problem="already covered"))
                continue

        # Busy time was only loaded up to the horizon. Scheduling past it would
        # place prep in days whose lessons were never fetched, so a long-dated
        # prep is capped here and picked up again as its deadline approaches.
        effective_due = min(due, horizon)
        jobs.append((change, est, scheduler.Job(
            change.assignment_id, row["label"] or row["title"], change.teacher,
            needed, assigned, effective_due)))

    # Planned together, so prep due together shares the time between them.
    plans = scheduler.plan_fairly(cfg.scheduling, [job for _, _, job in jobs],
                                  busy, now, tz, daily_load=daily_load,
                                  availability=availability)
    block = cfg.scheduling.block_minutes
    for (change, est, job), sessions in zip(jobs, plans):
        short = max(0, math.ceil(job.minutes / block) - len(sessions))
        if not sessions:
            outcomes.append(Outcome(change, est.minutes, est.source, [],
                                    problem="NO FREE SLOT before deadline",
                                    short=short))
            continue

        for session in sessions:
            uid = calendar.create(session)
            store.record_session(uid, change.assignment_id,
                                 session.start.isoformat(), session.end.isoformat(),
                                 session.minutes)

        outcomes.append(Outcome(change, est.minutes, est.source, sessions,
                                short=short))

    store.write_export(cfg.export_path)
    return outcomes


def schedule_provisional(cfg: Config, store: Store, calendar: CalendarBackend,
                         now: datetime | None = None,
                         horizon_days: int = 10) -> list[Outcome]:
    """Book what your teachers will almost certainly set, when Teams is down.

    Called only when a capture fails. Without it a dead session means an empty
    diary and prep discovered the night before it is due; with it your
    evenings keep the shape they have had for weeks, and the next successful
    capture corrects any guess.

    A shorter horizon than a normal run on purpose: a guess two weeks out is
    worth much less than one about Thursday, and it would be withdrawn by the
    next successful capture anyway.
    """
    from . import patterns as patterns_module

    tz = ZoneInfo(cfg.timezone)
    now = now or datetime.now(tz)
    horizon = now + timedelta(days=horizon_days)

    learned = patterns_module.learn(store, tz)
    if not learned:
        log.info("no teacher patterns learned yet; nothing to predict")
        return []

    # Deadlines already on record, so a prediction never duplicates real prep.
    known: dict[str, list[datetime]] = {}
    for row in store.upcoming(limit=200):
        due = scheduler.parse_dt(row["due_at"], tz)
        if due and due > now and row["teacher"]:
            known.setdefault(row["teacher"], []).append(due)

    predictions = patterns_module.predict(learned, now, horizon, known)
    if not predictions:
        return []

    busy, routing = diary_context(cfg, store, calendar, now, horizon, tz)
    availability = routing.availability if routing else None

    daily_load: dict[date, int] = {}
    for row in store.all_sessions():
        start = datetime.fromisoformat(row["start_local"])
        end = datetime.fromisoformat(row["end_local"])
        if end > now:
            busy.append(Interval(start, end))
            daily_load[start.date()] = (daily_load.get(start.date(), 0)
                                        + int(row["minutes"]))
    busy = scheduler.merge(busy)

    stats = store.teacher_stats()
    mu = store.global_mean()
    history = store.teacher_timings()
    outcomes: list[Outcome] = []

    for predicted in predictions:
        est = estimator.estimate(cfg.estimator, predicted.teacher,
                                 predicted.assigned, predicted.due,
                                 stats, mu, history=history)

        # A synthetic id, marked so it can be withdrawn wholesale later and
        # never confused with a real Teams assignment.
        assignment_id = (f"provisional-{predicted.teacher}-"
                         f"{predicted.due:%Y%m%d}").replace(" ", "_")
        label = predicted.label
        change = Change("new", assignment_id, label, predicted.teacher,
                        predicted.due.isoformat())

        store.conn.execute(
            """INSERT OR REPLACE INTO assignments
               (assignment_id, title, class_ref, class_id, teacher, label,
                assigned_at, due_at, id_synthetic, first_seen, last_seen,
                estimate_minutes, estimate_source)
               VALUES (?, ?, '', '', ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (assignment_id, label, predicted.teacher, label,
             predicted.assigned.isoformat(), predicted.due.isoformat(),
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat(),
             est.minutes, "predicted"))

        sessions = scheduler.plan(
            cfg.scheduling, assignment_id, label, predicted.teacher,
            est.minutes, max(predicted.assigned, now),
            min(predicted.due, horizon), busy, now, tz, daily_load=daily_load,
            availability=availability)

        if not sessions:
            outcomes.append(Outcome(change, est.minutes, "predicted", [],
                                    problem="NO FREE SLOT before deadline"))
            continue

        for session in sessions:
            uid = calendar.create(session)
            store.record_session(uid, assignment_id, session.start.isoformat(),
                                 session.end.isoformat(), session.minutes,
                                 provisional=True)
            busy = scheduler.merge(busy + [Interval(session.start, session.end)])

        outcomes.append(Outcome(change, est.minutes, "predicted", sessions))

    store.write_export(cfg.export_path)
    return outcomes
