"""Scan Claude Code session transcripts into a compact index the dispatcher
can reason over when choosing which session to fork.

Sessions live at ~/.claude/projects/<slugified-cwd>/<session-uuid>.jsonl
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, asdict

from .api_models import iso_local
from pathlib import Path

_PROJECTS = Path.home() / ".claude" / "projects"
_SKIP_LINE = re.compile(
    r"^\s*<(local-command-caveat|command-name|command-message|command-args|"
    r"local-command-stdout|system-reminder|command-stdout)\b",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
# Claude Code renames abandoned transcripts to `<uuid>.orphaned-<epoch>-<hash>`,
# whose stem is not a session id anything can resume. Filtering on the stem
# keeps unusable rows out of both the listings and the per-directory counts.
_RESUMABLE_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
# Folder -> (newest mtime, cwd). One folder is one project, so this is stable
# until that folder changes.
_DIR_CWD: dict[str, tuple[float, str]] = {}
# Transcript -> (mtime, has-conversation). A file that later gains messages
# gains an mtime with them, so the entry invalidates itself.
_HAS_MSG: dict[str, tuple[float, bool]] = {}


def _norm(path: str) -> str:
    """Compare directories across backends.

    Claude records `D:\\dotty-projects\\molly`; opencode records
    `D:/dotty-projects/molly`. Same directory, different spelling, and on
    Windows case differs too.
    """
    return os.path.normcase(os.path.normpath(str(Path(path).expanduser())))


def _cursor_encode(last_active: float, key: str) -> str:
    raw = json.dumps([last_active, key], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(cursor: str) -> tuple[float, str]:
    """Sessions have no monotonic sequence, so paging is by (time, id).

    An `after=N` cursor would be wrong here: timestamps tie and drift, and a
    tie would silently skip or repeat a row.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        ts, key = json.loads(raw)
        return float(ts), str(key)
    except Exception as exc:
        raise ValueError("invalid cursor") from exc


@dataclass
class DirInfo:
    cwd: str
    sessions: int
    last_active: float
    latest_session_id: str | None
    latest_title: str | None

    def to_public(self) -> dict:
        d = asdict(self)
        d["last_active"] = iso_local(self.last_active)
        return d


@dataclass
class SessionPage:
    sessions: list["SessionInfo"]
    total: int
    next_cursor: str | None


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
        # Published as ISO like every other timestamp the API hands out; the
        # epoch float stays on the dataclass for sorting the index.
        d["last_active"] = iso_local(self.last_active)
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
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
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

    Unlike the listings, this does not skip metadata-only transcripts. A listing
    is a recommendation and should offer only sessions worth continuing; a
    lookup by explicit id is an instruction, and reporting "no such session"
    about a file that plainly exists would send the caller somewhere worse.
    """
    if not session_id or not _PROJECTS.is_dir():
        return None
    for path in _PROJECTS.glob(f"*/{session_id}.jsonl"):
        info = _scan_file(path)
        if info:
            return info
    return None


def scan(limit: int = 40, cwd: str | None = None,
         cursor: str | None = None) -> SessionPage:
    """One page of sessions, newest first, optionally for a single directory.

    `cwd` is an **exact** directory match, not a prefix: a project and its
    sub-projects keep separate indexes, so a count means what it says.

    The old shape parsed the newest `limit * 3` transcripts *globally* and only
    then ranked by cwd, so a session in a quiet project was invisible whenever
    a busy one had filled the window. Filtering the candidate files first makes
    the bound per-directory, and `total` reports the real size either way.

    Transcripts in which nothing happened -- subagent metadata, slash-command
    residue -- are dropped before the limit and before `total`, so a count never
    includes a row the caller cannot resume into. See `_has_conversation` for
    what counts and why it is affordable.
    """
    if not _PROJECTS.is_dir():
        return SessionPage([], 0, None)
    if cwd:
        target = _norm(cwd)
        roots = [d for d in _project_dirs() if _dir_cwd(d) and _norm(_dir_cwd(d)) == target]
    else:
        roots = _project_dirs()

    files = [p for root in roots for p in _resumable(root)]
    files.sort(key=lambda p: (-p.stat().st_mtime, p.name))
    total = len(files)

    if cursor:
        after_ts, after_id = _cursor_decode(cursor)
        files = [p for p in files
                 if (-p.stat().st_mtime, p.name) > (-after_ts, after_id)]

    page = files[:limit]
    infos = [info for info in (_scan_file(p) for p in page) if info]
    nxt = None
    if len(files) > limit and page:
        last = page[-1]
        nxt = _cursor_encode(last.stat().st_mtime, last.name)
    return SessionPage(infos, total, nxt)


def list_dirs() -> list[DirInfo]:
    """Every directory that has sessions, complete and unpaged.

    This is the view an agent needs *before* it knows which project to ask
    about, and it is deliberately never truncated: the count is bounded by how
    many projects exist (tens), not by a window, so nothing can silently drop
    out of it the way sessions used to.
    """
    if not _PROJECTS.is_dir():
        return []
    out: list[DirInfo] = []
    for d in _project_dirs():
        cwd = _dir_cwd(d)
        if not cwd:
            continue                      # metadata-only folder; nothing to resume
        files = _resumable(d)
        if not files:
            continue                      # only stubs here; nothing to resume
        newest = max(files, key=lambda p: p.stat().st_mtime)
        out.append(DirInfo(
            cwd=cwd, sessions=len(files), last_active=newest.stat().st_mtime,
            latest_session_id=newest.stem,
            latest_title=_first_title(newest)))
    out.sort(key=lambda d: -d.last_active)
    return out


def _project_dirs() -> list[Path]:
    return [d for d in _PROJECTS.iterdir() if d.is_dir()]


def _has_conversation(path: Path, mtime: float, max_lines: int = 400) -> bool:
    """Did anything actually happen in this transcript?

    Two kinds of file carry a session id with nothing behind it, and counting
    `user`/`assistant` records does not separate either of them from real work.

    **Subagent transcripts.** Claude Code writes one per subagent but records the
    subagent's turns inline in its *parent*, so the child keeps only
    `ai-title`/`agent-name`. Ten of 106 rows here.

    **Slash-command residue.** Someone types `/login` or `/resume`, and the
    caveat, the command and its stdout are each stored as a `user` record. Eleven
    more rows here, showing `messages` of 2-6 and holding no prompt, no reply and
    no tool call -- `/resume` answered "that session is still running as a
    background agent" and the session was abandoned.

    So the test is whether a human spoke or the agent acted: a `user` record with
    text surviving `_clean_user_text`, or an assistant `tool_use`. The tool_use
    arm matters for a custom slash command that drives real work with no prose;
    none exist on this store, but the arm costs nothing and its absence would be
    a false negative, which is the failure this index keeps being fixed for.

    Cheap enough to run before the limit, which is the only place it works:
    filtering a page after slicing it frees no slots and returns short pages. A
    real prompt lands within the first few records, so the early exit fires
    almost immediately. Past `max_lines` the answer is yes -- an unbounded read
    is not worth a rarer mistake in the safe direction.
    """
    key = str(path)
    cached = _HAS_MSG.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    found = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    found = True
                    break
                line = line.strip()
                if not line.startswith("{") or '"type"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                if rtype == "user":
                    if _clean_user_text(msg.get("content")):
                        found = True
                        break
                elif rtype == "assistant":
                    content = msg.get("content")
                    if isinstance(content, list) and any(
                            isinstance(b, dict) and b.get("type") == "tool_use"
                            for b in content):
                        found = True
                        break
    except OSError:
        return False
    _HAS_MSG[key] = (mtime, found)
    return found


def _first_title(path: Path, max_lines: int = 400) -> str | None:
    """The first human-authored prompt, read without parsing the whole file.

    `list_dirs` wants one title per directory and nothing else, but `_scan_file`
    parses up to 4000 lines to also count messages and collect a summary that
    this caller discards -- 379 ms of the 390 ms the dirs view used to cost, on
    the one call an agent makes before it knows which project to ask about.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line.startswith("{") or '"user"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message")
                if rec.get("type") == "user" and isinstance(msg, dict):
                    text = _clean_user_text(msg.get("content"))
                    if text:
                        return text[:200]
    except OSError:
        return None
    return None


def _resumable(root: Path) -> list[Path]:
    """Transcripts in one folder that something could actually be resumed into."""
    return [p for p in root.glob("*.jsonl")
            if _RESUMABLE_ID.match(p.stem) and _has_conversation(p, p.stat().st_mtime)]


def _dir_cwd(project_dir: Path) -> str | None:
    """The working directory a folder's sessions belong to.

    One folder holds exactly one project, so this is read once per folder
    rather than once per transcript -- 17 bounded reads here instead of 120
    full parses. Derived from the recorded `cwd` rather than un-slugifying the
    folder name: that mapping is undocumented and lossy, and guessing it wrong
    would drop sessions, which is the bug being fixed.
    """
    key = str(project_dir)
    # Only transcripts with a conversation, and for a load-bearing reason: a
    # metadata-only stub records no cwd, so probing the newest few files raw
    # would resolve the whole folder to nothing whenever enough subagent stubs
    # landed on top of the real work -- and every session in it would drop out
    # of both views at once. Subagents produce those constantly.
    files = sorted(_resumable(project_dir),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    stamp = files[0].stat().st_mtime
    cached = _DIR_CWD.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    for candidate in files[:3]:
        cwd = _first_cwd(candidate)
        if cwd:
            _DIR_CWD[key] = (stamp, cwd)
            return cwd
    return None


def _first_cwd(path: Path, max_lines: int = 50) -> str | None:
    """`cwd` from the first records of a transcript. It lands on line 3-4."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("cwd"):
                    return rec["cwd"]
    except OSError:
        return None
    return None
