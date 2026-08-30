"""SQLite persistence for jobs and their event logs.

One lock protects the connection.  Every event source uses the same per-job
allocator, so an `after` cursor is globally monotonic even across worker events,
job-dir reports, HTTP reports, monitor transitions, failures, and restart
recovery.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid

import datetime

from . import jobdir
from .api_models import iso_local
from typing import Any


def _normalise_report(data: dict) -> tuple[dict, float]:
    """Return the payload with an ISO `ts`, plus the epoch to timestamp it with.

    An HTTP reporter may send an epoch float, and a report is otherwise the one
    place a bare epoch still reaches a reader -- hidden inside a passthrough dict
    the response models never touch. The event's own timestamp still needs the
    number, so both come back rather than the caller re-deriving one from the
    other: doing that is what broke the `--report-id` path, which parsed a `ts`
    another line had already rewritten to a string.

    Idempotent, and applied before hashing by both paths that produce a
    `message`, so a report keeps one dedup identity whether it arrives over HTTP
    or twice.
    """
    raw = data.get("ts")
    if isinstance(raw, bool) or raw is None:
        return data, time.time()
    if isinstance(raw, (int, float)):
        epoch = float(raw)
        return {**data, "ts": iso_local(epoch)}, epoch
    if isinstance(raw, str):
        try:                                  # already normalised; leave it alone
            return data, datetime.datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return data, time.time()
    return data, time.time()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    agent             TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    title             TEXT,
    title_norm        TEXT,
    fork              INTEGER,
    include_thinking  INTEGER,
    cwd               TEXT,
    session           TEXT,
    permission_mode   TEXT,
    model             TEXT,
    files             TEXT,
    result            TEXT,
    error             TEXT,
    cost_usd          REAL,
    created_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL,
    expect_report     INTEGER,
    report_deadline   REAL
);

CREATE TABLE IF NOT EXISTS events (
    job_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    ts      REAL NOT NULL,
    type    TEXT NOT NULL,
    data    TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, seq);

CREATE TABLE IF NOT EXISTS event_counters (
    job_id   TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS external_reports (
    job_id      TEXT NOT NULL,
    source      TEXT NOT NULL,
    report_id   TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    PRIMARY KEY (job_id, source, report_id)
);

CREATE TABLE IF NOT EXISTS monitors (
    id           TEXT PRIMARY KEY,
    job_id       TEXT,
    label        TEXT,
    poll_cmd     TEXT NOT NULL,
    map_spec     TEXT,
    interval_sec REAL NOT NULL,
    deadline     REAL,
    status       TEXT NOT NULL,
    detail       TEXT,
    result_paths TEXT,
    note         TEXT,
    created_at   REAL NOT NULL,
    last_poll_at REAL,
    next_poll_at REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_monitors_due ON monitors(status, next_poll_at);
CREATE INDEX IF NOT EXISTS idx_monitors_job ON monitors(job_id, created_at);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope         TEXT NOT NULL,
    key           TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at    REAL NOT NULL,
    PRIMARY KEY (scope, key)
);
"""

TERMINAL = {"succeeded", "failed", "canceled"}
#: Terminal for a *monitor*, which is not a job: `expired` says we stopped
#: watching, which is a different claim from the work having failed.
MONITOR_TERMINAL = {"finished", "failed", "expired", "canceled"}
# Non-terminal, and deliberately not "running": the turn is over and the
# agent process is gone, so everything that reads "running" as "an agent is
# alive" -- steering, the session-busy gate, restart recovery -- has to be
# able to tell the difference. The row stays open because the work it
# describes is still out there on a scheduler.
AWAITING_REPORT = "awaiting_report"


class IdempotencyConflict(ValueError):
    pass


class ReportConflict(ValueError):
    pass


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")


def derive_title(prompt: str, limit: int = 60) -> str:
    for line in (prompt or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:limit].rstrip()
    return ""


def _decode_files(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _decode_job(row, *, detail: bool = True) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out.pop("title_norm", None)
    out.pop("_rowid", None)
    # Dead since the session columns were collapsed, and still on disk in any
    # database old enough to have them.
    for legacy in ("requested_session", "chosen_session", "forked_session"):
        out.pop(legacy, None)
    out["fork"] = True if out.get("fork") is None else bool(out["fork"])
    out["include_thinking"] = bool(out.get("include_thinking"))
    out["files"] = _decode_files(out.get("files"))
    if not detail:
        for key in ("prompt", "permission_mode", "files", "result", "error"):
            out.pop(key, None)
    return out


def _decode_monitor(row) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out.pop("_rowid", None)
    out["result_paths"] = _decode_files(out.get("result_paths"))
    return out


def _cursor_encode(created_at: float, rowid: int) -> str:
    raw = json.dumps([created_at, rowid], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(cursor: str) -> tuple[float, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created, rowid = json.loads(raw)
        return float(created), int(rowid)
    except Exception as e:
        raise ValueError("invalid cursor") from e


class Database:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate_locked()
            self._conn.commit()

    def _migrate_locked(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        self._migrate_sessions_locked(cols)
        for name, decl in (("files", "TEXT"), ("title", "TEXT"),
                           ("title_norm", "TEXT"), ("fork", "INTEGER"),
                           ("include_thinking", "INTEGER"),
                           ("expect_report", "INTEGER"),
                           ("report_deadline", "REAL")):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title_norm)")
        self._backfill_titles_locked()
        # Existing sequence bands are retained. New allocation starts above the
        # historical maximum, preserving every cursor already held by a client.
        self._conn.execute(
            "INSERT INTO event_counters(job_id,next_seq) "
            "SELECT job_id,MAX(seq)+1 FROM events GROUP BY job_id "
            "ON CONFLICT(job_id) DO UPDATE SET next_seq="
            "MAX(event_counters.next_seq,excluded.next_seq)")

    def _migrate_sessions_locked(self, cols: set[str]) -> None:
        """Three session columns become one.

        `requested_session`, `chosen_session` and `forked_session` date from the
        dispatcher modes, where an agent chose which session to use and the
        three were genuinely different things. Under `direct` dispatch only one
        of them is ever written -- across a hundred real jobs on one gateway,
        the other two had never held a value -- and callers had to be told which
        of the three to read. Now there is one: what the caller asked for,
        overwritten by what the run actually used.

        The old columns are left where they are rather than dropped. `DROP
        COLUMN` wants a recent SQLite and cannot be undone on a login node at
        two in the morning; ignoring three dead columns costs nothing, and
        `_decode_job` keeps them out of the API either way.
        """
        if "session" in cols:
            return
        self._conn.execute("ALTER TABLE jobs ADD COLUMN session TEXT")
        legacy = [name for name in
                  ("forked_session", "chosen_session", "requested_session")
                  if name in cols]
        if legacy:
            # In that order: the id a run created wins over the one an agent
            # picked, which wins over the one the caller asked for.
            self._conn.execute(
                f"UPDATE jobs SET session = COALESCE({', '.join(legacy)})")

    def _backfill_titles_locked(self) -> None:
        rows = self._conn.execute(
            "SELECT id,prompt FROM jobs WHERE title IS NULL OR title=''"
        ).fetchall()
        for row in rows:
            title = derive_title(row["prompt"])
            self._conn.execute(
                "UPDATE jobs SET title=?,title_norm=? WHERE id=?",
                (title, norm_title(title), row["id"]))

    # ---- jobs -----------------------------------------------------------
    def _insert_job_locked(self, *, job_id: str, agent: str, prompt: str,
                           cwd: str | None, session: str | None,
                           permission_mode: str | None, model: str | None,
                           title: str | None, fork: bool,
                           include_thinking: bool, files: list[str] | None,
                           expect_report: bool = False) -> None:
        title = (title or "").strip() or derive_title(prompt)
        self._conn.execute(
            "INSERT INTO jobs (id,status,agent,prompt,cwd,session,"
            "permission_mode,model,title,title_norm,fork,include_thinking,files,"
            "expect_report,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, "queued", agent, prompt, cwd, session,
             permission_mode, model, title, norm_title(title),
             1 if fork else 0, 1 if include_thinking else 0,
             json.dumps(files or []), 1 if expect_report else 0, time.time()))

    def create_job(self, *, agent: str, prompt: str, cwd: str | None,
                   session: str | None, permission_mode: str | None,
                   model: str | None, title: str | None = None, fork: bool = True,
                   include_thinking: bool = False, files: list[str] | None = None,
                   expect_report: bool = False,
                   job_id: str | None = None) -> str:
        job_id = job_id or str(uuid.uuid4())
        with self._lock:
            self._insert_job_locked(
                job_id=job_id, agent=agent, prompt=prompt, cwd=cwd,
                session=session,
                permission_mode=permission_mode, model=model, title=title,
                fork=fork, include_thinking=include_thinking, files=files,
                expect_report=expect_report)
            self._conn.commit()
        return job_id

    def idempotency_lookup(self, scope: str, key: str,
                           request_hash: str) -> tuple[int, dict] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT request_hash,status_code,response_json FROM idempotency_keys "
                "WHERE scope=? AND key=?", (scope, key)).fetchone()
        if not row:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict("idempotency key was already used with a different request")
        return int(row["status_code"]), json.loads(row["response_json"])

    def create_job_idempotent(self, *, scope: str, key: str,
                              request_hash: str, response: dict,
                              job: dict) -> tuple[dict, bool]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT request_hash,response_json FROM idempotency_keys "
                    "WHERE scope=? AND key=?", (scope, key)).fetchone()
                if row:
                    if row["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "idempotency key was already used with a different request")
                    self._conn.commit()
                    replay = json.loads(row["response_json"])
                    replay["replayed"] = True
                    return replay, False
                self._insert_job_locked(**job)
                self._conn.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_hash,status_code,"
                    "response_json,created_at) VALUES (?,?,?,?,?,?)",
                    (scope, key, request_hash, 202,
                     json.dumps(response, separators=(",", ":")), time.time()))
                self._conn.commit()
                return response, True
            except Exception:
                self._conn.rollback()
                raise

    def set_job_files(self, job_id: str, files: list[str]) -> None:
        self._update(job_id, files=json.dumps(files))

    def start_queued_job(self, job_id: str, event_data: dict) -> dict | None:
        """Atomically win queued -> running and append its visible event.

        Cancellation uses the competing queued -> canceled transition. Exactly
        one can update the row, so a late worker can never revive a canceled
        job.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    "UPDATE jobs SET status='running',started_at=? "
                    "WHERE id=? AND status='queued'", (time.time(), job_id))
                if cursor.rowcount != 1:
                    self._conn.commit()
                    return None
                row = self._append_event_locked(
                    job_id, "status", event_data)
                self._conn.commit()
                return row
            except Exception:
                self._conn.rollback()
                raise

    def cancel_queued_job(self, job_id: str) -> dict | None:
        """Atomically cancel a queued job and append its terminal event."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                cursor = self._conn.execute(
                    "UPDATE jobs SET status='canceled',error=?,finished_at=? "
                    "WHERE id=? AND status='queued'",
                    ("canceled before start", now, job_id))
                if cursor.rowcount != 1:
                    self._conn.commit()
                    return None
                row = self._append_event_locked(
                    job_id, "status",
                    {"stage": "done", "status": "canceled"}, now)
                self._conn.commit()
                return row
            except Exception:
                self._conn.rollback()
                raise

    def finish_job_with_events(self, job_id: str, fields: dict[str, Any],
                               events: list[tuple[str, dict]],
                               report_timeout_sec: float = 0.0) -> list[dict]:
        """Publish terminal fields and their final events in one transaction.

        A job that declared `expect_report` and whose turn *succeeded* parks in
        `awaiting_report` instead: the turn is done but the work it started is
        not, and the row is what `ab wait` blocks on. `finished_at` stays null,
        because the job has not finished.

        Only a successful turn parks. If the turn failed or was canceled there is
        no reason to believe anything was submitted, and waiting for a report
        that nobody will send is worse than reporting the failure now.
        """
        values = dict(fields)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT expect_report FROM jobs WHERE id=?", (job_id,)).fetchone()
                parking = bool(row and row["expect_report"]
                               and values.get("status") == "succeeded")
                if parking:
                    values["status"] = AWAITING_REPORT
                    values["finished_at"] = None
                    if report_timeout_sec > 0:
                        values["report_deadline"] = time.time() + report_timeout_sec
                    events = [*events, ("status", {
                        "stage": "awaiting_report",
                        "status": AWAITING_REPORT,
                        "detail": "turn complete; waiting for ab-notify "
                                  "--status finished|failed"})]
                else:
                    values.setdefault("finished_at", time.time())
                columns = ", ".join(f"{key}=?" for key in values)
                cursor = self._conn.execute(
                    f"UPDATE jobs SET {columns} WHERE id=? "
                    "AND status NOT IN ('succeeded','failed','canceled')",
                    (*values.values(), job_id))
                if cursor.rowcount != 1:
                    self._conn.commit()
                    return []
                rows = [self._append_event_locked(job_id, kind, data)
                        for kind, data in events]
                self._conn.commit()
                return rows
            except Exception:
                self._conn.rollback()
                raise

    def set_job_session(self, job_id: str, session_id: str) -> None:
        """The session a run is using, the moment it says so.

        The terminal write records it too, but only at the end -- so a running
        job, which is the one anybody is actually watching, had an empty session
        for its whole life, and a run that died on an exception took the id with
        it.
        """
        self._update(job_id, session=session_id)

    def mark_running(self, job_id: str) -> None:
        self._update(job_id, status="running", started_at=time.time())

    def finish_job(self, job_id: str, **fields: Any) -> None:
        fields.setdefault("finished_at", time.time())
        self._update(job_id, **fields)

    def _update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{key}=?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
            self._conn.commit()

    _SELECT_JOB = (
        "SELECT j.*,j.rowid AS _rowid,"
        "(SELECT MAX(ts) FROM events e WHERE e.job_id=j.id) AS last_event_at "
        "FROM jobs j")

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"{self._SELECT_JOB} WHERE j.id=?", (job_id,)).fetchone()
        return _decode_job(row, detail=True)

    def find_jobs_by_title(self, title: str, limit: int = 10) -> list[dict]:
        key = norm_title(title)
        if not key:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"{self._SELECT_JOB} WHERE j.title_norm=? "
                "ORDER BY j.created_at DESC,j.rowid DESC LIMIT ?",
                (key, limit)).fetchall()
        return [_decode_job(row, detail=True) for row in rows]

    def find_jobs_by_prefix(self, prefix: str, limit: int = 10) -> list[dict]:
        if not prefix:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"{self._SELECT_JOB} WHERE substr(j.id,1,?)=? "
                "ORDER BY j.created_at DESC,j.rowid DESC LIMIT ?",
                (len(prefix), prefix, limit)).fetchall()
        return [_decode_job(row, detail=True) for row in rows]

    def list_jobs_page(self, limit: int = 50, cursor: str | None = None
                       ) -> tuple[list[dict], str | None, bool]:
        if not 1 <= int(limit) <= 200:
            raise ValueError("limit must be between 1 and 200")
        where = ""
        args: list[Any] = []
        if cursor:
            created, rowid = _cursor_decode(cursor)
            where = "WHERE (j.created_at < ? OR (j.created_at=? AND j.rowid<?))"
            args += [created, created, rowid]
        args.append(limit + 1)
        with self._lock:
            rows = self._conn.execute(
                f"{self._SELECT_JOB} {where} "
                "ORDER BY j.created_at DESC,j.rowid DESC LIMIT ?", args).fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _cursor_encode(last["created_at"], last["_rowid"])
        return ([_decode_job(row, detail=False) for row in visible],
                next_cursor, has_more)

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return self.list_jobs_page(limit)[0]

    def queued_job_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE status='queued' "
                "ORDER BY created_at,rowid").fetchall()
        return [row["id"] for row in rows]

    def job_ids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM jobs").fetchall()
        return {row["id"] for row in rows}

    # ---- events ---------------------------------------------------------
    def _allocate_seq_locked(self, job_id: str) -> int:
        row = self._conn.execute(
            "SELECT next_seq FROM event_counters WHERE job_id=?", (job_id,)).fetchone()
        if row:
            seq = int(row["next_seq"])
            self._conn.execute(
                "UPDATE event_counters SET next_seq=? WHERE job_id=?", (seq + 1, job_id))
            return seq
        maximum = self._conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM events WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        seq = int(maximum) + 1
        self._conn.execute(
            "INSERT INTO event_counters(job_id,next_seq) VALUES (?,?)",
            (job_id, seq + 1))
        return seq

    def _append_event_locked(self, job_id: str, etype: str, data: dict,
                             ts: float | None = None) -> dict:
        seq = self._allocate_seq_locked(job_id)
        row = {"job_id": job_id, "seq": seq, "ts": float(ts or time.time()),
               "type": etype, "data": data}
        self._conn.execute(
            "INSERT INTO events(job_id,seq,ts,type,data) VALUES (?,?,?,?,?)",
            (job_id, seq, row["ts"], etype,
             json.dumps(data, separators=(",", ":"))))
        return row

    def append_event(self, job_id: str, etype: str, data: dict,
                     ts: float | None = None) -> dict:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._append_event_locked(job_id, etype, data, ts)
                self._conn.commit()
                return row
            except Exception:
                self._conn.rollback()
                raise

    def add_event(self, job_id: str, seq: int, etype: str, data: dict) -> dict:
        """Compatibility wrapper; caller sequence is intentionally ignored."""
        return self.append_event(job_id, etype, data)

    def event_bounds(self, job_id: str) -> dict:
        """Total count and extent of a job's log.

        Without this a caller cannot tell how far it is from the end, which is
        why reading a long log meant paging forward blind. `first_ts` is here
        too so the events route can stamp each record's elapsed time from one
        query rather than re-reading event #1.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, MIN(seq) AS first_seq, "
                "MAX(seq) AS last_seq, MIN(ts) AS first_ts "
                "FROM events WHERE job_id=?", (job_id,)).fetchone()
        return {"total": int(row["total"] or 0),
                "first_seq": row["first_seq"], "last_seq": row["last_seq"],
                "first_ts": row["first_ts"]}

    def events_tail(self, job_id: str, limit: int, *, until_seq: int | None = None,
                    types: tuple[str, ...] = ()) -> list[dict]:
        """The last `limit` events, oldest-first.

        Selected descending then reversed, so the returned page is in the same
        chronological order as `events_after` — the response shape must not
        depend on which end the caller read from.

        `types` filters *inside* the limit. Applying it afterwards would make
        `--type result --tail 5` return nothing on any long job, because the
        last five events are rarely all results.
        """
        if not 1 <= int(limit) <= 1001:
            raise ValueError("limit must be between 1 and 1001")
        sql = "SELECT seq,ts,type,data FROM events WHERE job_id=?"
        params: list = [job_id]
        if until_seq is not None:
            sql += " AND seq<=?"
            params.append(int(until_seq))
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            params.extend(types)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for row in reversed(rows):
            item = dict(row)
            item["data"] = json.loads(item["data"])
            item["job_id"] = job_id
            out.append(item)
        return out

    def events_after(self, job_id: str, after_seq: int,
                     limit: int = 500) -> list[dict]:
        if after_seq < 0:
            raise ValueError("after must be non-negative")
        # The HTTP layer allows 1000 and asks for one look-ahead row.
        if not 1 <= int(limit) <= 1001:
            raise ValueError("limit must be between 1 and 1001")
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,ts,type,data FROM events WHERE job_id=? AND seq>? "
                "ORDER BY seq LIMIT ?", (job_id, after_seq, limit)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item["data"])
            item["job_id"] = job_id
            out.append(item)
        return out

    def _report_locked(self, job_id: str, source: str, report_id: str,
                       request_hash: str, data: dict,
                       epoch: float) -> tuple[dict, bool]:
        existing = self._conn.execute(
            "SELECT request_hash,seq FROM external_reports "
            "WHERE job_id=? AND source=? AND report_id=?",
            (job_id, source, report_id)).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise ReportConflict("report id was already used with different content")
            event = self._conn.execute(
                "SELECT seq,ts,type,data FROM events WHERE job_id=? AND seq=?",
                (job_id, existing["seq"])).fetchone()
            row = dict(event)
            row["data"] = json.loads(row["data"])
            row["job_id"] = job_id
            return row, True
        row = self._append_event_locked(job_id, "message", data, epoch)
        self._conn.execute(
            "INSERT INTO external_reports(job_id,source,report_id,request_hash,seq) "
            "VALUES (?,?,?,?,?)", (job_id, source, report_id, request_hash, row["seq"]))
        return row, False

    def _close_awaiting_locked(self, job_id: str, data: dict, epoch: float,
                               *, parked_only: bool = False) -> dict | None:
        """A terminal report closes the job. Anything else leaves it open.

        Only `finished` and `failed` are terminal; `running`, `queued` and
        `unknown` are progress. A terminal report is honoured whether the job
        is still `running` (the finish arrived mid-turn) or parked in
        `awaiting_report`; an already-terminal job is left alone so `wait` and
        SSE keep their monotonic status.

        `parked_only` is for the job dir. A file sitting in a directory is not
        the same promise as a call placed while the work was running: the
        delegate may write `finished` about a step, then keep working, and
        ending its row underneath it would strand a live agent. So a job dir
        can close a row that is *waiting* for a report and cannot end a turn
        that is still going -- the turn's own end does that.
        """
        status = data.get("status")
        if status not in ("finished", "failed"):
            return None
        allowed = (AWAITING_REPORT,) if parked_only else ("running", AWAITING_REPORT)
        row = self._conn.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] not in allowed:
            return None
        final = "succeeded" if status == "finished" else "failed"
        error = None
        if final == "failed":
            error = data.get("msg") or "batch work reported failed"
        self._conn.execute(
            "UPDATE jobs SET status=?,finished_at=?,report_deadline=NULL,"
            "error=COALESCE(error,?) WHERE id=?",
            (final, epoch, error, job_id))
        return self._append_event_locked(
            job_id, "status",
            {"stage": "done", "status": final, "reason": "batch_report"}, epoch)

    def expire_awaiting_reports(self, now: float | None = None) -> list[str]:
        """Fail parked jobs whose report never came.

        Without this a scheduler job that dies silently leaves its row open
        forever, and `ab wait` blocks on something nobody will ever send. The
        deadline is set when the job parks.
        """
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT id FROM jobs WHERE status=? AND report_deadline IS NOT NULL "
                    "AND report_deadline <= ?", (AWAITING_REPORT, now)).fetchall()
                expired = [r["id"] for r in rows]
                for jid in expired:
                    message = ("no ab-notify report arrived before the deadline; "
                               "the batch work may still be running")
                    self._conn.execute(
                        "UPDATE jobs SET status='failed',error=?,finished_at=?,"
                        "report_deadline=NULL WHERE id=?", (message, now, jid))
                    self._append_event_locked(
                        jid, "error",
                        {"code": "report_timeout", "message": message}, now)
                    self._append_event_locked(
                        jid, "status", {"stage": "done", "status": "failed",
                                        "reason": "report_timeout"}, now)
                self._conn.commit()
                return expired
            except Exception:
                self._conn.rollback()
                raise

    def add_message(self, job_id: str, data: dict,
                    report_id: str | None = None) -> dict:
        report_id = report_id or data.get("report_id")
        data, epoch = _normalise_report(data)
        request_hash = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if report_id:
                    row, duplicate = self._report_locked(
                        job_id, "report", str(report_id), request_hash, data, epoch)
                else:
                    row = self._append_event_locked(
                        job_id, "message", data, epoch)
                    duplicate = False
                closing = None
                if not duplicate:
                    closing = self._close_awaiting_locked(job_id, data, epoch)
                self._conn.commit()
                row["duplicate"] = duplicate
                # The caller has to publish this and close the stream: a
                # follower watching a parked job learns it ended here, not from
                # the worker, which stopped talking when the turn did.
                row["closing_event"] = closing
                return row
            except Exception:
                self._conn.rollback()
                raise

    def ingest_job_dir(self, job_id: str, job_dir: str) -> list[dict]:
        """Turn every new file in a job's directory into one `message` event.

        Dedup identity is the relative path *and* the content digest, which is
        what makes rewriting a file useful rather than either duplicated or
        ignored: `status` going `running` -> `finished` is two drops, writing
        the same milestone twice is one. That is also why this cannot reuse
        `report_id` semantics, where a changed body under a used id is a
        conflict.

        Returns the event rows it inserted, in order, so a caller holding the
        bus can publish them to a live follower. A row that ended a parked job
        is flagged `closing`; the caller closes the stream on it, exactly as the
        HTTP report path does.

        Cheap enough to call on every read: `jobdir.scan` stats a fixed set of
        names and one directory, and an unchanged dir inserts nothing.
        """
        drops = jobdir.scan(job_dir)
        if not drops:
            return []
        # A terminal `status` carries the report's text as its reason when both
        # were written before this scan, which is the usual order.
        reason = next((d.text for d in drops if d.rel == jobdir.REPORT_FILE), "")
        rows: list[dict] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for drop in drops:
                    data, epoch = _normalise_report(
                        jobdir.event_data(drop, reason))
                    request_hash = hashlib.sha256(json.dumps(
                        data, sort_keys=True, separators=(",", ":")
                    ).encode()).hexdigest()
                    key = f"{drop.rel}:{drop.digest}"
                    try:
                        row, duplicate = self._report_locked(
                            job_id, "job_dir", key, request_hash, data, epoch)
                    except ReportConflict:      # same path+content, other ts
                        continue
                    if duplicate:
                        continue
                    rows.append(row)
                    closing = self._close_awaiting_locked(
                        job_id, data, epoch, parked_only=True)
                    if closing:
                        closing["closing"] = True
                        rows.append(closing)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return rows

    def jobs_with_open_dirs(self, now: float | None = None,
                            grace_sec: float = 900.0) -> list[dict]:
        """Jobs whose directory is still worth scanning, newest first.

        A live follower has to learn about a milestone drop without
        reconnecting, and a parked job has to be closeable by a file that
        arrives after everyone stopped reading -- so the sweeper needs a bounded
        list of candidates rather than the whole table.

        Recently-finished jobs are included, and that is not tidiness: the
        delegate registers a monitor as the last thing it does, so the drop
        routinely lands in the same seconds the turn is ending. Watching only
        live rows lost exactly the watches that matter, which an end-to-end run
        found and no unit test would have.
        """
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE status IN ('running',?) "
                "OR (finished_at IS NOT NULL AND finished_at >= ?) "
                "ORDER BY created_at DESC LIMIT 200",
                (AWAITING_REPORT, now - grace_sec)).fetchall()
        return [dict(row) for row in rows]

    # -- monitors ---------------------------------------------------------
    def create_monitor(self, *, monitor_id: str, job_id: str | None,
                       poll_cmd: str, interval_sec: float,
                       deadline: float | None = None, label: str = "",
                       map_spec: str = "", note: str = "",
                       result_paths: list[str] | None = None,
                       now: float | None = None) -> dict | None:
        """Register a watch, or return None if that id already exists.

        Idempotent by id, and that is load-bearing rather than defensive: a
        monitor dropped as a file in the job dir is re-read on every sweep, so
        "already registered" is the normal outcome of the second scan and must
        not be an error, a duplicate row, or a reset deadline.
        """
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                exists = self._conn.execute(
                    "SELECT 1 FROM monitors WHERE id=?", (monitor_id,)).fetchone()
                if exists:
                    self._conn.commit()
                    return None
                self._conn.execute(
                    "INSERT INTO monitors(id,job_id,label,poll_cmd,map_spec,"
                    "interval_sec,deadline,status,detail,result_paths,note,"
                    "created_at,next_poll_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (monitor_id, job_id, label, poll_cmd, map_spec,
                     float(interval_sec), deadline, "queued", None,
                     json.dumps(result_paths or []), note, now, now))
                row = self._monitor_locked(monitor_id)
                self._conn.commit()
                return row
            except Exception:
                self._conn.rollback()
                raise

    def _monitor_locked(self, monitor_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        return _decode_monitor(row)

    def monitor(self, monitor_id: str) -> dict | None:
        with self._lock:
            return self._monitor_locked(monitor_id)

    def list_monitors(self, *, job_id: str | None = None,
                      status: str | None = None, active: bool | None = None,
                      limit: int = 50,
                      cursor: str | None = None) -> tuple[list[dict], str | None, bool]:
        """One page, newest first, with the same opaque cursor jobs use."""
        if not 1 <= int(limit) <= 200:
            raise ValueError("limit must be between 1 and 200")
        where, params = ["1=1"], []
        if job_id:
            where.append("job_id=?")
            params.append(job_id)
        if status:
            where.append("status=?")
            params.append(status)
        if active is not None:
            placeholders = ",".join("?" for _ in MONITOR_TERMINAL)
            where.append(f"status {'NOT IN' if active else 'IN'} ({placeholders})")
            params += sorted(MONITOR_TERMINAL)
        if cursor:
            created, rowid = _cursor_decode(cursor)
            where.append("(created_at < ? OR (created_at = ? AND rowid < ?))")
            params += [created, created, rowid]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT rowid AS _rowid,* FROM monitors WHERE {' AND '.join(where)} "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*params, limit + 1)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _cursor_encode(last["created_at"], last["_rowid"])
        return [_decode_monitor(row) for row in rows], next_cursor, has_more

    def count_active_monitors(self) -> int:
        placeholders = ",".join("?" for _ in MONITOR_TERMINAL)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM monitors "
                f"WHERE status NOT IN ({placeholders})",
                tuple(sorted(MONITOR_TERMINAL))).fetchone()
        return int(row["n"])

    def due_monitors(self, now: float | None = None,
                     limit: int = 50) -> list[dict]:
        now = time.time() if now is None else now
        placeholders = ",".join("?" for _ in MONITOR_TERMINAL)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM monitors WHERE status NOT IN ({placeholders}) "
                f"AND (next_poll_at IS NULL OR next_poll_at <= ?) "
                f"ORDER BY next_poll_at LIMIT ?",
                (*sorted(MONITOR_TERMINAL), now, limit)).fetchall()
        return [_decode_monitor(row) for row in rows]

    def record_poll(self, monitor_id: str, status: str, detail: str,
                    now: float | None = None) -> dict | None:
        """Store one poll's outcome. Returns the row when the status *changed*.

        Returning only transitions is what keeps a five-second sweep from
        writing an event every five seconds for eight hours: a caller wants to
        know that the run started and that it ended, not that it is still going.
        `unknown` never transitions -- a failed poll is not news about the work.
        """
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._monitor_locked(monitor_id)
                if row is None or row["status"] in MONITOR_TERMINAL:
                    self._conn.commit()
                    return None
                changed = status != "unknown" and status != row["status"]
                final = status if status in MONITOR_TERMINAL else None
                self._conn.execute(
                    "UPDATE monitors SET status=?,detail=?,last_poll_at=?,"
                    "next_poll_at=?,finished_at=? WHERE id=?",
                    (status if status != "unknown" else row["status"],
                     detail, now, now + row["interval_sec"],
                     now if final else None, monitor_id))
                updated = self._monitor_locked(monitor_id)
                self._conn.commit()
                return updated if changed else None
            except Exception:
                self._conn.rollback()
                raise

    def close_monitor(self, monitor_id: str, status: str, detail: str = "",
                      now: float | None = None) -> dict | None:
        """End a monitor from outside a poll: cancel, or a passed deadline."""
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._monitor_locked(monitor_id)
                if row is None or row["status"] in MONITOR_TERMINAL:
                    self._conn.commit()
                    return None
                self._conn.execute(
                    "UPDATE monitors SET status=?,detail=COALESCE(?,detail),"
                    "finished_at=?,next_poll_at=NULL WHERE id=?",
                    (status, detail or None, now, monitor_id))
                updated = self._monitor_locked(monitor_id)
                self._conn.commit()
                return updated
            except Exception:
                self._conn.rollback()
                raise

    def expire_monitors(self, now: float | None = None) -> list[dict]:
        """Stop watching past the deadline.

        A monitor with no deadline watches until it resolves or is cancelled,
        which is the right default for a job someone is waiting on -- but an
        abandoned one would otherwise poll for the life of the gateway.
        """
        now = time.time() if now is None else now
        placeholders = ",".join("?" for _ in MONITOR_TERMINAL)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id FROM monitors WHERE status NOT IN ({placeholders}) "
                f"AND deadline IS NOT NULL AND deadline <= ?",
                (*sorted(MONITOR_TERMINAL), now)).fetchall()
        out = []
        for row in rows:
            closed = self.close_monitor(
                row["id"], "expired",
                "deadline passed before the work reported a terminal state",
                now=now)
            if closed:
                out.append(closed)
        return out

    def reconcile_startup(self) -> list[str]:
        """Fail stale running work and return persisted queued jobs to requeue.

        `running` only, and that exclusion is deliberate: an `awaiting_report`
        job has no local process to have lost, and its scheduler work is still
        out there and will report whenever it lands. Failing those on restart
        would destroy exactly the state this status exists to keep. Do not
        broaden this query without moving them somewhere they survive.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                running = self._conn.execute(
                    "SELECT id FROM jobs WHERE status='running'").fetchall()
                now = time.time()
                for row in running:
                    jid = row["id"]
                    message = "gateway restarted while this job was running"
                    self._conn.execute(
                        "UPDATE jobs SET status='failed',error=?,finished_at=? WHERE id=?",
                        (message, now, jid))
                    self._append_event_locked(
                        jid, "error", {"code": "gateway_restarted", "message": message}, now)
                    self._append_event_locked(
                        jid, "status", {"stage": "done", "status": "failed",
                                        "reason": "gateway_restarted"}, now)
                queued = self._conn.execute(
                    "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at,rowid"
                ).fetchall()
                self._conn.commit()
                return [row["id"] for row in queued]
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()
