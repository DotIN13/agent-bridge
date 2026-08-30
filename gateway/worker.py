"""Persistent queue worker pool with graceful recovery and shutdown."""
from __future__ import annotations

import json
import queue
import threading
import time
import traceback

from . import jobdir
from .adapters import build as build_adapter
from .adapters.base import Cancellation, Event, JobSpec, Steering
from .bus import Bus
from .config import Config
from .db import WAITING, Database


class WorkerPool:
    def __init__(self, cfg: Config, db: Database, bus: Bus) -> None:
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self._q: queue.Queue[str] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cancels: dict[str, Cancellation] = {}
        self._cancel_requested: set[str] = set()
        self._steers: dict[str, Steering] = {}
        self._claimed: dict[str, str] = {}
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
        queued = self.db.reconcile_startup()
        for index in range(max(1, self.cfg.concurrency)):
            thread = threading.Thread(
                target=self._loop, name=f"worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        for job_id in queued:
            self.submit(job_id)

    @property
    def report_wait_sec(self) -> float:
        return float(getattr(self.cfg, "report_wait_sec", 0.0) or 0.0)

    def _on_turn_end(self, job_id: str, spec: JobSpec, fields: dict) -> None:
        """The agent has answered. Whether the job is done is another question.

        A job is finished when its turn has ended *and* its report is written.
        With the report already there — the short-job path, and the long-job path
        where a preliminary report was written before ending the turn — close the
        agent's stdin and let the run wind up normally. Without it, the row goes
        `waiting` and the process stays alive: it can still be steered, and it
        can still write the file (design/17).
        """
        steer = self._steers.get(job_id)
        if not spec.job_dir or jobdir.has_report(spec.job_dir):
            if steer is not None:
                steer.close()
            return
        self.db.save_result_fields(job_id, fields)
        deadline = (time.time() + self.report_wait_sec
                    if self.report_wait_sec > 0 else None)
        row = self.db.mark_waiting(job_id, deadline)
        if row:
            self.bus.publish(job_id, row)

    def submit(self, job_id: str) -> None:
        if self._stop.is_set():
            raise RuntimeError("worker pool is stopping")
        self._q.put(job_id)

    def cancel(self, job_id: str) -> str:
        with self._lock:
            token = self._cancels.get(job_id)
            self._cancel_requested.add(job_id)
            if token is not None:
                # Terminal persistence is serialized by this same pool lock.
                # Publish the flag before releasing it, then do the potentially
                # slow process interruption outside the lock.
                token.mark_cancelled()
        if token is not None:
            token.cancel()
            return "running"

        row = self.db.cancel_queued_job(job_id)
        if row is not None:
            self.bus.publish(job_id, row)
            self.bus.close(job_id)
            with self._lock:
                self._cancel_requested.discard(job_id)
            return "queued"

        # A worker may have won queued -> running between the first token read
        # and the database compare-and-set. It registers the token before that
        # transition, so re-checking here closes the race.
        with self._lock:
            token = self._cancels.get(job_id)
            if token is not None:
                token.mark_cancelled()
        if token is not None:
            token.cancel()
            return "running"
        job = self.db.get_job(job_id)
        status = job["status"] if job else "unknown"
        if status != "running":
            with self._lock:
                self._cancel_requested.discard(job_id)
        return status

    def steering(self, job_id: str) -> Steering | None:
        with self._lock:
            return self._steers.get(job_id)

    def claimant(self, session_id: str) -> str | None:
        if not session_id:
            return None
        with self._lock:
            return self._claimed.get(session_id)

    def _claim(self, job_id: str, session_id: str | None) -> str | None:
        if not session_id:
            return None
        with self._lock:
            holder = self._claimed.get(session_id)
            if holder and holder != job_id:
                return holder
            fresh = self._claimed.get(session_id) != job_id
            self._claimed[session_id] = job_id
        if fresh:
            # On the row, not only in memory. The claim is what stops two jobs
            # sharing a session; the row is what lets anybody see which session
            # a running job is in -- and the terminal write is far too late for
            # that, since it never comes at all if the run raises.
            self.db.set_job_session(job_id, session_id)
        return None

    def _release_locked(self, job_id: str) -> None:
        """Release per-job state while ``self._lock`` is already held."""
        self._cancels.pop(job_id, None)
        self._steers.pop(job_id, None)
        self._cancel_requested.discard(job_id)
        for sid in [sid for sid, owner in self._claimed.items()
                    if owner == job_id]:
            del self._claimed[sid]

    def _release(self, job_id: str) -> None:
        with self._lock:
            self._release_locked(job_id)

    def stop(self, timeout: float | None = None) -> None:
        """Interrupt live work, wake workers, and join before DB shutdown."""
        with self._lock:
            if not self._started:
                return
            self._stop.set()
            tokens = list(self._cancels.values())
            threads = list(self._threads)
        for token in tokens:
            token.cancel()
        for _thread in threads:
            self._q.put("")
        per_thread = timeout if timeout is not None else (
            self.cfg.cancel_grace_sec + 6.0)
        for thread in threads:
            thread.join(per_thread)
        alive = [thread.name for thread in threads if thread.is_alive()]
        with self._lock:
            self._threads = [thread for thread in threads if thread.is_alive()]
            self._started = bool(self._threads)
        if alive:
            raise RuntimeError(f"worker threads did not stop: {alive}")

    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._q.get()
            if not job_id or self._stop.is_set():
                continue
            try:
                self._run_job(job_id)
            except Exception:
                self._fail(job_id, traceback.format_exc())

    def _run_job(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if not job or job["status"] != "queued":
            return
        agent_name = job["agent"]
        agent_cfg = self.cfg.agents.get(agent_name)
        if agent_cfg is None:
            self._fail(job_id, f"unknown agent '{agent_name}'")
            return

        cancel = Cancellation(grace_sec=self.cfg.cancel_grace_sec)
        steer = Steering()
        with self._lock:
            if job_id in self._cancel_requested:
                self.bus.close(job_id)
                return
            self._cancels[job_id] = cancel
            self._steers[job_id] = steer

        try:
            # Created before the agent starts, so `$AB_JOB_DIR/progress/` is
            # already there the first time the delegate reaches for it. A dir we
            # cannot create is not worth failing a job over -- the job simply
            # runs without one, and the preamble then says nothing about it.
            try:
                job_dir = str(jobdir.prepare(self.cfg.data_dir, job_id))
            except OSError as exc:
                job_dir = ""
                self.db.append_event(job_id, "log", {
                    "job_dir": "unavailable", "reason": str(exc)})
            spec = JobSpec(
                job_id=job_id,
                prompt=job["prompt"],
                cwd=job["cwd"] or agent_cfg.default_cwd,
                requested_session=job["session"],
                permission_mode=job["permission_mode"],
                model=job["model"],
                cancel=cancel,
                steer=steer,
                files=tuple(job.get("files") or ()),
                title=job.get("title") or "",
                fork=bool(job.get("fork", True)),
                include_thinking=bool(job.get("include_thinking")),
                job_dir=job_dir,
            )
            if not spec.fork:
                holder = self._claim(job_id, spec.requested_session)
                if holder:
                    self._fail(
                        job_id,
                        f"session {spec.requested_session} is being written by "
                        f"job {holder}; steer that job or wait for it to finish")
                    return

            running_row = self.db.start_queued_job(job_id, {
                "stage": "running", "agent": agent_name})
            if running_row is None:
                steer.close()
                self._release(job_id)
                return
            self.bus.publish(job_id, running_row)
            if cancel.cancelled():
                steer.close()
                with self._lock:
                    rows = self.db.finish_job_with_events(
                        job_id, {"status": "canceled", "error": "canceled"},
                        [("status", {"stage": "done", "status": "canceled"})])
                    self._release_locked(job_id)
                for row in rows:
                    self.bus.publish(job_id, row)
                self.bus.close(job_id)
                return
            adapter = build_adapter(agent_cfg)

            def emit(event: Event) -> None:
                if event.type == "thinking" and not spec.include_thinking:
                    return
                if event.type == "status" and event.data.get("session_id"):
                    self._claim(job_id, event.data["session_id"])
                if event.type == "status" and \
                        event.data.get("stage") == "turn_end":
                    # Not persisted as-is: whichever way this goes writes its
                    # own event, and two would say the same thing twice.
                    self._on_turn_end(job_id, spec, event.data)
                    return
                self._emit(job_id, event)

            result = adapter.run(spec, emit)
        except Exception:
            steer.close()
            self._fail(job_id, traceback.format_exc())
            return

        # Keep the cancellation token registered while deciding and committing
        # the terminal state. A concurrent cancel either sets the token before
        # this lock is acquired (and wins), or observes the committed terminal
        # row after per-job state is released; it can never report an accepted
        # cancellation that is then overwritten by success.
        steer.close()
        with self._lock:
            fields = {
                "result": result.result or None,
                "session": result.session,
                "cost_usd": result.cost_usd,
            }
            # The report is the result (design/23). Where the delegate wrote one,
            # it is the answer `ab job` should print -- the turn's own final
            # message is already on the event stream, and asking for the same
            # content in both places is how the two come to disagree.
            reported = jobdir.read_report(spec.job_dir) if spec.job_dir else None
            if reported:
                fields["result"] = reported
            # Written before any status decision, and whatever the outcome: on
            # the hold-open path the run returns *after* the sweeper has already
            # finished the row, so a guarded terminal update would drop the
            # turn's own answer on the floor.
            self.db.save_result_fields(job_id, fields)
            waiting = False
            if cancel.cancelled():
                status = "canceled"
                fields.update(status=status, error="canceled")
            elif result.ok:
                status = "succeeded"
                # `result` deliberately not restated here: it was decided above,
                # and re-setting it from the turn would undo the report.
                fields.update(status=status, error=None)
                # A backend whose child exits with its turn (opencode, the
                # dispatcher modes) never reaches `_on_turn_end`, so the same
                # rule is applied here: no report yet means `waiting`, not
                # success. The row already waiting is left alone rather than
                # having its deadline pushed out.
                if spec.job_dir and not jobdir.has_report(spec.job_dir):
                    waiting = self.db.get_job(job_id)["status"] == WAITING
                    if not waiting:
                        row = self.db.mark_waiting(
                            job_id, time.time() + self.report_wait_sec
                            if self.report_wait_sec > 0 else None)
                        waiting = row is not None
                        if row:
                            self.bus.publish(job_id, row)
            else:
                status = "failed"
                fields.update(status=status, error=result.error or "run failed")
            rows = [] if waiting else self.db.finish_job_with_events(
                job_id, fields,
                [("status", {"stage": "done", "status": status})])
            # Releases the worker slot and the session claim. Done even while
            # `waiting`, because reaching here means the agent process has
            # exited: holding the claim would block every later resume of that
            # session for the whole grace window.
            self._release_locked(job_id)
        for row in rows:
            self.bus.publish(job_id, row)
        if not waiting:
            # A waiting job's stream stays open: the follower is here to see the
            # report land, and the sweeper closes it when it does.
            self.bus.close(job_id)

    def _emit(self, job_id: str, event: Event) -> None:
        row = self.db.append_event(job_id, event.type, event.data)
        self.bus.publish(job_id, row)

    def _fail(self, job_id: str, message: str) -> None:
        text = message[:4000]
        with self._lock:
            token = self._cancels.get(job_id)
            canceled = bool(token and token.cancelled())
            if canceled:
                fields = {"status": "canceled", "error": "canceled"}
                events = [("status", {"stage": "done", "status": "canceled"})]
            else:
                fields = {"status": "failed", "error": text}
                events = [("error", {"message": text}),
                          ("status", {"stage": "done", "status": "failed"})]
            rows = self.db.finish_job_with_events(job_id, fields, events)
            self._release_locked(job_id)
        for row in rows:
            self.bus.publish(job_id, row)
        self.bus.close(job_id)
