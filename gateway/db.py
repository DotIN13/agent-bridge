"""SQLite persistence for jobs and their event logs.

A single connection guarded by one lock (WAL mode). All writes and the short
backlog reads used by SSE go through it; SSE then leaves the DB alone and tails
the in-memory Bus, so the lock is held only briefly.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,          -- queued|running|succeeded|failed|canceled
    agent             TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    cwd               TEXT,
    requested_session TEXT,                   -- caller hint (optional)
    chosen_session    TEXT,                   -- session the dispatcher forked
    forked_session    TEXT,                   -- new session id created by the fork
    permission_mode   TEXT,
    model             TEXT,
    files             TEXT,                   -- JSON list of attached file paths
    result            TEXT,
    error             TEXT,
    cost_usd          REAL,
    created_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL
);

CREATE TABLE IF NOT EXISTS events (
    job_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    ts      REAL NOT NULL,
    type    TEXT NOT NULL,     -- status|assistant|thinking|tool_use|tool_result|result|error|log
    data    TEXT NOT NULL,     -- JSON
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, seq);
"""

TERMINAL = {"succeeded", "failed", "canceled"}


class Database:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a db was first created."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        if "files" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN files TEXT")

    # ---- jobs -----------------------------------------------------------
    def create_job(
        self,
        *,
        agent: str,
        prompt: str,
        cwd: str | None,
        requested_session: str | None,
        permission_mode: str | None,
        model: str | None,
    ) -> str:
        job_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, status, agent, prompt, cwd, requested_session,"
                " permission_mode, model, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, "queued", agent, prompt, cwd, requested_session,
                 permission_mode, model, time.time()),
            )
            self._conn.commit()
        return job_id

    def set_job_files(self, job_id: str, files: list[str]) -> None:
        self._update(job_id, files=json.dumps(files))

    def mark_running(self, job_id: str) -> None:
        self._update(job_id, status="running", started_at=time.time())

    def finish_job(self, job_id: str, **fields: Any) -> None:
        fields.setdefault("finished_at", time.time())
        self._update(job_id, **fields)

    def _update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {cols} WHERE id=?",
                (*fields.values(), job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- events ---------------------------------------------------------
    def add_event(self, job_id: str, seq: int, etype: str, data: dict) -> dict:
        row = {
            "job_id": job_id,
            "seq": seq,
            "ts": time.time(),
            "type": etype,
            "data": data,
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (job_id, seq, ts, type, data) VALUES (?,?,?,?,?)",
                (job_id, seq, row["ts"], etype, json.dumps(data)),
            )
            self._conn.commit()
        return row

    def events_after(self, job_id: str, after_seq: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, type, data FROM events WHERE job_id=? AND seq>?"
                " ORDER BY seq",
                (job_id, after_seq),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"])
            d["job_id"] = job_id
            out.append(d)
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
