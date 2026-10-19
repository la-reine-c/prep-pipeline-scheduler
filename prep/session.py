"""How long the saved Teams session will last, read from the browser profile.

    python3 -m prep.session --config config.yaml

Ticking "Stay signed in" is what makes unattended running possible: Microsoft
then issues a *persistent* cookie rather than one that dies when the browser
closes. The trouble is that you cannot tell by looking whether it took - a
window that showed your assignments looks identical either way, and you find
out a week later when nothing gets scheduled.

So this reads the profile's cookie store directly and says which kind you
have and when it runs out. It never reads a cookie *value*: only the name,
the host, and the expiry. Values are the credential, and nothing here needs
them.

No browser is launched. The store is Chromium's own SQLite database, copied
first so a running browser cannot lock it.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cookies

# Chromium stores expiry as microseconds since 1601-01-01, the Windows epoch.
WINDOWS_EPOCH_OFFSET = 11_644_473_600

# Microsoft's sign-in cookies. ESTSAUTHPERSISTENT is the one that appears only
# when "Stay signed in" was ticked - it is the whole question this asks.
PERSISTENT_MARKERS = ("estsauthpersistent",)
AUTH_MARKERS = ("estsauth", "estsauthlight", "buid", "esctx", "sso")
# Note "cloud.microsoft" has no .com, so none of the older patterns match it.
# Teams web is moving there (teams.cloud.microsoft) with an automatic redirect
# completing 30 September 2026. Without this entry a perfectly good session
# would be reported as "never signed in" the day the redirect reaches you -
# a false alarm sending you to fix an auth problem that does not exist.
MICROSOFT_HOSTS = ("login.microsoftonline.com", "microsoft.com",
                   "microsoftonline.com", "teams.microsoft.com", "office.com",
                   "cloud.microsoft")

# Below this, say something while there is still time to act on it.
WARN_DAYS = 7


@dataclass
class Cookie:
    name: str
    host: str
    expires: datetime | None      # None = dies when the browser closes

    @property
    def persistent(self) -> bool:
        return self.expires is not None


@dataclass
class SessionHealth:
    found: bool
    total: int = 0
    microsoft: list[Cookie] = field(default_factory=list)
    problem: str = ""

    @property
    def stay_signed_in(self) -> bool:
        """Did 'Stay signed in' actually take?"""
        return any(marker in cookie.name.lower()
                   for cookie in self.microsoft
                   for marker in PERSISTENT_MARKERS)

    @property
    def auth_cookies(self) -> list[Cookie]:
        return [c for c in self.microsoft
                if any(m in c.name.lower() for m in AUTH_MARKERS)]

    @property
    def expiry(self) -> datetime | None:
        """When the longest-lived sign-in cookie runs out.

        The longest, not the shortest: the short-lived ones are refreshed
        while a persistent one is still valid, so the last one to expire is
        what actually bounds the session.
        """
        dated = [c.expires for c in self.auth_cookies if c.expires]
        return max(dated) if dated else None

    @property
    def days_left(self) -> float | None:
        if self.expiry is None:
            return None
        return (self.expiry - datetime.now(timezone.utc)).total_seconds() / 86400

    def describe(self) -> str:
        if not self.found:
            return self.problem or "no cookie store in the profile"
        if not self.auth_cookies:
            return ("no Microsoft sign-in cookies - the profile has never "
                    "signed in, or was cleared")
        if not self.stay_signed_in:
            return ("session-only sign-in: 'Stay signed in' was NOT ticked, "
                    "so this dies when the browser closes")
        days = self.days_left
        if days is None:
            return "signed in, but no expiry recorded"
        if days < 0:
            return f"EXPIRED {abs(days):.0f} days ago"
        return f"persistent, about {days:.0f} days left"


def _to_datetime(value: int) -> datetime | None:
    if not value:
        return None                     # 0 means a session cookie
    try:
        seconds = value / 1_000_000 - WINDOWS_EPOCH_OFFSET
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def read(profile_dir: Path | str) -> SessionHealth:
    """Read cookie names, hosts and expiries. Never values."""
    profile = Path(profile_dir)
    candidates = [profile / "Default" / "Cookies", profile / "Cookies",
                  profile / "Default" / "Network" / "Cookies",
                  profile / "Network" / "Cookies"]
    store = next((path for path in candidates if path.exists()), None)
    if store is None:
        return SessionHealth(found=False,
                             problem=f"no cookie store under {profile}")

    # Copy first: a running browser holds a lock, and we must never write to
    # the real profile.
    with tempfile.TemporaryDirectory() as workspace:
        copy = Path(workspace) / "Cookies"
        try:
            shutil.copy2(store, copy)
            for suffix in ("-wal", "-shm"):
                extra = store.with_name(store.name + suffix)
                if extra.exists():
                    shutil.copy2(extra, copy.with_name(copy.name + suffix))
        except Exception as exc:
            return SessionHealth(found=False, problem=f"cannot read: {exc}")

        try:
            conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT name, host_key, expires_utc FROM cookies").fetchall()
            conn.close()
        except sqlite3.DatabaseError as exc:
            # A Chromium that changed its schema is worth saying plainly
            # rather than guessing around.
            return SessionHealth(found=False,
                                 problem=f"unexpected cookie store: {exc}")

    # Not named `cookies`: that is the module imported above, and a local
    # shadowing it here would be a trap for whoever next adds a line to this
    # function.
    stored = [Cookie(name=name, host=host, expires=_to_datetime(expires))
              for name, host, expires in rows]
    microsoft = [c for c in stored
                 if any(host in c.host.lower() for host in MICROSOFT_HOSTS)]
    return SessionHealth(found=True, total=len(stored), microsoft=microsoft)


# ---------------------------------------------------------------- snapshots
#
# Most lost sessions are not lost to authentication. A bad shutdown, a full
# disk, a Chromium upgrade that rewrites the profile - any of those costs you
# a sign-in for a purely mechanical reason. A snapshot of a known-good profile
# turns that into a restore.
#
# A snapshot contains a live session cookie, so it is exactly as sensitive as
# the profile itself: same machine, same owner, 0700, never committed. It is
# not a new credential - it is a copy of one that is already there, and it
# expires on the same schedule.

SNAPSHOT_PREFIX = "profile-snapshot-"
KEEP_SNAPSHOTS = 3


def snapshot_dir(profile_dir: Path) -> Path:
    return Path(profile_dir).parent / "snapshots"


def snapshots(profile_dir: Path) -> list[Path]:
    root = snapshot_dir(profile_dir)
    if not root.exists():
        return []
    return sorted((p for p in root.iterdir()
                   if p.is_dir() and p.name.startswith(SNAPSHOT_PREFIX)),
                  key=lambda p: p.name)


def take_snapshot(profile_dir: Path, health: SessionHealth) -> Path:
    """Copy a profile whose session is known good.

    Refuses a dead or session-only profile on purpose: restoring one of those
    would give you a confident restore and then a failed run, which is worse
    than knowing you have nothing to restore.
    """
    if not health.found or not health.auth_cookies:
        raise RuntimeError("nothing to snapshot - this profile has no sign-in")
    if not health.stay_signed_in:
        raise RuntimeError(
            "refusing to snapshot a session-only sign-in: it would not "
            "survive the restore either. Sign in again and tick the box.")
    days = health.days_left
    if days is not None and days <= 0:
        raise RuntimeError("refusing to snapshot an expired session")

    profile = Path(profile_dir)
    root = snapshot_dir(profile)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / f"{SNAPSHOT_PREFIX}{stamp}"
    # Two snapshots in the same second must not collide - and must still sort
    # in the order they were taken, since that is how the newest is found.
    suffix = 1
    while target.exists():
        target = root / f"{SNAPSHOT_PREFIX}{stamp}-{suffix:02d}"
        suffix += 1
    shutil.copytree(profile, target,
                    ignore=shutil.ignore_patterns("Singleton*", "*.lock"))
    target.chmod(0o700)

    for old in snapshots(profile)[:-KEEP_SNAPSHOTS]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def restore_snapshot(profile_dir: Path, source: Path | None = None) -> Path:
    """Put the newest good profile back. The current one is kept, not deleted."""
    profile = Path(profile_dir)
    available = snapshots(profile)
    chosen = source or (available[-1] if available else None)
    if chosen is None:
        raise RuntimeError(f"no snapshots under {snapshot_dir(profile)}")

    if profile.exists():
        # Never destroy the thing being replaced: if the snapshot turns out
        # to be older than you thought, you still have what you had.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        displaced = profile.parent / f"profile-replaced-{stamp}"
        profile.rename(displaced)

    shutil.copytree(chosen, profile)
    profile.chmod(0o700)
    return chosen


def main(argv: list[str] | None = None) -> int:
    from . import config as config_module

    parser = argparse.ArgumentParser(
        description="How long the saved Teams session will last")
    parser.add_argument("--config", required=True)
    parser.add_argument("--verbose", action="store_true",
                        help="list the sign-in cookies (names only)")
    parser.add_argument("--snapshot", action="store_true",
                        help="copy a known-good profile so a corrupted one "
                             "costs a restore, not a sign-in")
    parser.add_argument("--restore", action="store_true",
                        help="put the newest snapshot back")
    parser.add_argument("--list-snapshots", action="store_true")
    args = parser.parse_args(argv)

    cfg = config_module.load(args.config)
    profile = cfg.teams.profile_dir
    health = read(profile)

    if args.list_snapshots:
        found = snapshots(profile)
        print(f"snapshots under {snapshot_dir(profile)}:")
        for path in found:
            kept = read(path)
            print(f"  {path.name}  {kept.describe()}")
        if not found:
            print("  (none - take one with --snapshot once you are signed in)")
        return 0

    if args.restore:
        try:
            used = restore_snapshot(profile)
        except RuntimeError as exc:
            print(f"Not restored: {exc}")
            return 1
        print(f"Restored {used.name} to {profile}")
        print(f"Check it with: python3 -m prep.session --config {args.config}")
        return 0

    print(f"profile: {profile}")
    print(f"session: {health.describe()}")

    # The other half of "will this last". An expiry date is only the outer
    # limit: Teams renews itself long before it, in a hidden iframe that
    # cannot work without third-party cookies. A profile can therefore have
    # two months left on paper and days left in practice.
    state = cookies.read_state(profile)
    print(f"cookies: third-party {state.describe()}")

    if args.snapshot:
        try:
            target = take_snapshot(profile, health)
        except RuntimeError as exc:
            print(f"\nNot snapshotted: {exc}")
            return 1
        print(f"\nSnapshotted to {target}")
        print("This holds a live session cookie. Same sensitivity as the "
              "profile:\nkeep it on this machine, and never commit it.")
        return 0

    if args.verbose and health.auth_cookies:
        print("\nsign-in cookies (names only - values are never read):")
        for cookie in sorted(health.auth_cookies, key=lambda c: c.name):
            when = (cookie.expires.strftime("%d %b %Y")
                    if cookie.expires else "session only")
            print(f"  {cookie.name[:34]:<34} {cookie.host[:28]:<28} {when}")

    if state.allowed is False:
        print()
        for line in cookies.advice(state):
            print(line)
        return 1

    if not health.found or not health.auth_cookies:
        print(f"\nSign in with: python3 -m prep.login --config {args.config}")
        return 1
    if not health.stay_signed_in:
        print("\nSign in again and TICK 'Stay signed in'. Without it every "
              "headless run starts logged out.")
        return 1

    days = health.days_left
    if days is not None and days < WARN_DAYS:
        print(f"\nRe-authenticate soon: python3 -m prep.login "
              f"--config {args.config}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
