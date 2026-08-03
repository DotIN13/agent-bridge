"""SQLite persistence for jobs and their event logs.

A single connection guarded by one lock (WAL mode). All writes and the short
backlog reads used by SSE go through it; SSE then leaves the DB alone and tails
the in-memory Bus, so the lock is held only briefly.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

# Seq bands keep the three writers from ever colliding on (job_id, seq).
# Worker events count up from 1; messages live far above anything a run will
# reach. File-ingested seqs derive from line number, which makes re-reading a
# file a no-op under INSERT OR IGNORE — no cursor, no offset bookkeeping.
MSG_SEQ_HTTP_BASE = 1_000_000
MSG_SEQ_FILE_BASE = 2_000_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,          -- queued|running|succeeded|failed|canceled
    agent             TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    title             TEXT,                   -- human handle, shown in listings
    title_norm        TEXT,                   -- folded form used for lookup
    fork              INTEGER,                -- 1 fork a session, 0 resume in place
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


def norm_title(title: str) -> str:
    """Fold a title into a lookup key: lowercase, runs of non-alphanumerics
    collapsed to '-'. So `--title "Rebuild corpora"` can later be addressed as
    `rebuild-corpora`, which is what actually gets typed on a command line."""
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")


def derive_title(prompt: str, limit: int = 60) -> str:
    """A title from the first meaningful line of the prompt.

    Auto-derived so every job has a handle even when the caller sets none —
    otherwise the feature only helps people who remember to use it. Leading
    markdown hashes are stripped, since prompts here usually open with a
    heading.
    """
    for line in (prompt or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:limit].rstrip()
    return ""


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
        for name, decl in (("files", "TEXT"), ("title", "TEXT"),
                           ("title_norm", "TEXT"), ("fork", "INTEGER")):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title_norm)")
        self._backfill_titles()

    def _backfill_titles(self) -> None:
        """Give pre-existing rows a title too, so `ab jobs` isn't half blank
        and old jobs stay addressable by name."""
        rows = self._conn.execute(
            "SELECT id, prompt FROM jobs WHERE title IS NULL OR title=''"
        ).fetchall()
        for r in rows:
            t = derive_title(r["prompt"])
            self._conn.execute(
                "UPDATE jobs SET title=?, title_norm=? WHERE id=?",
                (t, norm_title(t), r["id"]))

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
        title: str | None = None,
        fork: bool = True,
    ) -> str:
        job_id = str(uuid.uuid4())
        title = (title or "").strip() or derive_title(prompt)
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, status, agent, prompt, cwd, requested_session,"
                " permission_mode, model, title, title_norm, fork, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, "queued", agent, prompt, cwd, requested_session,
                 permission_mode, model, title, norm_title(title),
                 1 if fork else 0, time.time()),
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

    # `last_event_at` is derived rather than stored. A batch job keeps sending
    # ab-notify messages long after `finished_at` — which only marks the end of
    # the *agent's turn*, not the end of the work — so the row needs a "last
    # heard from" that keeps moving. Computing it on read costs one indexed
    # MAX() per row and avoids an UPDATE on every single event.
    _SELECT_JOB = ("SELECT j.*, (SELECT MAX(ts) FROM events e WHERE e.job_id = j.id)"
                   " AS last_event_at FROM jobs j")

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"{self._SELECT_JOB} WHERE j.id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_jobs_by_title(self, title: str, limit: int = 10) -> list[dict]:
        """Jobs whose title matches `title` after folding, newest first.

        Titles are not unique — the same task resubmitted keeps its name — so
        this can legitimately return several and the caller must disambiguate
        rather than guess.
        """
        key = norm_title(title)
        if not key:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"{self._SELECT_JOB} WHERE j.title_norm=?"
                # rowid breaks created_at ties: two jobs submitted in the same
                # clock tick must still order deterministically.
                " ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?",
                (key, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def find_jobs_by_prefix(self, prefix: str, limit: int = 10) -> list[dict]:
        """Jobs whose id starts with `prefix`, newest first.

        Uses substr() rather than LIKE so `%` and `_` in caller input stay
        literal instead of turning into wildcards.
        """
        if not prefix:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"{self._SELECT_JOB} WHERE substr(j.id, 1, ?) = ?"
                # rowid breaks created_at ties: two jobs submitted in the same
                # clock tick must still order deterministically.
                " ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?",
                (len(prefix), prefix, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"{self._SELECT_JOB} ORDER BY j.created_at DESC, j.rowid DESC"
                " LIMIT ?", (limit,)
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

    # ---- messages from batch jobs ---------------------------------------
    # A job's compute-node script reports its own lifecycle with `ab-notify`.
    # Preferred path is HTTP (immediate, and publishes to the Bus so SSE sees
    # it); a shared-filesystem JSONL is the fallback for when the gateway is
    # unreachable from the node.
    #
    # Batch jobs cannot write this DB directly: it runs in WAL mode, whose
    # index is mmap'd shared memory and therefore requires every writer on one
    # host. Appending short lines with O_APPEND needs no locking at all.

    def add_message(self, job_id: str, data: dict) -> dict:
        """Record a message posted over HTTP; returns the event row."""
        ts = float(data.get("ts") or time.time())
        with self._lock:
            top = self._conn.execute(
                "SELECT COALESCE(MAX(seq), ?) FROM events"
                " WHERE job_id=? AND seq >= ? AND seq < ?",
                (MSG_SEQ_HTTP_BASE - 1, job_id,
                 MSG_SEQ_HTTP_BASE, MSG_SEQ_FILE_BASE),
            ).fetchone()[0]
            seq = top + 1
            self._conn.execute(
                "INSERT INTO events (job_id, seq, ts, type, data)"
                " VALUES (?,?,?,?,?)",
                (job_id, seq, ts, "message", json.dumps(data)),
            )
            self._conn.commit()
        return {"job_id": job_id, "seq": seq, "ts": ts,
                "type": "message", "data": data}

    def ingest_messages(self, job_id: str, messages_dir: str) -> int:
        """Fold any `ab-notify` fallback lines for this job into events.

        Idempotent: seq is derived from line number, so re-reading inserts
        nothing new.
        """
        path = os.path.join(messages_dir, f"{job_id}.jsonl")
        try:
            with open(path, encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        except OSError:
            return 0
        added = 0
        with self._lock:
            for i, ln in enumerate(lines):
                try:
                    data = json.loads(ln)
                except ValueError:
                    data = {"status": "unknown", "raw": ln[:2000]}
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO events (job_id, seq, ts, type, data)"
                    " VALUES (?,?,?,?,?)",
                    (job_id, MSG_SEQ_FILE_BASE + i,
                     float(data.get("ts") or time.time()),
                     "message", json.dumps(data)),
                )
                added += cur.rowcount or 0
            if added:
                self._conn.commit()
        return added

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
