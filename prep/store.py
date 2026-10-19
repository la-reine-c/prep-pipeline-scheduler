"""SQLite is the authoritative state. The txt export is a disposable view of it."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from .scraper import RawAssignment
from .isotime import fromiso

# A logged time shorter than this still marks the prep finished, but the
# estimator does not learn from it: the prediction stays as it was.
MIN_LEARNED_MINUTES = 30

# Past-due prep stays on the dashboard for this long, then drops off. Teams'
# past-due list goes back months, and a capture that scrolls far enough down
# it would otherwise fill the page with prep from last term.
PAST_DUE_DAYS = 7

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS assignments (
    assignment_id TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    class_ref     TEXT,
    class_id      TEXT,
    teacher       TEXT,
    label         TEXT,
    assigned_at   TEXT,
    due_at        TEXT,
    id_synthetic  INTEGER DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    estimate_minutes INTEGER,
    estimate_source  TEXT,
    override_minutes INTEGER
);

-- Times you cannot work. A weekday entry repeats; a dated one is one-off.
CREATE TABLE IF NOT EXISTS blackouts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday    INTEGER,          -- 0=Mon..6=Sun, NULL for a one-off
    on_date    TEXT,             -- YYYY-MM-DD, NULL for a weekly entry
    start_time TEXT NOT NULL,    -- HH:MM
    end_time   TEXT NOT NULL,
    label      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL,
    minutes       INTEGER NOT NULL,
    note          TEXT,
    logged_at     TEXT NOT NULL,
    FOREIGN KEY (assignment_id) REFERENCES assignments(assignment_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    uid           TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL,
    start_local   TEXT NOT NULL,
    end_local     TEXT NOT NULL,
    minutes       INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (assignment_id) REFERENCES assignments(assignment_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_assignment ON sessions(assignment_id);
CREATE INDEX IF NOT EXISTS idx_timings_assignment ON timings(assignment_id);
"""


@dataclass
class Change:
    kind: str                  # new | due_changed | title_changed | replan
    assignment_id: str
    label: str
    teacher: str
    due_at: str | None
    previous_due: str | None = None

    def describe(self) -> str:
        who = self.teacher or "?"
        if self.kind == "new":
            return f"NEW {who}: {self.label} (due {self.due_at})"
        if self.kind == "replan":
            return f"REPLAN {who}: {self.label}"
        if self.kind == "due_changed":
            return f"MOVED {who}: {self.label} ({self.previous_due} -> {self.due_at})"
        return f"EDIT {who}: {self.label}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ended(end_local: str, now: datetime) -> bool:
    try:
        return datetime.fromisoformat(end_local) <= now
    except (TypeError, ValueError):
        return False


def due_after(due_at: str | None, now: datetime) -> bool:
    try:
        due = fromiso(due_at or "")
    except ValueError:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due > now


def _parse_iso(value: str | None, what: str) -> datetime:
    try:
        parsed = fromiso(value or "")
    except ValueError:
        raise ValueError(f"'{value}' is not a {what} date") from None
    if parsed.tzinfo is None:
        raise ValueError(f"the {what} date needs a timezone")
    return parsed


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    # Columns added after the first release. CREATE TABLE IF NOT EXISTS does
    # nothing to a table that already exists, so an older database would be
    # missing these and fail on insert. Your logged timings live here, so the
    # database is migrated rather than thrown away.
    MIGRATIONS = {
        "assignments": {
            "class_id": "TEXT",
            "estimate_minutes": "INTEGER",
            "estimate_source": "TEXT",
            "override_minutes": "INTEGER",
            # Tasks you added on the dashboard rather than ones Teams set.
            # Kept in this table so estimating, scheduling, logging and
            # corrections all work on them unchanged.
            "manual": "INTEGER DEFAULT 0",
            # When Teams stopped listing it as work still to do: you turned
            # it in. NULL while it is still open. Counts as finished, like a
            # logged time, but teaches the estimator nothing.
            "handed_in_at": "TEXT",
        },
        # Blocks booked from a teacher's pattern while Teams was unreachable.
        # Kept apart from real sessions so they can be withdrawn wholesale the
        # moment a real capture succeeds.
        "sessions": {
            "provisional": "INTEGER DEFAULT 0",
            # Whether you actually did it: '' not confirmed yet, 'done',
            # 'missed' (to be booked again) or 'skipped' (missed, and its
            # time has been booked again). See prep/recheck.py.
            "status": "TEXT DEFAULT ''",
            "status_at": "TEXT",
        },
        # Where a blackout happens - a postcode or a named place. Empty means
        # "busy, wherever I am"; set, it is somewhere you have to travel to.
        "blackouts": {
            "location": "TEXT DEFAULT ''",
            # How you get there: "car", "public", or "" for the default.
            "travel": "TEXT DEFAULT ''",
        },
        # Whether the estimator learns from this timing: 0 for one under
        # MIN_LEARNED_MINUTES.
        "timings": {
            "learn": "INTEGER DEFAULT 1",
        },
    }

    def _migrate(self) -> None:
        added: set[tuple[str, str]] = set()
        for table, columns in self.MIGRATIONS.items():
            existing = {row[1] for row in
                        self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for column, column_type in columns.items():
                if column not in existing:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                    added.add((table, column))

        if ("timings", "learn") in added:
            self.conn.execute("UPDATE timings SET learn = (minutes >= ?)",
                              (MIN_LEARNED_MINUTES,))

        # Sessions booked before anyone was asked whether they happened. Left
        # unconfirmed, the first recheck would call every one of them missed
        # and book a term's worth of prep a second time.
        if ("sessions", "status") in added:
            now = datetime.now(timezone.utc)
            past = [row["uid"] for row in self.conn.execute(
                        "SELECT uid, end_local FROM sessions")
                    if _ended(row["end_local"], now)]
            self.conn.executemany(
                "UPDATE sessions SET status = 'done', status_at = ? WHERE uid = ?",
                [(_now(), uid) for uid in past])

        # A dry run used to record the sessions it only pretended to book.
        # They were never in any calendar, and while they sat here they
        # counted as booked, so the prep they belonged to was never booked
        # for real. Forgetting the estimate as well makes that prep look
        # unplanned, which the next recheck picks up and books properly.
        fake = self.conn.execute(
            "SELECT DISTINCT assignment_id FROM sessions "
            "WHERE uid LIKE 'dry-run-%'").fetchall()
        if fake:
            self.conn.execute("DELETE FROM sessions WHERE uid LIKE 'dry-run-%'")
            self.conn.executemany(
                """UPDATE assignments SET estimate_minutes = NULL,
                          estimate_source = NULL
                   WHERE assignment_id = ?
                     AND NOT EXISTS (SELECT 1 FROM sessions s
                                     WHERE s.assignment_id = ?)""",
                [(row[0], row[0]) for row in fake])

    def scratch_copy(self) -> "Store":
        """An in-memory copy of this database, for a dry run.

        A dry run must leave nothing behind. Recording the prep it saw would
        make the next real run think that prep was already known, and so
        never book it; recording sessions it never booked would make them
        count as done.
        """
        copy = Store(":memory:")
        self.conn.backup(copy.conn)
        return copy

    def close(self) -> None:
        self.conn.close()

    # ---------- assignments ----------

    def upsert(self, assignments: Iterable[RawAssignment],
               teacher_for: Callable[[RawAssignment], str]) -> list[Change]:
        changes: list[Change] = []
        now = _now()

        for item in assignments:
            teacher = teacher_for(item)
            row = self.conn.execute(
                "SELECT title, due_at FROM assignments WHERE assignment_id = ?",
                (item.assignment_id,),
            ).fetchone()

            if row is None:
                self.conn.execute(
                    """INSERT INTO assignments
                       (assignment_id, title, class_ref, class_id, teacher, label,
                        assigned_at, due_at, id_synthetic, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (item.assignment_id, item.title, item.class_ref, item.class_id,
                     teacher, item.label, item.assigned_at, item.due_at,
                     int(item.id_is_synthetic), now, now),
                )
                changes.append(Change("new", item.assignment_id, item.label,
                                      teacher, item.due_at))
                continue

            if row["due_at"] != item.due_at:
                changes.append(Change("due_changed", item.assignment_id, item.label,
                                      teacher, item.due_at, previous_due=row["due_at"]))
            elif row["title"] != item.title:
                changes.append(Change("title_changed", item.assignment_id, item.label,
                                      teacher, item.due_at))

            self.conn.execute(
                """UPDATE assignments
                   SET title = ?, class_ref = ?, class_id = ?, teacher = ?, label = ?,
                       assigned_at = ?, due_at = ?, last_seen = ?,
                       handed_in_at = NULL
                   WHERE assignment_id = ?""",
                (item.title, item.class_ref, item.class_id, teacher, item.label,
                 item.assigned_at, item.due_at, now, item.assignment_id),
            )

        return changes

    def mark_handed_in(self, open_ids: set[str], now: datetime,
                       complete: list[tuple[datetime | None, datetime | None]]
                       ) -> list[str]:
        """Prep Teams no longer lists as open: turned in. Returns their ids.

        Teams' list only carries work you have not turned in, so handing
        something in shows up as it vanishing from the capture. `complete`
        is the due-date ranges the capture holds in full (see
        scraper.complete_lists): prep due outside them may simply not have
        been fetched, so it is left alone. So are your own tasks and
        provisional guesses, which never come from Teams, and prep more than
        PAST_DUE_DAYS overdue, which is off the dashboard anyway.
        """
        def covered(due: datetime) -> bool:
            return any((lo is None or due >= lo) and (hi is None or due <= hi)
                       for lo, hi in complete)

        cutoff = now - timedelta(days=PAST_DUE_DAYS)
        rows = self.conn.execute(
            """SELECT assignment_id, due_at FROM assignments
               WHERE handed_in_at IS NULL AND COALESCE(manual, 0) = 0
                 AND assignment_id NOT LIKE 'provisional-%'""").fetchall()
        gone = []
        for row in rows:
            if row["assignment_id"] in open_ids or not due_after(row["due_at"], cutoff):
                continue
            try:
                due = _parse_iso(row["due_at"], "due")
            except ValueError:
                continue
            if covered(due):
                gone.append(row["assignment_id"])
        stamp = _now()
        self.conn.executemany(
            "UPDATE assignments SET handed_in_at = ? WHERE assignment_id = ?",
            [(stamp, aid) for aid in gone])
        return gone

    def record_estimate(self, assignment_id: str, minutes: int, source: str) -> None:
        self.conn.execute(
            "UPDATE assignments SET estimate_minutes = ?, estimate_source = ? "
            "WHERE assignment_id = ?", (int(minutes), source, assignment_id))

    def set_override(self, assignment_id: str, minutes: int | None) -> None:
        """Correct a wrong prediction by hand. None clears it."""
        if minutes is not None:
            minutes = int(minutes)
            if minutes <= 0 or minutes > 24 * 60:
                raise ValueError(f"{minutes} minutes is not a sensible correction")
        self.conn.execute(
            "UPDATE assignments SET override_minutes = ? WHERE assignment_id = ?",
            (minutes, assignment_id))

    # ---------- your own tasks ----------

    def add_task(self, title: str, due_at: str, minutes: int | None = None,
                 start_at: str | None = None, teacher: str = "") -> str:
        """Record work Teams does not know about. Returns its id.

        `due_at` and `start_at` are ISO datetimes. Minutes you give become the
        override, so they win over any prediction exactly as a correction
        does; leave them out and the task is estimated like prep.

        A teacher is optional. With one, the task counts towards that
        teacher's timings like anything they set in Teams. Without one it is
        kept out of every average, so a long personal project cannot inflate
        the estimate for prep.
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("a task needs a name")
        due = _parse_iso(due_at, "due")
        start = _parse_iso(start_at, "start") if start_at else None
        if start is not None and start >= due:
            raise ValueError("a task must be due after it starts")
        if minutes is not None:
            minutes = int(minutes)
            if minutes <= 0 or minutes > 24 * 60:
                raise ValueError(f"{minutes} minutes is not a sensible task length")

        task_id = f"task-{uuid.uuid4().hex[:12]}"
        now = _now()
        self.conn.execute(
            """INSERT INTO assignments
               (assignment_id, title, class_ref, class_id, teacher, label,
                assigned_at, due_at, id_synthetic, first_seen, last_seen,
                override_minutes, manual)
               VALUES (?, ?, '', '', ?, ?, ?, ?, 1, ?, ?, ?, 1)""",
            (task_id, title, (teacher or "").strip(), title,
             start_at or now, due_at, now, now, minutes))
        return task_id

    def delete_task(self, assignment_id: str) -> list[str]:
        """Remove one of your tasks. Returns calendar uids to delete.

        Refuses anything Teams set: the next capture would only bring it
        back, and its logged times belong to that teacher's history.
        """
        row = self.assignment(assignment_id)
        if row is None:
            raise ValueError("no such task")
        if not row["manual"]:
            raise ValueError("only tasks you added can be removed")
        uids = self.clear_sessions(assignment_id)
        self.conn.execute("DELETE FROM timings WHERE assignment_id = ?",
                          (assignment_id,))
        self.conn.execute("DELETE FROM assignments WHERE assignment_id = ?",
                          (assignment_id,))
        return uids

    def unplanned_tasks(self) -> list[sqlite3.Row]:
        """Tasks never successfully planned - added while the calendar or the
        timetable feed was down. The next run picks them up.

        A task that WAS planned and found no slot has an estimate recorded,
        so it is not retried every run and re-reported each time; the
        Reschedule button is for that.
        """
        return self.conn.execute(
            """SELECT * FROM assignments a
               WHERE a.manual = 1 AND a.estimate_minutes IS NULL
                 AND NOT EXISTS (SELECT 1 FROM sessions s
                                 WHERE s.assignment_id = a.assignment_id)
               ORDER BY a.due_at"""
        ).fetchall()

    def backlog(self) -> list[sqlite3.Row]:
        """Your tasks with nothing booked: no free slot before the deadline,
        or not planned yet. Unlike `upcoming`, it has no limit, so a saved
        task always has somewhere to show up on the dashboard.
        """
        return self.conn.execute(
            self._UPCOMING_SELECT +
            """ WHERE a.manual = 1
                  AND NOT EXISTS (SELECT 1 FROM sessions s
                                  WHERE s.assignment_id = a.assignment_id)
                ORDER BY julianday(a.due_at)"""
        ).fetchall()

    def known_teachers(self) -> list[str]:
        rows = self.conn.execute(
            """SELECT DISTINCT teacher FROM assignments
               WHERE teacher IS NOT NULL AND teacher <> ''
                 AND assignment_id NOT LIKE 'provisional-%'
               ORDER BY teacher COLLATE NOCASE""").fetchall()
        return [row["teacher"] for row in rows]

    # ---------- blackouts ----------

    def add_blackout(self, start_time: str, end_time: str, weekday: int | None = None,
                     on_date: str | None = None, label: str = "",
                     location: str = "", travel: str = "") -> int:
        if weekday is None and not on_date:
            raise ValueError("a blackout needs either a weekday or a date")
        if start_time >= end_time:
            raise ValueError("a blackout must end after it starts")
        cursor = self.conn.execute(
            """INSERT INTO blackouts (weekday, on_date, start_time, end_time,
                                      label, created_at, location, travel)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (weekday, on_date, start_time, end_time, label, _now(),
             (location or "").strip(),
             travel if travel in ("car", "public") else ""))
        return cursor.lastrowid

    def blackouts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM blackouts ORDER BY weekday IS NULL, weekday, on_date, "
            "start_time").fetchall()

    def delete_blackout(self, blackout_id: int) -> None:
        self.conn.execute("DELETE FROM blackouts WHERE id = ?", (int(blackout_id),))

    def find_by_label(self, needle: str) -> list[sqlite3.Row]:
        """Look up preps by part of their name.

        % and _ are LIKE wildcards, and prep titles really do contain them
        ("100% recall", "T1_prep"). Unescaped, "100%" would match every row.
        """
        needle = (needle or "").strip()
        if not needle:
            return []
        escaped = (needle.replace("\\", "\\\\")
                         .replace("%", "\\%")
                         .replace("_", "\\_"))
        return self.conn.execute(
            """SELECT assignment_id, label, teacher FROM assignments
               WHERE label LIKE ? ESCAPE '\\' COLLATE NOCASE
               ORDER BY due_at DESC""",
            (f"%{escaped}%",),
        ).fetchall()

    def assignment(self, assignment_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM assignments WHERE assignment_id = ?", (assignment_id,)
        ).fetchone()

    def upcoming(self, limit: int = 50, now: datetime | None = None
                 ) -> list[sqlite3.Row]:
        """Preps by due date, oldest first - or, given `now`, what is still to
        come first (soonest first), then what has passed (most recent first).

        The dashboard passes `now`. Oldest-first with a limit showed the
        dashboard the fifty OLDEST preps once a year of history had been
        captured, so everything actually upcoming - and any task you had just
        added - fell off the end of the list.
        """
        if now is not None:
            # Stored dates carry mixed offsets ("Z", "+01:00"), so they are
            # compared as instants, not as text.
            cutoff = now.astimezone(timezone.utc).isoformat()
            oldest = (now - timedelta(days=PAST_DUE_DAYS)
                      ).astimezone(timezone.utc).isoformat()
            return self.conn.execute(
                self._UPCOMING_SELECT +
                """ WHERE a.handed_in_at IS NULL
                      AND (a.due_at IS NULL
                           OR julianday(a.due_at) >= julianday(?))
                    ORDER BY CASE WHEN a.due_at IS NULL THEN 2
                                  WHEN julianday(a.due_at) >= julianday(?) THEN 0
                                  ELSE 1 END,
                             CASE WHEN julianday(a.due_at) >= julianday(?)
                                  THEN julianday(a.due_at) END ASC,
                             julianday(a.due_at) DESC
                    LIMIT ?""",
                (oldest, cutoff, cutoff, limit)).fetchall()
        return self.conn.execute(
            """SELECT a.*,
                      (SELECT COUNT(*) FROM sessions s WHERE s.assignment_id = a.assignment_id)
                        AS session_count,
                      (SELECT COALESCE(SUM(s.minutes), 0) FROM sessions s
                        WHERE s.assignment_id = a.assignment_id) AS scheduled_minutes,
                      (SELECT t.minutes FROM timings t
                        WHERE t.assignment_id = a.assignment_id
                        ORDER BY t.id DESC LIMIT 1) AS actual_minutes,
                      COALESCE(a.override_minutes, a.estimate_minutes) AS predicted_minutes
               FROM assignments a
               ORDER BY a.due_at IS NULL, a.due_at
               LIMIT ?""",
            (limit,),
        ).fetchall()

    _UPCOMING_SELECT = """SELECT a.*,
                      (SELECT COUNT(*) FROM sessions s WHERE s.assignment_id = a.assignment_id
                         AND COALESCE(s.status, '') NOT IN ('missed', 'skipped'))
                        AS session_count,
                      (SELECT COALESCE(SUM(s.minutes), 0) FROM sessions s
                        WHERE s.assignment_id = a.assignment_id
                          AND COALESCE(s.status, '') NOT IN ('missed', 'skipped'))
                        AS scheduled_minutes,
                      (SELECT t.minutes FROM timings t
                        WHERE t.assignment_id = a.assignment_id
                        ORDER BY t.id DESC LIMIT 1) AS actual_minutes,
                      COALESCE(a.override_minutes, a.estimate_minutes) AS predicted_minutes
               FROM assignments a"""

    # ---------- timings ----------

    def record_timing(self, assignment_id: str, minutes: int, note: str = "") -> None:
        # A zero or negative timing is always a typo, and it would drag down
        # both that teacher's mean and the global mean that every unlogged
        # teacher now falls back to. Reject rather than absorb it.
        minutes = int(minutes)
        if minutes <= 0:
            raise ValueError(f"a prep cannot take {minutes} minutes")
        if minutes > 24 * 60:
            raise ValueError(f"{minutes} minutes is longer than a day - typo?")
        self.conn.execute(
            "INSERT INTO timings (assignment_id, minutes, note, logged_at, learn) "
            "VALUES (?, ?, ?, ?, ?)",
            (assignment_id, int(minutes), note, _now(),
             int(minutes >= MIN_LEARNED_MINUTES)),
        )

    def teacher_stats(self) -> dict[str, tuple[int, float]]:
        """teacher -> (count, mean minutes) from logged timings."""
        rows = self.conn.execute(
            """SELECT a.teacher AS teacher, COUNT(t.id) AS n, AVG(t.minutes) AS mean
               FROM timings t JOIN assignments a ON a.assignment_id = t.assignment_id
               WHERE a.teacher IS NOT NULL AND a.teacher <> ''
                 AND t.learn = 1
               GROUP BY a.teacher"""
        ).fetchall()
        return {r["teacher"]: (r["n"], float(r["mean"])) for r in rows}

    def calibration(self) -> list[dict]:
        """Per teacher: typical prediction vs typical actual.

        Medians, not means. One 300-minute evening that the filter correctly
        damps would otherwise drag the chart's "actual" up and make a good
        prediction look badly under - the chart would contradict the estimator.
        """
        rows = self.conn.execute(
            """SELECT a.teacher AS teacher,
                      COALESCE(a.override_minutes, a.estimate_minutes) AS predicted,
                      t.minutes AS actual
               FROM assignments a
               JOIN timings t ON t.assignment_id = a.assignment_id
               WHERE a.teacher IS NOT NULL AND a.teacher <> ''
                 AND a.estimate_minutes IS NOT NULL
               ORDER BY a.teacher"""
        ).fetchall()

        def median(values: list[float]) -> float:
            ordered = sorted(values)
            middle = len(ordered) // 2
            if len(ordered) % 2:
                return float(ordered[middle])
            return (ordered[middle - 1] + ordered[middle]) / 2

        grouped: dict[str, dict[str, list[float]]] = {}
        for row in rows:
            bucket = grouped.setdefault(row["teacher"],
                                        {"predicted": [], "actual": []})
            bucket["predicted"].append(float(row["predicted"]))
            bucket["actual"].append(float(row["actual"]))

        return [
            {"teacher": teacher,
             "n": len(values["actual"]),
             "mean_predicted": median(values["predicted"]),
             "mean_actual": median(values["actual"])}
            for teacher, values in sorted(grouped.items())
        ]

    def teacher_timings(self) -> dict[str, list[float]]:
        """Every logged time per teacher, oldest first - the filter needs order."""
        rows = self.conn.execute(
            """SELECT a.teacher AS teacher, t.minutes AS minutes
               FROM timings t JOIN assignments a
                 ON a.assignment_id = t.assignment_id
               WHERE a.teacher IS NOT NULL AND a.teacher <> ''
                 AND t.learn = 1
               ORDER BY t.logged_at, t.id"""
        ).fetchall()
        history: dict[str, list[float]] = {}
        for row in rows:
            history.setdefault(row["teacher"], []).append(float(row["minutes"]))
        return history

    def global_mean(self) -> float | None:
        # Your own tasks with no teacher are left out: this mean is what every
        # unlogged teacher's prep is estimated from, and an evening on a
        # personal project says nothing about how long prep takes.
        row = self.conn.execute(
            """SELECT AVG(t.minutes) AS mean FROM timings t
               LEFT JOIN assignments a ON a.assignment_id = t.assignment_id
               WHERE t.learn = 1
                 AND NOT (COALESCE(a.manual, 0) = 1
                          AND COALESCE(a.teacher, '') = '')""").fetchone()
        return float(row["mean"]) if row and row["mean"] is not None else None

    # ---------- sessions ----------

    def sessions_for(self, assignment_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE assignment_id = ? ORDER BY start_local",
            (assignment_id,),
        ).fetchall()

    def clear_sessions(self, assignment_id: str) -> list[str]:
        uids = [r["uid"] for r in self.sessions_for(assignment_id)]
        self.conn.execute("DELETE FROM sessions WHERE assignment_id = ?", (assignment_id,))
        return uids

    def delete_sessions(self, uids: list[str]) -> None:
        self.conn.executemany("DELETE FROM sessions WHERE uid = ?",
                              [(uid,) for uid in uids])

    def record_session(self, uid: str, assignment_id: str, start_local: str,
                       end_local: str, minutes: int,
                       provisional: bool = False) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO sessions
               (uid, assignment_id, start_local, end_local, minutes,
                created_at, provisional)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (uid, assignment_id, start_local, end_local, int(minutes), _now(),
             int(provisional)),
        )

    def set_session_status(self, uid: str, status: str) -> sqlite3.Row:
        """Record whether a session happened. Returns the session."""
        if status not in ("", "done", "missed", "skipped"):
            raise ValueError(f"'{status}' is not a session status")
        row = self.conn.execute("SELECT * FROM sessions WHERE uid = ?",
                                (uid,)).fetchone()
        if row is None:
            raise ValueError("no such session")
        self.conn.execute(
            "UPDATE sessions SET status = ?, status_at = ? WHERE uid = ?",
            (status, _now(), uid))
        return row

    def unconfirmed(self, now: datetime) -> list[sqlite3.Row]:
        """Real sessions that have ended and that you have not said you did
        or missed, oldest first, for prep not already logged as finished."""
        rows = self.conn.execute(
            """SELECT s.*, a.label, a.teacher, a.due_at FROM sessions s
               JOIN assignments a ON a.assignment_id = s.assignment_id
               WHERE COALESCE(s.status, '') = '' AND s.provisional = 0
                 AND NOT EXISTS (SELECT 1 FROM timings t
                                 WHERE t.assignment_id = s.assignment_id)
               ORDER BY s.start_local""").fetchall()
        return [row for row in rows if _ended(row["end_local"], now)]

    def confirm_finished(self, now: datetime) -> int:
        """Sessions that have ended for prep you logged as finished: done.
        Returns how many."""
        rows = self.conn.execute(
            """SELECT s.uid, s.end_local FROM sessions s
               WHERE COALESCE(s.status, '') = '' AND s.provisional = 0
                 AND (EXISTS (SELECT 1 FROM timings t
                              WHERE t.assignment_id = s.assignment_id)
                      OR EXISTS (SELECT 1 FROM assignments a
                                 WHERE a.assignment_id = s.assignment_id
                                   AND a.handed_in_at IS NOT NULL))"""
        ).fetchall()
        ended = [row["uid"] for row in rows if _ended(row["end_local"], now)]
        self.conn.executemany(
            "UPDATE sessions SET status = 'done', status_at = ? WHERE uid = ?",
            [(_now(), uid) for uid in ended])
        return len(ended)

    def logged_ids(self) -> set[str]:
        """Finished prep: you logged a time for it, or turned it in on Teams."""
        return {row[0] for row in self.conn.execute(
            """SELECT assignment_id FROM timings
               UNION
               SELECT assignment_id FROM assignments
               WHERE handed_in_at IS NOT NULL""")}

    def unplanned(self, now: datetime) -> list[sqlite3.Row]:
        """Prep still to come that was saved but never planned.

        Planning records an estimate before it looks for a slot, so a prep
        with no estimate and nothing booked never got that far: the
        timetable feed or the calendar failed on the run that first saw it,
        or a dry run's pretend booking was cleared away. A prep that was
        planned and found no slot has its estimate, and is not retried
        every few minutes and re-reported each time.
        """
        rows = self.conn.execute(
            """SELECT * FROM assignments a
               WHERE a.estimate_minutes IS NULL
                 AND a.handed_in_at IS NULL
                 AND a.assignment_id NOT LIKE 'provisional-%'
                 AND NOT EXISTS (SELECT 1 FROM sessions s
                                 WHERE s.assignment_id = a.assignment_id)
                 AND NOT EXISTS (SELECT 1 FROM timings t
                                 WHERE t.assignment_id = a.assignment_id)
               ORDER BY a.due_at""").fetchall()
        return [row for row in rows if due_after(row["due_at"], now)]

    def provisional_sessions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE provisional = 1 ORDER BY start_local"
        ).fetchall()

    def clear_provisional(self) -> list[str]:
        """Withdraw every guessed block. Returns the uids to delete."""
        uids = [row["uid"] for row in self.provisional_sessions()]
        self.conn.execute("DELETE FROM sessions WHERE provisional = 1")
        self.conn.execute(
            "DELETE FROM assignments WHERE assignment_id LIKE 'provisional-%'")
        return uids

    def all_sessions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT s.*, a.label, a.teacher FROM sessions s
               JOIN assignments a ON a.assignment_id = s.assignment_id
               ORDER BY s.start_local"""
        ).fetchall()

    # ---------- runs ----------

    def log_run(self, status: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO runs (started_at, status, detail) VALUES (?, ?, ?)",
            (_now(), status, detail),
        )

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # ---------- export ----------

    def export_text(self) -> str:
        lines = ["# prep state (generated - edit nothing here, the database is authoritative)"]
        for row in self.upcoming():
            lines.append(
                f"{row['due_at'] or 'no-due-date':<28} "
                f"{(row['teacher'] or '?'):<10} "
                f"{row['label'][:50]:<52} "
                f"scheduled={row['scheduled_minutes']:>4}m "
                f"actual={row['actual_minutes'] if row['actual_minutes'] is not None else '-'}"
            )
        run = self.last_run()
        if run:
            lines.append(f"\n# last run {run['started_at']} status={run['status']} {run['detail']}")
        return "\n".join(lines) + "\n"

    def write_export(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.export_text())
