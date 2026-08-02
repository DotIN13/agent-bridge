"""Agent adapters. Register new backends (opencode, antigravity-cli, ...) here."""
from __future__ import annotations

from ..config import AgentConfig
from .base import AgentAdapter, Event, RunResult, JobSpec
from .claude import ClaudeAdapter

_REGISTRY = {
    "claude": ClaudeAdapter,
    # "opencode": OpenCodeAdapter,
    # "antigravity": AntigravityAdapter,
}


def build(cfg: AgentConfig) -> AgentAdapter:
    try:
        cls = _REGISTRY[cfg.name]
    except KeyError:
        raise ValueError(
            f"unknown agent '{cfg.name}'; known: {sorted(_REGISTRY)}"
        )
    return cls(cfg)


def known_agents() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "AgentAdapter",
    "Event",
    "RunResult",
    "JobSpec",
    "build",
    "known_agents",
]
