"""The dashboard, running on invented data - click around without touching anything real.

    python3 -m prep.demo                  # http://127.0.0.1:8099/
    python3 -m prep.demo --port 8123 --keep ~/prep-demo

The same world Phase A builds: six made-up teachers sharing classes, eight
weeks of history with a freak evening in it, and a diary with lessons,
orchestra and a Saturday fixture already in it. Prep is scheduled around that
diary exactly as the real run would schedule it around yours.

Nothing leaves the machine. There is no config file to write, no Teams
session, no network and no iCloud: the calendar is an in-memory fake that
knows about the dummy lessons, so a task you add lands around them and a task
you remove disappears from it. Everything is thrown away when you stop it,
unless you pass --keep.

It is the real web app, not a copy of it, so what works here works on the
mini. The one difference is the calendar behind it.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_module
from . import dummy, pipeline
from .scheduler import FakeCalendar
from .scraper import parse
from .store import Store

log = logging.getLogger(__name__)

# Only ever on this machine. The demo holds nothing secret, but it is the
# development server, and binding it wider teaches the wrong habit.
HOST = "127.0.0.1"


def build(root: Path, now: datetime | None = None):
    """Lay out a populated site under `root`. Returns (config, calendar).

    The calendar is returned rather than rebuilt per request so the dashboard
    and the schedule it shows share one diary: what you add is really booked
    around the dummy lessons, and what you remove really goes.
    """
    now = now or datetime.now(timezone.utc)
    world = dummy.build(now)
    cfg = config_module.load(dummy.write_site(root, world))
    # No token: the demo only binds to loopback, where token_warning agrees
    # one is not needed, and a token in the URL is one more thing to type.
    cfg.web.token = ""

    calendar = FakeCalendar(world.busy)
    store = Store(cfg.db_path)
    try:
        changes = store.upsert(parse(world.payloads),
                               pipeline.teacher_matcher(cfg))
        # Timings logged against past prep, so the estimates, the calibration
        # chart and the confidence badges have something to show.
        for assignment_id, minutes in world.timings:
            store.record_timing(assignment_id, minutes, "dummy")
        store.add_blackout("19:00", "20:00", weekday=3, label="dummy blackout")
        store.log_run("dry_run", "demo data")
        pipeline.reschedule(cfg, store, calendar, changes)
    finally:
        store.close()
    return cfg, calendar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="The prep dashboard on invented data. Nothing real is touched.")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--keep", metavar="DIR",
                        help="build the demo here and leave it afterwards")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        from .web import create_app
    except ImportError as exc:
        print(f"The dashboard needs Flask ({exc}). pip install -r requirements.txt")
        return 1

    if args.keep:
        root = Path(args.keep).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        if (root / "prep.sqlite3").exists():
            print(f"{root} already holds a demo database. Pick an empty "
                  f"directory, or delete it first - it is never overwritten.")
            return 1
    else:
        root = Path(tempfile.mkdtemp(prefix="prep-demo-"))

    try:
        cfg, calendar = build(root)
        app = create_app(cfg, calendar_factory=lambda: calendar)
        print()
        print("  Prep dashboard - DEMO, invented data only")
        print(f"  http://{HOST}:{args.port}/")
        print(f"  data in {root}{'' if args.keep else ' (deleted on exit)'}")
        print("  Nothing here reaches Teams, iCloud or the network. Ctrl+C to stop.")
        print()
        app.run(host=HOST, port=args.port)
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
