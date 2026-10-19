"""Where you are each day, and when that leaves you somewhere you can work.

Prep happens at home or at school. Never in transit, never at a lesson. A day
is a chain of commitments, each somewhere: lessons and clubs at school, an
instrument lesson at a teacher's house, a blackout you gave a postcode. In
every gap between two of them - including leaving home in the morning and
getting back at night - there are several ways to spend it:

    stay where you are and work there
    go home, work, then travel on
    stay at school, work, then go home
    some of each: school until a time, then home

They differ in how much usable prep time they leave, because a journey eats
time and only home and school are places you can work. Each one is scored by
the 30-minute blocks it can actually hold, and the best wins; on a tie, the
one that has you away from home least.

The result is, for each day with commitments, the exact windows when you are
somewhere you can work. The scheduler uses those instead of the fixed
evening window for that day. A day with no commitments is left alone.

Journey times come from `travel.times` when you have given one, otherwise
from the postcodes: looked up once on api.postcodes.io (only the postcode is
sent), cached, and scaled from a journey you know - your 75 minutes to school
says more about how you travel than any national average.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .scheduler import Interval, merge

log = logging.getLogger(__name__)

POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*([\dO][A-Z]{2})\b", re.I)
DEFAULT_MINUTES_PER_KM = 3.0      # ~20 km/h door to door: bus and train
DEFAULT_CAR_MINUTES_PER_KM = 1.8  # straight-line km, by road, with traffic
MODES = ("public", "car")

_CAR = re.compile(r"\b(by car|driving|drive|lift)\b|[\[(]car[\])]", re.I)
_PUBLIC = re.compile(r"\b(public transport|by (bus|train|coach)|on the (bus|train))\b"
                     r"|[\[(](bus|train|public)[\])]", re.I)


def mode_from_text(*texts: str) -> str:
    """'car', 'public' or '' from what an event says about getting there.

    For events you put straight into your calendar: write "by car" or
    "(bus)" in the title, location or notes. The dashboard sets it for you.
    """
    for text in texts:
        if text and _PUBLIC.search(text):
            return "public"
        if text and _CAR.search(text):
            return "car"
    return ""


def normalise_postcode(text: str) -> str:
    """'sw1a oaa' -> 'SW1A 0AA'. The inward part always starts with a digit,
    so a letter O there is a typo for zero."""
    match = POSTCODE.search(text or "")
    if not match:
        return ""
    outward, inward = match.group(1).upper(), match.group(2).upper()
    if inward[0] == "O":
        inward = "0" + inward[1:]
    return f"{outward} {inward}"


# ---------------------------------------------------------------- places

@dataclass(frozen=True)
class Place:
    key: str                  # "home", "school", a configured name, or a postcode
    name: str
    postcode: str = ""

    @property
    def is_home(self) -> bool:
        return self.key == "home"


class Geocoder:
    """Postcode -> (lat, lon), from api.postcodes.io, remembered on disk.

    Only ever sends the postcode. A postcode that does not exist is
    remembered as such; a network failure is not, so it is tried again.
    """
    URL = "https://api.postcodes.io/postcodes/{}"

    def __init__(self, cache_path: Path | None, enabled: bool = True,
                 fetch=None):
        self.cache_path = cache_path
        self.enabled = enabled
        self._fetch = fetch or self._http
        self.cache: dict[str, list[float] | None] = {}
        if cache_path and Path(cache_path).exists():
            try:
                self.cache = json.loads(Path(cache_path).read_text())
            except Exception:
                log.warning("ignoring unreadable postcode cache %s", cache_path)

    @staticmethod
    def _http(postcode: str):
        import requests
        from urllib.parse import quote

        response = requests.get(Geocoder.URL.format(quote(postcode)), timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        result = response.json()["result"]
        return [float(result["latitude"]), float(result["longitude"])]

    def lookup(self, postcode: str) -> tuple[float, float] | None:
        postcode = normalise_postcode(postcode)
        if not postcode:
            return None
        if postcode in self.cache:
            found = self.cache[postcode]
            return tuple(found) if found else None
        if not self.enabled:
            return None
        try:
            found = self._fetch(postcode)
        except Exception as exc:
            log.warning("could not look up %s: %s", postcode, exc)
            return None
        self.cache[postcode] = found
        if self.cache_path:
            try:
                path = Path(self.cache_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(self.cache, indent=1, sort_keys=True))
            except OSError as exc:
                log.warning("could not save postcode cache: %s", exc)
        return tuple(found) if found else None


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


class Places:
    """Turns an event's LOCATION into a Place, and two Places into minutes."""

    def __init__(self, travel_cfg, geocoder: Geocoder | None = None):
        self.cfg = travel_cfg
        self.geocoder = geocoder or Geocoder(travel_cfg.cache_path,
                                             travel_cfg.lookup_postcodes)
        self.home = Place("home", "home",
                          normalise_postcode(travel_cfg.home.postcode))
        self.school = Place("school", travel_cfg.school.name or "school",
                            normalise_postcode(travel_cfg.school.postcode))
        self.named = {key: Place(key, value.name or key,
                                 normalise_postcode(value.postcode))
                      for key, value in travel_cfg.places.items()}
        self._minutes: dict[tuple, int] = {}
        self._per_km: dict[str, float] = {}

    # --------------------------------------------------------- resolving
    def resolve(self, location: str, source: str = "") -> Place | None:
        """Where an event is, or None if that cannot be told.

        A postcode beats the calendar it came from: an away fixture in the
        school's own feed is still away.
        """
        text = (location or "").lower()
        postcode = normalise_postcode(location or "")

        for key, value in self.cfg.places.items():
            place = self.named[key]
            if (any(word and word in text for word in value.match)
                    or (place.postcode and place.postcode == postcode)):
                return place
        if text.strip() == "home" or (postcode and postcode == self.home.postcode):
            return self.home
        school_words = [w for w in [self.cfg.school.name.lower(),
                                    *self.cfg.school.match] if w]
        if ((postcode and postcode == self.school.postcode)
                or any(word in text for word in school_words)):
            return self.school
        if postcode:
            return Place(postcode, postcode, postcode)
        if source.strip().lower() in (*self.cfg.school_calendars,
                                      *self.cfg.school_feeds):
            return self.school
        return None

    def is_school_source(self, source: str) -> bool:
        return source.strip().lower() in (*self.cfg.school_calendars,
                                          *self.cfg.school_feeds)

    # --------------------------------------------------------- journeys
    def _coords(self, place: Place):
        return self.geocoder.lookup(place.postcode) if place.postcode else None

    def _times(self, mode: str) -> dict[frozenset, int]:
        return self.cfg.car_times if mode == "car" else self.cfg.times

    def _overhead(self, mode: str) -> int:
        return (self.cfg.car_overhead_minutes if mode == "car"
                else self.cfg.overhead_minutes)

    def _given(self, a: Place, b: Place, mode: str = "public") -> int | None:
        for left in (a.key, a.postcode):
            for right in (b.key, b.postcode):
                if left and right:
                    minutes = self._times(mode).get(frozenset({left, right}))
                    if minutes is not None:
                        return minutes
        return None

    def _minutes_per_km(self, mode: str = "public") -> float:
        """Calibrated from the journeys you have timed that way, so the
        estimate reflects how you actually travel. A bus and a car are
        calibrated apart: 75 minutes to school by bus says nothing about
        how long a lift takes."""
        if mode not in self._per_km:
            rates = []
            for pair, minutes in self._times(mode).items():
                ends = [self._by_key(k) for k in pair]
                if len(ends) != 2 or None in ends:
                    continue
                a, b = (self._coords(e) for e in ends)
                if a and b and _km(a, b) > 1:
                    rates.append(max(0.5, (minutes - self._overhead(mode))
                                     / _km(a, b)))
            default = (DEFAULT_CAR_MINUTES_PER_KM if mode == "car"
                       else DEFAULT_MINUTES_PER_KM)
            self._per_km[mode] = sum(rates) / len(rates) if rates else default
        return self._per_km[mode]

    def _by_key(self, key: str) -> Place | None:
        if key == "home":
            return self.home
        if key == "school":
            return self.school
        if key in self.named:
            return self.named[key]
        postcode = normalise_postcode(key)
        return Place(postcode, postcode, postcode) if postcode else None

    def mode_for(self, a: Place, b: Place, *chosen: str) -> str:
        """How a journey is made: as the commitment at either end says, or
        the school run's mode between home and school, or the default."""
        for value in chosen:
            if value in MODES:
                return value
        if {a.key, b.key} <= {"home", "school"}:
            return self.cfg.school_mode
        return self.cfg.default_mode

    def minutes(self, a: Place, b: Place, mode: str | None = None) -> int:
        if a.key == b.key:
            return 0
        mode = mode if mode in MODES else self.mode_for(a, b)
        key = (frozenset({a.key, b.key}), mode)
        if key not in self._minutes:
            given = self._given(a, b, mode)
            if given is not None:
                value = given
            else:
                ca, cb = self._coords(a), self._coords(b)
                if ca and cb:
                    raw = (self._overhead(mode)
                           + _km(ca, cb) * self._minutes_per_km(mode))
                    value = int(math.ceil(raw / 5) * 5)
                else:
                    value = self.cfg.unknown_minutes
            self._minutes[key] = value
        return self._minutes[key]


# ---------------------------------------------------------------- stops

@dataclass
class Stop:
    """Somewhere you have to be."""
    start: datetime
    end: datetime
    place: Place
    label: str
    lesson: bool = False      # a school-calendar event: work at school after it
    travel: str = ""          # "car", "public" or "" (the default for it)


@dataclass
class DayPlan:
    day: date
    windows: list[Interval] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class Routing:
    days: dict[date, DayPlan] = field(default_factory=dict)
    unplaced: list[str] = field(default_factory=list)

    @property
    def availability(self) -> dict[date, list[Interval]]:
        return {day: plan.windows for day, plan in self.days.items()}

    @property
    def problems(self) -> list[str]:
        return [p for plan in self.days.values() for p in plan.problems]


def collect_stops(places: Places, diary, feed_events, blackouts,
                  personal_calendar: str = "") -> tuple[list[Stop], list[str]]:
    """Commitments that put you somewhere, and the ones that could not be placed.

    `diary` is calendar events (start, end, summary, location, calendar,
    all_day); `feed_events` the timetable feeds; `blackouts` (interval,
    location, label) for blackouts you gave a place.
    """
    stops: list[Stop] = []
    unplaced: list[str] = []
    personal = personal_calendar.strip().lower()

    def add(start, end, location, source, label, travel=""):
        place = places.resolve(location, source)
        lesson = (places.is_school_source(source)
                  and source.strip().lower() in places.cfg.school_calendars)
        if place is None:
            if location.strip() or source.strip().lower() == personal:
                unplaced.append(f"{start:%a %d %b %H:%M} {label[:40]}"
                                + (f" @ {location[:30]}" if location.strip()
                                   else " (no location)"))
            return
        if place.is_home:
            return            # busy, but you are already where you work
        stops.append(Stop(start, end, place, label or place.name,
                          lesson=lesson and place.key == "school",
                          travel=travel))

    for event in diary:
        if getattr(event, "all_day", False):
            continue
        add(event.start, event.end, event.location or "", event.calendar,
            event.summary, getattr(event, "travel", "")
            or mode_from_text(event.summary, event.location or "",
                              getattr(event, "notes", "")))
    for event in feed_events:
        add(event.start, event.end, getattr(event, "location", "") or "",
            event.source, event.summary)
    for blackout in blackouts:
        interval, location, label = blackout
        if location.strip():
            add(interval.start, interval.end, location, "", label or "blackout",
                getattr(blackout, "travel", ""))
    stops.sort(key=lambda s: s.start)
    return stops, unplaced


# ---------------------------------------------------------------- routing

def _usable(windows: list[Interval], start: datetime, end: datetime,
            busy: list[Interval], block: int) -> tuple[int, list[Interval]]:
    """Whole blocks of free time inside `windows` between start and end."""
    total = 0
    kept: list[Interval] = []
    for window in windows:
        lo, hi = max(window.start, start), min(window.end, end)
        if hi <= lo:
            continue
        kept.append(Interval(lo, hi))
        cursor = lo
        for b in busy:
            if b.end <= cursor or b.start >= hi:
                continue
            if b.start > cursor:
                total += _blocks(cursor, b.start, block)
            cursor = max(cursor, b.end)
            if cursor >= hi:
                break
        if cursor < hi:
            total += _blocks(cursor, hi, block)
    return total, kept


def _blocks(start: datetime, end: datetime, block: int) -> int:
    minutes = int((end - start).total_seconds() // 60)
    return (minutes // block) * block


def route(cfg, places: Places, stops: list[Stop], busy: list[Interval],
          start: datetime, end: datetime, tz: ZoneInfo) -> Routing:
    """Choose, day by day, where to be between commitments."""
    routing = Routing()
    gap = timedelta(minutes=cfg.scheduling.gap_minutes)
    padded = merge([Interval(b.start - gap, b.end + gap) for b in busy])
    block = cfg.scheduling.block_minutes
    cap = cfg.scheduling.max_daily_minutes
    until = cfg.travel.school_work_until

    by_day: dict[date, list[Stop]] = {}
    for stop in stops:
        day = stop.start.astimezone(tz).date()
        if start.date() <= day <= end.date():
            by_day.setdefault(day, []).append(stop)

    for day, todays in sorted(by_day.items()):
        midnight = datetime.combine(day, time(0, 0), tzinfo=tz)
        home_window = cfg.scheduling.windows.get(day.weekday())
        windows: dict[str, list[Interval]] = {
            "home": ([Interval(datetime.combine(day, home_window.start, tzinfo=tz),
                               datetime.combine(day, home_window.end, tzinfo=tz))]
                     if home_window else []),
            "school": [],
        }
        lessons = [s.end for s in todays if s.lesson]
        if until and lessons:
            opens = max(lessons)
            closes = datetime.combine(day, until, tzinfo=tz)
            if opens < closes:
                windows["school"] = [Interval(opens, closes)]

        plan = DayPlan(day)
        chain = ([(places.home, midnight, midnight)]
                 + [(s.place, s.start, s.end) for s in todays]
                 + [(places.home, midnight + timedelta(days=1),
                     midnight + timedelta(days=1))])
        travel = [""] + [s.travel for s in todays] + [""]
        labels = ["home"] + [s.label for s in todays] + ["home"]
        for i in range(len(chain) - 1):
            (here, _, t1), (there, t2, _) = chain[i], chain[i + 1]
            if t2 <= t1:
                if here.key != there.key:
                    plan.problems.append(
                        f"{day:%a %d %b}: {labels[i]} and {labels[i + 1]} "
                        f"overlap in different places")
                continue
            # The commitment you are going to decides how you get there;
            # failing that, the one you are leaving.
            mode = places.mode_for(here, there, travel[i + 1], travel[i])
            best = _best_leg(places, windows, padded, block, cap,
                             here, t1, there, t2, mode)
            if best is None:
                need = places.minutes(here, there, mode)
                plan.problems.append(
                    f"{day:%a %d %b}: cannot get from {here.name} "
                    f"({labels[i]}, ends {t1:%H:%M}) to {there.name} "
                    f"({labels[i + 1]}, {t2:%H:%M}) - needs {need} min, "
                    f"has {int((t2 - t1).total_seconds() // 60)}")
                continue
            work, segments, kept = best
            plan.windows.extend(kept)
            if i == 0 and len(segments) == 1 and segments[0][0].is_home:
                continue              # staying in bed until you leave: not news
            if (not work and len(segments) == 1
                    and segments[0][0].key == here.key == there.key):
                continue              # a break between lessons: not news either
            route_text = " -> ".join(
                f"{place.name} {a:%H:%M}-{b:%H:%M}" for place, a, b in segments)
            start_text = "home" if i == 0 else f"{t1:%H:%M} {labels[i][:28]}"
            end_text = ("home" if i == len(chain) - 2
                        else f"{t2:%H:%M} {labels[i + 1][:28]}")
            plan.lines.append(f"{start_text} -> {route_text} -> {end_text}"
                              + (f"  ({work} min usable)" if work else ""))
        plan.windows = merge(plan.windows)
        routing.days[day] = plan
    return routing


def _best_leg(places: Places, windows, padded, block, cap,
              here: Place, t1: datetime, there: Place, t2: datetime,
              mode: str | None = None):
    """The best way to spend the time between two commitments, or None.

    `mode` is how this stretch of the day is travelled. Without one, each
    journey in it goes the way that journey usually would."""
    workplaces = [places.home] + ([places.school] if windows["school"] else [])
    candidates = []

    def go(a: Place, b: Place) -> int:
        return places.minutes(a, b, mode)

    def score(segments, travel):
        work, kept = 0, []
        for place, a, b in segments:
            got, k = _usable(windows.get(place.key, []), a, b, padded, block)
            work += got
            kept += k
        away = travel + sum(int((b - a).total_seconds() // 60)
                            for place, a, b in segments if not place.is_home)
        candidates.append(((min(work, cap), -away, -travel), work, segments, kept))

    singles = {p.key: p for p in [here, there, *workplaces]}
    for place in singles.values():
        arrive = t1 + timedelta(minutes=go(here, place))
        leave = t2 - timedelta(minutes=go(place, there))
        if arrive <= leave:
            score([(place, arrive, leave)],
                  go(here, place) + go(place, there))

    for first in workplaces:
        for second in workplaces:
            if first.key == second.key:
                continue
            between = timedelta(minutes=go(first, second))
            arrive = t1 + timedelta(minutes=go(here, first))
            leave = t2 - timedelta(minutes=go(second, there))
            latest = leave - between
            if latest < arrive:
                continue
            switches = {arrive, latest}
            switches |= {w.end for w in windows[first.key]}
            switches |= {w.start - between for w in windows[second.key]}
            step = arrive
            while step <= latest:
                switches.add(step)
                step += timedelta(minutes=15)
            travel = (go(here, first) + go(first, second)
                      + go(second, there))
            for switch in switches:
                if arrive <= switch <= latest:
                    score([(first, arrive, switch),
                           (second, switch + between, leave)], travel)

    if not candidates:
        return None
    _, work, segments, kept = max(candidates, key=lambda c: c[0])
    segments = [(p, a, b) for p, a, b in segments if b > a]
    return work, segments, kept
