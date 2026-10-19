"""Place prep sessions in free time.

The slot maths is pure and testable. Calendar access sits behind a small
protocol so tests can run against a fake instead of iCloud.
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
# Imported by name: `time` above is datetime.time, which would
# shadow the module and turn a retry into an AttributeError.
from time import sleep
from typing import Protocol
from zoneinfo import ZoneInfo

from .config import Config, SchedulingConfig
from .isotime import fromiso

log = logging.getLogger(__name__)


@dataclass
class Interval:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class PlannedSession:
    assignment_id: str
    label: str
    teacher: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class DiaryEvent:
    """A calendar entry, with where it is - travel planning needs both."""
    start: datetime
    end: datetime
    summary: str = ""
    location: str = ""
    calendar: str = ""
    all_day: bool = False
    travel: str = ""          # "car" / "public" when the event says; "" = default
    notes: str = ""


class CalendarBackend(Protocol):
    def busy(self, start: datetime, end: datetime) -> list[Interval]: ...
    def create(self, session: PlannedSession) -> str: ...
    # Returns the uids it could NOT delete. A caller about to book a
    # replacement must keep those, or the diary ends up with both.
    def delete_by_uid(self, uids: list[str]) -> list[str]: ...


def parse_dt(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        parsed = fromiso(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz)


def merge(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda i: i.start)
    merged = [Interval(ordered[0].start, ordered[0].end)]
    for current in ordered[1:]:
        last = merged[-1]
        if current.start <= last.end:
            last.end = max(last.end, current.end)
        else:
            merged.append(Interval(current.start, current.end))
    return merged


def day_windows(cfg: SchedulingConfig, day: date, tz: ZoneInfo,
                availability: dict[date, list[Interval]] | None = None
                ) -> list[Interval]:
    """When you can work on `day`: the routed windows if travel planning
    covered it, otherwise the fixed window from the config."""
    if availability is not None and day in availability:
        return availability[day]
    window = cfg.windows.get(day.weekday())
    if window is None:
        return []
    return [Interval(datetime.combine(day, window.start, tzinfo=tz),
                     datetime.combine(day, window.end, tzinfo=tz))]


def free_slots(cfg: SchedulingConfig, window_start: datetime, window_end: datetime,
               busy: list[Interval], tz: ZoneInfo,
               availability: dict[date, list[Interval]] | None = None
               ) -> list[Interval]:
    """Working-hours time inside the window that isn't already taken."""
    gap = timedelta(minutes=cfg.gap_minutes)
    padded = merge([Interval(b.start - gap, b.end + gap) for b in busy])
    slots: list[Interval] = []

    day = window_start.date()
    while day <= window_end.date():
        for window in day_windows(cfg, day, tz, availability):
            day_start = max(window.start, window_start)
            day_end = min(window.end, window_end)
            if day_start >= day_end:
                continue

            cursor = day_start
            for block in padded:
                if block.end <= cursor or block.start >= day_end:
                    continue
                if block.start > cursor:
                    slots.append(Interval(cursor, min(block.start, day_end)))
                cursor = max(cursor, block.end)
                if cursor >= day_end:
                    break
            if cursor < day_end:
                slots.append(Interval(cursor, day_end))
        day += timedelta(days=1)

    return [s for s in slots if s.minutes >= cfg.block_minutes]


def plan(cfg: SchedulingConfig, assignment_id: str, label: str, teacher: str,
         minutes: int, assigned: datetime | None, due: datetime,
         busy: list[Interval], now: datetime, tz: ZoneInfo,
         daily_load: dict[date, int] | None = None,
         availability: dict[date, list[Interval]] | None = None,
         warn: bool = True) -> list[PlannedSession]:
    """Spread `minutes` over available days between now and the due date.

    `daily_load` carries minutes already committed to *other* prep on each day,
    so no evening exceeds cfg.max_daily_minutes across everything combined.
    It is mutated as sessions are placed.
    """
    # Use the real due time. Feeds carry it (23:59 deadlines, 07:20 registration
    # slots), and overriding it with a fixed hour silently throws away an
    # evening of working time, or invents time that does not exist.
    if due.hour == 0 and due.minute == 0:
        deadline = datetime.combine(due.date(), cfg.due_time, tzinfo=tz)
    else:
        deadline = due

    start_from = max(now, assigned or now)
    if start_from >= deadline:
        log.warning("no time left before deadline for %s", label)
        return []

    slots = free_slots(cfg, start_from, deadline, busy, tz, availability)
    if not slots:
        return []

    blocks_needed = max(1, math.ceil(minutes / cfg.block_minutes))
    days_available = len({s.start.date() for s in slots}) or 1
    per_day_cap = max(1, math.ceil(blocks_needed / days_available))

    sessions: list[PlannedSession] = []
    placed_today: dict[date, int] = {}
    load = daily_load if daily_load is not None else {}
    block = timedelta(minutes=cfg.block_minutes)
    buffer = timedelta(minutes=cfg.flow_buffer_minutes)
    gap = timedelta(minutes=cfg.gap_minutes)

    for slot in slots:
        if blocks_needed == 0:
            break
        cursor = slot.start
        while blocks_needed > 0 and cursor + block <= slot.end:
            day = cursor.date()
            if placed_today.get(day, 0) >= per_day_cap:
                break
            if load.get(day, 0) + cfg.block_minutes > cfg.max_daily_minutes:
                break
            length = block + (buffer if sessions else timedelta(0))
            if cursor + length > slot.end:
                length = block
            sessions.append(PlannedSession(assignment_id, label, teacher,
                                           cursor, cursor + length))
            placed_today[day] = placed_today.get(day, 0) + 1
            load[day] = load.get(day, 0) + int(length.total_seconds() // 60)
            blocks_needed -= 1
            cursor += length + gap

    if blocks_needed > 0 and warn:
        log.warning("placed only %d blocks for %s, %d short",
                    len(sessions), label, blocks_needed)
    return sessions


@dataclass
class Job:
    """One prep to plan, as plan_fairly takes it."""
    assignment_id: str
    label: str
    teacher: str
    minutes: int
    assigned: datetime | None
    due: datetime


def _fair_shares(needs: list[int], pool: int) -> list[int]:
    """Split `pool` blocks so each gets an equal share, or less if it needs less.

    A remainder that cannot be split evenly goes to the earliest deadlines,
    which come first in `needs`.
    """
    shares = [0] * len(needs)
    waiting = sorted(range(len(needs)), key=lambda i: needs[i])
    left = pool
    while waiting:
        each = left // len(waiting)
        if needs[waiting[0]] <= each:
            i = waiting.pop(0)
            shares[i] = needs[i]
            left -= needs[i]
            continue
        extra = left - each * len(waiting)
        for i in sorted(waiting)[:extra]:
            shares[i] = each + 1
        for i in sorted(waiting)[extra:]:
            shares[i] = each
        break
    return shares


def plan_fairly(cfg: SchedulingConfig, jobs: list[Job], busy: list[Interval],
                now: datetime, tz: ZoneInfo,
                daily_load: dict[date, int] | None = None,
                availability: dict[date, list[Interval]] | None = None
                ) -> list[list[PlannedSession]]:
    """Plan several preps at once, sharing the time fairly when it runs short.

    Earliest deadline first, prep by prep, would let the first few take every
    evening before the deadlines and leave the rest with nothing. So when the
    time runs out, the preps competing for it - everything due no later than
    the first one left short - each get an equal share, or what they need if
    that is less. Slots nobody's share could use then go to whoever is still
    short. Returns the sessions for each job, in the order given.
    """
    block = cfg.block_minutes
    order = sorted(range(len(jobs)), key=lambda i: jobs[i].due)
    need = {i: max(1, math.ceil(jobs[i].minutes / block)) for i in order}
    caps = dict(need)

    def attempt(caps: dict[int, int]):
        taken = list(busy)
        load = dict(daily_load or {})
        placed: dict[int, list[PlannedSession]] = {}
        for i in order:
            job = jobs[i]
            placed[i] = plan(cfg, job.assignment_id, job.label, job.teacher,
                             caps[i] * block, job.assigned, job.due,
                             merge(taken), now, tz, daily_load=load,
                             availability=availability, warn=False) if caps[i] else []
            taken += [Interval(s.start, s.end) for s in placed[i]]
        # Anything still short gets whatever its share left unused.
        for i in order:
            missing = need[i] - len(placed[i])
            if missing <= 0 or caps[i] >= need[i]:
                continue
            job = jobs[i]
            more = plan(cfg, job.assignment_id, job.label, job.teacher,
                        missing * block, job.assigned, job.due, merge(taken),
                        now, tz, daily_load=load, availability=availability,
                        warn=False)
            placed[i] = sorted(placed[i] + more, key=lambda s: s.start)
            taken += [Interval(s.start, s.end) for s in more]
        return placed, load

    placed, load = attempt(caps)
    # Shares only ever shrink, so this settles.
    for _ in range(sum(need.values())):
        fairer = dict(caps)
        for s in (i for i in order if len(placed[i]) < need[i]):
            # Everything due by then was planned first and placed all of its
            # sessions before that deadline: that is the time they share.
            group = [i for i in order if jobs[i].due <= jobs[s].due]
            pool = sum(len(placed[i]) for i in group)
            for i, share in zip(group, _fair_shares([need[i] for i in group], pool)):
                fairer[i] = min(fairer[i], share)
        if fairer == caps:
            break
        caps = fairer
        placed, load = attempt(caps)

    for i in order:
        if len(placed[i]) < need[i]:
            log.warning("placed only %d blocks for %s, %d short",
                        len(placed[i]), jobs[i].label, need[i] - len(placed[i]))

    if daily_load is not None:
        daily_load.clear()
        daily_load.update(load)
    return [placed[i] for i in range(len(jobs))]


class CalendarAuthFailed(RuntimeError):
    """iCloud rejected the username or password."""


class CalendarUnreachable(RuntimeError):
    """The request never got an answer. Says nothing about the password."""


def _no_quic(client) -> bool:
    """Stop the authenticated retry going out over HTTP/3. Did it apply?

    iCloud answers the first, unauthenticated PROPFIND with 401 and an
    `alt-svc: h3=":443"` header. The HTTP library believes it, so the retry -
    the one carrying your password - goes out over QUIC, which is UDP. On any
    path with a smaller MTU than the datagram it wants to send, that fails:

        ConnectionError: ('Connection aborted.', OSError(90, 'Message too long'))

    Measured here at roughly one attempt in four. What makes it worth this
    much code is the shape of the failure, not the rate: the request that
    fails is always the authenticated one, so it looks exactly like the
    password being wrong, and the obvious response - generate another
    app-specific password - cannot possibly help.

    HTTP/1.1 and HTTP/2 both go over TCP, which fragments properly.
    """
    session = getattr(client, "session", None)
    if session is None:
        return False
    try:
        import niquests
    except ImportError:
        return False                      # older caldav on requests: no QUIC
    if not isinstance(session, niquests.Session):
        return False
    try:
        replacement = niquests.Session(
            disable_http3=True,
            # Preserved rather than assumed: caldav decides this from its
            # own feature set, and overriding it here would be a second
            # change nobody asked for. The public name first, the private
            # one as a fallback - niquests has used both.
            multiplexed=bool(getattr(session, "multiplexed",
                                     getattr(session, "_multiplexed", False))),
        )
    except TypeError:
        return False                      # a version without the knob
    try:
        session.close()
    except Exception:
        pass
    client.session = replacement
    return True


def connect(cfg: Config, attempts: int = 3):
    """Open a CalDAV client, and say plainly which way it failed.

    "Calendar unreachable" and "password rejected" call for completely
    different responses, and one of them wastes an evening regenerating
    credentials that were always fine.
    """
    import caldav
    from caldav.lib import error as caldav_error

    last: Exception | None = None
    for attempt in range(attempts):
        client = caldav.DAVClient(
            url=cfg.calendar.caldav_url,
            username=cfg.calendar.username,
            password=cfg.calendar.password,
        )
        _no_quic(client)
        try:
            return client, client.principal()
        except caldav_error.AuthorizationError as exc:
            # Credentials are not going to become correct on a retry.
            raise CalendarAuthFailed(
                f"iCloud rejected the sign-in for {cfg.calendar.username}"
            ) from exc
        except Exception as exc:
            last = exc
            log.warning("calendar connection attempt %d/%d failed: %s",
                        attempt + 1, attempts, exc)
            if attempt + 1 < attempts:
                sleep(1.5 * (attempt + 1))

    raise CalendarUnreachable(
        f"no answer from {cfg.calendar.caldav_url} after {attempts} attempts: "
        f"{last}") from last


class CalDAVCalendar:
    """Reads every other calendar as busy, writes only to the prep calendar."""

    def __init__(self, cfg: Config, tz: ZoneInfo):
        self.cfg = cfg
        self.tz = tz
        self.client, principal = connect(cfg)
        self.calendars = {c.name: c for c in principal.calendars()}
        name = cfg.calendar.prep_calendar
        if name not in self.calendars:
            raise RuntimeError(
                f"calendar '{name}' not found - create it in the Calendar app first. "
                f"Available: {sorted(self.calendars)}"
            )
        self.prep = self.calendars[name]

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        return merge([Interval(e.start, e.end)
                      for e in self.diary_events(start, end)])

    def diary_events(self, start: datetime, end: datetime) -> list[DiaryEvent]:
        """Every event in every calendar but the prep one and the ignored.

        Remembered per window, so the busy time and the travel planning read
        iCloud once between them.
        """
        key = (start, end)
        cache = self.__dict__.setdefault("_events", {})
        if key in cache:
            return cache[key]
        found: list[DiaryEvent] = []
        skip = {self.cfg.calendar.prep_calendar, *self.cfg.calendar.ignore_calendars}
        for name, calendar in self.calendars.items():
            if name in skip:
                continue
            try:
                results = calendar.search(start=start, end=end, event=True, expand=True)
            except Exception as exc:
                log.warning("cannot search calendar %s: %s", name, exc)
                continue
            for event in results:
                for component in event.icalendar_instance.walk("VEVENT"):
                    interval = self._to_interval(component)
                    if interval:
                        value = component.get("dtstart").dt
                        travel = str(component.get("x-prep-travel", "") or "")
                        found.append(DiaryEvent(
                            interval.start, interval.end,
                            str(component.get("summary", "") or ""),
                            str(component.get("location", "") or "").strip(),
                            name.strip(),
                            all_day=not isinstance(value, datetime),
                            travel=travel.strip().lower(),
                            notes=str(component.get("description", "") or "")))
        cache[key] = found
        return found

    def add_personal(self, summary: str, start: datetime, end: datetime,
                     location: str = "", travel: str = "") -> str:
        """Put an outside commitment in your Personal calendar."""
        from icalendar import Calendar as ICalendar, Event as IEvent

        wanted = self.cfg.calendar.personal_calendar.strip().lower()
        target = next((c for n, c in self.calendars.items()
                       if n.strip().lower() == wanted), None)
        if target is None:
            raise RuntimeError(
                f"no calendar called '{self.cfg.calendar.personal_calendar}' - "
                f"create it in the Calendar app first")
        uid = f"personal-{uuid.uuid4()}@prep-pi"
        cal = ICalendar()
        cal.add("prodid", "-//prep-pipeline//EN")
        cal.add("version", "2.0")
        event = IEvent()
        event.add("uid", uid)
        event.add("summary", summary[:120])
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("dtstamp", datetime.now(timezone.utc))
        if location:
            event.add("location", location)
        if travel:
            # Read back by the routing; the description is for you.
            event.add("x-prep-travel", travel)
            event.add("description", "Getting there: "
                      + ("by car" if travel == "car" else "public transport"))
        cal.add_component(event)
        target.save_event(cal.to_ical().decode())
        self.__dict__.pop("_events", None)
        return uid

    def _to_interval(self, component) -> Interval | None:
        dtstart = component.get("dtstart")
        if dtstart is None:
            return None
        dtend = component.get("dtend")
        start_value = dtstart.dt
        end_value = dtend.dt if dtend is not None else None

        if isinstance(start_value, date) and not isinstance(start_value, datetime):
            start = datetime.combine(start_value, time.min, tzinfo=self.tz)
            if isinstance(end_value, date) and not isinstance(end_value, datetime):
                end = datetime.combine(end_value, time.min, tzinfo=self.tz)
            else:
                end = start + timedelta(days=1)
            return Interval(start, end)

        start = start_value.astimezone(self.tz)
        end = end_value.astimezone(self.tz) if end_value else start + timedelta(hours=1)
        return Interval(start, end)

    def create(self, session: PlannedSession) -> str:
        from icalendar import Calendar as ICalendar, Event as IEvent

        uid = f"prep-{uuid.uuid4()}@prep-pi"
        cal = ICalendar()
        cal.add("prodid", "-//prep-pipeline//EN")
        cal.add("version", "2.0")

        event = IEvent()
        event.add("uid", uid)
        summary = f"Prep: {session.teacher} - {session.label}" if session.teacher \
            else f"Prep: {session.label}"
        event.add("summary", summary[:120])
        event.add("dtstart", session.start)
        event.add("dtend", session.end)
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("description", f"Auto-scheduled.\nassignment: {session.assignment_id}")
        event.add("x-prep-id", session.assignment_id)
        cal.add_component(event)

        self.prep.save_event(cal.to_ical().decode())
        return uid

    def delete_by_uid(self, uids: list[str]) -> list[str]:
        """Delete by uid, found through a date-range search.

        Not event_by_uid: that asks the server to filter on UID, which
        iCloud refuses with 412 Precondition Failed - so every delete
        failed, on every path, and old bookings stayed in the diary. A
        time-range search is the query iCloud does answer.
        """
        if not uids:
            return []
        try:
            found = self._events_by_uid(set(uids))
        except Exception as exc:
            log.warning("could not search the prep calendar: %s", exc)
            return list(uids)
        failed = []
        for uid in uids:
            event = found.get(uid)
            if event is None:
                continue        # not in the calendar: already gone
            try:
                event.delete()
            except Exception as exc:
                log.warning("could not delete %s: %s", uid, exc)
                failed.append(uid)
        return failed

    def _events_by_uid(self, wanted: set[str], days: int = 400) -> dict:
        now = datetime.now(self.tz)
        found = {}
        for event in self.prep.search(start=now - timedelta(days=days),
                                      end=now + timedelta(days=days),
                                      event=True):
            for component in event.icalendar_instance.walk("VEVENT"):
                uid = str(component.get("uid", ""))
                if uid in wanted:
                    found[uid] = event
        return found

    def pipeline_events(self, start: datetime,
                        end: datetime) -> list[tuple[str, str, datetime]]:
        """(uid, assignment id, start) of every event this pipeline wrote.

        Read from the prep calendar itself rather than the database, so a
        booking the database lost track of - a crash between create and
        record, a second machine - is still visible and can be cleaned up.
        """
        found = []
        for event in self.prep.search(start=start, end=end, event=True):
            for component in event.icalendar_instance.walk("VEVENT"):
                uid = str(component.get("uid", ""))
                if not uid.endswith("@prep-pi"):
                    continue
                interval = self._to_interval(component)
                found.append((uid, str(component.get("x-prep-id", "")),
                              interval.start if interval else start))
        return found


class DryRunCalendar:
    """Reads the real calendar, writes nothing.

    A dry run against an empty calendar is worse than useless: it would show
    prep landing in slots that are actually taken by lessons, and reassure you
    about a plan that could never happen. So the reads are real.
    """

    def __init__(self, backend: CalendarBackend):
        self.backend = backend
        self.would_create: list[PlannedSession] = []
        self.would_delete: list[str] = []

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        return self.backend.busy(start, end)

    def diary_events(self, start: datetime, end: datetime) -> list[DiaryEvent]:
        return self.backend.diary_events(start, end)

    def add_personal(self, summary, start, end, location="", travel=""):
        return "dry-run-personal"          # a dry run writes nothing

    def create(self, session: PlannedSession) -> str:
        self.would_create.append(session)
        return f"dry-run-{len(self.would_create)}"

    def delete_by_uid(self, uids: list[str]) -> list[str]:
        self.would_delete.extend(uids)
        return []

    def pipeline_events(self, start: datetime, end: datetime):
        return self.backend.pipeline_events(start, end)


class FakeCalendar:
    """In-memory backend used by the tests, and when no calendar is reachable."""

    def __init__(self, busy_intervals: list[Interval] | None = None,
                 diary: list[DiaryEvent] | None = None):
        self._busy = busy_intervals or []
        self.diary = list(diary or [])
        self.events: dict[str, PlannedSession] = {}

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        return merge([b for b in self._busy if b.end > start and b.start < end]
                     + [Interval(e.start, e.end) for e in self.diary
                        if e.end > start and e.start < end])

    def diary_events(self, start: datetime, end: datetime) -> list[DiaryEvent]:
        return [e for e in self.diary if e.end > start and e.start < end]

    def add_personal(self, summary: str, start: datetime, end: datetime,
                     location: str = "", travel: str = "") -> str:
        self.diary.append(DiaryEvent(start, end, summary, location, "Personal",
                                     travel=travel))
        return f"personal-{uuid.uuid4()}"

    def create(self, session: PlannedSession) -> str:
        uid = f"fake-{uuid.uuid4()}"
        self.events[uid] = session
        return uid

    def delete_by_uid(self, uids: list[str]) -> list[str]:
        for uid in uids:
            self.events.pop(uid, None)
        return []

    def pipeline_events(self, start: datetime, end: datetime):
        return [(uid, s.assignment_id, s.start) for uid, s in self.events.items()
                if s.end > start and s.start < end]
