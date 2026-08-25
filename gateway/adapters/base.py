"""Adapter interface shared by all agent backends.

An adapter turns a JobSpec into a stream of Events (persisted + fanned out for
SSE) and a final RunResult. Backends only produce data; persistence, queueing
and HTTP live outside so a new backend is just one file implementing `run`.
"""
from __future__ import annotations

import itertools
import json
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

# Cancel escalates rather than killing outright: SIGINT is what the interactive
# client's ESC maps to, so the agent stops the current turn, flushes its
# transcript and exits, leaving the session resumable. SIGKILL is the last
# resort and leaves the transcript mid-write.
INTERRUPT_GRACE_SEC = 15.0     # SIGINT -> SIGTERM
TERM_GRACE_SEC = 5.0           # SIGTERM -> SIGKILL

from ..config import AgentConfig
from ..sessions import DirInfo, SessionInfo, SessionPage


class Cancellation:
    """A cancel signal shared between the worker and a running adapter.

    The adapter binds any child process it spawns; cancel() sets the flag and
    kills every bound process group. Binding after cancel() kills immediately,
    closing the race between spawn and cancel.
    """

    def __init__(self, grace_sec: float = INTERRUPT_GRACE_SEC) -> None:
        self._event = threading.Event()
        self._procs: list = []
        self._lock = threading.Lock()
        self._grace = grace_sec

    def mark_cancelled(self) -> None:
        """Publish cancellation immediately, without waiting for process I/O."""
        self._event.set()

    def cancel(self) -> None:
        self.mark_cancelled()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            interrupt_group(p, self._grace)

    def cancelled(self) -> bool:
        return self._event.is_set()

    def bind(self, proc) -> None:
        with self._lock:
            self._procs.append(proc)
            already = self._event.is_set()
        if already:
            interrupt_group(proc, self._grace)


def resume_cwd(cfg: AgentConfig, session_id: str, recorded_cwd: str | None,
               fallback: str, emit: "EmitFn") -> str:
    """The directory a resumed session should actually run in.

    A session carries the project it was created in. Resuming it somewhere else
    hands the agent the whole history of project X while its relative paths,
    globs and shell commands resolve against something else — and nothing about
    the run looks wrong, which is what made this expensive to notice.

    So a recorded cwd **wins**, over both an explicit request and the configured
    default. That makes the old "did the caller pass a cwd or did the server
    default it" question moot, which is why no extra column is needed to answer
    it. The substitution is announced on the event stream; taking a different
    directory than the caller named is exactly the kind of thing that must not
    happen quietly.

    Falls back rather than failing. Every fallback keeps today's behaviour, so
    the worst case is the bug this replaces — never a resume that refuses to run.
    """
    if not recorded_cwd:
        emit(Event("log", {
            "cwd_source": "fallback",
            "reason": f"session {session_id} records no cwd",
            "cwd": fallback}))
        return fallback
    try:
        resolved = cfg.resolve_cwd(recorded_cwd)
    except ValueError as exc:
        emit(Event("log", {
            "cwd_source": "fallback",
            "reason": f"session cwd is not usable here: {exc}",
            "cwd": fallback}))
        return fallback
    if resolved != fallback:
        emit(Event("status", {
            "stage": "cwd",
            "cwd_source": "session",
            "session": session_id,
            "cwd": resolved,
            "replaced": fallback,
            "note": "running in the session's own directory"}))
    return resolved


class SteerError(RuntimeError):
    """A steering message could not be delivered to a running agent."""


class Steering:
    """A live input channel into a turn that is already running.

    `--input-format stream-json` is the only mechanism that reaches a turn in
    progress: the agent reads JSON lines from its stdin and picks them up at
    the next tool boundary. `--resume` cannot do this — it starts a *second*
    agent on a stale copy of the transcript, and one of the two branches is
    then discarded (docs/todo/01).

    The adapter binds the child's stdin here and the worker publishes the
    handle, so an HTTP steer can find the right process. A handle that was
    never bound means the backend has no such channel — the caller is told
    that rather than having the message silently dropped.
    """

    _ids = itertools.count(1)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stdin = None
        self._open = False
        self._bound = False

    def bind(self, stdin) -> None:
        with self._lock:
            self._stdin = stdin
            self._open = stdin is not None
            self._bound = self._bound or self._open

    def close(self) -> None:
        """Stop accepting input, which is also what ends the run.

        In streaming-input mode the agent stays alive waiting for more work
        after it answers, so closing stdin is the signal that the job is over.
        Idempotent — the adapter closes on the result record and again in its
        `finally`, because a child still holding an open stdin never exits and
        `proc.wait()` would block forever.
        """
        with self._lock:
            self._open = False
            stdin, self._stdin = self._stdin, None
        if stdin is not None:
            try:
                stdin.close()
            except (OSError, ValueError):
                pass

    @property
    def available(self) -> bool:
        with self._lock:
            return self._open

    @property
    def unavailable_reason(self) -> str:
        with self._lock:
            if self._open:
                return ""
            if self._bound:
                return ("the agent's turn has already ended, so there is "
                        "nothing left to steer")
            return ("this job has no input channel — steering needs the claude "
                    "adapter in 'direct' dispatch mode")

    def send(self, text: str) -> None:
        """Queue a user message into the running turn."""
        self._write({
            "type": "user",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]},
            "parent_tool_use_id": None,
        })

    def interrupt(self) -> None:
        """Stop the current turn in-band.

        The agent acknowledges this on stdout within milliseconds and reports
        what was still queued, which the signal ladder in `interrupt_group`
        cannot do. Kept separate from `Cancellation` for now: cancel still goes
        through signals, which also work on a child that has stopped reading.
        """
        self._write({
            "type": "control_request",
            "request_id": f"ab-{next(self._ids)}",
            "request": {"subtype": "interrupt"},
        })

    def _write(self, obj: dict) -> None:
        with self._lock:
            if not self._open or self._stdin is None:
                raise SteerError(self._unavailable_reason_locked())
            try:
                self._stdin.write(json.dumps(obj) + "\n")
                self._stdin.flush()
            except (OSError, ValueError) as e:
                # A broken or closed pipe means the child stopped reading. That
                # is the most reliable liveness signal available — better than
                # the job row, which can sit at `running` after the agent died.
                self._open = False
                raise SteerError(
                    f"the agent is no longer accepting input ({e}); its job row "
                    f"may be stale") from e

    def _unavailable_reason_locked(self) -> str:
        if self._bound:
            return "the agent's turn has already ended, so there is nothing left to steer"
        return ("this job has no input channel — steering needs the claude "
                "adapter in 'direct' dispatch mode")


def interrupt_group(proc, grace_sec: float = INTERRUPT_GRACE_SEC) -> None:
    """Stop a run the way ESC does in the interactive client, escalating only
    if that is ignored.

    SIGINT is delivered to the whole tree — the dispatcher and the nested
    `claude` it forked — because both are Claude Code processes that treat it
    as "interrupt this turn". They then wind down and flush, so the session
    stays resumable and its transcript stays well-formed. SIGKILL does none of
    that, which is why it is now only the fallback.

    Descendants are captured up front: once the parent dies they reparent to
    init and become unfindable, so a late escalation would miss them.
    """
    pid = proc.pid
    victims = _descendants(pid)
    _signal_all(victims + [pid], signal.SIGINT)

    if _settled(proc, victims, grace_sec):
        return
    _signal_all([p for p in victims if _alive(p)] +
                ([pid] if proc.poll() is None else []), signal.SIGTERM)
    if _settled(proc, victims, TERM_GRACE_SEC):
        return
    _kill_group(proc)


_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _signal_all(pids, sig) -> None:
    """Send `sig` to each pid and to its process group."""
    for target in pids:
        for send in _signal_senders(target, sig):
            try:
                send(target)
            except (ProcessLookupError, PermissionError, OSError):
                pass


def _signal_senders(target: int, sig):
    if hasattr(os, "killpg"):
        yield lambda t: os.killpg(t, sig)
    yield lambda t: os.kill(t, sig)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _settled(proc, victims: list[int], timeout: float) -> bool:
    """Wait for the parent and every captured descendant to exit."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None and not any(_alive(p) for p in victims):
            return True
        time.sleep(0.1)
    return False


def _kill_group(proc) -> None:
    """Kill the process and its descendant tree (SIGKILL on POSIX, SIGTERM on
    Windows where SIGKILL is absent)."""
    pid = proc.pid
    victims = _descendants(pid)
    for target in victims + [pid]:
        for send in _signal_senders(target, _SIGKILL):
            try:
                send(target)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    if hasattr(os, "getpgid") and hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(pid), _SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _descendants(pid: int) -> list[int]:
    """All transitive child PIDs of `pid`, via /proc PPID links (POSIX only)."""
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as fh:
                data = fh.read()
            after = data[data.rfind(")") + 2:].split()
            ppid = int(after[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    out: list[int] = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            out.append(child)
            stack.append(child)
    return out


@dataclass
class JobSpec:
    job_id: str
    prompt: str
    cwd: str                       # already validated against allowed_dirs
    requested_session: str | None  # optional caller hint
    permission_mode: str | None    # override; None -> adapter default
    model: str | None              # override; None -> adapter default
    cancel: Cancellation | None = None  # set by the worker; adapter binds procs
    steer: Steering | None = None  # set by the worker; adapter binds the child's stdin
    files: tuple[str, ...] = ()    # absolute paths of attached files (readable by the job)
    title: str = ""                # human handle for the job
    fork: bool = True              # False -> resume the target session in place
    include_thinking: bool = False # True -> keep model reasoning in the event stream


@dataclass
class Event:
    type: str      # status|assistant|thinking|tool_use|tool_result|result|error|log
    data: dict


@dataclass
class RunResult:
    ok: bool
    result: str = ""
    #: The session this run used. Starts as the caller's request, if there was
    #: one, and is replaced by the real id the moment the agent reports it.
    session: str | None = None
    cost_usd: float | None = None
    error: str | None = None


# Called by the adapter for each event. The worker assigns seq, persists, fans out.
EmitFn = Callable[[Event], None]


class AgentAdapter(Protocol):
    cfg: AgentConfig

    def capabilities(self) -> dict:
        """Machine-readable operations supported by this configured adapter."""
        ...

    def list_dirs(self) -> "list[DirInfo]":
        """Every directory holding sessions. Complete: never truncated."""
        ...

    def list_sessions(self, cwd: str | None = None, limit: int = 40,
                      cursor: str | None = None) -> "SessionPage":
        """One page of sessions; `cwd` is an exact directory match."""
        ...

    def run(self, spec: JobSpec, emit: EmitFn) -> RunResult:
        ...
