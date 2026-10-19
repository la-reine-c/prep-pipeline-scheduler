"""Capture assignments from Teams web.

We never scrape the DOM. A response listener keeps JSON bodies whose URL looks
like the assignment service, and the parser walks them for objects carrying a
due date. Identity is the Teams GUID; a composite key is only a last resort,
because a key containing the due date breaks when a due date moves.
"""
from __future__ import annotations

import hashlib
import json
import os
import logging
import re
import sys
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

from . import cookies
from .config import Config
from .isotime import fromiso

log = logging.getLogger(__name__)

GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# Landing on one of these means Teams bounced us to sign in. Keep them
# SPECIFIC: a broad entry like "cloud.microsoft" would match the Teams app
# itself at teams.cloud.microsoft and report every healthy capture as an
# expired session.
LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com",
               "login.microsoft.com", "login.cloud.microsoft")

TITLE_KEYS = ("displayName", "title", "name", "assignmentTitle")
DUE_KEYS = ("dueDateTime", "dueDate", "closeDateTime", "due")
ASSIGNED_KEYS = ("assignedDateTime", "publishedDateTime", "createdDateTime", "assignedDate")
CLASS_NAME_KEYS = ("className", "courseName", "classDisplayName", "teamName")
CLASS_ID_KEYS = ("classId", "classID", "teamId", "groupId")
ID_KEYS = ("id", "assignmentId", "objectId")
# Who set it. This is per assignment, so it separates teachers who share a
# class - which class id and title codes cannot do.
CREATOR_KEYS = ("createdBy", "lastModifiedBy", "assignedBy", "author", "teacher")
# Some tenants mark a wound-up class. Most do not, so this is a bonus signal -
# how long ago the class last set anything is the reliable one.
ARCHIVE_KEYS = ("isArchived", "archived", "classArchived", "isClassArchived",
                "teamArchived")

GUID_HINT = 32  # a Teams GUID is 36 chars with dashes

# --disable-dev-shm-usage because Chromium's default /dev/shm is too small in
# a container and it crashes rather than saying so.
BASE_ARGS = ("--disable-dev-shm-usage",)

# --no-sandbox is only added when running as root, where Chromium refuses to
# start without it. It is a real reduction in protection - the sandbox is what
# stops a malicious page reaching the rest of the machine - so it should not be
# on by default just because it was needed once. Chromium warns about it for
# good reason, and the right answer is usually to run as a normal user.
ROOT_ARGS = ("--no-sandbox",)

# Applied when disable_gpu is on, which it is by default.
#
# The symptom these fix is a window that opens and stays blank. Chromium has
# decided it has a GPU, handed rendering to it, and got nothing back. These
# three flags are old, stable and understood by every version, which matters
# more here than being optimal: a flag a newer Chromium has renamed would
# turn a rendering bug into a browser that will not start.
SOFTWARE_RENDER_ARGS = (
    "--disable-gpu",
    "--disable-gpu-compositing",
    "--disable-features=VizDisplayCompositor",
)

# Ordered attempts at getting pixels onto a screen, least invasive first.
#
# A window that opens and stays blank is the single most common way this
# system stops dead, and it depends entirely on the machine: a discrete
# NVIDIA card under X, a VM with no GPU at all, and an ARM box running a Mac
# GPU under Linux all fail differently and none of them tell you why. Rather
# than asking someone to guess flags, the sign-in walks down this ladder
# until something draws.
#
# "software" is the default rather than "gpu" because nothing here is fast
# enough to notice the difference, and a slow window you can see beats a fast
# one you cannot.
RENDER_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gpu", ()),
    ("software", SOFTWARE_RENDER_ARGS),
    # ANGLE over SwiftShader: a full software GL implementation, for hardware
    # whose driver accepts the work and then never returns it.
    ("swiftshader", SOFTWARE_RENDER_ARGS + ("--use-gl=angle",
                                            "--use-angle=swiftshader")),
    # Last resort, and Linux-only: Chromium's Wayland backend failing to
    # present is a different fault from the GPU failing to render, and X11
    # sidesteps it.
    ("x11", SOFTWARE_RENDER_ARGS + ("--use-gl=angle", "--use-angle=swiftshader",
                                    "--ozone-platform=x11")),
)

DEFAULT_RENDER_LEVEL = "software"


def render_flags(level: str) -> tuple[str, ...]:
    for name, flags in RENDER_LEVELS:
        if name == level:
            return flags
    raise ValueError(f"unknown render level {level!r}; try one of "
                     f"{', '.join(name for name, _ in RENDER_LEVELS)}")


def render_ladder(start: str = DEFAULT_RENDER_LEVEL) -> list[str]:
    """The levels to try, in order, beginning at `start`.

    Levels before `start` are skipped rather than reordered: someone who
    asked for the GPU gets the GPU first, and someone who did not is not
    handed it as a fallback.
    """
    names = [name for name, _ in RENDER_LEVELS]
    if start not in names:
        raise ValueError(f"unknown render level {start!r}")
    remaining = names[names.index(start):]
    if sys.platform != "linux":
        remaining = [name for name in remaining if name != "x11"]
    return remaining


# How many rows of a screenshot to look at. PNG rows are filtered against the
# row above, so they have to be read in order - but a window that drew nothing
# drew nothing at the top either, and stopping early keeps this to a few
# milliseconds however large the screen is.
BLANK_SAMPLE_ROWS = 300


def png_colours(shot: bytes, max_rows: int = BLANK_SAMPLE_ROWS) -> set | None:
    """The distinct colours in the top of a PNG, or None if it cannot be read.

    Written out rather than guessed at from the file size. The first version
    of this inferred "blank" from how well the image compressed, which sounds
    reasonable and is wrong: a plain white 1280x720 screenshot is several
    kilobytes, so the one case it existed to catch was the one it missed.
    """
    if not shot.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    width = height = bit_depth = colour_type = 0
    compressed = bytearray()
    at = 8
    while at + 8 <= len(shot):
        length = int.from_bytes(shot[at:at + 4], "big")
        kind = shot[at + 4:at + 8]
        body = shot[at + 8:at + 8 + length]
        at += 12 + length                      # 4 length, 4 type, 4 CRC
        if kind == b"IHDR" and len(body) >= 13:
            width = int.from_bytes(body[0:4], "big")
            height = int.from_bytes(body[4:8], "big")
            bit_depth, colour_type = body[8], body[9]
        elif kind == b"IDAT":
            compressed += body
        elif kind == b"IEND":
            break

    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(colour_type)
    if not (width and height and channels) or bit_depth != 8:
        return None                            # palettes and 16-bit: not ours

    rows = min(height, max_rows)
    stride = width * channels
    try:
        raw = zlib.decompressobj().decompress(bytes(compressed),
                                              (stride + 1) * rows)
    except zlib.error:
        return None

    colours = set()
    previous = bytearray(stride)
    for row in range(rows):
        start = row * (stride + 1)
        if start + stride + 1 > len(raw):
            break
        filter_type = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])

        # The five PNG filters, from the spec. Each byte is predicted from
        # its left neighbour, the byte above, or both.
        if filter_type == 1:                   # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:                 # Up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:                 # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:                 # Paeth
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up = previous[i]
                upper_left = previous[i - channels] if i >= channels else 0
                estimate = left + up - upper_left
                da, db, dc = (abs(estimate - left), abs(estimate - up),
                              abs(estimate - upper_left))
                if da <= db and da <= dc:
                    predictor = left
                elif db <= dc:
                    predictor = up
                else:
                    predictor = upper_left
                line[i] = (line[i] + predictor) & 0xFF

        # Every seventh pixel: enough to tell one colour from many, and a
        # seventh of the work.
        for x in range(0, stride, channels * 7):
            colours.add(bytes(line[x:x + channels]))
        previous = line

    return colours


def window_looks_blank(page) -> bool | None:
    """Did anything actually draw? None when it cannot be told.

    A screenshot is captured through a different path from the window on
    screen, so a clean screenshot does NOT prove the window is not white -
    which is why every caller also asks the person sitting in front of it.
    What this catches without troubling anyone is the case where nothing
    rendered at all.

    Both ways of being wrong are cheap: a false "blank" costs one relaunch at
    a slower render level, and a false "fine" falls through to the question.
    """
    try:
        shot = page.screenshot()
    except Exception:
        return None
    colours = png_colours(shot)
    if colours is None:
        return None
    # One colour is a blank window. Two allows for a scrollbar or a border
    # on an otherwise empty page, which is still nothing having drawn.
    return len(colours) <= 2


def render_advice(level: str) -> list[str]:
    """The config lines that make a working render level permanent."""
    if level == DEFAULT_RENDER_LEVEL:
        return []
    if level == "gpu":
        return ["teams:", "  disable_gpu: false"]
    extra = [flag for flag in render_flags(level)
             if flag not in SOFTWARE_RENDER_ARGS]
    return ["teams:", "  disable_gpu: true",
            "  browser_args: [" + ", ".join(f'"{flag}"' for flag in extra) + "]"]


# Chromium keeps only the LAST --disable-features it is given; a second one
# replaces the first rather than adding to it. Two parts of this file now want
# that switch, so they are merged into one before launch. Getting this wrong
# would be miserable to debug: a change about cookies would silently undo the
# rendering fix and produce a blank window.
FEATURE_FLAGS = ("--disable-features", "--enable-features")


def _merge_feature_flags(args: list[str]) -> list[str]:
    """Fold repeated --disable-features / --enable-features into one each."""
    values: dict[str, list[str]] = {}
    for arg in args:
        name, sep, value = arg.partition("=")
        if not sep or name not in FEATURE_FLAGS:
            continue
        bucket = values.setdefault(name, [])
        for item in value.split(","):
            item = item.strip()
            if item and item not in bucket:
                bucket.append(item)

    merged: list[str] = []
    emitted: set[str] = set()
    for arg in args:
        name, sep, _ = arg.partition("=")
        if sep and name in FEATURE_FLAGS:
            if name in emitted:
                continue          # already folded into the first occurrence
            emitted.add(name)
            merged.append(f"{name}={','.join(values[name])}")
        else:
            merged.append(arg)
    return merged


def _running_as_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:      # not POSIX
        return False


def browser_args(teams, render: str = "") -> list[str]:
    """The flags for launching Chromium.

    Shared by the scraper and by prep.login on purpose. If the two launched
    with different flags you could sign in successfully in a window that
    renders and then have every headless capture fail, which is a miserable
    thing to debug.

    `render` overrides the configured rendering for one launch, which is what
    the sign-in walks down when a window opens blank. Left empty, the config
    decides, so a scheduled run is never affected by what a person tried once.
    """
    args = list(BASE_ARGS)

    # Only where it is actually required.
    if getattr(teams, "no_sandbox", None) is True:
        args.extend(ROOT_ARGS)
    elif getattr(teams, "no_sandbox", None) is None and _running_as_root():
        args.extend(ROOT_ARGS)
        log.warning("running as root, so Chromium needs --no-sandbox. The "
                    "sandbox is what stops a malicious page reaching the rest "
                    "of this machine; a normal user account is safer.")

    if render:
        args.extend(render_flags(render))
    elif getattr(teams, "disable_gpu", True):
        args.extend(SOFTWARE_RENDER_ARGS)

    # Newer Chromium builds phase third-party cookies out by default, which
    # breaks Teams renewing its own access in the background. See prep/cookies.py.
    if getattr(teams, "third_party_cookies", True):
        args.append("--disable-features=" + ",".join(cookies.LAUNCH_FEATURES))

    args.extend(getattr(teams, "browser_args", []) or [])
    return _merge_feature_flags(args)


class SessionExpired(RuntimeError):
    """Teams bounced us to a sign-in page instead of the app."""


class ProfileBusy(RuntimeError):
    """Another Chromium already has this profile open."""


class PlaywrightMissing(RuntimeError):
    """The playwright package is not installed for this interpreter."""


def require_playwright(chromium_path: str = ""):
    """Import playwright, or explain how to get it.

    "No module named 'playwright'" is accurate and useless: it fires at the
    exact moment someone is following a setup guide, and says nothing about
    which of the several install routes they should take. It is also easy to
    install it into one interpreter and run another, which this catches by
    naming the interpreter that came up short.
    """
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError as exc:
        lines = [
            "playwright is not installed for this interpreter.",
            f"  interpreter: {sys.executable}",
            "",
            "  In a venv:      pip install playwright",
            "  Without one:    pip install --break-system-packages playwright",
        ]
        if chromium_path:
            lines += [
                "",
                f"  You have chromium_path set to {chromium_path}, so that is",
                "  all you need - the browser itself is already there.",
            ]
        else:
            lines += [""] + ["  Then " + BROWSER_ADVICE[0][0].lower()
                             + BROWSER_ADVICE[0][1:]] + [
                "  " + line for line in BROWSER_ADVICE[1:]]
        raise PlaywrightMissing("\n".join(lines)) from exc


# How to get a browser, shared by every place that has to say it.
#
# Playwright's own build first: Ubuntu is the platform Playwright is built and
# tested on. Ubuntu's apt has no Chromium at all - "apt install chromium" has
# no candidate, and chromium-browser is a stub that installs the snap - so the
# old advice to use the system package fails outright there.
BROWSER_ADVICE = (
    "Download Playwright's own browser (the right choice on Ubuntu):",
    "    python -m playwright install --with-deps chromium",
    "--with-deps installs the system libraries it needs, via sudo apt.",
    "",
    "Or use Ubuntu's snap (sudo snap install chromium) and set under teams:",
    "    chromium_path: /snap/bin/chromium",
    "A snap can only write to plain folders in your home directory, so",
    "profile_dir must be one - ~/prep/profile is; /tmp and ~/.hidden are not.",
    "On Debian or Kali, apt has a real package: sudo apt install -y chromium,",
    "then chromium_path: /usr/bin/chromium.",
)


def is_snap(chromium_path: str) -> bool:
    """Whether this browser runs confined as a snap.

    /usr/bin/chromium-browser on Ubuntu looks like a system browser and is a
    shell script that runs the snap, so the file is read as well as the path.
    """
    if not chromium_path:
        return False
    path = Path(chromium_path)
    if str(path.resolve()).startswith("/snap/") or str(path).startswith("/snap/"):
        return True
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return not head.startswith(b"\x7fELF") and b"/snap/bin/" in head


def snap_profile_problem(chromium_path: str, profile_dir: Path) -> str:
    """Why a snap browser cannot use this profile, or "" if it can.

    Measured on Ubuntu 24.04, both failures are ones you would not guess:
    under /tmp the snap launches, reports success, and writes the profile
    into its own private /tmp instead - so the sign-in never reaches the
    folder prep.session reads, and every run starts logged out. In a hidden
    folder it refuses to start with "profile is already in use", which sends
    you looking for a second browser that does not exist.
    """
    if not is_snap(chromium_path):
        return ""
    home = Path.home().resolve()
    profile = Path(profile_dir).expanduser().resolve()
    try:
        inside = profile.relative_to(home)
    except ValueError:
        return (f"{chromium_path} is a snap, and a snap cannot write to "
                f"{profile} - it only sees plain folders in {home}. Move "
                f"profile_dir there (e.g. ~/prep/profile), or leave "
                f"chromium_path empty to use Playwright's own browser.")
    hidden = [part for part in inside.parts if part.startswith(".")]
    if hidden:
        return (f"{chromium_path} is a snap, and a snap cannot use {profile}: "
                f"'{hidden[0]}' is a hidden folder, which snaps are denied. "
                f"Use a plain folder such as ~/prep/profile, or leave "
                f"chromium_path empty to use Playwright's own browser.")
    return ""


def check_profile_lock(profile_dir: Path) -> str:
    """Clear a dead lock, refuse a live one.

    Chromium writes SingletonLock into the profile and will not properly share
    it. Two symptoms come from ignoring this, and they look nothing like a
    lock: a page that will not reload, and a capture that sees a different
    number of assignments than the browser does - because the second Chromium
    quietly fell back to a scratch profile that is signed in but empty.

    A lock left behind by a crash is safe to remove. One held by a living
    process is not, so say so rather than fighting over the profile.
    """
    lock = Path(profile_dir) / "SingletonLock"
    if not (lock.exists() or lock.is_symlink()):
        return ""

    target = ""
    try:
        target = os.readlink(lock)
    except OSError:
        pass

    # The target looks like "hostname-12345"; the pid is after the last dash.
    pid = 0
    if "-" in target:
        tail = target.rsplit("-", 1)[-1]
        if tail.isdigit():
            pid = int(tail)

    if pid:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass                      # dead, so the lock is stale
        except PermissionError:
            raise ProfileBusy(
                f"another Chromium (pid {pid}) is using {profile_dir}"
            ) from None
        else:
            raise ProfileBusy(
                f"another Chromium (pid {pid}) is already using "
                f"{profile_dir}. Close it, or: kill {pid}")

    try:
        lock.unlink()
        log.info("removed a stale profile lock (%s)", target or "no target")
        return f"removed a stale lock left by pid {pid}" if pid else "removed a stale lock"
    except OSError as exc:
        log.warning("could not remove stale lock: %s", exc)
        return ""


@dataclass
class RawAssignment:
    assignment_id: str          # stable identity
    title: str
    class_ref: str              # human-readable class name, often absent
    class_id: str               # the class GUID, which is what actually identifies it
    creator_id: str             # the teacher who set it, if the tenant sends it
    creator_name: str
    assigned_at: str | None
    due_at: str | None
    archived: bool | None = None   # the tenant's own flag; None when it says nothing
    id_is_synthetic: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """For display. Never the class GUID - that is noise to a human."""
        return f"{self.title} ({self.class_ref})" if self.class_ref else self.title


def _looks_like_assignment_url(url: str, hints: list[str]) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in hints)


def capture(cfg: Config, dump: bool = False,
            notes: dict | None = None) -> list[dict[str, Any]]:
    """Load the Assignments page and return the JSON payloads it fetched.

    `notes`, when given, is filled in with how the view was reached, whether
    the list was seen to its end, and where third-party cookies stand. The
    payloads alone cannot tell you any of that, and all three are ways a
    capture comes back short while looking perfectly healthy.
    """
    sync_playwright = require_playwright(cfg.teams.chromium_path)

    payloads: list[dict[str, Any]] = []
    teams = cfg.teams
    problem = snap_profile_problem(teams.chromium_path, teams.profile_dir)
    if problem:
        raise RuntimeError(problem)
    teams.profile_dir.mkdir(parents=True, exist_ok=True)
    # Before the cookie work, and before launching: Chromium holds its
    # preferences in memory and writes them out when it exits, so anything
    # written under a running browser is undone.
    check_profile_lock(teams.profile_dir)
    state = cookies.prepare(teams.profile_dir,
                            getattr(teams, "third_party_cookies", True))
    if notes is not None:
        notes["cookies"] = state
    if state.allowed is False:
        # Not fatal. The session may still be within its current window, and
        # failing here would turn a warning into a week of no prep.
        log.warning("third-party cookies are %s - Teams cannot renew its own "
                    "access, so this session will start failing within days",
                    state.describe())

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(teams.profile_dir),
            executable_path=teams.chromium_path or None,
            headless=teams.headless,
            viewport={"width": 1280, "height": 1600},
            args=browser_args(teams),
        )

        if teams.block_resources:
            def _route(route):
                if route.request.resource_type in teams.block_resources:
                    return route.abort()
                return route.continue_()
            context.route("**/*", _route)

        page = context.pages[0] if context.pages else context.new_page()

        def _on_response(response):
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            if not _looks_like_assignment_url(response.url, teams.url_hints):
                return
            try:
                body = response.json()
            except Exception:
                return
            payloads.append({"url": response.url, "body": body})

        # Context level, not page level. Teams renders the Assignments edu
        # app inside an iframe, frequently on another origin, and a
        # page-scoped listener is a needless risk of missing it. A context
        # listener sees every frame and any popup.
        context.on("response", _on_response)
        try:
            page.goto(teams.assignments_url, wait_until="domcontentloaded",
                      timeout=teams.nav_timeout_ms)
            settle(page, payloads, teams, notes=notes)
            if any(host in page.url for host in LOGIN_HOSTS):
                raise SessionExpired(f"redirected to sign-in: {page.url}")
            if not reach_assignments(page, payloads, teams, notes=notes):
                # A picture of what the browser was actually looking at is
                # worth more than any amount of reasoning about why it saw
                # nothing - especially on a headless machine you cannot watch.
                save_failure_screenshot(page, cfg.dump_dir)
        finally:
            context.close()

    if dump:
        cfg.dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = cfg.dump_dir / f"payloads-{stamp}.json"
        target.write_text(json.dumps(payloads, indent=2, default=str))
        log.info("wrote %d payloads to %s", len(payloads), target)

    if not payloads:
        raise SessionExpired(
            "no assignment payloads captured - session may be dead or url_hints wrong"
        )
    return payloads


# Teams does not put the Assignments view in the address bar, so there is no
# URL that reaches it. This was true of teams.microsoft.com and is still true
# of teams.cloud.microsoft, where the address stays on the app root whether
# you are looking at chat or at your prep. Navigation has to be a click.
#
# This is not DOM scraping: the data still comes only from JSON responses. The
# DOM is used to get to the view, never to read what is in it.
ASSIGNMENT_LABEL = re.compile(r"assignment", re.I)

# The left rail holds a few pinned apps and hides the rest behind an overflow.
# Assignments is often in the overflow rather than missing, so "nothing named
# Assignments on this page" is worth one more click before believing it.
MORE_LABEL = re.compile(r"more app|view more|^more$|^\.\.\.$", re.I)


def label_pattern(teams=None):
    """What to click. Configurable, because tenants rename this app.

    A school that calls it "Classwork" has no Assignments link to find, and
    the failure looks exactly like a broken session.
    """
    labels = [str(label).strip()
              for label in (getattr(teams, "assignment_labels", None) or [])
              if str(label).strip()]
    if not labels:
        return ASSIGNMENT_LABEL
    return re.compile("|".join(re.escape(label) for label in labels), re.I)


def _frames(page) -> list:
    """Every frame in the page, or nothing if this object has no frames."""
    try:
        return list(page.frames)
    except Exception:
        return []


def _click_labelled(root, pattern) -> str:
    """Try each handle in turn on one page or frame. Returns what worked.

    Several strategies because Teams' markup changes between versions and
    tenants, and an accessible name is the most durable handle available -
    it is what a screen reader uses, so Microsoft has a reason to keep it.
    """
    attempts = (
        ("treeitem", lambda: root.get_by_role("treeitem", name=pattern)),
        ("link", lambda: root.get_by_role("link", name=pattern)),
        ("button", lambda: root.get_by_role("button", name=pattern)),
        ("tab", lambda: root.get_by_role("tab", name=pattern)),
        ("menuitem", lambda: root.get_by_role("menuitem", name=pattern)),
        ("aria-label", lambda: root.locator('[aria-label*="ssignment"]')),
        ("title", lambda: root.locator('[title*="ssignment"]')),
        ("text", lambda: root.get_by_text(pattern)),
    )
    for how, build in attempts:
        try:
            target = build().first
            if not target.is_visible(timeout=2000):
                continue
            target.click(timeout=5000)
            log.info("clicked Assignments via %s", how)
            return how
        except Exception:
            continue
    return ""


def open_app_overflow(page) -> bool:
    """Open the rail's "More apps" menu. Returns whether one opened.

    Only ever called after a direct click found nothing, so a wrong guess
    here costs a click on a menu, not a wrong view.
    """
    attempts = (
        lambda: page.get_by_role("button", name=MORE_LABEL),
        lambda: page.locator('[data-tid="app-bar-menu"]'),
        lambda: page.locator('[aria-label*="More app"]'),
    )
    for build in attempts:
        try:
            target = build().first
            if not target.is_visible(timeout=2000):
                continue
            target.click(timeout=5000)
            log.info("opened the app overflow menu")
            return True
        except Exception:
            continue
    return False


# Teams jumps to the Nth app in the left rail on Ctrl+N. Which number
# Assignments is depends on how your rail is arranged, and nobody should have
# to remember theirs - so this tries them and watches for assignment data,
# which is the only thing that actually confirms where it landed.
#
# It is here because a click needs something visible and hittable. In a
# headless window the rail can be collapsed to icons, rendered off-screen, or
# still booting, and then there is nothing to click while the shortcut works
# perfectly well.
RAIL_SHORTCUTS = 9


def keyboard_to_assignments(page, payloads: list, teams=None) -> str:
    """Try the rail shortcuts. Returns the one that worked, or "".

    Navigation only: it presses the same keys a person would and then looks
    at what the app fetched. Nothing is read out of the page.
    """
    before = len(payloads)
    for number in range(1, RAIL_SHORTCUTS + 1):
        try:
            # Escape first, in case an earlier attempt opened a menu that is
            # now swallowing the keystroke.
            page.keyboard.press("Escape")
            page.keyboard.press(f"Control+{number}")
        except Exception:
            return ""
        try:
            page.wait_for_timeout(1200)
        except Exception:
            return ""
        if len(payloads) > before:
            log.info("reached Assignments with Control+%d", number)
            return f"Control+{number}"
    return ""


def describe_rail(page) -> list[str]:
    """What the app bar actually offers, for when none of it matched.

    "Nothing named Assignments" is a useless thing to be told when the app
    may be calling it Classwork, or may not have finished booting. This says
    what was there instead, which is the difference between a guess and a
    one-line config change.

    Navigation diagnostics, not scraping: accessible names only, and never on
    the success path.
    """
    script = """() => {
      const names = new Set();
      const selector = '[role="treeitem"], [role="tab"], [role="menuitem"], '
                     + '[role="button"], [aria-label], [title]';
      for (const el of document.querySelectorAll(selector)) {
        const name = (el.getAttribute('aria-label') || el.getAttribute('title')
                      || el.textContent || '').trim();
        if (name && name.length < 40) names.add(name);
      }
      return [...names].slice(0, 60);
    }"""
    found: list[str] = []
    for frame in _frames(page) or [page]:
        try:
            found.extend(frame.evaluate(script) or [])
        except Exception:
            continue
    return sorted(set(found))


def click_assignments(page, teams=None) -> str:
    """Click whatever takes us to Assignments. Returns what worked, or "".

    Three places to look, in the order that costs least: the rail itself, the
    rail's overflow menu, and then inside the frames - Teams runs the edu app
    in an iframe, and once you are in it the tabs live there rather than on
    the page around it.
    """
    pattern = label_pattern(teams)

    how = _click_labelled(page, pattern)
    if how:
        return how

    if open_app_overflow(page):
        try:
            page.wait_for_timeout(1000)
        except Exception:
            pass
        how = _click_labelled(page, pattern)
        if how:
            return f"{how} in the More menu"

    # frames[0] is the page itself, which has just been tried.
    for frame in _frames(page)[1:]:
        how = _click_labelled(frame, pattern)
        if how:
            return f"{how} inside a frame"
    return ""


def save_failure_screenshot(page, dump_dir) -> str:
    """Photograph the page when no assignment data arrived."""
    try:
        dump_dir = Path(dump_dir)
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = dump_dir / f"no-assignments-{stamp}.png"
        page.screenshot(path=str(target), full_page=False)
        log.warning("saved a screenshot of what the browser saw: %s", target)
        return str(target)
    except Exception as exc:
        log.debug("could not screenshot: %s", exc)
        return ""


def reach_assignments(page, payloads: list, teams, notes=None) -> bool:
    """Make sure we are actually on the Assignments view.

    Teams is a single-page app and the "#/edu/assignments" part of the URL is
    a fragment, not a path. If the app boots at its own default route and
    ignores the deep link - which it often does after an auth round trip -
    you end up on chat. The scraper then captures whatever incidental data
    that page fetched, which is the worst possible outcome: some prep, no
    error, and a count you have to already know is wrong.

    So if no assignment data arrived, re-apply the route from inside the app,
    and failing that reload with the fragment present. Returns whether any
    assignment data was seen in the end.
    """
    if payloads:
        _route(notes, "opened straight onto the assignment data")
        return True

    # Clicking first, because the address bar does not follow the in-app view:
    # whatever URL we asked for, the app decides where it opens, and the only
    # thing that reliably changes that is the same click a person would make.
    how = click_assignments(page, teams)
    if how:
        page.wait_for_timeout(1500)
        settle(page, payloads, teams, notes=notes)
        if payloads:
            _route(notes, f"clicked through to it ({how})")
            return True

    # Nothing to click does not mean nowhere to go: the rail may be collapsed
    # to icons, off-screen, or still booting, and the shortcut still works.
    key = keyboard_to_assignments(page, payloads, teams)
    if key:
        settle(page, payloads, teams, notes=notes)
        if payloads:
            _route(notes, f"keyboard shortcut ({key})")
            return True

    fragment = ""
    if "#" in teams.assignments_url:
        fragment = "#" + teams.assignments_url.split("#", 1)[1]
    if fragment:
        try:
            page.evaluate(
                "hash => { if (location.hash !== hash) location.hash = hash; }",
                fragment)
            page.wait_for_timeout(1500)
            settle(page, payloads, teams, notes=notes)
        except Exception as exc:
            log.debug("in-app routing failed: %s", exc)
        if payloads:
            _route(notes, "re-applied the URL fragment from inside the app")
            return True

    try:
        page.goto(teams.assignments_url, wait_until="domcontentloaded",
                  timeout=teams.nav_timeout_ms)
        settle(page, payloads, teams, notes=notes)
        if payloads:
            _route(notes, "reloaded at the fragment")
        else:
            how = click_assignments(page, teams)
            if how:
                page.wait_for_timeout(1500)
                settle(page, payloads, teams, notes=notes)
                if payloads:
                    _route(notes, f"reloaded, then clicked ({how})")
    except Exception as exc:
        log.debug("reload failed: %s", exc)

    if not payloads:
        log.warning(
            "no assignment data after clicking, the rail shortcuts, routing "
            "and reloading. The app may be showing a different view; URL is "
            "%s (which Teams does not update for the Assignments view, so it "
            "tells you little).",
            page.url)
        # What WAS there. Only on the failure path, and only accessible names.
        labels = describe_rail(page)
        if labels:
            log.warning("the app bar offered: %s", ", ".join(labels[:25]))
            if notes is not None:
                notes["rail"] = labels
        _route(notes, "never reached it")
    return bool(payloads)


def _route(notes, how: str) -> None:
    if notes is not None:
        notes["route"] = how


# Scroll every region that can scroll, in whichever frame it is in, and say
# whether anything actually moved. Three reasons it is done in the page rather
# than with the mouse:
#
#   - the list is in an iframe, and a wheel event goes to whatever is under
#     the pointer, which at rest is the rail rather than the list;
#   - "did it move?" is the only honest way to know the list has been seen to
#     the end, and a wheel event cannot tell you;
#   - Teams virtualises the list, so what has never been scrolled into view
#     has never been fetched, and a capture that stops early looks exactly
#     like a capture of a short list.
SCROLL_JS = """() => {
  let moved = false;
  const push = (el, amount) => {
    if (!el) return;
    const before = el.scrollTop;
    el.scrollTop = before + amount;
    if (el.scrollTop !== before) moved = true;
  };
  for (const el of document.querySelectorAll('*')) {
    if (el.scrollHeight - el.clientHeight < 120) continue;
    const overflow = getComputedStyle(el).overflowY;
    if (overflow !== 'auto' && overflow !== 'scroll') continue;
    push(el, Math.max(el.clientHeight * 0.9, 400));
  }
  push(document.scrollingElement, window.innerHeight || 800);
  return moved;
}"""


@dataclass
class Settled:
    """What one settle() learned. Returned so a test can report it.

    at_bottom is a three-way answer on purpose: True the list would not
    scroll any further, False it was still moving when we stopped, None the
    page could not be asked. Collapsing None into either of the others is how
    a short capture gets reported as a complete one.
    """
    waited_ms: int
    payloads: int
    scrolls: int
    at_bottom: bool | None
    still_arriving: bool      # gave up with data still coming in

    def describe(self) -> str:
        parts = [f"{self.payloads} payload(s) in {self.waited_ms / 1000:.0f}s",
                 f"{self.scrolls} scroll(s)"]
        if self.at_bottom is True:
            parts.append("list fully scrolled")
        elif self.at_bottom is False:
            parts.append("MORE LIST BELOW")
        if self.still_arriving:
            parts.append("STILL ARRIVING")
        return ", ".join(parts)


def scroll_for_more(page) -> bool | None:
    """Pull the rest of a virtualised list into view.

    Returns True if something scrolled, False if nothing could (the list is
    at its end), and None when this page cannot be asked - a bare wheel event
    was sent instead and there is no way to tell what it did. The three are
    kept apart because "nothing moved" is what lets the capture stop early,
    and guessing it would truncate the capture.
    """
    frames = _frames(page)
    if frames:
        moved = False
        asked = False
        for frame in frames:
            try:
                result = frame.evaluate(SCROLL_JS)
            except Exception:
                continue           # a frame can be detached mid-scroll
            asked = True
            moved = moved or bool(result)
        if asked:
            return moved

    # No frames to ask, so nudge and admit we cannot tell. Each step is its
    # own attempt: a page with no mouse must still get the keypress.
    try:
        page.mouse.move(640, 800)     # over the list, not the rail
    except Exception:
        pass
    try:
        page.mouse.wheel(0, 1600)
    except Exception:
        pass
    try:
        page.keyboard.press("End")
    except Exception:
        pass
    return None


def settle(page, payloads: list, teams, on_tick=None, notes=None) -> Settled:
    """Wait until the assignment data stops arriving, not for a fixed time.

    Teams loads assignments progressively: a first page, then more as the list
    is scrolled or as views finish initialising. A fixed wait is a lottery -
    too short and you capture some of your prep and never know, which is worse
    than capturing none, because nothing looks wrong.

    So: wait at least settle_ms, then keep going until no new assignment
    payload has arrived for quiet_ms, scrolling to make a virtualised list
    fetch the rest, and give up at max_settle_ms however busy it is.
    """
    step_ms = 500
    waited = 0
    last_count = len(payloads)
    quiet_for = 0
    scrolls = 0
    confirmed = False
    scroll_answer: bool | None = None   # what the last scroll actually reported
    # Only ever set by a page that reported movement. A page that cannot be
    # asked leaves it False, so an unknown answer never extends the scrolling
    # past scroll_passes - it would never stop.
    still_moving = False

    while waited < teams.max_settle_ms:
        page.wait_for_timeout(step_ms)
        waited += step_ms

        if len(payloads) != last_count:
            last_count = len(payloads)
            quiet_for = 0
            confirmed = False      # more arrived, so it was not done after all
        else:
            quiet_for += step_ms

        if on_tick:
            on_tick(waited, last_count, quiet_for)

        # Nudge the list along. A virtualised list only fetches what it is
        # about to show, so without this the capture sees one screenful.
        #
        # scroll_passes is a floor rather than a ceiling: while the page is
        # still moving under us there is more list to see, and stopping at a
        # fixed number of passes is how a long list gets half captured. The
        # clock still bounds it, because max_settle_ms bounds everything.
        if (scrolls < teams.scroll_passes or still_moving) and waited % 1500 == 0:
            moved = scroll_for_more(page)
            scrolls += 1
            scroll_answer = moved
            still_moving = moved is True
            if moved is not False:
                quiet_for = 0      # a scroll may have started a new fetch

        if waited >= teams.settle_ms and quiet_for >= teams.quiet_ms:
            # One last nudge before believing it. Teams can go quiet for
            # several seconds in the middle of loading, and stopping there
            # means a capture that is silently short - which is the worst
            # failure this has, because nothing about it looks wrong.
            if not confirmed:
                confirmed = True
                quiet_for = 0
                final = scroll_for_more(page)
                if final is not None:
                    scroll_answer = final
                if final is True:
                    still_moving = True
                continue
            log.info("settled after %.1fs with %d payload(s), %d scroll(s)%s",
                     waited / 1000, last_count, scrolls,
                     "" if still_moving else " (reached the end of the list)")
            return _settled(waited, last_count, scrolls, scroll_answer,
                            still_arriving=False, notes=notes)

    log.warning("still receiving data after %.0fs; capturing what arrived "
                "(%d payloads). Raise max_settle_ms if the count looks short.",
                teams.max_settle_ms / 1000, last_count)
    return _settled(waited, last_count, scrolls, scroll_answer,
                    still_arriving=True, notes=notes)


def _settled(waited, payloads, scrolls, scroll_answer, still_arriving,
             notes) -> Settled:
    """Build the record, and copy it into the caller's notes if it wants one.

    at_bottom inverts the last scroll's answer: it moved, so there is more
    below; it did not, so there is not; it could not say, so neither can we.
    """
    at_bottom = None if scroll_answer is None else not scroll_answer
    result = Settled(waited_ms=waited, payloads=payloads, scrolls=scrolls,
                     at_bottom=at_bottom, still_arriving=still_arriving)
    if notes is not None:
        notes["settled"] = result
    return result


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _first(node: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = node.get(key)
        if value not in (None, ""):
            return value
    return None


def _archived(node: dict[str, Any]) -> bool | None:
    """Whether the tenant explicitly marks this class as finished.

    None rather than False when nothing is said, so "not stated" is never
    mistaken for "confirmed active".
    """
    for key in ARCHIVE_KEYS:
        value = node.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
    return None


def _creator(node: dict[str, Any]) -> tuple[str, str]:
    """Pull a teacher identity out of createdBy / lastModifiedBy.

    The shape is usually {"user": {"id": ..., "displayName": ...}}, but some
    responses nest it differently or send a bare string, so handle all three.
    displayName is frequently null even when the id is present - the id alone
    is enough, because it is stable and you name it once.
    """
    for key in CREATOR_KEYS:
        value = node.get(key)
        if not value:
            continue
        if isinstance(value, str):
            if GUID_RE.fullmatch(value):
                return value, ""
            return "", value.strip()
        if isinstance(value, dict):
            user = value.get("user") if isinstance(value.get("user"), dict) else value
            identifier = user.get("id") or user.get("userId") or user.get("objectId")
            name = (user.get("displayName") or user.get("name")
                    or user.get("emailAddress") or "")
            if identifier or name:
                return (str(identifier) if identifier else "",
                        str(name).strip() if name else "")
    return "", ""


def _synthetic_id(title: str, class_ref: str) -> str:
    """Fallback identity. Deliberately excludes the due date, which can move."""
    digest = hashlib.sha1(f"{title}|{class_ref}".encode()).hexdigest()[:16]
    return f"syn-{digest}"


def parse(payloads: list[dict[str, Any]]) -> list[RawAssignment]:
    found: dict[str, RawAssignment] = {}

    for payload in payloads:
        url_class = GUID_RE.search(payload.get("url", "") or "")
        for node in _walk(payload.get("body")):
            due = _first(node, DUE_KEYS)
            title = _first(node, TITLE_KEYS)
            if not isinstance(due, str) or not isinstance(title, str):
                continue

            # Keep these apart. Many tenants send only the GUID, and letting it
            # stand in for a class name means every keyword match scores against
            # a GUID and silently resolves nothing.
            name = _first(node, CLASS_NAME_KEYS)
            class_ref = str(name) if name else ""
            identifier = _first(node, CLASS_ID_KEYS)
            class_id = str(identifier) if identifier else (
                url_class.group(0) if url_class else "")

            candidate = _first(node, ID_KEYS)
            synthetic = not (isinstance(candidate, str) and len(candidate) >= GUID_HINT)
            assignment_id = (
                _synthetic_id(title, class_ref or class_id) if synthetic
                else str(candidate)
            )

            creator_id, creator_name = _creator(node)
            archived = _archived(node)
            assigned = _first(node, ASSIGNED_KEYS)
            found[assignment_id] = RawAssignment(
                assignment_id=assignment_id,
                title=title.strip(),
                class_ref=class_ref,
                class_id=class_id,
                creator_id=creator_id,
                creator_name=creator_name,
                archived=archived,
                assigned_at=assigned if isinstance(assigned, str) else None,
                due_at=due,
                id_is_synthetic=synthetic,
                raw=node,
            )

    return sorted(found.values(), key=lambda a: (a.due_at or "", a.title))


def turned_in(item: RawAssignment) -> bool:
    """Whether Teams says you have already turned this in.

    isCompleted is the student's own flag on /me/work and is the one to
    trust. Without it, a submission that is submitted or returned counts.
    """
    node = item.raw or {}
    flag = node.get("isCompleted")
    if isinstance(flag, bool):
        return flag
    for submission in node.get("submissions") or []:
        if isinstance(submission, dict) and str(
                submission.get("status", "")).lower() in ("submitted", "returned"):
            return True
    return False


_WORK_BOUND = re.compile(r"duedatetime\s+(ge|le)\s+(\S+?)(?:[)&\s]|$)")
_SKIPTOKEN = re.compile(r"[&?]\$skiptoken=([^&]*)")
_ORDER = re.compile(r"\$orderby=duedatetime\s+(asc|desc)")


def _bound(text: str) -> datetime | None:
    try:
        when = fromiso(text.upper())
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def complete_lists(payloads: list[dict[str, Any]]) -> list[tuple[datetime | None,
                                                                 datetime | None]]:
    """The due-date ranges Teams sent in full, as (from, to); None is open.

    Teams lists only work you have not turned in, so handing something in
    shows up as it vanishing. That can only be read off a list that arrived
    whole: the upcoming list ("dueDateTime ge <today>") normally comes in one
    page, while the past-due list ("dueDateTime le <now>") is paged and
    usually is not.

    A paged list still covers part of its range. It is sorted by due date
    away from its bound (newest first for past-due), so the pages fetched in
    order from the first one hold everything from the bound back to the
    oldest prep on them. Prep due yesterday missing from those pages was
    handed in. Past the last page fetched, missing prep proves nothing.
    """
    lists: dict[str, dict[str | None, dict]] = {}
    for payload in payloads:
        url = unquote(str(payload.get("url") or "")).lower()
        body = payload.get("body")
        if "/me/work" not in url or not isinstance(body, dict):
            continue
        if not isinstance(body.get("value"), list):
            continue
        token = _SKIPTOKEN.search(url)
        query = _SKIPTOKEN.sub("", url)
        lists.setdefault(query, {})[token.group(1) if token else None] = body

    ranges = []
    for query, pages in lists.items():
        match = _WORK_BOUND.search(query)
        if not match:
            continue
        when = _bound(match.group(2))
        if when is None:
            continue
        ge = match.group(1) == "ge"
        # Follow the pages from the first, as Teams handed them out. A gap
        # means the pages after it say nothing about what lies between.
        token: str | None = None
        seen: set[str | None] = set()
        furthest: datetime | None = None
        whole = False
        while token in pages and token not in seen:
            seen.add(token)
            body = pages[token]
            for item in body["value"]:
                due = _bound(str(item.get("dueDateTime") or "")) if isinstance(
                    item, dict) else None
                if due is not None and (furthest is None
                                        or (due > furthest if ge else due < furthest)):
                    furthest = due
            following = body.get("@odata.nextLink")
            if not following:
                whole = True
                break
            nxt = _SKIPTOKEN.search(unquote(str(following)).lower())
            if not nxt:
                break
            token = nxt.group(1)
        if whole:
            ranges.append((when, None) if ge else (None, when))
            continue
        order = _ORDER.search(query)
        sorted_away = order and order.group(1) == ("asc" if ge else "desc")
        if furthest is None or not sorted_away:
            continue
        # Prep due at exactly the furthest point may continue on the next page.
        step = timedelta(seconds=1)
        ranges.append((when, furthest - step) if ge else (furthest + step, when))
    return ranges


def load_payload_file(path: str | Path) -> list[dict[str, Any]]:
    """Read a --dump file back in, so the pipeline can run without a browser."""
    return json.loads(Path(path).read_text())
