"""Invented prep that behaves like the real thing, for Phase A.

Phase A has to prove the logic works without a Teams session, a calendar
password or a network. That means the data has to come from here.

Everything in this file is made up. The teachers, the class ids and the
creator ids are inventions; no real name, class or GUID appears anywhere in
this repository. What is real is the *shape*: Microsoft's assignment JSON,
classes shared between two teachers, a class nobody set anything in for half a
year, one prep whose author the tenant never sent, and a set of past timings
with a freak evening in them.

The world is built relative to today, not frozen to a date, so "still to come"
stays still to come next term. The random jitter on the timings is seeded, so
the same run twice gives the same answer and an assertion about it means
something.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

SEED = 20260925

# How much history to invent. Eight weeks of past prep is enough for the
# filter to have converged on a teacher and for a rejected outlier to show.
WEEKS_BACK = 8
WEEKS_FORWARD = 2


@dataclass(frozen=True)
class DummyTeacher:
    name: str
    code: str
    creator_id: str
    class_id: str
    set_weekday: int      # 0 = Monday
    due_weekday: int
    typical: int          # what their prep really takes, in minutes
    logs: bool = True     # False = a teacher you have never timed
    anomaly: int = 0      # one freak evening, which the filter should reject


# Two teachers per class, deliberately. Every real class here is shared, so a
# class GUID cannot identify who set a prep - only the creator id can. Phase A
# asserts exactly that.
TEACHERS: tuple[DummyTeacher, ...] = (
    DummyTeacher("Archer", "AAA", "t-archer-9f21", "cls-maths",      0, 4, 85),
    DummyTeacher("Elwood",   "EEE", "t-elwood-3c07",   "cls-maths",      1, 3, 45),
    DummyTeacher("Barratt",  "BBB", "t-barratt-5d88",  "cls-science",    3, 0, 70),
    DummyTeacher("Carver",   "CCC", "t-carver-1a40",   "cls-science",    4, 4, 110),
    DummyTeacher("Dalton",  "DDD", "t-dalton-77b2",  "cls-humanities", 2, 2, 60,
                 anomaly=300),
    DummyTeacher("Foster",  "FFF", "t-foster-0e55",  "cls-humanities", 0, 4, 75,
                 logs=False),
)

ARCHIVED_CLASS = "cls-latin"      # finished last year; must never be scheduled
DORMANT_CLASS = "cls-german"      # silent for months; should be OFFERED for archiving
UNKNOWN_CLASS = "cls-unknown"     # no creator id at all; must surface as unresolved


@dataclass
class DummyWorld:
    payloads: list[dict[str, Any]]
    learned: dict[str, Any]
    timings: list[tuple[str, int]]          # (assignment_id, minutes)
    busy: list[Any]                         # scheduler.Interval
    ids_by_teacher: dict[str, list[str]] = field(default_factory=dict)
    due_by_id: dict[str, datetime] = field(default_factory=dict)
    logged_by_teacher: dict[str, list[int]] = field(default_factory=dict)

    @property
    def assignment_count(self) -> int:
        return sum(len(p["body"]["value"]) for p in self.payloads)


def _guid(*parts: str) -> str:
    """A deterministic 36-character id.

    Length matters: the parser treats anything shorter than 32 characters as
    not-really-an-id and falls back to a synthetic key, so dummy ids have to
    be the same shape as Teams ids or Phase A would test the wrong path.
    """
    digest = hashlib.md5("|".join(parts).encode()).hexdigest()
    return (f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-"
            f"{digest[16:20]}-{digest[20:32]}")


def _assignment(assignment_id: str, class_id: str, title: str,
                assigned: datetime, due: datetime,
                creator_id: str = "", creator_name: str = "") -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": assignment_id,
        "classId": class_id,
        "displayName": title,
        "assignedDateTime": assigned.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dueDateTime": due.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "assigned",
    }
    if creator_id or creator_name:
        # displayName is usually null in the real payloads - the id is what
        # you actually get, and the id is what has to be enough.
        node["createdBy"] = {"user": {"id": creator_id,
                                      "displayName": creator_name or None}}
    return node


def build(now: datetime | None = None, tz: str = "Europe/London") -> DummyWorld:
    from .scheduler import Interval

    zone = ZoneInfo(tz)
    now = now or datetime.now(timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    rng = random.Random(SEED)

    this_monday = (now_utc - timedelta(days=now_utc.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)

    items: list[dict[str, Any]] = []
    ids_by_teacher: dict[str, list[str]] = {t.name: [] for t in TEACHERS}
    due_by_id: dict[str, datetime] = {}
    past_ids: dict[str, list[str]] = {t.name: [] for t in TEACHERS}
    counter: dict[str, int] = {t.name: 0 for t in TEACHERS}

    for week in range(-WEEKS_BACK, WEEKS_FORWARD + 1):
        monday = this_monday + timedelta(weeks=week)
        for teacher in TEACHERS:
            assigned = monday + timedelta(days=teacher.set_weekday, hours=9)
            due = monday + timedelta(days=teacher.due_weekday, hours=8)
            if due <= assigned:
                due += timedelta(days=7)
            if assigned > now_utc + timedelta(weeks=WEEKS_FORWARD):
                continue

            counter[teacher.name] += 1
            assignment_id = _guid(teacher.code, str(week))
            title = f"{teacher.code} Prep {counter[teacher.name]}"
            items.append(_assignment(assignment_id, teacher.class_id, title,
                                     assigned, due, teacher.creator_id))
            ids_by_teacher[teacher.name].append(assignment_id)
            due_by_id[assignment_id] = due
            if due < now_utc:
                past_ids[teacher.name].append(assignment_id)

    # A class that finished last year. Its prep is still in the capture, which
    # is the whole problem: without the archive flag it would be scheduled.
    old_due = now_utc - timedelta(days=210)
    items.append(_assignment(_guid("latin", "1"), ARCHIVED_CLASS,
                             "Latin unseen translation",
                             old_due - timedelta(days=7), old_due,
                             "t-greaves-4b19"))

    # Silent for months but not yet marked. Should be offered for you to
    # confirm, never archived by the code on its own.
    quiet_due = now_utc - timedelta(days=150)
    items.append(_assignment(_guid("german", "1"), DORMANT_CLASS,
                             "German listening practice",
                             quiet_due - timedelta(days=5), quiet_due,
                             "t-harrow-6c73"))

    # The tenant sent no createdBy for this one. It must show up as unresolved
    # rather than being guessed at: a wrong teacher poisons that teacher's
    # timing history and every estimate after it.
    mystery_due = now_utc + timedelta(days=5)
    items.append(_assignment(_guid("unknown", "1"), UNKNOWN_CLASS,
                             "Read chapter 4 and answer the questions",
                             now_utc - timedelta(days=2), mystery_due))

    payloads = [{
        "url": "https://teams.microsoft.test/api/v1/assignments/list",
        "body": {"value": items},
    }]

    # ---- what you have already told it, including a realistic mistake ----
    # Archer appears twice: once as a person and once as a whole class. That
    # is exactly the slip the duplicate check exists to catch, so Phase A plants
    # it and checks the warning fires. The creator id still wins, so Elwood -
    # who shares that class - is still resolved correctly.
    learned = {
        "class_map": {"cls-maths": "Archer"},
        "title_codes": {},
        "people_map": {t.creator_id: t.name for t in TEACHERS},
        "archived": {ARCHIVED_CLASS: True},
    }

    # ---- how long past prep actually took ----
    timings: list[tuple[str, int]] = []
    logged_by_teacher: dict[str, list[int]] = {}
    for teacher in TEACHERS:
        if not teacher.logs:
            continue
        history = past_ids[teacher.name]
        for index, assignment_id in enumerate(history):
            if teacher.anomaly and index == max(0, len(history) - 3):
                minutes = teacher.anomaly       # the evening that went wrong
            else:
                minutes = max(15, int(round(
                    rng.gauss(teacher.typical, teacher.typical * 0.12))))
            timings.append((assignment_id, minutes))
            logged_by_teacher.setdefault(teacher.name, []).append(minutes)

    # ---- a diary with things already in it ----
    busy: list[Any] = []
    day = now.astimezone(zone).date()
    for offset in range(0, 22):
        current = day + timedelta(days=offset)
        weekday = current.weekday()
        if weekday < 5:
            busy.append(Interval(
                datetime.combine(current, time(8, 30), tzinfo=zone),
                datetime.combine(current, time(16, 0), tzinfo=zone)))
        if weekday == 1:      # orchestra
            busy.append(Interval(
                datetime.combine(current, time(18, 0), tzinfo=zone),
                datetime.combine(current, time(20, 0), tzinfo=zone)))
        if weekday == 5:      # Saturday fixture
            busy.append(Interval(
                datetime.combine(current, time(9, 0), tzinfo=zone),
                datetime.combine(current, time(13, 0), tzinfo=zone)))

    return DummyWorld(payloads=payloads, learned=learned, timings=timings,
                      busy=busy, ids_by_teacher=ids_by_teacher,
                      due_by_id=due_by_id, logged_by_teacher=logged_by_teacher)


CONFIG_TEMPLATE = """\
# Generated by prep.dummy for Phase A. Nothing here is real, and nothing here
# reaches the network: no feeds, and the calendar is replaced by a fake.
timezone: {tz}
db_path: {root}/prep.sqlite3
dump_dir: {root}/dumps
export_path: {root}/prep.txt
learned_map_path: {root}/teachers.json
feed_cache_dir: {root}/feed-cache

teams:
  assignments_url: https://teams.microsoft.test/assignments
  url_hints: [assignment]
  profile_dir: {root}/profile

calendar:
  caldav_url: https://calendar.invalid/
  username: phase-a
  password: phase-a
  prep_calendar: Prep

estimator:
  per_day_minutes: 30
  cap_minutes: 120
  shrinkage_k: 3.0
  min_minutes: 15

scheduling:
  block_minutes: 30
  max_daily_minutes: 120
  flow_buffer_minutes: 5
  gap_minutes: 10
  due_time: "08:30"
  skip_past_due: true
  windows:
    monday:    {{start: "17:00", end: "21:30"}}
    tuesday:   {{start: "17:00", end: "21:30"}}
    wednesday: {{start: "17:00", end: "21:30"}}
    thursday:  {{start: "17:00", end: "21:30"}}
    friday:    {{start: "17:00", end: "21:00"}}
    saturday:  {{start: "09:30", end: "18:00"}}
    sunday:    {{start: "09:30", end: "20:00"}}

feeds: {{}}

web:
  host: 127.0.0.1
  port: 8099
  token: phase-a-token
"""


def write_site(root, world: DummyWorld, tz: str = "Europe/London"):
    """Lay the dummy world out on disk as a real installation would be."""
    import json
    from pathlib import Path

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "dumps").mkdir(exist_ok=True)
    (root / "teachers.json").write_text(json.dumps(world.learned, indent=2))
    (root / "dumps" / "payloads-dummy.json").write_text(
        json.dumps(world.payloads, indent=2))
    config = root / "config.yaml"
    config.write_text(CONFIG_TEMPLATE.format(root=root, tz=tz))
    return config
