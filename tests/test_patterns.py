"""Predicting prep from a teacher's habits, for when Teams is unreachable.

The value of this is entirely in its restraint: it must predict for teachers
who really are regular, refuse to for those who are not, never duplicate prep
already known, and be withdrawn the moment real data arrives.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from prep import patterns
from prep.store import Store

TZ = ZoneInfo("Europe/London")


def store_with(rows) -> Store:
    """rows: (teacher, assigned, due) as datetimes."""
    store = Store(":memory:")
    for index, (teacher, assigned, due) in enumerate(rows):
        store.conn.execute(
            """INSERT INTO assignments (assignment_id, title, teacher, label,
                   assigned_at, due_at, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, '', '')""",
            (f"a{index}", f"prep {index}", teacher, f"prep {index}",
             assigned.isoformat(), due.isoformat()))
    return store


def weekly(teacher, set_wd, due_wd, weeks=6, start=None):
    """A teacher who sets on one weekday and is due on another, every week."""
    base = start or datetime(2026, 9, 7, 9, 0, tzinfo=TZ)   # a Monday
    out = []
    for week in range(weeks):
        monday = base + timedelta(weeks=week)
        assigned = monday + timedelta(days=set_wd)
        due = monday + timedelta(days=due_wd)
        if due <= assigned:
            due += timedelta(days=7)
        out.append((teacher, assigned, due))
    return out


def test_a_regular_teacher_is_learned():
    store = store_with(weekly("Archer", set_wd=0, due_wd=4))
    learned = patterns.learn(store, TZ)
    assert "Archer" in learned
    assert learned["Archer"].set_weekday == 0
    assert learned["Archer"].due_weekday == 4
    assert learned["Archer"].gap_days == 4
    store.close()


def test_an_irregular_teacher_is_refused():
    """Inventing a pattern would book prep on days at random."""
    rows = []
    base = datetime(2026, 9, 7, 9, 0, tzinfo=TZ)
    for week, due_wd in enumerate([0, 2, 4, 1, 3, 5]):
        monday = base + timedelta(weeks=week)
        rows.append(("Scattered", monday, monday + timedelta(days=due_wd)))
    store = store_with(rows)
    assert "Scattered" not in patterns.learn(store, TZ)
    store.close()


def test_too_little_history_is_refused():
    store = store_with(weekly("New", set_wd=0, due_wd=4, weeks=2))
    assert patterns.learn(store, TZ) == {}
    store.close()


def test_a_mostly_regular_teacher_is_still_learned():
    """Real teachers move the odd deadline. That must not erase the pattern."""
    rows = weekly("Carver", set_wd=4, due_wd=4, weeks=6)
    teacher, assigned, due = rows[-1]
    rows[-1] = (teacher, assigned, due + timedelta(days=2))   # one moved
    store = store_with(rows)
    learned = patterns.learn(store, TZ)
    assert "Carver" in learned
    assert learned["Carver"].dominance < 1.0
    store.close()


def test_predictions_land_on_the_usual_weekday():
    store = store_with(weekly("Archer", set_wd=0, due_wd=4))
    learned = patterns.learn(store, TZ)
    now = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)        # a Monday
    got = patterns.predict(learned, now, now + timedelta(days=14))
    assert got, "predicted nothing for a weekly teacher"
    assert all(p.due.weekday() == 4 for p in got), [p.due for p in got]
    assert all(now < p.due <= now + timedelta(days=14) for p in got)
    store.close()


def test_a_prediction_never_duplicates_prep_already_known():
    """The real thing is better in every way, and two blocks means two evenings."""
    store = store_with(weekly("Archer", set_wd=0, due_wd=4))
    learned = patterns.learn(store, TZ)
    now = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)
    real_due = datetime(2026, 10, 23, 8, 30, tzinfo=TZ)   # that Friday

    without = patterns.predict(learned, now, now + timedelta(days=14))
    with_known = patterns.predict(learned, now, now + timedelta(days=14),
                                  {"Archer": [real_due]})
    assert len(with_known) == len(without) - 1
    assert all(abs((p.due - real_due).days) > 1 for p in with_known)
    store.close()


def test_the_assigned_date_follows_the_usual_gap():
    store = store_with(weekly("Barratt", set_wd=3, due_wd=0))
    learned = patterns.learn(store, TZ)
    now = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)
    got = patterns.predict(learned, now, now + timedelta(days=14))
    for prediction in got:
        assert (prediction.due - prediction.assigned).days == \
            learned["Barratt"].gap_days
        assert prediction.assigned < prediction.due
    store.close()


def test_a_prediction_is_labelled_so_nobody_wonders_if_it_is_real():
    store = store_with(weekly("Archer", set_wd=0, due_wd=4))
    learned = patterns.learn(store, TZ)
    now = datetime(2026, 10, 19, 17, 0, tzinfo=TZ)
    got = patterns.predict(learned, now, now + timedelta(days=10))
    assert "not yet in Teams" in got[0].label
    store.close()
