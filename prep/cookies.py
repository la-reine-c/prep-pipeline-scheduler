"""Third-party cookies in the browser profile, which decide whether Teams
can renew its own access without asking you.

This exists because of a failure that looks nothing like its cause. With
third-party cookies blocked, signing in works, the page renders, and prep
appears - and then, days later, every capture starts failing with what looks
like a dead session. Signing in again fixes it for a few days, and the cycle
repeats.

The reason is that Microsoft renews access in a hidden iframe pointed at
login.microsoftonline.com. In that iframe login.microsoftonline.com is a
*third party*, because the page around it is teams.cloud.microsoft. Block
third-party cookies and the renewal silently fails: the sign-in cookie is
still sitting in the profile, still unexpired, and unusable.

Nothing here spoofs anything or weakens a protection Teams relies on. It sets
the same preference a person sets in Settings -> Privacy when they choose to
allow third-party cookies, in one profile that exists only to read your own
assignments.

Three places can block them, and they do not override each other in the order
you would guess:

    managed policy   /etc/chromium/policies/managed/*.json, and the macOS
                     equivalent. WINS over everything, including the setting
                     in the browser. A profile preference cannot fix this.
    profile          the Preferences file inside the profile directory. This
                     is what the Settings page writes, and what we write.
    feature flags    newer Chromium builds phase third-party cookies out by
                     default; --disable-features=TrackingProtection3pcd opts
                     this one profile back out of that.

Written before Chromium is launched, never while it is running: Chromium
rewrites Preferences from memory on exit and would undo it.
"""
from __future__ import annotations

import json
import logging
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 0 = allow third-party cookies, 1 = block them in incognito only,
# 2 = block them everywhere. 2 is the one that breaks silent renewal.
COOKIE_CONTROLS_OFF = 0
ALLOW_COOKIES = 1          # profile.default_content_setting_values.cookies

# Chromium only honours the LAST --disable-features it is passed, so this is
# merged with the rendering flags rather than appended. See scraper.browser_args.
#
# TrackingProtection3pcd is the third-party cookie deprecation rollout: when
# it is on, the profile preference below is ignored and third-party cookies
# are blocked regardless. An unknown feature name here is ignored by older
# builds, which is why naming it is safe across versions.
LAUNCH_FEATURES = ("TrackingProtection3pcd",)

POLICY_DIRS = (
    "/etc/chromium/policies/managed",
    "/etc/chromium-browser/policies/managed",
    "/etc/opt/chrome/policies/managed",
    "/etc/chrome/policies/managed",
)

MACOS_POLICY_PLISTS = (
    "/Library/Managed Preferences/org.chromium.Chromium.plist",
    "/Library/Managed Preferences/com.google.Chrome.plist",
)


@dataclass
class CookieState:
    """What the profile will actually do about third-party cookies.

    `allowed` is None when there is nothing to read yet - a profile that has
    never been launched. That is deliberately not False: "not yet known" and
    "blocked" call for completely different advice.
    """
    allowed: bool | None
    source: str = ""
    detail: str = ""

    @property
    def enforced(self) -> bool:
        """Set by a policy we cannot override from here."""
        return self.source == "managed policy"

    def describe(self) -> str:
        if self.allowed is None:
            return self.detail or "no browser profile yet"
        if self.allowed:
            return f"allowed ({self.source})" if self.source else "allowed"
        return f"BLOCKED by {self.source}: {self.detail}" if self.detail else \
               f"BLOCKED by {self.source}"


def preferences_path(profile_dir: Path | str) -> Path:
    """Where Chromium keeps this profile's settings.

    A persistent context uses Default/ inside the user data directory. An
    older profile may have Preferences at the top level, so an existing one
    is preferred over the conventional location.
    """
    profile = Path(profile_dir)
    top = profile / "Preferences"
    if top.exists():
        return top
    return profile / "Default" / "Preferences"


def _read_prefs(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def policy_block() -> CookieState | None:
    """A machine-wide policy blocking third-party cookies, if there is one.

    Returns None when no policy has an opinion. This is checked first because
    a policy beats the profile preference, and quietly writing a preference
    that will be ignored is worse than saying so.
    """
    for directory in POLICY_DIRS:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            data = _read_prefs(path)
            if not isinstance(data, dict):
                continue
            if data.get("BlockThirdPartyCookies") is True:
                return CookieState(False, "managed policy",
                                   f"BlockThirdPartyCookies in {path}")
            if data.get("DefaultCookiesSetting") == 2:
                return CookieState(False, "managed policy",
                                   f"DefaultCookiesSetting: 2 in {path}")
            if data.get("BlockThirdPartyCookies") is False:
                return CookieState(True, "managed policy", str(path))

    if sys.platform == "darwin":
        import plistlib
        for candidate in MACOS_POLICY_PLISTS:
            path = Path(candidate)
            if not path.exists():
                continue
            try:
                with path.open("rb") as handle:
                    data = plistlib.load(handle)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("BlockThirdPartyCookies") is True:
                return CookieState(False, "managed policy",
                                   f"BlockThirdPartyCookies in {path}")
            if data.get("BlockThirdPartyCookies") is False:
                return CookieState(True, "managed policy", str(path))
    return None


def read_state(profile_dir: Path | str) -> CookieState:
    """What this profile does about third-party cookies right now."""
    enforced = policy_block()
    if enforced is not None:
        return enforced

    path = preferences_path(profile_dir)
    if not path.exists():
        return CookieState(None, detail=f"no profile yet at {path.parent}")

    prefs = _read_prefs(path)
    if not prefs:
        return CookieState(None, detail=f"could not read {path}")

    profile = prefs.get("profile") if isinstance(prefs.get("profile"), dict) else {}
    mode = profile.get("cookie_controls_mode")
    if mode == 2:
        return CookieState(False, "the browser profile",
                           "cookie_controls_mode: 2 (block third-party cookies)")
    if profile.get("block_third_party_cookies") is True:
        return CookieState(False, "the browser profile",
                           "block_third_party_cookies: true")

    defaults = profile.get("default_content_setting_values")
    if isinstance(defaults, dict) and defaults.get("cookies") == 2:
        return CookieState(False, "the browser profile",
                           "cookies are blocked outright, not just third-party ones")

    tracking = prefs.get("tracking_protection")
    if isinstance(tracking, dict) and tracking.get("block_all_3pc_toggle_enabled") is True:
        return CookieState(False, "the browser profile",
                           "tracking protection is blocking all third-party cookies")

    if mode == COOKIE_CONTROLS_OFF:
        return CookieState(True, "the browser profile", "cookie_controls_mode: 0")
    # Chromium's own default (1) blocks them in incognito only, which this
    # profile never is.
    return CookieState(True, "Chromium's default", f"cookie_controls_mode: {mode}")


def _put(node: dict, key: str, value) -> bool:
    """Set one preference. Returns whether it was not already that."""
    if node.get(key) == value:
        return False
    node[key] = value
    return True


def allow(profile_dir: Path | str) -> str:
    """Make this profile accept third-party cookies. Returns what changed.

    Safe to call before every launch: it rewrites nothing when the profile is
    already set this way, and an empty return means there was nothing to do.

    Must not be called while Chromium is running on this profile - it keeps
    preferences in memory and writes them out on exit, which would silently
    undo this. Callers check the profile lock first.
    """
    path = preferences_path(profile_dir)
    prefs = _read_prefs(path) if path.exists() else {}
    if not isinstance(prefs, dict):
        prefs = {}

    changed: list[str] = []

    profile = prefs.setdefault("profile", {})
    if not isinstance(profile, dict):
        profile = {}
        prefs["profile"] = profile
    if _put(profile, "cookie_controls_mode", COOKIE_CONTROLS_OFF):
        changed.append("cookie_controls_mode -> 0")
    if _put(profile, "block_third_party_cookies", False):
        changed.append("block_third_party_cookies -> false")

    defaults = profile.setdefault("default_content_setting_values", {})
    if isinstance(defaults, dict):
        if _put(defaults, "cookies", ALLOW_COOKIES):
            changed.append("default cookie setting -> allow")

    # Newer builds keep the third-party decision here as well, and the two
    # disagreeing is how you end up with a Settings page that says one thing
    # and a browser that does another.
    tracking = prefs.setdefault("tracking_protection", {})
    if isinstance(tracking, dict):
        if _put(tracking, "block_all_3pc_toggle_enabled", False):
            changed.append("tracking protection 3pc toggle -> off")

    if not changed:
        return ""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written through a temporary file in the same directory: a half
        # written Preferences file is not a broken setting, it is a profile
        # Chromium resets - which costs the sign-in this is protecting.
        temporary = path.with_name(path.name + ".prep-tmp")
        temporary.write_text(json.dumps(prefs))
        temporary.replace(path)
    except OSError as exc:
        log.warning("could not set third-party cookies in %s: %s", path, exc)
        return ""

    log.info("third-party cookies allowed in %s (%s)", path, ", ".join(changed))
    return ", ".join(changed)


def prepare(profile_dir: Path | str, enabled: bool = True) -> CookieState:
    """Allow third-party cookies if asked to, and report where things stand.

    The single call every launcher makes. Nothing is written when a managed
    policy is in charge, because the write would do nothing and the message
    would be wrong.
    """
    if not enabled:
        return read_state(profile_dir)

    enforced = policy_block()
    if enforced is not None and not enforced.allowed:
        return enforced

    allow(profile_dir)
    return read_state(profile_dir)


ADVICE = (
    "Teams renews its own access in a hidden iframe on "
    "login.microsoftonline.com, which is a third party to the Teams page "
    "around it. With third-party cookies blocked that renewal fails "
    "silently, and a session that is still perfectly valid starts reading "
    "as dead a few days after every sign-in.",
)

# Every caller of advice() prints it into a terminal, most of them behind an
# indent, so it is wrapped here rather than in four slightly different ways.
WRAP_AT = 68


def advice(state: CookieState) -> list[str]:
    """What to do about it, wrapped for a terminal. Empty when all is well."""
    if state.allowed is not False:
        return []

    paragraphs = list(ADVICE)
    if state.enforced:
        paragraphs.append(
            "This one is a machine-wide policy, so the browser's own setting "
            "cannot override it: " + state.detail)
        paragraphs.append(
            "Edit that policy file (BlockThirdPartyCookies: false), or point "
            "profile_dir at a machine without it.")
    else:
        paragraphs.append(
            "Set third_party_cookies: true under teams: in your config and "
            "run prep.login again - it is written into the profile before "
            "Chromium starts.")

    lines: list[str] = []
    for paragraph in paragraphs:
        if lines:
            lines.append("")          # callers strip these if they cannot use them
        lines.extend(textwrap.wrap(paragraph, WRAP_AT))
    return lines
