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
from ..sessions import SessionInfo
from .base import Event, JobSpec, RunResult, interrupt_group

_AUTO_PERMISSIONS = (None, "", "auto", "bypassPermissions", "acceptEdits")


class OpenCodeAdapter:
    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg

    # -- sessions ---------------------------------------------------------
    def _db_path(self) -> Path:
        data = os.environ.get("XDG_DATA_HOME")
        base = Path(data) if data else Path.home() / ".local" / "share"
        return base / "opencode" / "opencode.db"

    def list_sessions(self, cwd_filter: str | None = None) -> list[SessionInfo]:
        db = self._db_path()
        if not db.is_file():
            return []
        limit = self.cfg.max_sessions_in_index
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, directory, title, time_updated FROM session "
                "WHERE time_archived IS NULL "
                "ORDER BY time_updated DESC LIMIT ?",
                (limit * 3,)).fetchall()
            infos: list[SessionInfo] = []
            for r in rows:
                title, summary, nmsg = _session_details(con, r["id"])
                cwd = r["directory"] or ""
                infos.append(SessionInfo(
                    session_id=r["id"],
                    cwd=cwd,
                    project=Path(cwd).name if cwd else "",
                    title=title or r["title"] or "(no title)",
                    summary=summary,
                    git_branch="",
                    last_active=(r["time_updated"] or 0) / 1000.0,
                    messages=nmsg,
                    path="",
                ))
        finally:
            con.close()

        if cwd_filter:
            cf = str(Path(cwd_filter).expanduser().resolve())
            infos.sort(key=lambda s: (not _under(s.cwd, cf), -s.last_active))
        else:
            infos.sort(key=lambda s: -s.last_active)
        return infos[:limit]

    # -- run --------------------------------------------------------------
    def run(self, spec: JobSpec, emit: Callable[[Event], None]) -> RunResult:
        if self.cfg.dispatch_mode != "direct":
            raise ValueError(
                f"opencode adapter supports only dispatch_mode='direct' "
                f"(got '{self.cfg.dispatch_mode}'); the dispatcher modes need "
                f"Claude-only flags")
        return self._run_direct(spec, emit)

    def _run_direct(self, spec: JobSpec, emit) -> RunResult:
        perm = spec.permission_mode or self.cfg.permission_mode
        args = [self.cfg.bin, "run", "-", "--format", "json", "--dir", spec.cwd]
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
        self._stream(args, spec.cwd, spec.prompt, emit, res, text,
                     cancel=spec.cancel)
        return res

    # -- shared streaming -------------------------------------------------
    def _stream(self, args, cwd, prompt, emit, res: RunResult, text: list[str],
                cancel=None):
        proc = subprocess.Popen(
            args, cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
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
            cost = part.get("cost")
            if isinstance(cost, (int, float)) and cost is not None:
                res.cost_usd = cost
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
