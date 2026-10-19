"""Read subscribed ICS calendar feeds (school timetable, fixtures).

Why this exists: CalDAV serves only calendars STORED in iCloud. A calendar you
subscribed to from an ICS/webcal link lives on the device, so the scheduler
cannot see it over CalDAV and would happily book prep during lessons. The Pi
fetches those feeds directly instead.

Feed URLs usually embed a personal key, so treat one as a credential: keep it
in the environment, never in a file you commit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 45
# Beyond this a cached timetable is worse than no timetable: terms end,
# sets change, and prep would be booked over lessons with full confidence.
MAX_CACHE_AGE_HOURS = 72
MAX_BYTES = 20 * 1024 * 1024


@dataclass
class FeedEvent:
    start: datetime
    end: datetime
    summary: str
    source: str
    location: str = ""

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


class FeedError(RuntimeError):
    """The feed could not be fetched or parsed."""


def fetch(url: str, timeout: int = DEFAULT_TIMEOUT, cache: Path | None = None,
          max_cache_age_hours: float = MAX_CACHE_AGE_HOURS) -> bytes:
    """Download an ICS feed, falling back to a cached copy if the network fails.

    A timetable that is briefly unreachable must not silently become 'no
    commitments', which would let prep be scheduled over every lesson.
    """
    import requests

    try:
        response = requests.get(url, timeout=timeout,
                                headers={"User-Agent": "prep-pipeline/1.0"})
        response.raise_for_status()
        data = response.content
        if len(data) > MAX_BYTES:
            raise FeedError(f"feed larger than {MAX_BYTES} bytes")
        if b"BEGIN:VCALENDAR" not in data[:4096]:
            raise FeedError("response is not an iCalendar document "
                            "(wrong URL, or the key has expired)")
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
        return data
    except FeedError:
        raise
    except Exception as exc:
        if cache and cache.exists():
            age_hours = ((datetime.now(timezone.utc).timestamp()
                          - cache.stat().st_mtime) / 3600)
            if age_hours > max_cache_age_hours:
                raise FeedError(
                    f"fetch failed ({exc}) and the cached copy is stale "
                    f"({age_hours:.0f} hours old, limit {max_cache_age_hours:.0f})"
                ) from exc
            log.warning("feed fetch failed (%s); using cached copy %.0f hours old",
                        exc, age_hours)
            return cache.read_bytes()
        raise FeedError(f"could not fetch feed and no cache available: {exc}") from exc


def parse(data: bytes, window_start: datetime, window_end: datetime,
          tz: ZoneInfo, source: str = "feed",
          skip_all_day: bool = True) -> list[FeedEvent]:
    """Expand a feed into concrete events inside the window.

    Recurrence matters: a school timetable is almost entirely RRULEs, so a
    naive VEVENT walk would see one lesson instead of a term of them.
    """
    import icalendar
    import recurring_ical_events

    try:
        calendar = icalendar.Calendar.from_ical(data)
    except Exception as exc:
        raise FeedError(f"could not parse iCalendar data: {exc}") from exc

    try:
        occurrences = recurring_ical_events.of(calendar).between(
            window_start.astimezone(timezone.utc), window_end.astimezone(timezone.utc))
    except Exception as exc:
        raise FeedError(f"could not expand recurrences: {exc}") from exc

    events: list[FeedEvent] = []
    for component in occurrences:
        dtstart = component.get("dtstart")
        if dtstart is None:
            continue
        dtend = component.get("dtend")
        start_value = dtstart.dt
        end_value = dtend.dt if dtend is not None else None

        all_day = isinstance(start_value, date) and not isinstance(start_value, datetime)
        if all_day:
            if skip_all_day:
                # Term dates and staff INSET days are all-day markers. Treating
                # them as busy would block whole days of legitimate prep time.
                continue
            start = datetime.combine(start_value, time.min, tzinfo=tz)
            end = (datetime.combine(end_value, time.min, tzinfo=tz)
                   if isinstance(end_value, date) else start + timedelta(days=1))
        else:
            if start_value.tzinfo is None:
                start_value = start_value.replace(tzinfo=tz)
            start = start_value.astimezone(tz)
            if isinstance(end_value, datetime):
                if end_value.tzinfo is None:
                    end_value = end_value.replace(tzinfo=tz)
                end = end_value.astimezone(tz)
            else:
                end = start + timedelta(hours=1)

        if end <= start:
            end = start + timedelta(minutes=30)

        summary = str(component.get("summary", "") or "").strip()
        location = str(component.get("location", "") or "").strip()

        # A transparent event is explicitly "free" - honour that.
        if str(component.get("transp", "")).upper() == "TRANSPARENT":
            continue
        # So is a cancelled one.
        if str(component.get("status", "")).upper() == "CANCELLED":
            continue

        events.append(FeedEvent(start, end, summary, source, location))

    events.sort(key=lambda e: e.start)
    return events


def load_all(feeds: dict[str, str], window_start: datetime, window_end: datetime,
             tz: ZoneInfo, cache_dir: Path | None = None) -> tuple[list[FeedEvent], list[str]]:
    """Fetch and parse every configured feed.

    Returns the events and a list of problems. A feed that fails is reported,
    never silently treated as empty.
    """
    events: list[FeedEvent] = []
    problems: list[str] = []

    for name, url in feeds.items():
        cache = (cache_dir / f"{name}.ics") if cache_dir else None
        try:
            data = fetch(url, cache=cache)
            found = parse(data, window_start, window_end, tz, source=name)
            events.extend(found)
            log.info("feed %s: %d events in window", name, len(found))
            if not found:
                problems.append(f"{name}: fetched fine but no events in the window")
        except FeedError as exc:
            problems.append(f"{name}: {exc}")

    events.sort(key=lambda e: e.start)
    return events, problems
