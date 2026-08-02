"""Adapter interface shared by all agent backends.

An adapter turns a JobSpec into a stream of Events (persisted + fanned out for
SSE) and a final RunResult. Backends only produce data; persistence, queueing
and HTTP live outside so a new backend is just one file implementing `run`.
"""
from __future__ import annotations

import os
import signal
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..config import AgentConfig
from ..sessions import SessionInfo


class Cancellation:
    """A cancel signal shared between the worker and a running adapter.

    The adapter binds any child process it spawns; cancel() sets the flag and
    kills every bound process group. Binding after cancel() kills immediately,
    closing the race between spawn and cancel.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._procs: list = []
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            _kill_group(p)

    def cancelled(self) -> bool:
        return self._event.is_set()

    def bind(self, proc) -> None:
        with self._lock:
            self._procs.append(proc)
            already = self._event.is_set()
        if already:
            _kill_group(proc)


def _kill_group(proc) -> None:
    """SIGKILL the process and its ENTIRE descendant tree.

    A plain killpg misses grandchildren that started their own session — e.g.
    the dispatcher's nested `claude`, which would otherwise be orphaned and keep
    running. So we enumerate descendants from /proc *before* killing (once the
    parent dies they reparent to init and become unfindable), then kill each
    process, each process's group, and the parent's group.
    """
    pid = proc.pid
    victims = _descendants(pid)  # capture before anything dies
    for target in victims + [pid]:
        # kill the process's own group (catches a child that called setsid)
        try:
            os.killpg(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.kill(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _descendants(pid: int) -> list[int]:
    """All transitive child PIDs of `pid`, via /proc PPID links."""
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
            # fields after the "(comm)" group: state ppid ...
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
    files: tuple[str, ...] = ()    # absolute paths of attached files (readable by the job)


@dataclass
class Event:
    type: str      # status|assistant|thinking|tool_use|tool_result|result|error|log
    data: dict


@dataclass
class RunResult:
    ok: bool
    result: str = ""
    chosen_session: str | None = None
    forked_session: str | None = None
    cost_usd: float | None = None
    error: str | None = None


# Called by the adapter for each event. The worker assigns seq, persists, fans out.
EmitFn = Callable[[Event], None]


class AgentAdapter(Protocol):
    cfg: AgentConfig

    def list_sessions(self, cwd_filter: str | None = None) -> list[SessionInfo]:
        ...

    def run(self, spec: JobSpec, emit: EmitFn) -> RunResult:
        ...
