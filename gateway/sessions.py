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

    Metadata-only transcripts are dropped before the limit and before `total`,
    so a count never includes a row the caller cannot resume into. See
    `_has_message` for why that is affordable.
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
        info = _scan_file(newest)
        out.append(DirInfo(
            cwd=cwd, sessions=len(files), last_active=newest.stat().st_mtime,
            latest_session_id=newest.stem,
            latest_title=info.title if info else None))
    out.sort(key=lambda d: -d.last_active)
    return out


def _project_dirs() -> list[Path]:
    return [d for d in _PROJECTS.iterdir() if d.is_dir()]


def _has_message(path: Path, mtime: float, max_lines: int = 400) -> bool:
    """Does this transcript hold any conversation at all?

    Claude Code writes a transcript for every subagent, but a subagent's turns
    are recorded inline in its *parent*, so the child file ends up holding only
    `ai-title`/`agent-name` metadata -- a session id with nothing behind it.
    Resuming one lands in an empty conversation, so they are not sessions in the
    sense this index means, and 10 of the 106 rows here were exactly that.

    Cheap enough to run before the limit, which is the only place it works:
    filtering a page after slicing it frees no slots and returns short pages.
    The first real record lands on line 3 of a real transcript, so the early
    exit fires almost immediately -- 12 ms across 113 files and 573 MB here.
    Metadata-only files are read whole, but they are under 1.5 KB.
    """
    key = str(path)
    cached = _HAS_MSG.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    found = False
    try:
        with open(path, "r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    found = True   # far past any metadata preamble
                    break
                line = line.strip()
                if not line.startswith("{") or '"type"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") in ("user", "assistant"):
                    found = True
                    break
    except OSError:
        return False
    _HAS_MSG[key] = (mtime, found)
    return found


def _resumable(root: Path) -> list[Path]:
    """Transcripts in one folder that something could actually be resumed into."""
    return [p for p in root.glob("*.jsonl")
            if _RESUMABLE_ID.match(p.stem) and _has_message(p, p.stat().st_mtime)]


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
        with open(path, "r", errors="replace") as fh:
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
