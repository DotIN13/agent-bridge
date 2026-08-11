"""Scan Claude Code session transcripts into a compact index the dispatcher
can reason over when choosing which session to fork.

Sessions live at ~/.claude/projects/<slugified-cwd>/<session-uuid>.jsonl
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path

_PROJECTS = Path.home() / ".claude" / "projects"
_SKIP_LINE = re.compile(
    r"^\s*<(local-command-caveat|command-name|command-message|command-args|"
    r"local-command-stdout|system-reminder|command-stdout)\b",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")


@dataclass
class SessionInfo:
    session_id: str
    cwd: str
    project: str          # the slug dir name
    title: str            # first real user prompt, truncated
    summary: str          # Claude's own summary if present
    git_branch: str
    last_active: float     # epoch seconds (file mtime)
    messages: int
    path: str

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("path", None)
        return d


def _clean_user_text(content) -> str:
    """Return human-authored prompt text from a user record, or '' if it's just
    a slash-command / caveat / tool-result wrapper."""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
    elif isinstance(content, str):
        text = content
    else:
        return ""
    text = text.strip()
    if not text or _SKIP_LINE.match(text):
        return ""
    # drop any residual tag noise but keep the words
    text = _TAG.sub("", text).strip()
    return text


def _scan_file(path: Path, max_lines: int = 4000) -> SessionInfo | None:
    cwd = ""
    branch = ""
    title = ""
    summary = ""
    messages = 0
    try:
        with open(path, "r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                if not cwd and rec.get("cwd"):
                    cwd = rec["cwd"]
                if not branch and rec.get("gitBranch"):
                    branch = rec["gitBranch"]
                if rtype == "summary" and rec.get("summary"):
                    summary = rec["summary"]
                if rtype in ("user", "assistant"):
                    messages += 1
                    if not title and rtype == "user":
                        msg = rec.get("message")
                        if isinstance(msg, dict):
                            t = _clean_user_text(msg.get("content"))
                            if t:
                                title = t[:200]
    except OSError:
        return None

    return SessionInfo(
        session_id=path.stem,
        cwd=cwd,
        project=path.parent.name,
        title=title or "(no prompt captured)",
        summary=summary,
        git_branch=branch,
        last_active=path.stat().st_mtime,
        messages=messages,
        path=str(path),
    )


def find(session_id: str) -> SessionInfo | None:
    """One session by id, looked up directly.

    Deliberately not built on `scan()`: that parses only a bounded window of the
    most recently modified transcripts, so a session outside the window would
    come back as "not found" and the caller would silently fall back to a
    default. A glob on the id is exact and costs one directory walk.
    """
    if not session_id or not _PROJECTS.is_dir():
        return None
    for path in _PROJECTS.glob(f"*/{session_id}.jsonl"):
        info = _scan_file(path)
        if info:
            return info
    return None


def scan(limit: int = 40, cwd_filter: str | None = None) -> list[SessionInfo]:
    """Return up to `limit` most-recently-active sessions, newest first.

    If cwd_filter is given, sessions under that directory are preferred (listed
    first) but others still follow so the dispatcher can cross a boundary.
    """
    if not _PROJECTS.is_dir():
        return []
    files = sorted(
        _PROJECTS.glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    # Parse a bounded superset (limit*3) then rank, so cwd_filter can reorder
    # without missing recent matches.
    infos: list[SessionInfo] = []
    for p in files[: max(limit * 3, limit)]:
        info = _scan_file(p)
        if info:
            infos.append(info)

    if cwd_filter:
        cf = str(Path(cwd_filter).expanduser().resolve())
        infos.sort(
            key=lambda s: (not _under(s.cwd, cf), -s.last_active)
        )
    else:
        infos.sort(key=lambda s: -s.last_active)
    return infos[:limit]


def _under(cwd: str, base: str) -> bool:
    if not cwd:
        return False
    try:
        c = Path(cwd).resolve()
    except (OSError, ValueError):
        return False
    b = Path(base)
    return c == b or b in c.parents
