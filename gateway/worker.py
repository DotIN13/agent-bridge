"""Persistent queue worker pool with graceful recovery and shutdown."""
from __future__ import annotations

import json
import queue
import threading
import traceback

from .adapters import build as build_adapter
from .adapters.base import Cancellation, Event, JobSpec, Steering
from .bus import Bus
from .config import Config
from .db import AWAITING_REPORT, Database


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

    @property
    def report_timeout_sec(self) -> float:
        return float(getattr(self.cfg, "report_timeout_sec", 0.0) or 0.0)

    def _parked(self, job_id: str) -> bool:
        """Did the turn end without ending the job? Then keep the stream open."""
        row = self.db.get_job(job_id)
        return bool(row and row["status"] == AWAITING_REPORT)

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
            self._claimed[session_id] = job_id
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
            spec = JobSpec(
                job_id=job_id,
                prompt=job["prompt"],
                cwd=job["cwd"] or agent_cfg.default_cwd,
                requested_session=job["requested_session"],
                permission_mode=job["permission_mode"],
                model=job["model"],
                cancel=cancel,
                steer=steer,
                files=tuple(job.get("files") or ()),
                title=job.get("title") or "",
                fork=bool(job.get("fork", True)),
                include_thinking=bool(job.get("include_thinking")),
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
                "chosen_session": result.chosen_session,
                "forked_session": result.forked_session,
                "cost_usd": result.cost_usd,
            }
            if cancel.cancelled():
                status = "canceled"
                fields.update(status=status, error="canceled")
            elif result.ok:
                status = "succeeded"
                fields.update(status=status, result=result.result, error=None)
            else:
                status = "failed"
                fields.update(status=status, error=result.error or "run failed")
            rows = self.db.finish_job_with_events(
                job_id, fields,
                [("status", {"stage": "done", "status": status})],
                report_timeout_sec=self.report_timeout_sec)
            # Releases the worker slot and the session claim either way: the
            # agent process is gone even when the row stays open, so holding
            # the claim would block every later resume of that session.
            self._release_locked(job_id)
        for row in rows:
            self.bus.publish(job_id, row)
        if not self._parked(job_id):
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
