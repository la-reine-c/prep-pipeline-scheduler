"""Planning off the request, so the dashboard answers at once.

A re-plan reads iCloud and the timetable feed and writes to the calendar,
which takes long enough that waiting on it made every button feel broken.
Each job runs on one worker thread per process, in the order asked for; the
planning lock already keeps processes from planning at the same time.

What is running, and what the last job said, is kept in a small file next
to the database, so the page shows it whichever gunicorn worker serves it.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import queue
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# A job that has been "running" this long died with its process.
STALE_SECONDS = 15 * 60


def _alive(job: dict) -> bool:
    """Whether a job recorded as running can still be running: young
    enough, and its process still there. A worker restarted mid-plan would
    otherwise leave the page saying "updating" for a quarter of an hour."""
    if time.time() - float(job.get("since", 0)) >= STALE_SECONDS:
        return False
    try:
        os.kill(int(job.get("pid", 0)), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, OverflowError):
        pass
    return True


class Planner:
    """Runs planning jobs in the background, or inline when not `enabled`.

    Inline is what the tests and the demo use: the job's notice comes back
    from submit() exactly as it did before planning moved off the request.
    """

    def __init__(self, status_path: Path | str, enabled: bool = True):
        self.status_path = Path(status_path).expanduser()
        self.enabled = enabled
        self._queue: queue.Queue = queue.Queue()
        self._waiting: set[str] = set()
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None

    def submit(self, key: str, what: str, job: Callable[[], str]) -> str:
        """Queue `job`, which returns the notice to show when it is done.

        A job with the same key that is still waiting to start is not
        queued twice: it reads the database when it runs, so it will see
        this change too.
        """
        if not self.enabled:
            return job()
        with self._guard:
            if key in self._waiting:
                return "Already queued - the diary will catch up in a moment."
            self._waiting.add(key)
            if self._thread is None or not self._thread.is_alive():
                # Started on first use, not at import: gunicorn forks its
                # workers, and a thread does not survive a fork.
                self._thread = threading.Thread(target=self._work,
                                                name="prep-planner", daemon=True)
                self._thread.start()
        job_id = uuid.uuid4().hex[:12]
        self._update(lambda state: state["running"].__setitem__(
            job_id, {"what": what, "since": time.time(), "pid": os.getpid()}))
        self._queue.put((key, job_id, what, job))
        return f"{what} in the background - this page updates when it is done."

    def _work(self) -> None:
        while True:
            key, job_id, what, job = self._queue.get()
            with self._guard:
                self._waiting.discard(key)
            try:
                notice = job() or f"{what}: done, nothing needed moving."
            except Exception as exc:              # never kill the worker
                log.exception("background planning failed: %s", what)
                notice = f"{what} failed: {exc}"[:300]

            def finish(state, notice=notice):
                state["running"].pop(job_id, None)
                state["last"] = {"id": job_id, "what": what, "notice": notice,
                                 "at": time.time()}
            self._update(finish)
            self._queue.task_done()

    def status(self) -> dict:
        """{"running": [what, ...], "last": {...} or None}."""
        state = self._read()
        running = [job["what"] for job in state["running"].values()
                   if _alive(job)]
        return {"running": running, "last": state.get("last")}

    # ---------- the shared file ----------

    def _read(self) -> dict:
        try:
            state = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        if not isinstance(state.get("running"), dict):
            state["running"] = {}
        return state

    @contextmanager
    def _locked(self):
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.status_path.with_name(self.status_path.name + ".lock")
        with open(lock, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _update(self, change) -> None:
        try:
            with self._locked():
                state = self._read()
                change(state)
                # Jobs left behind by a worker that was restarted mid-plan.
                state["running"] = {k: v for k, v in state["running"].items()
                                    if _alive(v)}
                tmp = self.status_path.with_name(self.status_path.name + ".tmp")
                tmp.write_text(json.dumps(state))
                tmp.replace(self.status_path)
        except OSError as exc:
            # The status is a courtesy; failing to write it must not stop
            # the plan itself.
            log.warning("could not record planning status: %s", exc)
