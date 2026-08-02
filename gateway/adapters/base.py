"""Adapter interface shared by all agent backends.

An adapter turns a JobSpec into a stream of Events (persisted + fanned out for
SSE) and a final RunResult. Backends only produce data; persistence, queueing
and HTTP live outside so a new backend is just one file implementing `run`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..config import AgentConfig
from ..sessions import SessionInfo


@dataclass
class JobSpec:
    job_id: str
    prompt: str
    cwd: str                       # already validated against allowed_dirs
    requested_session: str | None  # optional caller hint
    permission_mode: str | None    # override; None -> adapter default
    model: str | None              # override; None -> adapter default


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
