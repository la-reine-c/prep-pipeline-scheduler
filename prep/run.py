"""CLI entry point.

    python -m prep.run --config config.yaml
    python -m prep.run --config config.yaml --dump --dry-run     # first run
    python -m prep.run --config config.yaml --payload-file dumps/x.json --dry-run
    python -m prep.run --config config.yaml --log Dalton:50
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config as config_module
from . import notify, pipeline
from .scheduler import CalDAVCalendar, DryRunCalendar
from .scraper import (SessionExpired, capture, complete_lists,
                      load_payload_file, parse, turned_in)
from .store import PAST_DUE_DAYS, Change, Store, due_after

log = logging.getLogger("prep")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Teams prep scraper and scheduler")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dump", action="store_true", help="save raw captured payloads")
    parser.add_argument("--payload-file", help="use a saved dump instead of a browser")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan against a fake calendar, write nothing to iCloud")
    parser.add_argument("--horizon-days", type=int, default=21)
    parser.add_argument("--log", metavar="TEACHER:MINUTES",
                        help="record how long that teacher's latest prep took")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--test-alerts", action="store_true",
                        help="send a test alert on every configured channel")
    return parser


def _fall_back_to_patterns(cfg, store, args) -> str:
    """Book what the teachers will almost certainly set. Returns a summary."""
    if args.dry_run:
        return "Nothing was booked (dry run)."
    try:
        live = CalDAVCalendar(cfg, ZoneInfo(cfg.timezone))
        store.clear_provisional()          # never stack guess upon guess
        outcomes = pipeline.schedule_provisional(cfg, store, live)
    except Exception as exc:
        log.warning("could not schedule from patterns: %s", exc)
        return "No prep is being scheduled until you sign in again."

    placed = [o for o in outcomes if o.sessions]
    if not placed:
        return ("No prep is being scheduled until you sign in again "
                "(not enough history yet to predict from).")
    return ("In the meantime I have booked " + str(len(placed)) +
            " provisional block(s) from your teachers' usual patterns:\n" +
            "\n".join(f"  {o.change.label} due {o.change.due_at[:10]}"
                       for o in placed) +
            "\nThese are marked provisional and will be replaced by the real "
            "prep as soon as you are signed in again.")


def _warn_if_session_is_running_out(cfg) -> None:
    """Alert once while there is still time to act, never on every run."""
    from . import session as session_module

    try:
        health = session_module.read(cfg.teams.profile_dir)
    except Exception as exc:               # never let a warning break a run
        log.debug("could not read session health: %s", exc)
        return

    if not health.found or not health.auth_cookies:
        return
    if not health.stay_signed_in:
        notify.push(
            cfg.notify, "Prep: sign-in will not survive",
            "The Teams session is a browser-session cookie, so it dies when "
            "the browser closes and every scheduled run starts logged out.\n\n"
            "Fix: python3 -m prep.login --config config.yaml, and tick "
            "'Stay signed in'.",
            priority="high", dedupe_key="session_not_persistent")
        return

    notify.clear(cfg.notify, "session_not_persistent")
    days = health.days_left
    if days is not None and 0 < days < session_module.WARN_DAYS:
        notify.push(
            cfg.notify, f"Prep: sign in again within {days:.0f} days",
            f"The saved Teams session expires in about {days:.0f} days. "
            f"Prep will stop being scheduled when it does.\n\n"
            f"Fix, at a time that suits you: python3 -m prep.login "
            f"--config config.yaml",
            dedupe_key="session_expiring")
    elif days is not None and days >= session_module.WARN_DAYS:
        notify.clear(cfg.notify, "session_expiring")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = config_module.load(args.config)

    if args.test_alerts:
        channels = []
        if cfg.notify.ntfy_topic:
            channels.append("ntfy")
        if cfg.notify.email.to and cfg.notify.email.username:
            channels.append(f"email -> {cfg.notify.email.to}")
        if cfg.notify.telegram.bot_token and cfg.notify.telegram.chat_id:
            channels.append(f"telegram -> chat {cfg.notify.telegram.chat_id}")
        if cfg.notify.imessage_to:
            channels.append(f"imessage -> {cfg.notify.imessage_to}")
        if not channels:
            print("No alert channels configured. Nothing would ever reach you.")
            return 1
        print("configured: " + ", ".join(channels))
        ok = notify.push(cfg.notify, "Prep: test alert",
                         "If you are reading this, alerting works.\n"
                         "This is what a sign-in failure will look like.",
                         priority="high")
        print("delivered on at least one channel" if ok
              else "NOTHING DELIVERED - check credentials and the log above")
        return 0 if ok else 1

    store = Store(cfg.db_path)

    if args.log:
        if ":" not in args.log:
            print("expected TEACHER:MINUTES or 'part of a title':MINUTES")
            return 1
        key, minutes = args.log.rsplit(":", 1)
        key = key.strip().strip("'\"")
        if not minutes.strip().isdigit():
            print(f"'{minutes}' is not a number of minutes")
            return 1

        # Match a teacher first, then fall back to the title. Requiring a
        # resolved teacher was a trap: before the class map is filled in,
        # every teacher is blank, so nothing could ever be logged.
        row = store.conn.execute(
            """SELECT assignment_id, label, teacher FROM assignments
               WHERE teacher = ? COLLATE NOCASE
               ORDER BY due_at DESC LIMIT 1""", (key,)).fetchone()

        if row is None:
            rows = store.find_by_label(key)
            if len(rows) > 1:
                print(f"'{key}' matches {len(rows)} preps - be more specific:")
                for candidate in rows[:8]:
                    print(f"    {candidate['label'][:60]}")
                return 1
            row = rows[0] if rows else None

        if row is None:
            print(f"nothing on record matching '{key}'. Recent preps:")
            for candidate in store.conn.execute(
                    "SELECT label FROM assignments ORDER BY due_at DESC LIMIT 10"):
                print(f"    {candidate['label'][:60]}")
            return 1

        try:
            store.record_timing(row["assignment_id"], int(minutes))
        except ValueError as exc:
            print(f"not logged: {exc}")
            return 1
        store.write_export(cfg.export_path)
        who = row["teacher"] or "unassigned teacher"
        print(f"logged {minutes.strip()} min against {row['label']} ({who})")
        return 0

    if args.dry_run:
        # A dry run plans on a copy. Recording the prep it saw would make the
        # next real run think that prep was already known and never book it.
        real, store = store, store.scratch_copy()
        real.close()

    capture_notes: dict = {}
    try:
        if args.payload_file:
            payloads = load_payload_file(args.payload_file)
        else:
            payloads = capture(cfg, dump=args.dump, notes=capture_notes)
    except SessionExpired as exc:
        store.log_run("session_expired", str(exc))
        # A dead session used to mean an empty diary. It does not have to:
        # your teachers' patterns are already in the database and do not stop
        # being true because a cookie expired.
        booked = _fall_back_to_patterns(cfg, store, args)
        notify.push(
            cfg.notify, "Prep: Teams sign-in needed",
            "The Teams session has expired.\n\n" + booked +
            "\n\nFix: python3 -m prep.login --config config.yaml "
            "(over VNC from your phone if you are out - see "
            "deploy/remote-access.md), and tick 'Stay signed in'.\n\n"
            + str(exc),
            priority="high", dedupe_key="session_expired")
        log.error("session expired: %s", exc)
        store.close()
        return 2
    except Exception as exc:
        store.log_run("capture_failed", str(exc))
        notify.push(cfg.notify, "Prep: scraper failed", str(exc),
                    priority="high", dedupe_key="capture_failed")
        log.exception("capture failed")
        return 3

    # Capture worked, so any previous alert about it is stale. Clearing one
    # tells you it is fixed, and lets the next failure alert again.
    for key in ("session_expired", "capture_failed"):
        notify.clear(cfg.notify, key)

    # Real prep beats a guess. Anything booked from a pattern while the
    # session was down is withdrawn now, before the real schedule is built,
    # so the two can never both occupy the same evening.
    withdrawn = store.clear_provisional()
    if withdrawn:
        log.info("withdrawing %d provisional session(s)", len(withdrawn))

    # A capture that worked today says nothing about next week. Warning while
    # the session is still alive turns an outage into an errand: you
    # re-authenticate when it suits you, rather than finding out because a
    # week of prep silently went unscheduled.
    _warn_if_session_is_running_out(cfg)

    assignments = parse(payloads)
    log.info("parsed %d assignments from %d payloads", len(assignments), len(payloads))

    # Scrolling far enough down Teams' past-due list brings back prep from
    # months ago. It cannot be done now, so it is not worth keeping.
    now = datetime.now(ZoneInfo(cfg.timezone))
    oldest = now - timedelta(days=PAST_DUE_DAYS)
    current = [a for a in assignments
               if not a.due_at or due_after(a.due_at, oldest)]
    if len(current) < len(assignments):
        log.info("ignoring %d prep more than %d days overdue",
                 len(assignments) - len(current), PAST_DUE_DAYS)
    done = [a for a in current if turned_in(a)]
    open_now = [a for a in current if not turned_in(a)]

    changes = store.upsert(open_now, pipeline.teacher_matcher(cfg))

    # Turned in on Teams: finished, so its future sessions come out of the
    # diary and it leaves the dashboard. A capture still loading data, or
    # stopped short of the end of the list, proves nothing about what is
    # missing from it, so only a settled one is read this way.
    settled = capture_notes.get("settled")
    handed_in: list[str] = []
    if settled is None or not (settled.still_arriving or settled.at_bottom is False):
        handed_in = store.mark_handed_in(
            {a.assignment_id for a in open_now}, now, complete_lists(payloads))
        known = {row[0] for row in store.conn.execute(
            "SELECT assignment_id FROM assignments")}
        for item in done:
            if item.assignment_id in known:
                store.conn.execute(
                    "UPDATE assignments SET handed_in_at = COALESCE(handed_in_at, ?) "
                    "WHERE assignment_id = ?", (now.isoformat(), item.assignment_id))
                handed_in.append(item.assignment_id)
    if handed_in:
        log.info("%d prep handed in on Teams", len(handed_in))

    # Tasks you added on the dashboard while iCloud or the timetable was
    # unreachable were saved but never placed. Nothing else would ever plan
    # them - they are not in any capture - so they ride along with this run.
    for row in store.unplanned_tasks():
        changes.append(Change("new", row["assignment_id"], row["label"],
                              row["teacher"] or "", row["due_at"]))

    try:
        live = CalDAVCalendar(cfg, ZoneInfo(cfg.timezone))
        # A dry run still READS the real diary, so the plan it shows is the
        # plan you would actually get. It just never writes.
        calendar = DryRunCalendar(live) if args.dry_run else live
    except Exception as exc:
        store.log_run("calendar_failed", str(exc))
        notify.push(cfg.notify, "Prep: calendar error", str(exc),
                    priority="high", dedupe_key="calendar_failed")
        log.exception("calendar connection failed")
        return 4
    if not args.dry_run:
        notify.clear(cfg.notify, "calendar_failed")

    if handed_in:
        with pipeline.planning_lock(cfg):
            pipeline._clear_future(store, calendar, handed_in, now)

    # Even with nothing new from Teams the run carries on: a lesson added to
    # your Personal calendar, or a club that moved, can leave prep booked on
    # the train. reschedule() finds those bookings and moves them.
    try:
        outcomes = pipeline.reschedule(cfg, store, calendar, changes,
                                       horizon_days=args.horizon_days)
    except pipeline.FeedsUnavailable as exc:
        # Better to schedule nothing than to schedule over lessons.
        store.log_run("feeds_unavailable", str(exc))
        if not changes:
            # Nothing new was waiting to be booked, so nothing is at stake
            # beyond checking for clashes - not worth a high-priority alert.
            log.warning("%s", exc)
            return 5
        notify.push(cfg.notify, "Prep: timetable unreadable",
                    f"Scheduled nothing - the timetable feed failed, and "
                    f"booking prep without it would clash with lessons.\n{exc}",
                    priority="high", dedupe_key="feeds_unavailable")
        log.error("%s", exc)
        return 5
    if not args.dry_run:
        notify.clear(cfg.notify, "feeds_unavailable")
    if not outcomes:
        store.log_run("dry_run" if args.dry_run else "ok", "no changes")
        store.write_export(cfg.export_path)
        print("no changes")
        return 0
    report = "\n".join(o.describe() for o in outcomes)
    store.log_run("dry_run" if args.dry_run else "ok", f"{len(outcomes)} changes")
    if not args.dry_run:
        notify.push(cfg.notify, "Prep rescheduled", report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
