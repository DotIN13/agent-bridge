"""Queue-worker pool.

A bounded thread pool pulls job ids off an in-process queue, builds the right
adapter, and drives it. Each event the adapter emits is assigned a per-job seq,
persisted to SQLite, and published on the Bus for live SSE. Terminal status is
written back to the job row.
"""
from __future__ import annotations

import queue
import threading
import traceback

from .adapters import build as build_adapter
from .adapters.base import Event, JobSpec
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

    def start(self) -> None:
        for i in range(max(1, self.cfg.concurrency)):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, job_id: str) -> None:
        self._q.put(job_id)

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

        self.db.mark_running(job_id)
        seq = _Seq()
        self._emit(job_id, seq, Event("status", {"stage": "running", "agent": agent_name}))

        spec = JobSpec(
            job_id=job_id,
            prompt=job["prompt"],
            cwd=job["cwd"] or agent_cfg.default_cwd,
            requested_session=job["requested_session"],
            permission_mode=job["permission_mode"],
            model=job["model"],
        )
        adapter = build_adapter(agent_cfg)

        def emit(ev: Event) -> None:
            self._emit(job_id, seq, ev)

        result = adapter.run(spec, emit)

        if result.ok:
            self.db.finish_job(
                job_id, status="succeeded", result=result.result,
                chosen_session=result.chosen_session,
                forked_session=result.forked_session, cost_usd=result.cost_usd,
            )
        else:
            self.db.finish_job(
                job_id, status="failed", error=result.error or "run failed",
                result=result.result or None,
                chosen_session=result.chosen_session,
                forked_session=result.forked_session, cost_usd=result.cost_usd,
            )
        self._emit(job_id, seq, Event("status", {
            "stage": "done",
            "status": "succeeded" if result.ok else "failed",
        }))
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
