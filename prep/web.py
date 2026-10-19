"""Small always-on web UI: see upcoming prep, log how long one actually took.

Token-protected, LAN-bound. It holds school data, so don't port-forward it -
reach it over Tailscale from outside the house.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from datetime import date, datetime, time, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, redirect, render_template, request, url_for

from . import config as config_module
from . import pipeline
from .background import Planner
from .config import Config
from .isotime import fromiso
from .scheduler import CalDAVCalendar, FakeCalendar
from .store import Store, _ended

log = logging.getLogger(__name__)


class PrefixMiddleware:
    """Serve the app under a sub-path, e.g. /prep.

    Needed when something else already owns the root of this host. Strips the
    prefix before Flask routes, and tells Flask to build URLs with it, so links
    and redirects stay correct.
    """

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = "/" + prefix.strip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == self.prefix or path.startswith(self.prefix + "/"):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            environ["SCRIPT_NAME"] = self.prefix
            return self.app(environ, start_response)
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [f"not here - try {self.prefix}/".encode()]


def create_app(cfg: Config, calendar_factory=None,
               background: bool = False) -> Flask:
    """`background` does the slow part of every button - reading iCloud
    and the timetable, re-planning, writing the calendar - off the request,
    as the real dashboard does, so the page answers at once and reloads
    itself when the work is done. Off, each request waits for it, which is
    what the tests want."""
    app = Flask(__name__)
    app.config["PREP"] = cfg
    app.config["CALENDAR_FACTORY"] = calendar_factory or (
        lambda: CalDAVCalendar(cfg, ZoneInfo(cfg.timezone))
    )
    db_path = Path(cfg.db_path).expanduser()
    planner = Planner(db_path.with_name(db_path.name + ".planning.json"),
                      enabled=background)

    def store() -> Store:
        return Store(cfg.db_path)

    def later(key: str, what: str, job) -> str:
        """Run `job(db)` in the background, with its own connection, and
        return the notice to show now. `job` returns the notice to show
        when it is done."""
        def run() -> str:
            db = store()
            try:
                return job(db)
            finally:
                db.close()
        return planner.submit(key, what, run)

    def done(notice: str) -> str:
        """Success said out loud only when it happened in the background:
        waited for, the page itself showing the change is enough."""
        return notice if background else ""

    @app.template_filter("dmy")
    def dmy(value, with_time: bool = True) -> str:
        """An ISO date or datetime as day/month/year, in local time.

        Stored datetimes carry mixed offsets ("Z", "+01:00"), so they are
        converted rather than sliced - slicing showed a UTC due time as if
        it were local.
        """
        if not value:
            return ""
        try:
            parsed = fromiso(value)
        except ValueError:
            return str(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo(cfg.timezone))
        if not with_time or len(str(value)) <= 10:
            return parsed.strftime("%d/%m/%Y")
        return parsed.strftime("%d/%m/%Y %H:%M")

    def require_token(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not cfg.web.token:
                return view(*args, **kwargs)
            # request.form matters: every form on the page carries a hidden
            # token, and without this each button 403s the moment the cookie
            # lapses or is blocked.
            supplied = (request.args.get("token")
                        or request.form.get("token")
                        or request.headers.get("X-Prep-Token")
                        or request.cookies.get("prep_token"))
            if supplied != cfg.web.token:
                # Say what is missing. A bare "forbidden" on a LAN address
                # looks like a firewall or a crash, not a missing link.
                return Response(
                    "forbidden - this dashboard needs its token.\n\n"
                    "Open the link the dashboard printed when it started; it "
                    "ends in ?token=...\nOnce per browser: a cookie remembers "
                    "it after that.\n", status=403, mimetype="text/plain")
            response = app.make_response(view(*args, **kwargs))
            response.set_cookie("prep_token", cfg.web.token, max_age=60 * 60 * 24 * 365,
                                samesite="Lax")
            return response
        return wrapper

    @app.get("/")
    @require_token
    def index():
        db = store()
        try:
            rows = db.upcoming(now=datetime.now(ZoneInfo(cfg.timezone)))
            backlog = db.backlog()
            sessions = db.all_sessions()
            stats = db.teacher_stats()
            last = db.last_run()
            blackouts = db.blackouts()
            mu = db.global_mean()
            calibration = db.calibration()
            history = db.teacher_timings()
            teachers = db.known_teachers()
            to_confirm = db.unconfirmed(datetime.now(ZoneInfo(cfg.timezone)))
        finally:
            db.close()

        # When each unconfirmed session stops counting as done.
        grace = timedelta(hours=cfg.scheduling.confirm_grace_hours)
        confirm_by = {
            row["uid"]: (datetime.fromisoformat(row["end_local"]) + grace
                         ).astimezone(ZoneInfo(cfg.timezone)).isoformat()
            for row in to_confirm}

        # Group the schedule by day so the page reads like a diary. Days gone
        # by are history, not plans, so the diary starts today.
        from collections import OrderedDict
        now = datetime.now(ZoneInfo(cfg.timezone))
        today = now.date().isoformat()
        # Hours still ahead of you, not hours ever booked.
        to_come = [s for s in sessions if not _ended(s["end_local"], now)]
        by_day: "OrderedDict[str, list]" = OrderedDict()
        for session in sessions:
            if session["start_local"][:10] >= today:
                by_day.setdefault(session["start_local"][:10], []).append(session)

        from . import filtering as filtering_module
        filter_cfg = filtering_module.FilterConfig()
        # How variable each teacher is, for the +/- on each card.
        tracking = {}
        for teacher, timings in history.items():
            if len(timings) >= filter_cfg.min_observations:
                state = filtering_module.run(timings, filter_cfg, prior_minutes=mu)
                if state:
                    tracking[teacher] = {
                        "central": round(state.minutes),
                        "spread": round(state.relative_spread(filter_cfg) * 100),
                        "n": state.n,
                        "damped": state.rejected,
                    }

        from . import estimator as estimator_module
        confidence = {
            row["assignment_id"]: estimator_module.confidence(
                row["teacher"] or "", stats, mu, bool(row["override_minutes"]),
                history=history)
            for row in list(rows) + list(backlog)
        }
        # Backlog tasks get their own section, so they are not listed twice.
        in_backlog = {row["assignment_id"] for row in backlog}
        rows = [row for row in rows if row["assignment_id"] not in in_backlog]

        # How much longer the Teams session will last. Shown here because the
        # dashboard is the thing you can reach from your phone: seeing "3 days
        # left" on the bus is what turns re-authenticating into an errand you
        # choose, instead of an outage you discover.
        #
        # It reports. It does not offer to take your password: no page here
        # will ever ask for your school credentials, so any page that appears
        # to is not this one.
        from . import session as session_module
        try:
            health = session_module.read(cfg.teams.profile_dir)
            teams_session = {
                "summary": health.describe(),
                "days": health.days_left,
                "persistent": health.stay_signed_in,
                "ok": bool(health.found and health.auth_cookies
                           and health.stay_signed_in
                           and (health.days_left is None
                                or health.days_left > session_module.WARN_DAYS)),
            }
        except Exception as exc:                # never break the page over it
            teams_session = {"summary": f"could not be read ({exc})",
                             "days": None, "persistent": None, "ok": None}

        return render_template(
            "index.html", rows=rows, backlog=backlog, sessions=to_come, by_day=by_day,
            stats=stats, last_run=last, blackouts=blackouts, global_mean=mu,
            calibration=calibration, confidence=confidence, tracking=tracking,
            teams_session=teams_session, teachers=teachers,
            to_confirm=to_confirm, confirm_by=confirm_by,
            grace_hours=cfg.scheduling.confirm_grace_hours,
            notice=request.args.get("notice", "")[:600],
            planning=planner.status(),
            today=datetime.now(ZoneInfo(cfg.timezone)).date().isoformat(),
            travel_on=cfg.travel is not None,
            travel_default=(cfg.travel.default_mode if cfg.travel else "public"),
            personal_calendar=cfg.calendar.personal_calendar,
            token=cfg.web.token, prefix=cfg.web.url_prefix)

    @app.post("/override")
    @require_token
    def override():
        """Correct a wrong prediction."""
        assignment_id = request.form["assignment_id"]
        raw = (request.form.get("minutes") or "").strip()
        db = store()
        try:
            if raw == "":
                db.set_override(assignment_id, None)
            else:
                db.set_override(assignment_id, int(raw))
        except ValueError as exc:
            return Response(str(exc), status=400)
        finally:
            db.close()
        return back(later("replan", "Re-planning around the new estimate",
                          replan_now))

    @app.post("/blackout")
    @require_token
    def add_blackout():
        """Record a time you cannot work."""
        weekday = request.form.get("weekday", "")
        on_date = (request.form.get("on_date") or "").strip()
        db = store()
        try:
            db.add_blackout(
                start_time=request.form["start_time"],
                end_time=request.form["end_time"],
                weekday=int(weekday) if weekday not in ("", "none") else None,
                on_date=on_date or None,
                label=request.form.get("label", "").strip(),
                location=request.form.get("location", "").strip(),
                travel=request.form.get("travel", ""),
            )
        except (ValueError, KeyError) as exc:
            return Response(str(exc), status=400)
        finally:
            db.close()
        where = request.form.get("location", "").strip()

        def job(db: Store) -> str:
            return (place_warning(where) if where else "") + replan_now(db)
        return back(later("replan", "Re-planning around the blocked-out time",
                          job))

    @app.post("/blackout/<int:blackout_id>/delete")
    @require_token
    def remove_blackout(blackout_id: int):
        db = store()
        try:
            db.delete_blackout(blackout_id)
        finally:
            db.close()
        return back(later("replan", "Re-planning into the freed time",
                          replan_now))

    @app.post("/log")
    @require_token
    def log_timing():
        assignment_id = request.form["assignment_id"]
        try:
            minutes = int(request.form["minutes"])
        except (TypeError, ValueError):
            return Response("minutes must be a whole number", status=400)
        note = request.form.get("note", "")
        db = store()
        try:
            db.record_timing(assignment_id, minutes, note)
            db.write_export(cfg.export_path)
        except ValueError as exc:
            return Response(str(exc), status=400)
        finally:
            db.close()
        # Logged means finished: what is still booked for it comes out of
        # the diary, and a new estimate may move other prep.
        return back(later("recheck", "Updating the diary", recheck_now))

    @app.post("/session/<uid>/<status>")
    @require_token
    def confirm_session(uid: str, status: str):
        """Say whether a session happened. Missed time is booked again."""
        if status not in ("done", "missed"):
            return Response("a session is done or missed", status=404)
        db = store()
        try:
            row = db.conn.execute("SELECT end_local FROM sessions WHERE uid = ?",
                                  (uid,)).fetchone()
            if row is None:
                return Response("no such session", status=404)
            # Before it has happened there is nothing to confirm; to keep an
            # evening free, block it out instead.
            if datetime.fromisoformat(row["end_local"]) > datetime.now(
                    ZoneInfo(cfg.timezone)):
                return Response("that session has not finished yet - block "
                                "the time out instead", status=400)
            db.set_session_status(uid, status)
        finally:
            db.close()
        if status != "missed":
            return back()
        return back(later("recheck", "Booking the missed time again",
                          recheck_now))

    def recheck_now(db: Store) -> str:
        """Run the recheck straight away rather than in up to five minutes.

        Whatever it cannot do now - the calendar or the timetable being
        unreachable - the scheduled recheck does later, so nothing is lost.
        """
        from . import recheck
        try:
            result = recheck.run(cfg, db, app.config["CALENDAR_FACTORY"])
        except Exception as exc:
            log.warning("recheck failed: %s", exc)
            return "Saved. The diary will be updated on the next check."
        if result.error:
            return "Saved. The diary will be updated on the next check."
        if not result.findings:
            return ""
        log.info("recheck: %s", result.describe(db).replace("\n", "; "))
        placed = [o for o in result.outcomes if o.sessions]
        stuck = [o for o in result.outcomes if "NO FREE SLOT" in o.problem]
        parts = []
        if result.removed:
            parts.append(f"removed {result.removed} booking(s) for finished prep")
        if placed:
            parts.append(f"re-planned {len(placed)} prep")
        if stuck:
            parts.append(f"{len(stuck)} no longer fit before the deadline - "
                         f"first: {stuck[0].change.label}")
        return ("Done: " + "; ".join(parts) + ".") if parts else ""

    def plan_now(db: Store, change) -> str:
        """Schedule one change straight away. Returns a notice, "" if placed.

        A failure here never loses the task: it is already saved, and a task
        that was never planned is picked up by the next scheduled run.
        """
        try:
            calendar = app.config["CALENDAR_FACTORY"]()
            outcomes = pipeline.reschedule(cfg, db, calendar, [change])
        except pipeline.FeedsUnavailable:
            return ("Saved, not scheduled yet: the timetable feed is "
                    "unreachable. The next run will place it.")
        except Exception as exc:
            log.warning("could not schedule %s: %s", change.assignment_id, exc)
            return ("Saved, not scheduled yet: the calendar could not be "
                    "reached. The next run will place it.")
        log.info("planned: %s", "; ".join(o.describe() for o in outcomes))
        problems = [o.problem for o in outcomes if o.problem]
        if problems:
            return f"Saved, but {problems[0].lower()}."
        return ""

    def replan_now(db: Store) -> str:
        """Re-plan everything still to come after a constraint changed.

        The change itself is already saved. If the diary or the timetable
        cannot be read nothing is moved, rather than re-planning blind.
        """
        try:
            calendar = app.config["CALENDAR_FACTORY"]()
            outcomes = pipeline.replan_all(cfg, db, calendar)
        except pipeline.FeedsUnavailable:
            return ("Saved, but nothing was moved: the timetable feed is "
                    "unreachable, so your bookings were left as they were.")
        except Exception as exc:
            log.warning("could not replan: %s", exc)
            return ("Saved, but nothing was moved: the calendar could not be "
                    "reached, so your bookings were left as they were.")
        log.info("replanned: %s", "; ".join(o.describe() for o in outcomes))
        placed = [o for o in outcomes if o.sessions]
        stuck = [o for o in outcomes if "NO FREE SLOT" in o.problem]
        if stuck:
            return (f"Rescheduled {len(placed)} prep, but {len(stuck)} no longer "
                    f"fit before the deadline - first: {stuck[0].change.label}.")
        return f"Rescheduled {len(placed)} prep around the change." if placed else ""

    def place_warning(where: str) -> str:
        """Say so now if a place cannot be found, rather than silently
        treating it as no journey at all."""
        if cfg.travel is None:
            return ""
        from . import travel
        if travel.Places(cfg.travel).resolve(where, "Personal") is None:
            return (f"'{where}' is not a postcode or a place in your config, "
                    f"so no journey was counted for it. ")
        return ""

    def day_route(on: date) -> str:
        """How that day is now routed, for the notice after a change."""
        if cfg.travel is None:
            return ""
        tz = ZoneInfo(cfg.timezone)
        start = datetime.combine(on, time(0, 0), tzinfo=tz)
        db = store()
        try:
            _, routing = pipeline.diary_context(
                cfg, db, app.config["CALENDAR_FACTORY"](), start,
                start.replace(hour=23, minute=59), tz)
        except Exception as exc:
            log.warning("could not describe %s: %s", on, exc)
            return ""
        finally:
            db.close()
        plan = routing.days.get(on) if routing else None
        if plan is None:
            return ""
        text = "; ".join(plan.lines + plan.problems)
        return f" {on:%a %d %b}: {text}" if text else ""

    @app.post("/outside")
    @require_token
    def add_outside():
        """An outside commitment: into the Personal calendar, then re-plan."""
        tz = ZoneInfo(cfg.timezone)
        form = request.form
        try:
            on = date.fromisoformat(form["on_date"])
            start = datetime.combine(on, time.fromisoformat(form["start_time"]),
                                     tzinfo=tz)
            end = datetime.combine(on, time.fromisoformat(form["end_time"]),
                                   tzinfo=tz)
            title = form["title"].strip()
        except (KeyError, ValueError):
            return Response("needs a title, a date, and a start and end time",
                            status=400)
        if end <= start or not title:
            return Response("it must end after it starts, and have a title",
                            status=400)
        where = form.get("location", "").strip()
        travel = form.get("travel", "")
        travel = travel if travel in ("car", "public") else ""

        def job(db: Store) -> str:
            try:
                app.config["CALENDAR_FACTORY"]().add_personal(title, start, end,
                                                              where, travel)
            except Exception as exc:
                log.warning("could not add to %s: %s",
                            cfg.calendar.personal_calendar, exc)
                return f"{title} not added: {exc}"[:300]
            return (f"Added {title}. " + place_warning(where) + replan_now(db)
                    + day_route(on))
        return back(later(f"outside:{uuid.uuid4().hex}", f"Adding {title}", job))

    def back(notice: str = ""):
        return redirect(url_for("index", notice=notice) if notice
                        else url_for("index"))

    @app.post("/task")
    @require_token
    def add_task():
        """Add work Teams does not know about, and schedule it now."""
        tz = ZoneInfo(cfg.timezone)
        form = request.form
        try:
            due_day = date.fromisoformat(form["due_date"])
            # No time given means "that day", which the scheduler reads as
            # the configured due time - the same as a date-only Teams prep.
            due_time = (time.fromisoformat(form["due_time"])
                        if form.get("due_time") else time(0, 0))
            due = datetime.combine(due_day, due_time, tzinfo=tz)
            start = None
            if form.get("start_date"):
                start = datetime.combine(date.fromisoformat(form["start_date"]),
                                         time(0, 0), tzinfo=tz)
            raw = (form.get("minutes") or "").strip()
            minutes = int(raw) if raw else None
        except (KeyError, ValueError):
            return Response("a task needs a name and a valid due date",
                            status=400)

        db = store()
        try:
            task_id = db.add_task(
                form.get("title", ""), due.isoformat(), minutes=minutes,
                start_at=start.isoformat() if start else None,
                teacher=form.get("teacher", ""))
            row = db.assignment(task_id)
        except ValueError as exc:
            return Response(str(exc), status=400)
        finally:
            db.close()
        from .store import Change
        change = Change("new", task_id, row["label"], row["teacher"] or "",
                        row["due_at"])
        return back(later(f"plan:{task_id}", f"Scheduling {row['label']}",
                          lambda db: plan_now(db, change) or
                          done(f"Scheduled {row['label']}.")))

    @app.post("/task/<task_id>/delete")
    @require_token
    def remove_task(task_id: str):
        """Remove one of your tasks and every block booked for it."""
        db = store()
        try:
            row = db.assignment(task_id)
            if row is None or not row["manual"]:
                return Response("only tasks you added can be removed",
                                status=404)
        finally:
            db.close()
        label = row["label"] or row["title"]

        def job(db: Store) -> str:
            # Calendar first. If iCloud is unreachable the task stays, so its
            # blocks are never stranded in the diary with nothing pointing
            # at them.
            uids = [s["uid"] for s in db.sessions_for(task_id)]
            if uids:
                try:
                    app.config["CALENDAR_FACTORY"]().delete_by_uid(uids)
                except Exception as exc:
                    log.warning("could not remove %s from calendar: %s",
                                task_id, exc)
                    return (f"{label} not removed: the calendar could not be "
                            "reached, and removing it here alone would "
                            "leave its blocks in your diary.")
            db.delete_task(task_id)
            db.write_export(cfg.export_path)
            return done(f"Removed {label}.")
        return back(later(f"delete:{task_id}", f"Removing {label}", job))

    @app.post("/reschedule")
    @require_token
    def force_reschedule():
        """Re-plan a single prep, e.g. after correcting its estimate."""
        assignment_id = request.form["assignment_id"]
        db = store()
        try:
            row = db.assignment(assignment_id)
            if row is None:
                return Response("unknown assignment", status=404)
            from .store import Change
            change = Change("due_changed", assignment_id, row["label"] or row["title"],
                            row["teacher"] or "", row["due_at"], row["due_at"])
        finally:
            db.close()
        return back(later(f"plan:{assignment_id}", f"Rescheduling {change.label}",
                          lambda db: plan_now(db, change) or
                          done(f"Rescheduled {change.label}.")))

    @app.get("/export.txt")
    @require_token
    def export_txt():
        db = store()
        try:
            return Response(db.export_text(), mimetype="text/plain")
        finally:
            db.close()

    @app.get("/planning")
    @require_token
    def planning_status():
        """What is being done in the background, for the page to poll."""
        return planner.status()

    @app.get("/healthz")
    def healthz():
        db = store()
        try:
            last = db.last_run()
            status = last["status"] if last else "never-run"
            return {"status": status, "last_run": last["started_at"] if last else None}
        finally:
            db.close()

    if cfg.web.url_prefix:
        app.wsgi_app = PrefixMiddleware(app.wsgi_app, cfg.web.url_prefix)

    return app


MIN_TOKEN_LENGTH = 24


def token_warning(cfg: Config) -> str:
    """Why this deployment is weak, or "" if it is not.

    The dashboard's only authentication is a bearer token, so its strength is
    the whole of the security model. Shared so the web server and the test
    phases cannot disagree about what counts as safe.
    """
    loopback = cfg.web.host in ("127.0.0.1", "::1", "localhost")
    if not cfg.web.token:
        if loopback:
            return ""          # only reachable from the machine itself
        return (f"no token set and bound to {cfg.web.host} - anyone who can "
                f"reach this port can read your diary and reschedule it")
    if len(cfg.web.token) < MIN_TOKEN_LENGTH:
        return (f"token is {len(cfg.web.token)} characters; use at least "
                f"{MIN_TOKEN_LENGTH} (python3 -c "
                f"'import secrets; print(secrets.token_urlsafe(32))')")
    return ""


def lan_address() -> str:
    """This machine's address on the local network, or "" if unknown.

    A UDP connect sends nothing; it only asks the kernel which interface
    would be used, which is the address other devices reach it on.
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return ""


def from_env():
    """App factory for gunicorn: PREP_CONFIG names the config file.

        gunicorn --bind 127.0.0.1:8080 "prep.web:from_env()"

    gunicorn cannot pass arguments to a factory, so the path comes from the
    environment rather than argv.
    """
    import os

    path = os.environ.get("PREP_CONFIG")
    if not path:
        raise RuntimeError(
            "PREP_CONFIG is not set. Point it at your config.yaml - the "
            "systemd unit does this for you.")
    cfg = config_module.load(path)
    warning = token_warning(cfg)
    if warning:
        logging.getLogger("prep.web").warning("INSECURE: %s", warning)
    return create_app(cfg, background=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Prep dashboard")
    parser.add_argument("--config", required=True)
    parser.add_argument("--fake-calendar", action="store_true",
                        help="never touch iCloud (for testing the UI)")
    parser.add_argument("--state-dir",
                        help="use the database and export in this directory "
                             "instead of the config's, e.g. ~/prep/live-test")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    cfg = config_module.load(args.config)
    if args.state_dir:
        from pathlib import Path
        state = Path(args.state_dir).expanduser()
        cfg.db_path = state / "prep.sqlite3"
        cfg.export_path = state / "prep.txt"
    factory = (lambda: FakeCalendar()) if args.fake_calendar else None
    app = create_app(cfg, calendar_factory=factory, background=True)

    # Flask's own server is for development. Saying so once, plainly, is
    # better than letting it end up exposed because it happened to work.
    warning = token_warning(cfg)
    if warning:
        print(f"\n  INSECURE: {warning}\n")
    if cfg.web.host not in ("127.0.0.1", "::1", "localhost"):
        print("  This is Flask's development server. For anything reachable "
              "beyond this\n  machine, put nginx and gunicorn in front of it: "
              "see deploy/nginx-prep.conf.\n")

    token = f"?token={cfg.web.token}" if cfg.web.token else ""
    path = f":{cfg.web.port}{cfg.web.url_prefix}/{token}"
    print(f"\n  on this machine:   http://localhost{path}")
    if cfg.web.host not in ("127.0.0.1", "::1", "localhost"):
        address = lan_address()
        if address:
            print(f"  from your network: http://{address}{path}")
    print(f"  database:          {cfg.db_path}\n")
    app.run(host=cfg.web.host, port=cfg.web.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
