"""Watch a capture happen, and say why the count is what it is.

    python3 -m prep.diagnose --config config.yaml
    python3 -m prep.diagnose --config config.yaml --headed    # watch it

"The wrong number of assignments" is the hardest failure in this system to
reason about, because nothing errors. You get *some* prep, it all looks
plausible, and the only clue is a number you have to know is wrong.

This prints the capture as it happens - when each payload arrived, how many
assignments were in it, and whether data was still coming when we stopped -
so the answer is visible rather than inferred.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_module
from . import cookies
from .scraper import (LOGIN_HOSTS, ProfileBusy, SessionExpired,
                      _looks_like_assignment_url, browser_args,
                      check_profile_lock, parse, reach_assignments,
                      require_playwright, save_failure_screenshot,
                      scroll_for_more, settle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch a capture and explain the assignment count")
    parser.add_argument("--config", required=True)
    parser.add_argument("--headed", action="store_true",
                        help="show the browser window")
    parser.add_argument("--expect", type=int,
                        help="how many you believe you have, for comparison")
    parser.add_argument("--max-seconds", type=int,
                        help="override max_settle_ms for this run")
    args = parser.parse_args(argv)

    cfg = config_module.load(args.config)
    teams = cfg.teams
    if args.max_seconds:
        teams.max_settle_ms = args.max_seconds * 1000

    print(f"profile:  {teams.profile_dir}")
    print(f"url:      {teams.assignments_url}")
    print(f"hints:    {', '.join(teams.url_hints)}")
    print(f"settle:   min {teams.settle_ms/1000:.0f}s, quiet {teams.quiet_ms/1000:.0f}s, "
          f"max {teams.max_settle_ms/1000:.0f}s, {teams.scroll_passes} scrolls")
    if teams.assignment_labels:
        print(f"labels:   {', '.join(teams.assignment_labels)}")

    # ---------------------------------------------------------- the lock
    try:
        note = check_profile_lock(teams.profile_dir)
        if note:
            print(f"\n[fixed]  {note}")
            print("         A stale lock makes a second Chromium fall back to "
                  "a scratch\n         profile - signed in, but with none of "
                  "your data. That would\n         explain both a wrong count "
                  "and a page that will not reload.")
    except ProfileBusy as exc:
        print(f"\n[STOP]   {exc}")
        print("         Close that browser and run this again. Two Chromiums "
              "on one\n         profile is exactly the failure you are seeing.")
        return 1

    # ------------------------------------------- third-party cookies
    #
    # Checked here because it explains a failure that arrives days late: with
    # these blocked, Teams cannot renew its own access in the background, and
    # a session that signed in perfectly starts reading as dead.
    state = cookies.prepare(teams.profile_dir, teams.third_party_cookies)
    if state.allowed is False:
        print(f"\n[STOP]   third-party cookies {state.describe()}")
        for line in cookies.advice(state):
            print(f"         {line}" if line else "")
    elif state.allowed:
        print(f"\n[ ok ]   third-party cookies {state.describe()}")

    sync_playwright = require_playwright(teams.chromium_path)

    events: list[tuple[float, str, int]] = []
    payloads: list[dict] = []
    started = time.monotonic()

    print("\n--- capturing ---")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(teams.profile_dir),
            executable_path=teams.chromium_path or None,
            headless=not args.headed and teams.headless,
            viewport={"width": 1280, "height": 1600},
            args=browser_args(teams),
        )
        if teams.block_resources:
            context.route("**/*", lambda route: (
                route.abort() if route.request.resource_type in teams.block_resources
                else route.continue_()))

        page = context.pages[0] if context.pages else context.new_page()

        def _on_response(response):
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            if not _looks_like_assignment_url(response.url, teams.url_hints):
                return
            try:
                body = response.json()
            except Exception:
                events.append((time.monotonic() - started,
                               f"{response.url[:58]} (not JSON)", 0))
                return
            payloads.append({"url": response.url, "body": body})
            # How many assignments this one payload contributed on its own.
            count = len(parse([{"url": response.url, "body": body}]))
            at = time.monotonic() - started
            events.append((at, response.url[:58], count))
            print(f"  {at:5.1f}s  {count:>3} assignments  {response.url[:56]}")

        # Context level: Teams runs the Assignments edu app in an iframe,
        # and a page-scoped listener risks missing it entirely.
        context.on("response", _on_response)
        try:
            page.goto(teams.assignments_url, wait_until="domcontentloaded",
                      timeout=teams.nav_timeout_ms)
            settle(page, payloads, teams)
            if not payloads:
                print("  ...no assignment data yet; clicking Assignments")
                reached = reach_assignments(page, payloads, teams)
                if reached:
                    print("  ...that worked")
            # Whether the list was seen to its end, asked of the page itself
            # rather than assumed. A list that will still scroll is a list
            # with prep in it that has never been fetched.
            at_bottom = scroll_for_more(page) is False
            final_url = page.url
            if not payloads:
                shot = save_failure_screenshot(page, cfg.dump_dir)
                if shot:
                    print(f"\n         screenshot of what it saw: {shot}")
        except Exception as exc:
            print(f"\n[FAIL]   navigation: {exc}")
            context.close()
            return 1
        finally:
            try:
                context.close()
            except Exception:
                pass

    elapsed = time.monotonic() - started

    # ---------------------------------------------------------- verdict
    print("\n--- what that means ---")
    if any(host in final_url for host in LOGIN_HOSTS):
        print(f"[FAIL]   ended on a sign-in page: {final_url}")
        print("         python3 -m prep.login --config " + args.config)
        return 1

    if not payloads:
        print("[FAIL]   no assignment data at all.")
        print(f"         ended on: {final_url}")
        fragment = ("#" + teams.assignments_url.split("#", 1)[1]
                    if "#" in teams.assignments_url else "")
        on_route = fragment and fragment in final_url
        if not on_route:
            print("\n         Teams does not put the Assignments view in the "
                  "address bar, so\n         the URL tells you little and no "
                  "URL can reach it - navigation\n         has to be a click, "
                  "which was tried and did not find anything\n         "
                  "labelled 'Assignments'.")
            print("\n         Look at the screenshot above. If Assignments is "
                  "visible in it,\n         the label differs in your tenant; "
                  "send me what it says. If the\n         page is blank or "
                  "still loading, raise max_settle_ms.")
        else:
            print(f"\n         You are on the right route, so it is the "
                  f"hints that did not match:\n         "
                  f"{', '.join(teams.url_hints)}")
            print(f"         Run: python3 -m prep.inspect --config "
                  f"{args.config}")
        return 1

    fragment = ("#" + teams.assignments_url.split("#", 1)[1]
                if "#" in teams.assignments_url else "")
    if fragment and fragment not in final_url:
        print(f"[warn]   the app ended on a different route than you asked "
              f"for.\n         asked: {fragment}\n         ended: "
              f"{final_url[-60:]}")
        print("         Data still arrived, so something was fetched - but if "
              "the count is\n         short, this is why. prep.login prints "
              "the URL that works.")

    assignments = parse(payloads)
    print(f"[ ok ]   {len(payloads)} payload(s), {len(assignments)} distinct "
          f"assignments, {elapsed:.0f}s")

    # Which domain answered. Teams web is moving to teams.cloud.microsoft with
    # an automatic redirect, and knowing you have been moved explains a one-off
    # re-authentication that would otherwise look like a fault.
    from urllib.parse import urlparse
    landed = urlparse(final_url).netloc
    if "cloud.microsoft" in landed:
        print(f"         served from {landed} (the new Teams domain)")
    elif landed:
        print(f"         served from {landed}")
    hosts = {urlparse(p["url"]).netloc for p in payloads}
    if len(hosts) > 1 or (hosts and landed and not (hosts & {landed})):
        print(f"         data came from: {', '.join(sorted(hosts))}")

    if at_bottom:
        print("[ ok ]   the list would not scroll any further, so everything "
              "in it was\n         loaded at least once.")
    else:
        print("[warn]   the list could still be scrolled when this stopped. "
              "Teams only\n         fetches the part it is about to show, so "
              "anything below that\n         point has never been requested. "
              "Raise scroll_passes and\n         max_settle_ms.")

    # Did it plateau, or were we still receiving when the clock ran out?
    last_arrival = max((at for at, _, count in events if count), default=0.0)
    quiet_at_end = elapsed - last_arrival
    if quiet_at_end < teams.quiet_ms / 1000:
        print(f"[warn]   data was STILL ARRIVING {quiet_at_end:.0f}s before "
              f"the end.")
        print(f"         This count is probably short. Try:")
        print(f"           python3 -m prep.diagnose --config {args.config} "
              f"--max-seconds {int(elapsed * 2)}")
        print(f"         and if that finds more, raise max_settle_ms in your "
              f"config.")
    else:
        print(f"[ ok ]   nothing new for the last {quiet_at_end:.0f}s, so the "
              f"page had finished.")

    now = datetime.now(timezone.utc).isoformat()
    current = [a for a in assignments if a.due_at and a.due_at > now]
    print(f"         {len(current)} still to come, "
          f"{len(assignments) - len(current)} already past")

    creators = Counter(a.creator_id or a.creator_name or "(none)"
                       for a in assignments)
    classes = Counter(a.class_id or "(none)" for a in assignments)
    print(f"         {len(creators)} distinct creator(s), "
          f"{len(classes)} distinct class(es)")
    if creators.get("(none)"):
        print(f"[warn]   {creators['(none)']} assignment(s) carry no creator, "
              f"so their teacher\n         cannot be resolved from who set "
              f"them.")

    if args.expect:
        difference = len(assignments) - args.expect
        if difference == 0:
            print(f"[ ok ]   matches the {args.expect} you expected.")
        else:
            print(f"[warn]   you expected {args.expect}; this saw "
                  f"{len(assignments)} ({difference:+d}).")
            if difference < 0:
                print("         Most likely: the page had not finished, or the "
                      "view you are\n         comparing against includes "
                      "completed prep this one does not.\n"
                      "         Try --max-seconds 120, and check whether the "
                      "missing ones are\n         all older than the rest.")
            else:
                print("         More than expected is usually finished prep "
                      "from earlier in\n         the year, which is normal and "
                      "harmless - past-due prep is\n         skipped when "
                      "scheduling.")

    oldest = min((a.due_at for a in assignments if a.due_at), default=None)
    newest = max((a.due_at for a in assignments if a.due_at), default=None)
    if oldest and newest:
        print(f"         due dates span {oldest[:10]} to {newest[:10]}")

    cfg.dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = cfg.dump_dir / f"payloads-{stamp}.json"
    import json
    target.write_text(json.dumps(payloads, indent=2, default=str))
    print(f"\n         saved to {target}")
    print(f"         inspect it: python3 -m prep.inspect --payload-file {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
