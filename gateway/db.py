"""SQLite persistence for jobs and their event logs.

One lock protects the connection.  Every event source uses the same per-job
allocator, so an `after` cursor is globally monotonic even across worker events,
HTTP reports, filesystem reports, failures, and restart recovery.
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
from typing import Any

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
    requested_session TEXT,
    chosen_session    TEXT,
    forked_session    TEXT,
    permission_mode   TEXT,
    model             TEXT,
    files             TEXT,
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
_REPORT_BATCH_LINES = 256
_REPORT_LINE_MAX_BYTES = 64 * 1024


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
    out["fork"] = True if out.get("fork") is None else bool(out["fork"])
    out["include_thinking"] = bool(out.get("include_thinking"))
    out["files"] = _decode_files(out.get("files"))
    if not detail:
        for key in ("prompt", "permission_mode", "files", "result", "error"):
            out.pop(key, None)
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
        for name, decl in (("files", "TEXT"), ("title", "TEXT"),
                           ("title_norm", "TEXT"), ("fork", "INTEGER"),
                           ("include_thinking", "INTEGER")):
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
                           cwd: str | None, requested_session: str | None,
                           permission_mode: str | None, model: str | None,
                           title: str | None, fork: bool,
                           include_thinking: bool, files: list[str] | None) -> None:
        title = (title or "").strip() or derive_title(prompt)
        self._conn.execute(
            "INSERT INTO jobs (id,status,agent,prompt,cwd,requested_session,"
            "permission_mode,model,title,title_norm,fork,include_thinking,files,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, "queued", agent, prompt, cwd, requested_session,
             permission_mode, model, title, norm_title(title),
             1 if fork else 0, 1 if include_thinking else 0,
             json.dumps(files or []), time.time()))

    def create_job(self, *, agent: str, prompt: str, cwd: str | None,
                   requested_session: str | None, permission_mode: str | None,
                   model: str | None, title: str | None = None, fork: bool = True,
                   include_thinking: bool = False, files: list[str] | None = None,
                   job_id: str | None = None) -> str:
        job_id = job_id or str(uuid.uuid4())
        with self._lock:
            self._insert_job_locked(
                job_id=job_id, agent=agent, prompt=prompt, cwd=cwd,
                requested_session=requested_session,
                permission_mode=permission_mode, model=model, title=title,
                fork=fork, include_thinking=include_thinking, files=files)
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
                               events: list[tuple[str, dict]]) -> list[dict]:
        """Publish terminal fields and their final events in one transaction."""
        values = dict(fields)
        values.setdefault("finished_at", time.time())
        columns = ", ".join(f"{key}=?" for key in values)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
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
                       request_hash: str, data: dict) -> tuple[dict, bool]:
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
        row = self._append_event_locked(
            job_id, "message", data, float(data.get("ts") or time.time()))
        self._conn.execute(
            "INSERT INTO external_reports(job_id,source,report_id,request_hash,seq) "
            "VALUES (?,?,?,?,?)", (job_id, source, report_id, request_hash, row["seq"]))
        return row, False

    def add_message(self, job_id: str, data: dict,
                    report_id: str | None = None) -> dict:
        report_id = report_id or data.get("report_id")
        request_hash = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if report_id:
                    row, duplicate = self._report_locked(
                        job_id, "report", str(report_id), request_hash, data)
                else:
                    row = self._append_event_locked(
                        job_id, "message", data, float(data.get("ts") or time.time()))
                    duplicate = False
                self._conn.commit()
                row["duplicate"] = duplicate
                return row
            except Exception:
                self._conn.rollback()
                raise

    def ingest_messages(self, job_id: str, messages_dir: str) -> int:
        """Ingest fallback JSONL with bounded memory and transactions.

        A malformed or non-object JSON value is retained as a safe diagnostic
        event instead of poisoning job/event reads. Logical lines are capped in
        memory while their full bytes still contribute to the deduplication id.
        """
        path = os.path.join(messages_dir, f"{job_id}.jsonl")

        def records():
            try:
                source = open(path, "rb")
            except OSError:
                return
            with source:
                index = 0
                while True:
                    chunk = source.readline(_REPORT_LINE_MAX_BYTES + 1)
                    if not chunk:
                        break
                    digest = hashlib.sha256()
                    digest.update(chunk)
                    preview = bytearray(chunk[:_REPORT_LINE_MAX_BYTES])
                    oversized = len(chunk) > _REPORT_LINE_MAX_BYTES
                    while chunk and not chunk.endswith(b"\n"):
                        chunk = source.readline(_REPORT_LINE_MAX_BYTES + 1)
                        if not chunk:
                            break
                        digest.update(chunk)
                        remaining = _REPORT_LINE_MAX_BYTES - len(preview)
                        if remaining > 0:
                            preview.extend(chunk[:remaining])
                        oversized = oversized or len(preview) >= _REPORT_LINE_MAX_BYTES
                    text = bytes(preview).decode("utf-8", errors="replace").rstrip("\r\n")
                    yield index, text, digest.hexdigest(), oversized
                    index += 1

        def ingest_batch(batch) -> int:
            inserted = 0
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for index, line, line_digest, oversized in batch:
                        if not line.strip() and not oversized:
                            continue
                        if oversized:
                            data = {"status": "unknown", "raw": line[:2000],
                                    "error": "report line exceeded 65536 bytes"}
                        else:
                            try:
                                parsed = json.loads(line)
                            except (TypeError, ValueError):
                                parsed = None
                            if isinstance(parsed, dict):
                                data = parsed
                            else:
                                data = {"status": "unknown", "raw": line[:2000],
                                        "error": "report line must be a JSON object"}
                        explicit_id = data.get("report_id")
                        report_id = str(explicit_id or hashlib.sha256(
                            f"{os.path.abspath(path)}:{index}:{line_digest}".encode()
                        ).hexdigest())
                        request_hash = hashlib.sha256(json.dumps(
                            data, sort_keys=True, separators=(",", ":")
                        ).encode()).hexdigest()
                        source_kind = "report" if explicit_id else "file"
                        try:
                            _row, duplicate = self._report_locked(
                                job_id, source_kind, report_id, request_hash, data)
                        except ReportConflict:
                            duplicate = True
                        if not duplicate:
                            inserted += 1
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
            return inserted

        added = 0
        batch = []
        for record in records() or ():
            batch.append(record)
            if len(batch) >= _REPORT_BATCH_LINES:
                added += ingest_batch(batch)
                batch.clear()
        if batch:
            added += ingest_batch(batch)
        return added

    def reconcile_startup(self) -> list[str]:
        """Fail stale running work and return persisted queued jobs to requeue."""
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
