"""The five-minute recheck: re-plan only when something has actually changed.

    python3 -m prep.recheck --config config.yaml
    python3 -m prep.recheck --config config.yaml --dry-run

The full run (prep.run) opens Teams a few times a day. Between those runs the
diary can still go stale, for four reasons, and this puts each one right:

  finished    You logged a time for a prep, so it is done. Its remaining
              bookings are taken out of the diary, and anything it booked
              that you had not confirmed counts as done.
  missed      A session ended more than `confirm_grace_hours` (24) ago and you
              never said you did it - or you pressed "Missed" on the
              dashboard. That time no longer counts towards the prep, and the
              prep is re-planned to make it up before the deadline.
  timing      The estimate for a prep has moved by at least one block since
              it was booked: you logged a time for that teacher, or corrected
              it by hand. Its future bookings are re-planned to the new length.
  unplanned   Prep that was saved but never booked, because the timetable or
              the calendar was unreachable on the run that first saw it.

Until the grace period runs out an unconfirmed session counts as done. You
may not get round to saying so the same evening, and booking the time again
the minute a session ends would fill the next evening with prep you had in
fact already done.

When none of these has happened - which is almost every time - it does no
network I/O at all: it reads the database and stops. Nothing that is fine is
moved, so the diary does not reshuffle itself every five minutes.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config as config_module
from . import notify, pipeline, scheduler
from .config import Config
from .store import Change, Store

log = logging.getLogger(__name__)


@dataclass
class Findings:
    """What the database says needs doing. Nothing has been done yet."""
    finished: dict[str, list[str]] = field(default_factory=dict)  # aid -> future uids
    missed: dict[str, list[str]] = field(default_factory=dict)    # aid -> missed uids
    timing: dict[str, tuple[int, int]] = field(default_factory=dict)  # aid -> (old, new)
    unplanned: list[str] = field(default_factory=list)
    # Missed sessions of prep already due: nothing to make up, only to record.
    settled: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.finished or self.missed or self.timing or self.unplanned)

    def replans(self) -> list[str]:
        """Prep to re-plan, in a stable order with no repeats."""
        seen: dict[str, None] = {}
        for aid in [*self.missed, *self.timing, *self.unplanned]:
            seen.setdefault(aid, None)
        return list(seen)


@dataclass
class Result:
    findings: Findings
    outcomes: list[pipeline.Outcome] = field(default_factory=list)
    removed: int = 0                 # bookings taken out for finished prep
    newly_missed: int = 0            # sessions that ran out of grace this time
    error: str = ""

    def changed(self) -> bool:
        return bool(self.removed or any(o.sessions for o in self.outcomes))

    def describe(self, store: Store) -> str:
        lines = []
        for aid, uids in self.findings.finished.items():
            lines.append(f"FINISHED {_label(store, aid)}: "
                         f"{len(uids)} booking(s) removed")
        for aid, uids in self.findings.missed.items():
            lines.append(f"MISSED {_label(store, aid)}: "
                         f"{len(uids)} session(s) to make up")
        for aid, (old, new) in self.findings.timing.items():
            lines.append(f"TIMING {_label(store, aid)}: {old} -> {new} min")
        for aid in self.findings.unplanned:
            lines.append(f"UNPLANNED {_label(store, aid)}")
        lines.extend(o.describe() for o in self.outcomes)
        if self.error:
            lines.append(f"NOT DONE: {self.error}")
        return "\n".join(lines)


def _label(store: Store, assignment_id: str) -> str:
    row = store.assignment(assignment_id)
    return (row["label"] or row["title"]) if row else assignment_id


def _blocks(minutes: int | None, block: int) -> int:
    return math.ceil((minutes or 0) / block) if minutes else 0


def expire_unconfirmed(cfg: Config, store: Store, now: datetime) -> int:
    """Mark sessions missed once their grace period has run out. Returns how
    many. Database only."""
    grace = timedelta(hours=cfg.scheduling.confirm_grace_hours)
    count = 0
    for row in store.unconfirmed(now - grace):
        store.set_session_status(row["uid"], "missed")
        count += 1
    if count:
        log.info("%d session(s) unconfirmed for %s h: counted as missed",
                 count, cfg.scheduling.confirm_grace_hours)
    return count


def inspect(cfg: Config, store: Store, now: datetime) -> Findings:
    """Read the database and say what needs doing. Changes nothing."""
    tz = ZoneInfo(cfg.timezone)
    findings = Findings()
    logged = store.logged_ids()
    archived = cfg.scheduling.archived_classes

    def live(row) -> bool:
        due = scheduler.parse_dt(row["due_at"], tz)
        class_id = (row["class_id"] or "").lower()
        return (due is not None and due > now
                and not (class_id and class_id in archived)
                and not row["assignment_id"].startswith("provisional-"))

    for row in store.all_sessions():
        if row["provisional"]:
            continue
        aid = row["assignment_id"]
        start = datetime.fromisoformat(row["start_local"])
        status = row["status"] or ""
        if aid in logged:
            if start >= now:
                findings.finished.setdefault(aid, []).append(row["uid"])
        elif status == "missed":
            findings.missed.setdefault(aid, []).append(row["uid"])

    # Missed sessions of prep that is already due cannot be made up; they are
    # settled without a re-plan.
    for aid in list(findings.missed):
        row = store.assignment(aid)
        if row is None or not live(row):
            findings.settled.extend(findings.missed.pop(aid))

    # Estimates that have moved by a whole block since they were booked.
    stats = store.teacher_stats()
    mu = store.global_mean()
    history = store.teacher_timings()
    block = cfg.scheduling.block_minutes
    booked = {row["assignment_id"] for row in store.all_sessions()
              if not row["provisional"]}
    for aid in booked - logged - set(findings.missed):
        row = store.assignment(aid)
        if row is None or not live(row) or row["estimate_minutes"] is None:
            continue
        due = scheduler.parse_dt(row["due_at"], tz)
        assigned = scheduler.parse_dt(row["assigned_at"], tz)
        est = pipeline.estimate_for(cfg, row, row["teacher"] or "", assigned,
                                    due, stats, mu, history)
        old = int(row["estimate_minutes"])
        if _blocks(est.minutes, block) != _blocks(old, block):
            findings.timing[aid] = (old, est.minutes)

    findings.unplanned = [row["assignment_id"] for row in store.unplanned(now)
                          if live(row)]
    return findings


def _remove_finished(cfg: Config, store: Store, calendar,
                     finished: dict[str, list[str]]) -> int:
    uids = [uid for group in finished.values() for uid in group]
    if not uids:
        return 0
    with pipeline.planning_lock(cfg):
        failed = set(calendar.delete_by_uid(uids) or [])
        store.delete_sessions([uid for uid in uids if uid not in failed])
    if failed:
        log.warning("%d booking(s) for finished prep could not be removed",
                    len(failed))
    return len(uids) - len(failed)


def run(cfg: Config, store: Store, calendar_factory,
        now: datetime | None = None) -> Result:
    """Do whatever the database says needs doing, and nothing else.

    `calendar_factory` is only called when there is work, so a quiet recheck
    never touches the network.
    """
    tz = ZoneInfo(cfg.timezone)
    now = now or datetime.now(tz)

    # Prep logged as finished: what it booked and you never confirmed was
    # evidently done, so none of it can later expire into "missed".
    store.confirm_finished(now)

    newly_missed = expire_unconfirmed(cfg, store, now)
    findings = inspect(cfg, store, now)
    for uid in findings.settled:
        store.set_session_status(uid, "skipped")
    result = Result(findings, newly_missed=newly_missed)
    if not findings:
        return result

    try:
        calendar = calendar_factory()
        result.removed = _remove_finished(cfg, store, calendar, findings.finished)
        changes = []
        for aid in findings.replans():
            row = store.assignment(aid)
            if row is None:
                continue
            # Never booked at all: planned like new prep. Otherwise a re-plan,
            # which keeps what is already done and books only the rest.
            kind = "new" if aid in findings.unplanned else "replan"
            changes.append(Change(kind, aid, row["label"] or row["title"],
                                  row["teacher"] or "", row["due_at"]))
        if changes:
            result.outcomes = pipeline.reschedule(cfg, store, calendar,
                                                  changes, now=now)
    except pipeline.FeedsUnavailable as exc:
        # Missed sessions stay "missed", so the next recheck tries again.
        result.error = f"timetable feed unreachable, nothing moved ({exc})"
        log.warning("%s", result.error)
        return result
    except Exception as exc:
        result.error = f"calendar unreachable, nothing moved ({exc})"
        log.warning("%s", result.error)
        return result

    # Booked again, so the missed time is settled. A re-plan may already
    # have taken a missed booking out, if it had not yet started.
    for uids in findings.missed.values():
        for uid in uids:
            try:
                store.set_session_status(uid, "skipped")
            except ValueError:
                pass
    store.write_export(cfg.export_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-plan only what has changed since the last check")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would change; write nothing anywhere")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_module.load(args.config)
    tz = ZoneInfo(cfg.timezone)

    store = Store(cfg.db_path)
    if args.dry_run:
        import tempfile
        from pathlib import Path

        real, store = store, store.scratch_copy()
        real.close()
        cfg.export_path = Path(tempfile.mkdtemp(prefix="prep-recheck-")) / "prep.txt"

    def calendar_factory():
        live = scheduler.CalDAVCalendar(cfg, tz)
        return scheduler.DryRunCalendar(live) if args.dry_run else live

    try:
        result = run(cfg, store, calendar_factory)
        if not result.findings:
            print("nothing to recheck")
            return 0
        report = result.describe(store)
        print(report)
        if result.error:
            return 1
        if result.changed() and not args.dry_run:
            title = ("Prep: missed session rebooked" if result.findings.missed
                     else "Prep rescheduled")
            notify.push(cfg.notify, title, report)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
