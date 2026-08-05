"""Queue-worker pool.

A bounded thread pool pulls job ids off an in-process queue, builds the right
adapter, and drives it. Each event the adapter emits is assigned a per-job seq,
persisted to SQLite, and published on the Bus for live SSE. Terminal status is
written back to the job row.
"""
from __future__ import annotations

import json
import queue
import threading
import traceback

from .adapters import build as build_adapter
from .adapters.base import Cancellation, Event, JobSpec
from .bus import Bus
from .config import Config
from .db import Database


class WorkerPool:
    def __init__(self, cfg: Config, db: Database, bus: Bus) -> None:
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self._q: queue.Queue[str] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cancels: dict[str, Cancellation] = {}   # running jobs
        self._cancel_requested: set[str] = set()      # requested before start

    def start(self) -> None:
        for i in range(max(1, self.cfg.concurrency)):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, job_id: str) -> None:
        self._q.put(job_id)

    def cancel(self, job_id: str) -> str:
        """Request cancellation. Returns 'running' (killed) or 'queued'
        (marked canceled; the worker will skip it if/when dequeued)."""
        with self._lock:
            tok = self._cancels.get(job_id)
            self._cancel_requested.add(job_id)
        if tok is not None:
            tok.cancel()
            return "running"
        # not started yet: finalize now so status reflects immediately
        self.db.finish_job(job_id, status="canceled", error="canceled before start")
        return "queued"

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:
            self._q.put("")  # unblock

    # -- internals --------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._q.get()
            if not job_id or self._stop.is_set():
                continue
            try:
                self._run_job(job_id)
            except Exception:  # never let a worker thread die
                self._fail(job_id, traceback.format_exc())

    def _run_job(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        agent_name = job["agent"]
        agent_cfg = self.cfg.agents.get(agent_name)
        if agent_cfg is None:
            self._fail(job_id, f"unknown agent '{agent_name}'")
            return

        cancel = Cancellation(grace_sec=self.cfg.cancel_grace_sec)
        with self._lock:
            if job_id in self._cancel_requested:
                # canceled while queued; cancel() already finalized the row
                self.bus.close(job_id)
                return
            self._cancels[job_id] = cancel

        seq = _Seq()
        try:
            self.db.mark_running(job_id)
            self._emit(job_id, seq,
                       Event("status", {"stage": "running", "agent": agent_name}))

            spec = JobSpec(
                job_id=job_id,
                prompt=job["prompt"],
                cwd=job["cwd"] or agent_cfg.default_cwd,
                requested_session=job["requested_session"],
                permission_mode=job["permission_mode"],
                model=job["model"],
                cancel=cancel,
                files=tuple(json.loads(job["files"]) if job["files"] else ()),
                title=job.get("title") or "",
                # NULL on rows created before the column existed -> fork, the
                # historical behaviour.
                fork=job.get("fork") is None or bool(job["fork"]),
                include_thinking=bool(job.get("include_thinking")),
            )
            adapter = build_adapter(agent_cfg)

            include_thinking = spec.include_thinking

            def emit(ev: Event) -> None:
                # Reasoning is noisy and can be long; keep it out of the stream
                # (not persisted, not fanned out to SSE) unless the caller asked
                # for it when submitting the job.
                if ev.type == "thinking" and not include_thinking:
                    return
                self._emit(job_id, seq, ev)

            result = adapter.run(spec, emit)
        finally:
            with self._lock:
                self._cancels.pop(job_id, None)
                self._cancel_requested.discard(job_id)

        if cancel.cancelled():
            status = "canceled"
            self.db.finish_job(
                job_id, status="canceled", error="canceled",
                result=result.result or None,
                chosen_session=result.chosen_session,
                forked_session=result.forked_session, cost_usd=result.cost_usd,
            )
        elif result.ok:
            status = "succeeded"
            self.db.finish_job(
                job_id, status="succeeded", result=result.result,
                chosen_session=result.chosen_session,
                forked_session=result.forked_session, cost_usd=result.cost_usd,
            )
        else:
            status = "failed"
            self.db.finish_job(
                job_id, status="failed", error=result.error or "run failed",
                result=result.result or None,
                chosen_session=result.chosen_session,
                forked_session=result.forked_session, cost_usd=result.cost_usd,
            )
        self._emit(job_id, seq, Event("status", {"stage": "done", "status": status}))
        self.bus.close(job_id)

    def _emit(self, job_id: str, seq: "_Seq", ev: Event) -> None:
        row = self.db.add_event(job_id, seq.next(), ev.type, ev.data)
        self.bus.publish(job_id, row)

    def _fail(self, job_id: str, message: str) -> None:
        try:
            self.db.finish_job(job_id, status="failed", error=message[:4000])
            row = self.db.add_event(job_id, 10**9, "error", {"message": message[:4000]})
            self.bus.publish(job_id, row)
        finally:
            self.bus.close(job_id)


class _Seq:
    """Monotonic per-job sequence; a job is owned by exactly one worker."""
    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n
