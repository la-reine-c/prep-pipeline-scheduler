"""Configuration loading. Secrets come from the environment, never the YAML."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import yaml

from .notify import EmailConfig, NotifyConfig, TelegramConfig

_DAY_KEYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def parse_time(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour=hour, minute=minute)


def _env(raw: Any, required: bool = True) -> str:
    """Resolve a secret reference.

        cred:NAME   a systemd encrypted credential (preferred)
        env:NAME    an environment variable, usually from .env
        anything else is taken literally

    `cred:` is better than `env:` for the secrets this machine genuinely must
    hold unattended - the iCloud app password, the timetable feed key, the
    bot token. systemd decrypts them into a private tmpfs that only this
    service can read, rather than leaving them in a file that every process
    of yours can read and that is one mistaken `cat` away from a screenshot.
    See deploy/systemd-creds.md.
    """
    text = str(raw)

    if text.startswith("cred:"):
        name = text[5:]
        directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
        if not directory:
            if required:
                raise RuntimeError(
                    f"config asks for credential '{name}' but this process was "
                    f"not started by systemd with LoadCredentialEncrypted. "
                    f"Run it under the unit, or use env: instead.")
            return ""
        path = Path(directory) / name
        try:
            # Trailing newlines are almost always an artefact of however the
            # value was typed in, and a password with a stray \n fails in a
            # way that looks like a wrong password.
            return path.read_text().strip()
        except OSError as exc:
            if required:
                raise RuntimeError(
                    f"credential '{name}' was not passed to this service "
                    f"({exc}). Check LoadCredentialEncrypted= in the unit."
                ) from exc
            return ""

    if text.startswith("env:"):
        name = text[4:]
        # Stripped for the same reason the credential above is: a value that
        # picked up a trailing newline or space on its way into the
        # environment fails exactly like a wrong password, and this asymmetry
        # meant `cred:` was protected from that and `env:` was not.
        value = os.environ.get(name, "").strip()
        if not value and required:
            raise RuntimeError(
                f"environment variable {name} is not set - add {name}=... "
                f"to .env beside your config.yaml, or export it")
        return value

    return text


@dataclass
class TeamsConfig:
    assignments_url: str
    url_hints: list[str]
    profile_dir: Path
    chromium_path: str = ""   # empty = use Playwright's own build (macOS)
    headless: bool = True
    settle_ms: int = 8000          # MINIMUM wait after the page loads
    quiet_ms: int = 5000           # stop once no new assignment data for this long
    max_settle_ms: int = 60000     # absolute cap, however busy the page is
    scroll_passes: int = 8         # nudges to make a virtualised list load more
    nav_timeout_ms: int = 120000
    block_resources: list[str] = field(default_factory=lambda: ["image", "media", "font"])
    # Teams renews its own access in a hidden iframe on login.microsoftonline.com,
    # which is a third party to the Teams page around it. Blocked, that renewal
    # fails silently and a good session starts reading as dead a few days after
    # every sign-in. See prep/cookies.py. Off only if you are deliberately
    # testing what happens without it.
    third_party_cookies: bool = True
    # What the Assignments app is called in your tenant. Empty means the
    # default, which matches anything containing "assignment"; a school that
    # renames it to Classwork has no Assignments link to find, and that looks
    # exactly like a broken session.
    assignment_labels: list[str] = field(default_factory=list)
    # A window that opens but never renders is almost always GPU acceleration
    # failing on hardware Chromium cannot drive - a VM, a headless desktop, or
    # a Mac's GPU under Linux. Software rendering is slower in a way nothing
    # here notices, and it works everywhere, so it is the default.
    disable_gpu: bool = True
    # None = add --no-sandbox only when running as root, which is the only
    # case that needs it. True forces it on, False keeps it off even as root
    # (Chromium will then refuse to start, which is the honest outcome).
    no_sandbox: bool | None = None
    browser_args: list[str] = field(default_factory=list)


@dataclass
class CalendarConfig:
    caldav_url: str
    username: str
    password: str
    prep_calendar: str
    ignore_calendars: list[str] = field(default_factory=list)
    # Where outside commitments live - instrument lessons and the like. The
    # dashboard writes to it; the scheduler reads it like any other calendar.
    personal_calendar: str = "Personal"


@dataclass
class DayWindow:
    start: time
    end: time


@dataclass
class EstimatorConfig:
    per_day_minutes: int = 30
    cap_minutes: int = 120
    shrinkage_k: float = 3.0
    min_minutes: int = 15


@dataclass
class SchedulingConfig:
    block_minutes: int = 30
    max_daily_minutes: int = 120
    flow_buffer_minutes: int = 5
    gap_minutes: int = 10
    due_time: time = time(8, 30)
    windows: dict[int, DayWindow] = field(default_factory=dict)
    teacher_keywords: dict[str, list[str]] = field(default_factory=dict)
    class_map: dict[str, str] = field(default_factory=dict)      # classId -> teacher
    people_map: dict[str, str] = field(default_factory=dict)     # creator id -> teacher
    title_codes: dict[str, str] = field(default_factory=dict)    # "AAA" -> teacher
    archived_classes: set = field(default_factory=set)  # class ids to ignore
    skip_past_due: bool = True
    # How long after a session ends you have to say whether you did it. Until
    # then it counts as done; after it, an unconfirmed session counts as
    # missed and its time is booked again.
    confirm_grace_hours: float = 24.0


@dataclass
class PlaceConfig:
    name: str
    postcode: str = ""
    # Words that mark an event as being here, matched against its LOCATION.
    # Case-insensitive.
    match: list[str] = field(default_factory=list)


@dataclass
class TravelConfig:
    """Where you are, and how long it takes to get between places.

    Prep happens at home or at school - never in transit, never at a lesson.
    Each day is routed: between two commitments, whichever of staying put,
    going home or staying at school leaves the most usable prep time wins.
    """
    home: PlaceConfig
    school: PlaceConfig
    # Every event in these calendars / feeds is at school, whatever room it
    # names. Matched ignoring case and surrounding spaces.
    school_calendars: list[str] = field(default_factory=list)
    school_feeds: list[str] = field(default_factory=list)
    # Latest you can stay working at school; from your last lesson until then.
    # None = school is never somewhere you work.
    school_work_until: time | None = None
    places: dict[str, PlaceConfig] = field(default_factory=dict)
    # Known journey times, either direction: {frozenset({"home", "school"}): 75}
    times: dict[frozenset, int] = field(default_factory=dict)
    # Look postcodes up on api.postcodes.io (only the postcode is sent) and
    # scale an estimate from a known journey. Off = unknown_minutes for
    # anywhere without a time in `times`.
    # Known journey times by car, the same way round as `times`.
    car_times: dict[frozenset, int] = field(default_factory=dict)
    # How you get to and from outside commitments that do not say ("car" or
    # "public"), and between home and school. `times` is public transport.
    default_mode: str = "public"
    school_mode: str = "public"
    car_overhead_minutes: int = 5       # fixed part of an estimated drive
    lookup_postcodes: bool = True
    overhead_minutes: int = 15          # fixed part of any journey
    unknown_minutes: int = 60           # a place that cannot be located
    cache_path: Path = Path("~/prep/places.json").expanduser()


def _travel(raw: Any) -> TravelConfig | None:
    if not raw:
        return None

    def place(name: str, value: Any) -> PlaceConfig:
        value = value or {}
        return PlaceConfig(
            name=str(value.get("name", name)),
            postcode=str(value.get("postcode", "") or "").strip().upper(),
            match=[str(m).lower() for m in (value.get("match", []) or [])])

    school_raw = raw.get("school", {}) or {}

    def journeys(name: str) -> dict[frozenset, int]:
        found = {}
        for pair, minutes in (raw.get(name, {}) or {}).items():
            ends = [part.strip() for part in str(pair).split("-") if part.strip()]
            if len(ends) != 2:
                raise RuntimeError(f"travel.{name}: '{pair}' should be 'a-b'")
            found[frozenset(ends)] = int(minutes)
        return found

    def mode(name: str) -> str:
        value = str(raw.get(name, "public") or "public").strip().lower()
        if value not in ("public", "car"):
            raise RuntimeError(f"travel.{name} must be 'public' or 'car', not '{value}'")
        return value

    times = journeys("times")
    until = school_raw.get("work_until")
    return TravelConfig(
        home=place("home", raw.get("home")),
        school=place("school", school_raw),
        school_calendars=[str(c).strip().lower()
                          for c in (school_raw.get("calendars", []) or [])],
        school_feeds=[str(f).strip().lower()
                      for f in (school_raw.get("feeds", []) or [])],
        school_work_until=parse_time(until) if until else None,
        places={str(k): place(str(k), v)
                for k, v in (raw.get("places", {}) or {}).items()},
        times=times,
        car_times=journeys("car_times"),
        default_mode=mode("default_mode"),
        school_mode=mode("school_mode"),
        car_overhead_minutes=int(raw.get("car_overhead_minutes", 5)),
        lookup_postcodes=bool(raw.get("lookup_postcodes", True)),
        overhead_minutes=int(raw.get("overhead_minutes", 15)),
        unknown_minutes=int(raw.get("unknown_minutes", 60)),
        cache_path=Path(raw.get("cache_path", "~/prep/places.json")).expanduser(),
    )


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    token: str = ""
    url_prefix: str = ""      # e.g. "/prep" when sharing a port with another app





@dataclass
class Config:
    teams: TeamsConfig
    calendar: CalendarConfig
    estimator: EstimatorConfig
    scheduling: SchedulingConfig
    web: WebConfig
    notify: NotifyConfig
    feeds: dict[str, str]        # name -> ICS url (subscribed calendars)
    feed_cache_dir: Path
    learned_map_path: Path
    db_path: Path
    dump_dir: Path
    export_path: Path
    timezone: str = "Europe/London"
    travel: TravelConfig | None = None


def _required(raw: Any, setting: str) -> str:
    """A secret the run cannot do without, wherever it is written.

    `env:` and `cred:` already refuse to resolve to nothing. A value typed
    straight into config.yaml did not, so a blank iCloud password reached
    CalDAV, which then reported "no supported authentication methods" -
    read by Phase C as iCloud never answering, the one diagnosis that sends
    you to check the network instead of the config.
    """
    value = _env("" if raw is None else raw)
    if not value.strip():
        raise RuntimeError(f"{setting} is empty - fill it in in config.yaml")
    return value.strip()


def _json_map(raw: Any) -> dict[str, str]:
    """Accept a YAML mapping, or `env:NAME` holding a JSON object.

    The class map grows as you identify classes, so it is convenient to keep
    it in .env rather than editing the config each time.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    text = _env(raw, required=False)
    if not text:
        return {}
    import json
    try:
        return {str(k): str(v) for k, v in json.loads(text).items()}
    except Exception as exc:
        raise RuntimeError(f"could not parse map from {raw}: {exc}") from exc


def load_dotenv(path: Path) -> list[str]:
    """Export a .env file into this process, never overriding what is set.

    On the mini, systemd reads .env through EnvironmentFile=. Run by hand -
    `make phase-c`, `python3 -m prep.run` - nothing did, so every `env:`
    secret came up "not set" even with a perfectly good .env beside the
    config. Values already in the environment win, so systemd, a
    credential or an explicit `export` is never second-guessed. Parsed the
    way EnvironmentFile= parses it: comments, blank lines and a leading
    `export ` are skipped, and one pair of surrounding quotes is removed so
    they do not travel as part of a password.
    """
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return loaded


def load(path: str | Path) -> Config:
    load_dotenv(Path(path).expanduser().resolve().parent / ".env")
    data = yaml.safe_load(Path(path).read_text())

    t = data["teams"]
    teams = TeamsConfig(
        assignments_url=t["assignments_url"],
        url_hints=[h.lower() for h in t["url_hints"]],
        profile_dir=Path(t["profile_dir"]).expanduser(),
        chromium_path=t.get("chromium_path", "") or "",
        headless=t.get("headless", True),
        settle_ms=t.get("settle_ms", 8000),
        quiet_ms=t.get("quiet_ms", 5000),
        max_settle_ms=t.get("max_settle_ms", 60000),
        scroll_passes=t.get("scroll_passes", 8),
        nav_timeout_ms=t.get("nav_timeout_ms", 120000),
        block_resources=t.get("block_resources", ["image", "media", "font"]),
        third_party_cookies=t.get("third_party_cookies", True),
        assignment_labels=[str(label) for label in
                           (t.get("assignment_labels", []) or [])],
        disable_gpu=t.get("disable_gpu", True),
        no_sandbox=t.get("no_sandbox", None),
        browser_args=list(t.get("browser_args", []) or []),
    )

    c = data["calendar"]
    calendar = CalendarConfig(
        caldav_url=c["caldav_url"],
        username=_required(c.get("username"), "calendar.username"),
        password=_required(c.get("password"), "calendar.password"),
        prep_calendar=c["prep_calendar"],
        ignore_calendars=c.get("ignore_calendars", []) or [],
        personal_calendar=str(c.get("personal_calendar", "Personal")).strip(),
    )

    e = data.get("estimator", {}) or {}
    estimator = EstimatorConfig(
        per_day_minutes=e.get("per_day_minutes", 30),
        cap_minutes=e.get("cap_minutes", 120),
        shrinkage_k=e.get("shrinkage_k", 3.0),
        min_minutes=e.get("min_minutes", 15),
    )

    s = data["scheduling"]
    windows = {
        _DAY_KEYS[key.lower()]: DayWindow(parse_time(val["start"]), parse_time(val["end"]))
        for key, val in s["windows"].items()
    }
    scheduling = SchedulingConfig(
        block_minutes=s.get("block_minutes", 30),
        max_daily_minutes=s.get("max_daily_minutes", 120),
        flow_buffer_minutes=s.get("flow_buffer_minutes", 5),
        gap_minutes=s.get("gap_minutes", 10),
        due_time=parse_time(s.get("due_time", "08:30")),
        windows=windows,
        teacher_keywords={
            teacher: [w.lower() for w in words]
            for teacher, words in (s.get("teacher_keywords", {}) or {}).items()
        },
        class_map=_json_map(s.get("class_map")),
        people_map=_json_map(s.get("people_map")),
        title_codes=_json_map(s.get("title_codes")),
        archived_classes={str(key).lower()
                          for key in (s.get("archived_classes", []) or [])},
        skip_past_due=s.get("skip_past_due", True),
        confirm_grace_hours=float(s.get("confirm_grace_hours", 24)),
    )

    # Mappings taught via `python3 -m prep.identify` live in a JSON file and
    # take precedence, so naming a prep beats hand-editing GUIDs in YAML.
    learned_path = Path(data.get("learned_map_path",
                                 "~/prep/teachers.json")).expanduser()
    if learned_path.exists():
        try:
            import json
            learned = json.loads(learned_path.read_text())
            scheduling.class_map.update(
                {str(k): str(v) for k, v in (learned.get("class_map") or {}).items() if v})
            scheduling.title_codes.update(
                {str(k): str(v) for k, v in (learned.get("title_codes") or {}).items() if v})
            scheduling.people_map.update(
                {str(k): str(v) for k, v in (learned.get("people_map") or {}).items() if v})
            scheduling.archived_classes.update(
                str(k).lower() for k, v in (learned.get("archived") or {}).items() if v)
        except Exception as exc:
            raise RuntimeError(f"could not read {learned_path}: {exc}") from exc

    w = data.get("web", {}) or {}
    web = WebConfig(
        host=w.get("host", "0.0.0.0"),
        port=int(w.get("port", 8080)),
        token=_env(w.get("token", ""), required=False),
        url_prefix=("/" + str(w.get("url_prefix", "")).strip("/"))
                   if w.get("url_prefix") else "",
    )

    n = data.get("notify", {}) or {}
    mail = n.get("email", {}) or {}
    notify = NotifyConfig(
        ntfy_topic=n.get("ntfy_topic", ""),
        ntfy_server=n.get("ntfy_server", "https://ntfy.sh"),
        email=EmailConfig(
            host=mail.get("host", "smtp.gmail.com"),
            port=int(mail.get("port", 587)),
            username=_env(mail.get("username", ""), required=False),
            password=_env(mail.get("password", ""), required=False),
            to=_env(mail.get("to", ""), required=False),
            sender=_env(mail.get("sender", ""), required=False),
        ),
        telegram=TelegramConfig(
            bot_token=_env((n.get("telegram", {}) or {}).get("bot_token", ""),
                           required=False),
            chat_id=_env((n.get("telegram", {}) or {}).get("chat_id", ""),
                         required=False),
        ),
        imessage_to=_env(n.get("imessage_to", ""), required=False),
        state_path=Path(n.get("state_path", "~/prep/alerts.json")).expanduser(),
    )

    return Config(
        teams=teams,
        calendar=calendar,
        estimator=estimator,
        scheduling=scheduling,
        web=web,
        notify=notify,
        feeds={name: _required(url, f"feeds.{name}")
               for name, url in (data.get("feeds", {}) or {}).items()},
        feed_cache_dir=Path(data.get("feed_cache_dir",
                                     "~/prep/feed-cache")).expanduser(),
        learned_map_path=learned_path,
        db_path=Path(data.get("db_path", "~/prep/prep.sqlite3")).expanduser(),
        dump_dir=Path(data.get("dump_dir", "~/prep/dumps")).expanduser(),
        export_path=Path(data.get("export_path", "~/prep/prep.txt")).expanduser(),
        timezone=data.get("timezone", "Europe/London"),
        travel=_travel(data.get("travel")),
    )
