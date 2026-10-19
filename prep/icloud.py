"""Why iCloud will not sign in.

    python3 -m prep.icloud --config config.yaml

"Calendar unreachable" and "password rejected" are completely different
problems with completely different answers, and for a long time they shared
one message: *you need an app-specific password*. That advice is right for
one of them. Told it when the real fault was the network, you generate
another password, it fails identically, and you conclude the whole approach
is broken.

So this separates them, and checks the things that make a *correct* password
fail before it blames the password:

    the shape of what was actually read from the environment - whitespace and
    quotes survive an export and are invisible afterwards

    whether iCloud answers at all, over plain HTTPS, before any library is
    involved

    whether the authenticated request gets through, which is the one that
    breaks on a small-MTU network path (see scheduler._no_quic)

No password is ever printed, and nothing is written to any calendar.
"""
from __future__ import annotations

import argparse
import sys

from . import config as config_module
from .scheduler import CalendarAuthFailed, CalendarUnreachable, connect

OK, WARN, FAIL = "[ ok ]", "[warn]", "[FAIL]"


def shape(secret: str) -> str:
    """Describe a secret without revealing it.

    Length and the position of the dashes are enough to recognise an Apple
    app-specific password, and not enough to be worth hiding.
    """
    if not secret:
        return "empty"
    pattern = "".join("-" if ch == "-" else ("#" if ch.isalnum() else "?")
                      for ch in secret)
    return f"{len(secret)} chars, shaped {pattern}"


def check_secret_shape(name: str, value: str, raw: str) -> list[tuple[str, str]]:
    """Problems with the value itself, before anything is sent anywhere."""
    found: list[tuple[str, str]] = []
    if not value:
        found.append((FAIL, f"{name} is empty. {raw} resolved to nothing - "
                            f"is it exported in THIS shell?"))
        return found

    if raw.startswith("env:"):
        import os
        actual = os.environ.get(raw[4:], "")
        if actual != actual.strip():
            found.append((WARN, f"{name} had whitespace around it in the "
                                f"environment. It is stripped before use now, "
                                f"so this is no longer fatal - but the export "
                                f"is worth tidying."))
    if value != value.strip():
        found.append((FAIL, f"{name} still has whitespace around it."))
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        found.append((FAIL, f"{name} is wrapped in {value[0]} quotes. The "
                            f"shell strips those; something else did not, so "
                            f"the quotes are part of the password being sent."))
    return found


def check_app_password(value: str) -> list[tuple[str, str]]:
    """Does it look like the thing appleid.apple.com hands out?

    Apple's app-specific passwords are sixteen lower-case letters in four
    dash-separated groups. A value that is not that shape is usually the
    ordinary Apple password, which cannot work over CalDAV however correct it
    is - and that is worth saying before a single request is sent.
    """
    stripped = value.replace("-", "")
    groups = value.split("-")
    looks_right = (len(groups) == 4 and all(len(g) == 4 for g in groups)
                   and stripped.isalpha() and stripped.islower())
    if looks_right:
        return [(OK, "Password has the shape of an app-specific password")]
    if len(stripped) == 16 and stripped.isalpha():
        return [(WARN, "Password is 16 letters but not in Apple's "
                       "xxxx-xxxx-xxxx-xxxx grouping. Paste it exactly as "
                       "appleid.apple.com shows it, dashes included.")]
    return [(WARN, "Password does not look like an app-specific password. "
                   "Your ordinary Apple password cannot work over CalDAV, "
                   "however correct it is: generate one at appleid.apple.com "
                   "under Sign-In and Security -> App-Specific Passwords.")]


def check_reachable(url: str) -> list[tuple[str, str]]:
    """Does iCloud answer at all, before any library is in the picture?

    Deliberately the standard library over HTTPS: if this works and the real
    client does not, the fault is in how the client talks, not in the network
    or the credentials.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="PROPFIND")
    try:
        urllib.request.urlopen(request, timeout=20)
        return [(WARN, f"{url} answered without asking for credentials, "
                       f"which is not what iCloud normally does")]
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            realm = exc.headers.get("WWW-Authenticate", "")
            return [(OK, f"iCloud answered and asked for a password "
                         f"({realm[:40] or 'no realm given'})")]
        return [(WARN, f"iCloud answered with HTTP {exc.code}")]
    except Exception as exc:
        return [(FAIL, f"could not reach {url}: {exc}")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Work out why iCloud will not sign in")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    import yaml
    from pathlib import Path
    raw = yaml.safe_load(Path(args.config).read_text()).get("calendar", {}) or {}

    cfg = config_module.load(args.config)
    calendar = cfg.calendar

    lines: list[tuple[str, str]] = []
    print(f"url:      {calendar.caldav_url}")
    print(f"username: {calendar.username}")
    print(f"password: {shape(calendar.password)}   (never printed)")
    print(f"calendar: {calendar.prep_calendar}\n")

    lines += check_secret_shape("username", calendar.username,
                                str(raw.get("username", "")))
    lines += check_secret_shape("password", calendar.password,
                                str(raw.get("password", "")))
    if "@" not in calendar.username:
        lines.append((FAIL, "username is not an email address. iCloud wants "
                            "the whole Apple ID, not a short name."))
    if calendar.password:
        lines += check_app_password(calendar.password)
    lines += check_reachable(calendar.caldav_url)

    for status, text in lines:
        print(f"{status} {text}")

    # ------------------------------------------------- the real thing
    print("\n--- signing in for real ---")
    try:
        _client, principal = connect(cfg)
    except CalendarAuthFailed as exc:
        print(f"{FAIL} {exc}")
        print("\niCloud answered, and refused the credentials. That is a "
              "genuine\nsign-in failure rather than a network one - work "
              "through the\npassword points above.")
        return 1
    except CalendarUnreachable as exc:
        print(f"{FAIL} {exc}")
        print("\niCloud never answered. This says NOTHING about your "
              "password: do not\ngo and generate another one. If the "
              "reachability check above passed\nwhile this did not, the fault "
              "is in the connection the library makes,\nnot in your "
              "credentials or your network.")
        return 1
    except Exception as exc:
        print(f"{FAIL} {type(exc).__name__}: {exc}")
        return 1

    names = sorted(c.name for c in principal.calendars())
    print(f"{OK} signed in, {len(names)} calendar(s) visible")
    for name in names:
        mark = " <- prep goes here" if name == calendar.prep_calendar else ""
        print(f"       {name}{mark}")

    if calendar.prep_calendar not in names:
        print(f"\n{FAIL} '{calendar.prep_calendar}' is not among them.")
        print("       Create it in the Calendar app with exactly that name, "
              "let it\n       sync, and run this again. The sign-in itself is "
              "working.")
        return 1

    print("\nNothing wrong here: the sign-in works and the prep calendar "
          "exists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
