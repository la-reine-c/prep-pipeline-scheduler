"""Open a visible Chromium so you can sign into Teams once.

    python3 -m prep.login --config config.yaml

This uses the *same* profile directory and the *same* Chromium as the scraper,
read from the same config file. That matters: signing in with a hand-typed
one-liner that points somewhere slightly different is the classic way to sign
in successfully and still have every scheduled run report a dead session.

When you close it, it reopens headless and checks that the session really did
survive - because the only thing that matters is whether an unattended run can
use it, and that is not what you just tested by watching a window.
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config as config_module
from . import cookies
from . import session
from .scraper import (DEFAULT_RENDER_LEVEL, LOGIN_HOSTS, PlaywrightMissing,
                     ProfileBusy, SessionExpired, _looks_like_assignment_url,
                     browser_args, capture, check_profile_lock, label_pattern,
                     parse, render_advice, render_ladder, require_playwright,
                     snap_profile_problem, window_looks_blank)

log = logging.getLogger(__name__)


def can_ask() -> bool:
    """Is there someone at a terminal to answer a question?"""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def ask_yes(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not answer:
        return default
    return answer.startswith("y")


def open_window(cfg) -> tuple[int, str]:
    """Launch a visible browser, wait for you, return (payloads seen, url).

    Walks down the render ladder until the window actually draws. A blank
    window is the commonest way a sign-in fails outright, it depends entirely
    on the machine - a discrete NVIDIA card, a VM with no GPU, an ARM box
    running a Mac's GPU under Linux - and the flags that fix each are
    different. Guessing them by hand, one relaunch at a time, is the thing
    this removes.
    """
    sync_playwright = require_playwright(cfg.teams.chromium_path)

    teams = cfg.teams
    # Before anything opens: with a snap browser and a profile it cannot
    # write, you would sign in successfully and the session would then
    # vanish, because the snap kept it somewhere else.
    problem = snap_profile_problem(teams.chromium_path, teams.profile_dir)
    if problem:
        raise RuntimeError(problem)
    teams.profile_dir.mkdir(parents=True, exist_ok=True)
    seen: list[str] = []

    # Both before the launch, and in this order. The lock check is what stops
    # a second Chromium quietly using a scratch profile, and the cookie
    # preferences have to be written while nothing is holding the profile
    # open - Chromium writes its own copy back out when it exits.
    check_profile_lock(teams.profile_dir)
    state = cookies.prepare(teams.profile_dir, teams.third_party_cookies)
    print(f"cookies:  third-party {state.describe()}")
    for line in cookies.advice(state):
        print(f"          {line}" if line else "")

    start = DEFAULT_RENDER_LEVEL if getattr(teams, "disable_gpu", True) else "gpu"
    ladder = render_ladder(start)

    with sync_playwright() as pw:
        context = None
        for attempt, level in enumerate(ladder):
            if context is not None:
                # Always close before relaunching: two Chromiums on one
                # profile is a separate bug, and the second one silently
                # falls back to a scratch profile.
                try:
                    context.close()
                except Exception:
                    pass
            if attempt:
                print(f"\nReopening with {level} rendering...")

            try:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(teams.profile_dir),
                    executable_path=teams.chromium_path or None,
                    headless=False,      # the entire point of this command
                    viewport={"width": 1280, "height": 900},
                    args=browser_args(teams, render=level),
                )
            except Exception as exc:
                text = str(exc)
                if "Missing X server" in text or "DISPLAY" in text:
                    raise RuntimeError(
                        "No desktop session to open a window in. Sign in at the "
                        "machine itself, or forward a display:\n"
                        "    ssh -X you@the-machine    then rerun this command"
                    ) from exc
                # A flag this Chromium does not understand is a reason to try
                # the next rung, not to give up on signing in.
                if attempt + 1 < len(ladder):
                    print(f"\n{level} rendering would not start: "
                          f"{text.splitlines()[0][:90]}")
                    context = None
                    continue
                raise

            page = context.pages[0] if context.pages else context.new_page()

            def _on_response(response):
                if response.request.resource_type not in ("xhr", "fetch"):
                    return
                if _looks_like_assignment_url(response.url, teams.url_hints):
                    seen.append(response.url)

            page.on("response", _on_response)
            page.goto(teams.assignments_url, wait_until="domcontentloaded",
                      timeout=teams.nav_timeout_ms)
            page.wait_for_timeout(2500)   # give it time to paint before judging

            last = attempt + 1 >= len(ladder)

            # Nothing drew at all: no need to trouble anyone, just drop a rung.
            if window_looks_blank(page) is True and not last:
                print(f"\nThat window drew nothing at all ({level} rendering).")
                continue

            # It may still be blank on screen while screenshotting cleanly,
            # and only the person looking at it can say. So ask - once, with
            # the useful default, and never when nobody is there to answer.
            if can_ask() and not last:
                print(f"\nA window should be open on Teams ({level} rendering).")
                if not ask_yes("Can you actually see the page in it?"):
                    continue

            if level != start:
                print(f"\n{level} rendering worked. To keep it, put this in "
                      f"config.yaml:\n")
                for line in render_advice(level):
                    print(f"    {line}")
                print()
            break

        print("\n" + "=" * 68)
        print("A Chromium window is open. In it:")
        print("  1. Sign in if you are asked to.")
        print("  2. TICK 'Stay signed in' - without it the session dies in a")
        print("     day and every scheduled run will alert you.")
        print("  3. NAVIGATE TO ASSIGNMENTS YOURSELF. The app opens on Teams,")
        print("     and on teams.cloud.microsoft the address bar does not")
        print("     change when you get there - so the click is the only way")
        print("     in, and the URL will look identical afterwards. That is")
        print("     expected; this watches the requests, not the address.")
        print("  4. SCROLL TO THE BOTTOM of the list. Teams only fetches the")
        print("     part it is about to show, so prep you have not scrolled")
        print("     past has never been requested and cannot be seen here.")
        print("  5. DO NOT close the browser window yourself - come back here")
        print("     and press Enter. Closing it by hand skips the clean")
        print("     shutdown, and the clean shutdown is what writes your")
        print("     session to disk. That is why a sign-in can work and then")
        print("     be gone the next time.")
        print("=" * 68)
        try:
            input("\nThen come back here and press Enter. ")
        except (EOFError, KeyboardInterrupt):
            print("\n(interrupted - still closing the browser cleanly so the "
                  "session is saved)")

        # Read the URL before closing, and never let a failure here skip the
        # close: an unflushed profile is a sign-in that silently did not stick.
        try:
            url = page.url
        except Exception:
            url = ""
            print("\nThe window was already closed, so I could not read the "
                  "final URL.\nNext time press Enter instead of closing it - "
                  "the session may not have\nbeen written to disk.")
        finally:
            try:
                context.close()
            except Exception as exc:
                log.warning("browser did not close cleanly: %s", exc)

    return len(seen), url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sign into Teams once, in the profile the scraper uses")
    parser.add_argument("--config", required=True)
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the headless check afterwards")
    args = parser.parse_args(argv)

    cfg = config_module.load(args.config)
    print(f"profile:  {cfg.teams.profile_dir}")
    where = cfg.teams.chromium_path or "Playwright's own build"
    print(f"chromium: {where}")
    # Printed because a blank window is the failure this is most likely to
    # hit, and knowing which flags were actually applied is the difference
    # between a two-minute fix and an evening.
    print(f"flags:    {' '.join(browser_args(cfg.teams))}")
    if not cfg.teams.disable_gpu:
        print("          (GPU acceleration is ON - if the window opens but "
              "never renders,\n           set disable_gpu: true under teams: "
              "in your config)")

    try:
        count, url = open_window(cfg)
    except ProfileBusy as exc:
        print(f"\n{exc}")
        print("Two Chromiums on one profile is how a sign-in appears to work "
              "and then\nturns out to have been written to a scratch profile.")
        return 1
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1
    except PlaywrightMissing as exc:
        print(f"\n{exc}")
        return 1
    except Exception as exc:
        print(f"\nCould not open a browser: {exc}")
        return 1

    if any(host in url for host in LOGIN_HOSTS):
        print("\nYou were still on a Microsoft sign-in page when you closed it, "
              "so the sign-in did not finish. Run this again.")
        return 1
    print(f"\nSaw {count} assignment request(s) while the window was open.")

    # The URL you ended on can be worth copying - but only when it actually
    # names the view. On teams.cloud.microsoft it usually does not: the
    # address stays on the app root however far into Assignments you click,
    # and copying that in would point the config at chat.
    pattern = label_pattern(cfg.teams)
    names_the_view = bool(pattern.search(url or ""))
    if url and url.rstrip("/") != cfg.teams.assignments_url.rstrip("/"):
        print("\n" + "-" * 68)
        print("You finished on a different URL from the one in your config.")
        print(f"  config: {cfg.teams.assignments_url}")
        print(f"  yours:  {url}")
        if count and names_the_view:
            print("\nAssignment data arrived while you were there, and this "
                  "URL names the\nview, so yours is the right one. Put it in "
                  "config.yaml under teams:")
            print(f"\n  assignments_url: \"{url}\"\n")
        elif count:
            print("\nAssignment data arrived, so the click worked - but this "
                  "URL does not\nname Assignments, which is normal on "
                  "teams.cloud.microsoft: the address\nstays on the app root "
                  "wherever you are in it. LEAVE YOUR CONFIG AS IT IS.\n"
                  "The capture clicks through the same way you just did.")
        else:
            print("\nNo assignment data arrived, so this may not be the "
                  "Assignments view.\nOpen it, scroll to the bottom of your "
                  "prep, and run this again.")
        print("-" * 68)
    elif not count:
        print("\nNo assignment requests were seen at all. Either you did not "
              "open Assignments,\nor url_hints does not match what your "
              "tenant calls it:")
        print(f"  url_hints: {', '.join(cfg.teams.url_hints)}")
        print(f"  check with: python3 -m prep.inspect --config {args.config}")

    if args.no_verify:
        return 0

    # The real test. A window you watched proves nothing about an unattended
    # run: headless is a different browser launch, and the profile has only
    # just been written to disk.
    print("\nReopening headless to check an unattended run can use it...")
    cfg.teams.headless = True
    try:
        payloads = capture(cfg, dump=False)
    except SessionExpired as exc:
        print(f"\nThe session did NOT survive: {exc}")
        print("Most often this means 'Stay signed in' was not ticked. "
              "Run this again and tick it.")
        return 1
    except Exception as exc:
        print(f"\nHeadless capture failed: {exc}")
        return 1

    assignments = parse(payloads)
    print(f"\nWorking. A headless run sees {len(assignments)} prep.")

    # Whether the box was ticked is the difference between signing in once a
    # term and signing in every time the browser closes - and you cannot see
    # it from the window, because both look identical.
    health = session.read(cfg.teams.profile_dir)
    print(f"Session:  {health.describe()}")

    # Asked again after the browser has been and gone: Chromium writes its
    # preferences back out on exit, so this is the state an unattended run
    # will actually launch with, rather than the one we asked for.
    state = cookies.read_state(cfg.teams.profile_dir)
    print(f"Cookies:  third-party {state.describe()}")
    if state.allowed is False:
        for line in cookies.advice(state):
            print(f"          {line}" if line else "")
        print("\nThis sign-in will work now and stop working within days: "
              "Teams cannot\nrenew its own access without it.")
        return 1
    if health.found and health.auth_cookies and not health.stay_signed_in:
        print("\n'Stay signed in' was not ticked, so this session dies when "
              "the browser closes.\nRun this again and tick it - otherwise "
              "every scheduled run starts logged out.")
        return 1

    print(f"\nNext: python3 -m prep.phases B --config {args.config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
