"""What each teacher is about to set, learned from what they have set before.

The point of this is to keep working when Teams is unreachable.

Your teachers are regular: one sets Friday due Friday, another sets Thursday
due Monday. After a few weeks that regularity is in the database, and it does
not stop being true because a session cookie expired. So when a capture fails,
rather than scheduling nothing and leaving your evenings empty, the scheduler
books *provisional* blocks where the prep almost certainly is.

They are marked provisional in the calendar, and the next successful capture
replaces them with the real thing. A provisional block that turns out to be
wrong costs you a wasted hour in the diary; scheduling nothing costs you the
prep.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .isotime import fromiso

log = logging.getLogger(__name__)

# Below this, a "pattern" is just noise. Three matching weeks is the point at
# which a guess becomes worth putting in your diary.
MIN_OBSERVATIONS = 3

# How dominant the weekday has to be. A teacher who sets on whatever day suits
# them has no pattern, and inventing one would book prep at random.
MIN_DOMINANCE = 0.6


@dataclass
class Pattern:
    teacher: str
    set_weekday: int
    due_weekday: int
    gap_days: int
    observations: int
    dominance: float

    def describe(self) -> str:
        days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        return (f"{self.teacher}: sets {days[self.set_weekday]}, "
                f"due {days[self.due_weekday]} "
                f"({self.observations} seen, {self.dominance*100:.0f}% regular)")


@dataclass
class Predicted:
    teacher: str
    due: datetime
    assigned: datetime
    pattern: Pattern

    @property
    def label(self) -> str:
        # Reads clearly in a calendar, where it appears as
        # "Prep: Archer - expected prep (not yet in Teams)". Nobody should
        # have to wonder whether a block is real.
        return "expected prep (not yet in Teams)"


def _parse(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        parsed = fromiso(value)
    except ValueError:
        return None
    return parsed.astimezone(tz)


def learn(store, tz: ZoneInfo) -> dict[str, Pattern]:
    """Find each teacher's habitual set and due weekdays."""
    rows = store.conn.execute(
        """SELECT teacher, assigned_at, due_at FROM assignments
           WHERE teacher IS NOT NULL AND teacher <> ''
             AND due_at IS NOT NULL"""
    ).fetchall()

    seen: dict[str, list[tuple[int, int, int]]] = {}
    for row in rows:
        due = _parse(row["due_at"], tz)
        assigned = _parse(row["assigned_at"], tz)
        if due is None:
            continue
        if assigned is None:
            continue
        gap = max((due.date() - assigned.date()).days, 1)
        seen.setdefault(row["teacher"], []).append(
            (assigned.weekday(), due.weekday(), gap))

    patterns: dict[str, Pattern] = {}
    for teacher, observations in seen.items():
        if len(observations) < MIN_OBSERVATIONS:
            continue
        set_days = Counter(o[0] for o in observations)
        due_days = Counter(o[1] for o in observations)
        set_day, set_count = set_days.most_common(1)[0]
        due_day, due_count = due_days.most_common(1)[0]

        # Judge on the due day: that is the deadline the schedule hangs off,
        # and a teacher can be regular about deadlines while setting whenever.
        dominance = due_count / len(observations)
        if dominance < MIN_DOMINANCE:
            log.info("%s has no reliable pattern (%.0f%%)", teacher,
                     dominance * 100)
            continue

        gaps = sorted(o[2] for o in observations)
        patterns[teacher] = Pattern(
            teacher=teacher, set_weekday=set_day, due_weekday=due_day,
            gap_days=gaps[len(gaps) // 2],
            observations=len(observations), dominance=dominance)

    return patterns


def predict(patterns: dict[str, Pattern], now: datetime,
            horizon: datetime, known_due: dict[str, list[datetime]] | None = None,
            ) -> list[Predicted]:
    """Where each teacher's next prep probably falls, up to the horizon.

    `known_due` is what the database already holds per teacher. A prediction
    landing on a deadline you already know about is dropped: the real one is
    better in every way, and booking both would double the work.
    """
    known = known_due or {}
    out: list[Predicted] = []

    for teacher, pattern in sorted(patterns.items()):
        # Walk forward to each occurrence of the habitual due weekday.
        day = now.date()
        ahead = (pattern.due_weekday - day.weekday()) % 7
        if ahead == 0:
            ahead = 7          # today's deadline has already been set or missed
        day = day + timedelta(days=ahead)

        while True:
            due = datetime.combine(day, now.timetz()).replace(
                hour=8, minute=30, second=0, microsecond=0)
            if due > horizon:
                break
            already = any(abs((d - due).total_seconds()) < 36 * 3600
                          for d in known.get(teacher, []))
            if not already:
                out.append(Predicted(
                    teacher=teacher, due=due,
                    assigned=due - timedelta(days=pattern.gap_days),
                    pattern=pattern))
            day += timedelta(days=7)

    return out
