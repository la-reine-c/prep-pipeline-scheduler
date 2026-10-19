"""Name teachers from prep you have already finished. A separate test.

    python3 -m prep.completed --config config.yaml
    python3 -m prep.completed --config config.yaml --weeks 4
    python3 -m prep.completed --config config.yaml --payload-file ~/prep/dumps/completed-*.json

The normal capture opens Assignments on Upcoming, which asks Teams only for
prep due today or later. A teacher whose prep you finish between two runs is
therefore never seen, and never gets a name. This opens the Completed tab as
well and lists who set what you finished in the last few weeks.

It is a copy of the capture rather than a change to it, on purpose: the
working capture in scraper.py is left exactly as it is, and nothing here is
used by prep.run. It reuses scraper's helpers by importing them.

It only reads and reports. Names are still recorded with prep.setup:

    python3 -m prep.setup --config config.yaml --set-teacher <id> "Name"

The capture is saved as completed-*.json, not payloads-*.json, so prep.setup
--no-refresh and the pipeline never pick it up by mistake - completed prep
must never be scheduled.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config as config_module
from . import cookies, identify, setup
from .scraper import (LOGIN_HOSTS, RawAssignment, SessionExpired, _frames,
                      _looks_like_assignment_url, browser_args,
                      check_profile_lock, load_payload_file, parse,
                      reach_assignments, require_playwright,
                      save_failure_screenshot, settle, snap_profile_problem)

log = logging.getLogger(__name__)

DEFAULT_WEEKS = 2
COMPLETED_LABEL = re.compile(r"^\s*completed\b", re.I)


def click_completed(page) -> str:
    """Click the Completed tab. Returns what worked, or "".

    The tab lives inside the edu app's iframe, so every frame is tried, the
    page itself first. Accessible names only, as with the Assignments click.
    """
    roots = _frames(page) or [page]
    for index, root in enumerate(roots):
        attempts = (
            ("tab", lambda: root.get_by_role("tab", name=COMPLETED_LABEL)),
            ("button", lambda: root.get_by_role("button", name=COMPLETED_LABEL)),
            ("link", lambda: root.get_by_role("link", name=COMPLETED_LABEL)),
            ("text", lambda: root.get_by_text(COMPLETED_LABEL)),
        )
        for how, build in attempts:
            try:
                target = build().first
                if not target.is_visible(timeout=2000):
                    continue
                target.click(timeout=5000)
                where = "" if index == 0 else " inside a frame"
                log.info("clicked Completed via %s%s", how, where)
                return f"{how}{where}"
            except Exception:
                continue
    return ""


def capture_completed(cfg, notes: dict | None = None) -> list[dict[str, Any]]:
    """Reach Assignments as the normal capture does, then open Completed.

    Returns only what arrived after the Completed click, so Upcoming prep
    cannot be counted as finished. Raises if the tab could not be found
    rather than quietly returning the Upcoming list.
    """
    sync_playwright = require_playwright(cfg.teams.chromium_path)
    teams = cfg.teams
    problem = snap_profile_problem(teams.chromium_path, teams.profile_dir)
    if problem:
        raise RuntimeError(problem)
    teams.profile_dir.mkdir(parents=True, exist_ok=True)
    check_profile_lock(teams.profile_dir)
    cookies.prepare(teams.profile_dir, getattr(teams, "third_party_cookies", True))

    payloads: list[dict[str, Any]] = []
    notes = notes if notes is not None else {}

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

        context.on("response", _on_response)
        try:
            page.goto(teams.assignments_url, wait_until="domcontentloaded",
                      timeout=teams.nav_timeout_ms)
            settle(page, payloads, teams, notes=notes)
            if any(host in page.url for host in LOGIN_HOSTS):
                raise SessionExpired(f"redirected to sign-in: {page.url}")
            if not reach_assignments(page, payloads, teams, notes=notes):
                save_failure_screenshot(page, cfg.dump_dir)
                raise SessionExpired("never reached Assignments")

            before = len(payloads)
            how = click_completed(page)
            notes["completed"] = how or "not found"
            if not how:
                save_failure_screenshot(page, cfg.dump_dir)
                raise RuntimeError("no Completed tab found on Assignments")
            page.wait_for_timeout(1500)
            settle(page, payloads, teams)
        finally:
            context.close()

    return payloads[before:]


def save(cfg, payloads: list[dict[str, Any]]) -> str:
    cfg.dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = cfg.dump_dir / f"completed-{stamp}.json"
    target.write_text(json.dumps(payloads, indent=2, default=str))
    return str(target)


def finished(assignments: list[RawAssignment], weeks: int,
             now: datetime | None = None) -> list[RawAssignment]:
    """Prep due in the last `weeks` weeks, up to now. Nothing still to come."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks)
    kept = []
    for item in assignments:
        due = setup._parse_due(item.due_at)
        if due is not None and cutoff <= due <= now:
            kept.append(item)
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Name teachers from completed prep (separate test; "
                    "changes nothing)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                        help=f"how far back to look (default {DEFAULT_WEEKS})")
    parser.add_argument("--payload-file",
                        help="use a saved completed-*.json instead of Teams")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    cfg = config_module.load(args.config)
    learned = identify.load_learned(cfg.learned_map_path)

    if args.payload_file:
        payloads = load_payload_file(args.payload_file)
    else:
        print("capturing the Completed tab from Teams...")
        notes: dict = {}
        try:
            payloads = capture_completed(cfg, notes)
        except SessionExpired as exc:
            print(f"\nTeams session expired: {exc}")
            print(f"Sign in again: python3 -m prep.login --config {args.config}")
            return 1
        except Exception as exc:
            print(f"\nCapture failed: {exc}")
            return 1
        print(f"opened Completed ({notes.get('completed')}), "
              f"{len(payloads)} payload(s), saved to {save(cfg, payloads)}")

    done = finished(parse(payloads), args.weeks)
    print(f"\n{len(done)} completed prep due in the last {args.weeks} week(s).")
    if not done:
        return 1

    for item in done:
        print(f"  {item.due_at[:10]}  {item.title[:60]}")

    people = setup.gather(done, learned, weeks=args.weeks)
    named = [p for p in people if p.teacher]
    print(f"\n{len(people)} people set it; {len(named)} already named:")
    for person in named:
        print(f"  {person.teacher:<16} {person.titles[0][:50]}")
    print()
    setup.print_unknown(people, args.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
