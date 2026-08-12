"""opencode adapter.

Runs `opencode run` (one-shot, `--format json`) in the session the caller
named, or a fresh one. Each JSON record opencode writes to stdout carries the
*session actually run* — which is how the forked/created session id is recovered
in direct mode, no dispatcher to scrape.

Sessions live in a SQLite DB at ~/.local/share/opencode/opencode.db, so
list_sessions() reads it read-only (WAL-safe even while an interactive opencode
is running on the same node). There are no per-session transcript files to glob
like Claude's ~/.claude/projects/*/*.jsonl.

Supported surface: dispatch_mode = "direct" only. The dispatcher modes
(agent_exec / select_then_exec) depend on Claude-only flags (--tools,
--json-schema, structured output) and are rejected rather than half-supported.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Callable

from ..config import AgentConfig
from ..sessions import (DirInfo, SessionInfo, SessionPage, _cursor_decode,
                        _cursor_encode, _norm)
from .base import Event, JobSpec, RunResult, interrupt_group, resume_cwd

_AUTO_PERMISSIONS = (None, "", "auto", "bypassPermissions", "acceptEdits")

# A session with no message is one nothing can be resumed into -- the same class
# of row the Claude backend drops as a metadata-only transcript, and dropped in
# both places so "a session" means one thing across backends. Rare here (3 of
# 1522 on the store this was measured against) but free to exclude.
#
# `message` is the authoritative table and `session_message` is an empty one
# from a newer schema; correlating against the latter would have hidden all
# 1522 sessions rather than 3. Check before changing this predicate.
_NON_EMPTY = "EXISTS (SELECT 1 FROM message m WHERE m.session_id = session.id)"


class OpenCodeAdapter:
    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg

    def capabilities(self) -> dict:
        return {
            "sessions": True,
            "fork": True,
            "in_place_resume": True,
            "steering": False,
            "thinking_events": True,
            "file_attachments": True,
            "permission_modes": ["auto", "acceptEdits"],
            "model_policy": "advertised-passthrough",
        }

    # -- sessions ---------------------------------------------------------
    def _db_path(self) -> Path:
        data = os.environ.get("XDG_DATA_HOME")
        if data:
            return Path(data) / "opencode" / "opencode.db"
        base = Path.home() / ".local" / "share"
        db = base / "opencode" / "opencode.db"
        if db.is_file():
            return db
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            win_db = Path(local_appdata) / "opencode" / "opencode.db"
            if win_db.is_file():
                return win_db
        return db

    def _connect(self):
        db = self._db_path()
        if not db.is_file():
            return None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        con.row_factory = sqlite3.Row
        return con

    def list_dirs(self) -> list[DirInfo]:
        """Every directory with sessions, complete and unpaged.

        `directory` is a column here, so the grouping the Claude backend has to
        infer from folders is just a GROUP BY. One scan with window functions
        gets the count and the newest session per directory together.
        """
        con = self._connect()
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT directory, id, title, time_updated, n FROM ("
                "  SELECT directory, id, title, time_updated,"
                "         COUNT(*) OVER (PARTITION BY directory) AS n,"
                "         ROW_NUMBER() OVER (PARTITION BY directory"
                "             ORDER BY time_updated DESC) AS rn"
                "  FROM session WHERE time_archived IS NULL"
                f"    AND directory IS NOT NULL AND {_NON_EMPTY}"
                ") WHERE rn = 1").fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()
        out = [DirInfo(cwd=r["directory"], sessions=int(r["n"]),
                       last_active=(r["time_updated"] or 0) / 1000.0,
                       latest_session_id=r["id"],
                       latest_title=r["title"] or None)
               for r in rows]
        out.sort(key=lambda d: -d.last_active)
        return out

    def list_sessions(self, cwd: str | None = None, limit: int = 40,
                      cursor: str | None = None) -> SessionPage:
        """One page of sessions, newest first, optionally for one directory.

        Previously this read the newest `limit * 3` sessions across *every*
        directory and only then ranked by cwd, so a quiet project was invisible
        whenever a busy one filled the window -- measured on a real store, two
        directories holding 33 and 88 sessions returned zero rows each.
        Filtering in SQL puts the limit after the filter, where it belongs.
        """
        con = self._connect()
        if con is None:
            return SessionPage([], 0, None)
        try:
            where = f"time_archived IS NULL AND {_NON_EMPTY}"
            params: list = []
            if cwd:
                # Exact match, resolved in Python: the two backends spell paths
                # differently and SQL cannot normalise separators or case.
                target = _norm(cwd)
                dirs = [r[0] for r in con.execute(
                    "SELECT DISTINCT directory FROM session "
                    "WHERE directory IS NOT NULL").fetchall()
                    if r[0] and _norm(r[0]) == target]
                if not dirs:
                    return SessionPage([], 0, None)
                where += f" AND directory IN ({','.join('?' * len(dirs))})"
                params.extend(dirs)
            total = con.execute(
                f"SELECT COUNT(*) FROM session WHERE {where}", params).fetchone()[0]
            page_where, page_params = where, list(params)
            if cursor:
                after_ts, after_id = _cursor_decode(cursor)
                page_where += " AND (time_updated < ? OR (time_updated = ? AND id > ?))"
                page_params += [int(after_ts * 1000), int(after_ts * 1000), after_id]
            rows = con.execute(
                f"SELECT id, directory, title, time_updated FROM session "
                f"WHERE {page_where} ORDER BY time_updated DESC, id LIMIT ?",
                page_params + [limit + 1]).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            infos: list[SessionInfo] = []
            for r in rows:
                title, summary, nmsg = _session_details(con, r["id"])
                directory = r["directory"] or ""
                infos.append(SessionInfo(
                    session_id=r["id"], cwd=directory,
                    project=Path(directory).name if directory else "",
                    title=title or r["title"] or "(no title)",
                    summary=summary, git_branch="",
                    last_active=(r["time_updated"] or 0) / 1000.0,
                    messages=nmsg, path=""))
        finally:
            con.close()
        nxt = None
        if has_more and infos:
            nxt = _cursor_encode(infos[-1].last_active, infos[-1].session_id)
        return SessionPage(infos, int(total), nxt)

    # -- run --------------------------------------------------------------
    def run(self, spec: JobSpec, emit: Callable[[Event], None]) -> RunResult:
        if self.cfg.dispatch_mode != "direct":
            raise ValueError(
                f"opencode adapter supports only dispatch_mode='direct' "
                f"(got '{self.cfg.dispatch_mode}'); the dispatcher modes need "
                f"Claude-only flags")
        return self._run_direct(spec, emit)

    def _session_cwd(self, session_id: str) -> str | None:
        """One session's recorded directory, by id.

        A targeted query rather than a scan of `list_sessions()`, which returns
        only a bounded window — a session outside it would look absent and the
        caller would fall back to a default.
        """
        db = self._db_path()
        if not db.is_file():
            return None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            row = con.execute("SELECT directory FROM session WHERE id=?",
                              (session_id,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            con.close()
        return (row[0] or None) if row else None

    def _cwd_for(self, spec: JobSpec, emit) -> str:
        if not spec.requested_session:
            return spec.cwd
        return resume_cwd(self.cfg, spec.requested_session,
                          self._session_cwd(spec.requested_session),
                          spec.cwd, emit)

    def _run_direct(self, spec: JobSpec, emit) -> RunResult:
        perm = spec.permission_mode or self.cfg.permission_mode
        # A named session runs in its own directory: `--dir` and the process cwd
        # must agree, or the agent's history and its filesystem disagree.
        cwd = self._cwd_for(spec, emit)
        args = [self.cfg.bin, "run", "-", "--format", "json", "--dir", cwd]
        # Non-interactive: without pre-approval opencode denies every tool it
        # hasn't been told to allow, which reads as a failed run. `--auto` is
        # the opencode spelling of the gateway's default bypassPermissions.
        if perm in _AUTO_PERMISSIONS:
            args += ["--auto"]
        # Reasoning events are opt-in: ask opencode to surface them only when
        # the job's caller wanted them (the worker also drops `thinking` events
        # unless include_thinking is set).
        if spec.include_thinking:
            args += ["--thinking"]
        if spec.requested_session:
            args += ["-s", spec.requested_session]
            if spec.fork:
                args += ["--fork"]
        elif not spec.fork:
            res = RunResult(ok=False)
            res.error = "fork=false requires a session to resume"
            emit(Event("error", {"message": res.error}))
            return res
        if spec.model or self.cfg.model:
            args += ["-m", spec.model or self.cfg.model]
        if spec.title:
            args += ["--title", spec.title]
        for f in spec.files:
            args += ["-f", f]

        emit(Event("status", {"stage": "direct", "agent": "opencode",
                              "session": spec.requested_session or "NEW",
                              "fork": spec.fork}))
        text: list[str] = []
        res = RunResult(ok=False, chosen_session=spec.requested_session)
        self._stream(args, cwd, spec.prompt, emit, res, text,
                     cancel=spec.cancel)
        return res

    # -- shared streaming -------------------------------------------------
    def _stream(self, args, cwd, prompt, emit, res: RunResult, text: list[str],
                cancel=None):
        if os.name == "nt":
            # Windows: start_new_session requires CREATE_NEW_PROCESS_GROUP;
            # .cmd/.ps1 binaries need shell=True to be found.
            popen_kw: dict = dict(
                cwd=cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                shell=True,
            )
        else:
            popen_kw: dict = dict(
                cwd=cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, start_new_session=True,
            )
        proc = subprocess.Popen(args, **popen_kw)
        if cancel is not None:
            cancel.bind(proc)  # kills the process group on cancel (even if already set)
        # timeout_sec <= 0 disables the wall-clock kill (jobs run unbounded).
        timer = None
        if self.cfg.timeout_sec and self.cfg.timeout_sec > 0:
            timer = threading.Timer(self.cfg.timeout_sec, _kill, [proc])
            timer.start()
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    emit(Event("log", {"raw": line[:2000]}))
                    continue
                self._handle_record(rec, emit, res, text)
        finally:
            if timer is not None:
                timer.cancel()
            rc = proc.wait()
            stderr = proc.stderr.read() if proc.stderr else ""

        if res.error or rc != 0:
            res.ok = False
            if not res.error:
                res.error = (stderr or f"opencode exited with code {rc}").strip()[:4000]
            emit(Event("error", {"message": res.error, "returncode": rc}))
        else:
            res.ok = True
            res.result = "\n".join(text)
            emit(Event("result", {"text": res.result, "cost_usd": res.cost_usd,
                                  "chosen_session": res.chosen_session,
                                  "forked_session": res.forked_session,
                                  "is_error": False}))

    def _handle_record(self, rec, emit, res: RunResult, text: list[str]):
        sid = rec.get("sessionID")
        if sid and not res.forked_session:
            res.forked_session = sid
        t = rec.get("type")
        part = rec.get("part") or {}
        if t == "step_start":
            emit(Event("status", {"stage": "step", "session_id": sid}))
        elif t == "text":
            chunk = part.get("text", "")
            if chunk:
                text.append(chunk)
                emit(Event("assistant", {"text": chunk}))
        elif t == "reasoning":
            chunk = part.get("text", "")
            if chunk:
                emit(Event("thinking", {"text": chunk}))
        elif t == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            emit(Event("tool_use", {"name": part.get("tool"),
                                    "input": state.get("input", {})}))
            out = state.get("output")
            if out is not None:
                # Stored whole — see the matching note in adapters/claude.py.
                # Truncation belongs to the client, not the record.
                emit(Event("tool_result", {"text": str(out)}))
        elif t == "error":
            err = rec.get("error") or {}
            data = err.get("data") or {}
            msg = data.get("message") or err.get("name") or "opencode error"
            res.error = str(msg)[:4000]
            emit(Event("error", {"message": str(msg)}))
        elif t == "step_finish":
            # Accumulate. opencode emits one step_finish per step and each
            # carries only that step's cost, so assigning kept the last step
            # and silently under-reported every multi-step run — which is all
            # of them that use a tool.
            cost = part.get("cost")
            if isinstance(cost, (int, float)):
                res.cost_usd = (res.cost_usd or 0.0) + cost
        else:
            emit(Event("log", {"raw": json.dumps(rec)[:2000]}))


def _session_details(con, session_id: str) -> tuple[str, str, int]:
    """Return (title, summary, message_count) for one opencode session.

    title = the most recent real user text; summary = the most recent assistant
    text; message_count = rows in the `message` table. Reasonable for the ~120
    rows a typical session keeps.
    """
    title = ""
    summary = ""
    try:
        rows = con.execute(
            "SELECT m.id AS mid, m.data AS mdata, p.data AS pdata "
            "FROM part p JOIN message m ON p.message_id = m.id "
            "WHERE p.session_id = ? ORDER BY p.time_created DESC LIMIT 120",
            (session_id,)).fetchall()
    except sqlite3.Error:
        return "", "", 0
    seen: set[str] = set()
    for mid, mdata, pdata in rows:
        try:
            m = json.loads(mdata)
            p = json.loads(pdata)
        except (TypeError, json.JSONDecodeError):
            continue
        role = m.get("role")
        if role and mid not in seen:
            seen.add(mid)
        if p.get("type") == "text" and p.get("text"):
            text = p["text"].strip()
            if role == "user" and not title:
                title = text[:200]
            elif role == "assistant" and not summary:
                summary = text[:400]
    return title, summary, len(seen)


def _under(cwd: str, base: str) -> bool:
    if not cwd:
        return False
    try:
        c = Path(cwd).resolve()
    except (OSError, ValueError):
        return False
    b = Path(base)
    return c == b or b in c.parents


def _kill(proc: subprocess.Popen):
    # Wall-clock timeout. Interrupt the whole tree rather than killing it, so
    # a timed-out run still flushes and stays resumable; escalates to SIGKILL
    # if ignored.
    interrupt_group(proc)
