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

**Steering** works differently here than for claude, because opencode's own
mechanism is different. claude reads stdin for the life of the turn, so a steer
is a JSON line into a live pipe. `opencode run` reads its whole prompt from
stdin and closes it *before* the turn starts, and the server it talks to is
in-process on a fetch shim (`baseUrl: "http://opencode.internal"`) with no
listener -- so there is nothing to write to and nothing to connect to. What
opencode has instead is a first-class steering verb on its HTTP API:

    POST /api/session/<id>/prompt  {"prompt": {"text": ...},
                                    "delivery": "steer" | "queue"}

`steer` goes into the turn that is already running; `queue` waits for it to go
idle. `POST /api/session/<id>/interrupt` stops the turn, and the response to a
prompt is a real receipt (`admittedSeq`, and `promotedSeq` once it is taken)
rather than an echo we have to wait for.

Reaching that API means the run has to be attached to a server with a port. So
a steerable job gets a private `opencode serve` on loopback, and runs with
`--attach`. See docs/design/18 for the alternatives and what attaching costs.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable

from ..config import AgentConfig
from ..sessions import (DirInfo, SessionInfo, SessionPage, _cursor_decode,
                        _cursor_encode, _norm)
from .base import (Event, JobSpec, RunResult, SteerError, Steering,
                   child_env, interrupt_group, job_dir_note, resume_cwd)

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

#: `--attach` cannot assume the server shares a filesystem with the client, so
#: `opencode run` inlines each attached file as a data: url, caps it at 10 MiB,
#: and refuses a directory outright. Here the two *are* the same host, but the
#: check is opencode's and it exits non-zero before the turn starts -- so a job
#: whose attachments would trip it runs unattached, and unsteerable, rather
#: than failing.
ATTACH_MAX_BYTES = 10 * 1024 * 1024

#: `opencode serve` announces itself on stdout: "opencode server listening on
#: http://127.0.0.1:41234". Parsed rather than assumed, because `--port 0` means
#: the port is the kernel's choice -- and asking for a fixed one would collide
#: with the operator's own server, or with the next job.
_LISTENING = re.compile(r"listening on\s+(https?://[^\s]+)")

#: A server that has not announced a port by now is not going to. Generous
#: because the first `opencode serve` on a cold node loads providers and config;
#: a job that waits this long and gets nothing simply runs unattached.
SERVE_WAIT_SEC = 30.0

#: The username opencode's basic auth defaults to when only a password is set
#: (OPENCODE_SERVER_USERNAME, `server/auth.ts`).
_SERVE_USER = "opencode"


class _Server:
    """An `opencode serve` this job can reach, and the credential for it.

    Either one the adapter started for this job (`proc` set, stopped when the
    run ends) or the operator's own, named in `[agents.<name>] server_url`.
    """

    def __init__(self, base: str, password: str,
                 proc: subprocess.Popen | None = None) -> None:
        self.base = base.rstrip("/")
        self.password = password
        self.proc = proc

    def headers(self, cwd: str = "") -> dict[str, str]:
        out = {"Content-Type": "application/json"}
        if self.password:
            raw = f"{_SERVE_USER}:{self.password}".encode()
            out["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
        if cwd:
            # The server loads one instance per request from this header, and
            # expects it percent-encoded (`sdk/v2/client.js`).
            out["x-opencode-directory"] = urllib.parse.quote(cwd, safe="")
        return out

    def stop(self) -> None:
        """Wind the server down, if it is ours to wind down."""
        if self.proc is None:
            return
        interrupt_group(self.proc)


def _request(server: _Server, method: str, path: str, *, body=None,
             cwd: str = "", timeout: float = 15.0) -> dict:
    """One call to an opencode server, decoded.

    Proxies are explicitly disabled: this is a loopback address, and an ambient
    `HTTPS_PROXY` in the gateway's environment -- normal on a managed host --
    would otherwise swallow every steer.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.base + path, data=data, method=method,
                                 headers=server.headers(cwd))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _http_reason(exc: urllib.error.HTTPError) -> str:
    """What the server said, in the words it used.

    opencode answers with a tagged error (`_tag`, `message`) -- a 409
    `ConflictError` on a session that cannot take the message, a 404
    `SessionNotFoundError` on one that has gone. Passing that through beats
    "steering failed", which tells the caller nothing about which it was.
    """
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
    except Exception:
        payload = {}
    tag = payload.get("_tag") or ""
    message = payload.get("message") or ""
    if tag or message:
        return f"opencode refused the steer: {tag or exc.code} {message}".strip()
    return f"opencode refused the steer with HTTP {exc.code}"


class OpenCodeAdapter:
    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg

    @property
    def steerable(self) -> bool:
        """Is this backend configured to make its jobs reachable mid-turn?

        A claim about the configuration, not about any one job: a job whose
        server fails to start, or whose attachments `--attach` would refuse,
        runs unsteerable and says so on its own stream.
        """
        return bool(self.cfg.steering)

    def capabilities(self) -> dict:
        return {
            "sessions": True,
            "fork": True,
            "in_place_resume": True,
            "steering": self.cfg.dispatch_mode == "direct" and self.steerable,
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

    # -- the steering channel ---------------------------------------------
    def _server_for(self, spec: JobSpec, cwd: str,
                    emit) -> tuple[_Server | None, str]:
        """The server this job will attach to, or `None` and why not.

        Every "no" here is a job that still runs -- unattached, unsteerable,
        and with a reason on its own event stream. Steering is worth a second
        process; it is not worth failing work over.
        """
        if not self.steerable:
            return None, (f"steering is off for this backend: set "
                          f"[agents.{self.cfg.name}] steering = true")
        blocked = _unattachable(spec.files)
        if blocked:
            return None, (f"this job runs unattached because {blocked}, and an "
                          f"unattached `opencode run` has no port to reach")
        if self.cfg.server_url:
            # The operator's own server. Its password comes from the gateway's
            # environment, never from the config file -- same rule the auth
            # token follows.
            server = _Server(self.cfg.server_url,
                             os.environ.get("OPENCODE_SERVER_PASSWORD", ""))
            if not _healthy(server):
                return None, (f"the configured opencode server at "
                              f"{server.base} did not answer /api/health, so "
                              f"this job ran on its own in-process one")
            return server, ""
        server = self._spawn_server(emit)
        if server is None:
            return None, ("no opencode server could be started for this job, "
                          "so there is nothing to steer through; see the log "
                          "events for what it said")
        return server, ""

    def _spawn_server(self, emit) -> _Server | None:
        """Start a private `opencode serve` on loopback for one job.

        Private rather than shared: its lifetime is the job's, so a crash or a
        stuck turn cannot take the next job's steering with it, and there is no
        long-lived listener to secure between runs. The password is generated
        per job and passed in the environment -- `opencode serve` prints a
        warning and serves *unauthenticated* if it finds none.
        """
        password = secrets.token_urlsafe(24)
        args = [self.cfg.bin, "serve", "--hostname", "127.0.0.1", "--port", "0"]
        env = dict(os.environ, OPENCODE_SERVER_PASSWORD=password)
        kw: dict = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, bufsize=1, env=env)
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            kw["shell"] = True
        else:
            kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(args, **kw)
        except OSError as exc:
            emit(Event("log", {"opencode_server": "could not start",
                               "reason": str(exc)}))
            return None
        url, tail = _await_listening(proc, SERVE_WAIT_SEC)
        if not url:
            emit(Event("log", {"opencode_server": "never announced a port",
                               "waited_sec": SERVE_WAIT_SEC,
                               "output": tail[-2000:]}))
            interrupt_group(proc)
            return None
        server = _Server(url, password, proc)
        if not _healthy(server):
            # `--attach` would still work against a server without the v2 API,
            # but steering would not -- and attaching costs the attachment
            # limits above. An unattached run is the better trade.
            emit(Event("log", {
                "opencode_server": "no /api/health; too old for steering",
                "server": server.base}))
            server.stop()
            return None
        return server

    def _bind_steering(self, steer: Steering, server: _Server, cwd: str,
                       res: RunResult, emit) -> None:
        """Wire `POST /v1/jobs/<id>/steer` to this run's opencode session.

        The session id is read at send time, not now: on a fresh session
        opencode only reveals it in the first records it streams back, so a
        steer that arrives in the first second has to say "not yet" rather than
        address the wrong session.
        """
        def session() -> str:
            sid = res.session
            if not sid:
                raise SteerError(
                    "opencode has not reported its session id yet — the turn "
                    "is seconds old; try again")
            return urllib.parse.quote(sid, safe="")

        def send(text: str) -> None:
            try:
                out = _request(server, "POST",
                               f"/api/session/{session()}/prompt",
                               body={"prompt": {"text": text},
                                     "delivery": "steer"}, cwd=cwd)
            except urllib.error.HTTPError as exc:
                raise SteerError(_http_reason(exc)) from exc
            except (OSError, urllib.error.URLError) as exc:
                raise SteerError(
                    f"opencode's server is not answering ({exc}); this job's "
                    f"row may be stale") from exc
            # The receipt is the response, not an echo. claude logs a steer when
            # the agent replays it on stdout, which is the moment it was taken;
            # opencode admits the input synchronously and promotes it into the
            # running turn afterwards, so `admitted_seq` is "we have it" and
            # `promoted_seq` is "the turn has it".
            admitted = (out or {}).get("data") or {}
            emit(Event("steer", {
                "text": text,
                "source": "opencode",
                "delivery": admitted.get("delivery") or "steer",
                "message_id": admitted.get("id") or "",
                "admitted_seq": admitted.get("admittedSeq"),
                "promoted_seq": admitted.get("promotedSeq")}))

        def interrupt() -> None:
            try:
                _request(server, "POST", f"/api/session/{session()}/interrupt",
                         cwd=cwd)
            except urllib.error.HTTPError as exc:
                raise SteerError(_http_reason(exc)) from exc
            except (OSError, urllib.error.URLError) as exc:
                raise SteerError(f"opencode's server is not answering ({exc})") \
                    from exc

        steer.bind_remote(
            send=send, interrupt=interrupt,
            note=("opencode admits this into the session and promotes it into "
                  "the running turn (delivery=steer)"))

    def _run_direct(self, spec: JobSpec, emit) -> RunResult:
        perm = spec.permission_mode or self.cfg.permission_mode
        # Refused before anything is started: a bad request must not leave a
        # server process behind it.
        if not spec.requested_session and not spec.fork:
            res = RunResult(ok=False)
            res.error = "fork=false requires a session to resume"
            emit(Event("error", {"message": res.error}))
            return res
        # A named session runs in its own directory: `--dir` and the process cwd
        # must agree, or the agent's history and its filesystem disagree.
        cwd = self._cwd_for(spec, emit)
        # Decided before the command line is built, because attaching changes
        # it -- and a job that cannot attach must still get a working one.
        server, no_steering = self._server_for(spec, cwd, emit)
        try:
            return self._launch(spec, emit, perm, cwd, server, no_steering)
        finally:
            if server is not None:
                server.stop()

    def _launch(self, spec: JobSpec, emit, perm: str, cwd: str,
                server: _Server | None, no_steering: str) -> RunResult:
        args = [self.cfg.bin, "run", "-", "--format", "json", "--dir", cwd]
        if server is not None:
            args += ["--attach", server.base]
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
        if spec.model or self.cfg.model:
            args += ["-m", spec.model or self.cfg.model]
        if spec.title:
            args += ["--title", spec.title]
        for f in spec.files:
            args += ["-f", f]

        emit(Event("status", {"stage": "direct", "agent": "opencode",
                              "session": spec.requested_session or "NEW",
                              "fork": spec.fork,
                              "steerable": server is not None}))
        text: list[str] = []
        res = RunResult(ok=False, session=spec.requested_session)
        # A spec without a handle is a caller that is not offering steering
        # (some tests, and any future non-worker path); it must not be a crash.
        steer = spec.steer if spec.steer is not None else Steering()
        if server is not None:
            # Bound before the child starts, so a steer that arrives in the
            # first second is told "the session id is not known yet" rather
            # than "this job has no channel" -- one is a retry, the other is
            # not. The url is loopback and worth having on the stream; the
            # password never is.
            self._bind_steering(steer, server, cwd, res, emit)
            emit(Event("log", {"opencode_server": server.base,
                               "own_server": server.proc is not None}))
        else:
            steer.unavailable(no_steering)
            emit(Event("log", {"steering": "unavailable",
                               "reason": no_steering}))
        # `opencode run` has no system-prompt flag, so the reporting preamble
        # rides in front of the prompt on stdin. Weaker than a system prompt --
        # it is visible to the model as something the caller said, and a caller
        # could contradict it -- but the alternative is a job that cannot report
        # at all, and AB_JOB_DIR is in the environment either way.
        note = job_dir_note(spec)
        prompt = f"{note}\n{spec.prompt}" if note else spec.prompt
        self._stream(args, cwd, prompt, emit, res, text,
                     cancel=spec.cancel, env=_run_env(spec, server))
        return res

    # -- shared streaming -------------------------------------------------
    def _stream(self, args, cwd, prompt, emit, res: RunResult, text: list[str],
                cancel=None, env: dict[str, str] | None = None):
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
        if env is not None:
            popen_kw["env"] = env
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
                                  "session": res.session,
                                  "is_error": False}))

    def _handle_record(self, rec, emit, res: RunResult, text: list[str]):
        sid = rec.get("sessionID")
        if sid:
            res.session = sid
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


def _run_env(spec: JobSpec, server: _Server | None) -> dict[str, str] | None:
    """The environment for `opencode run`, including the server credential.

    The password goes here and not on the command line: argv is world-readable
    through /proc on a shared host, which is the same reason the gateway's own
    token is kept out of a job's environment. `opencode run --attach` reads
    `OPENCODE_SERVER_PASSWORD` when `--password` is absent (`server/auth.ts`),
    so nothing is lost by the move.
    """
    env = child_env(spec)
    if server is None or not server.password:
        return env
    base = dict(env) if env is not None else dict(os.environ)
    base["OPENCODE_SERVER_PASSWORD"] = server.password
    base["OPENCODE_SERVER_USERNAME"] = _SERVE_USER
    return base


def _unattachable(files: tuple[str, ...]) -> str:
    """Why `--attach` would refuse this job's attachments, or "".

    Checked here rather than discovered from a non-zero exit: opencode fails
    the whole run on either of these, and a job losing its work to gain
    steering is the wrong trade.
    """
    for path in files:
        try:
            if os.path.isdir(path):
                return f"{Path(path).name} is a directory"
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > ATTACH_MAX_BYTES:
            return (f"{Path(path).name} is larger than "
                    f"{ATTACH_MAX_BYTES // (1024 * 1024)} MiB")
    return ""


def _healthy(server: _Server) -> bool:
    """Does this server answer the v2 API?

    `/api/health` is the cheapest thing only a v2 server has, and steering is a
    v2 verb -- an older opencode accepts `--attach` and would then refuse every
    steer, which is worse than not attaching at all.
    """
    try:
        _request(server, "GET", "/api/health", timeout=5.0)
    except Exception:
        return False
    return True


def _await_listening(proc: subprocess.Popen,
                     timeout: float) -> tuple[str, str]:
    """Wait for `opencode serve` to announce its port; return (url, output).

    Both pipes are drained for the life of the process, not just until the url
    appears: a server whose stdout fills its pipe buffer stops serving, and a
    64 KB buffer is a few hundred log lines. The tail is kept for the event that
    explains a server which never came up.
    """
    found: list[str] = []
    ready = threading.Event()
    tail: deque[str] = deque(maxlen=40)

    def drain(stream, watch: bool) -> None:
        try:
            for line in stream:
                tail.append(line.rstrip())
                if watch and not found:
                    match = _LISTENING.search(line)
                    if match:
                        found.append(match.group(1))
                        ready.set()
        except (OSError, ValueError):
            pass
        finally:
            if watch:
                # stdout at EOF means the process is gone. Unblock now rather
                # than sitting out the full timeout on a corpse.
                ready.set()

    for stream, watch in ((proc.stdout, True), (proc.stderr, False)):
        if stream is not None:
            threading.Thread(target=drain, args=(stream, watch),
                             daemon=True).start()
    ready.wait(timeout)
    # One more beat: the url and EOF race when the server dies right after
    # printing, and the line is the more useful of the two.
    if not found:
        time.sleep(0.05)
    return (found[0] if found else ""), "\n".join(tail)


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
