"""Sandboxed file upload, listing, staging, and streamed download helpers."""
from __future__ import annotations

import base64
import bisect
import hashlib
import json
import os
import shutil
from fnmatch import fnmatch
from dataclasses import dataclass
from pathlib import Path

from .config import Config

_CHUNK = 1 << 20


@dataclass
class SavedFile:
    name: str
    path: str
    size: int
    sha256: str


class FileError(ValueError):
    pass


def _within(cfg: Config, path):
    try:
        return cfg.within_allowed(path)
    except ValueError as e:
        raise FileError(str(e))


def _safe_join(dest_dir: Path, name: str) -> Path:
    raw_name = name or ""
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_name):
        raise FileError("empty or invalid file name")
    name = raw_name.strip().lstrip("/\\")
    if not name:
        raise FileError("empty or invalid file name")
    candidate = (dest_dir / name).resolve()
    dest = dest_dir.resolve()
    if candidate != dest and dest not in candidate.parents:
        raise FileError(f"file name escapes destination: {name!r}")
    return candidate


def job_dir(cfg: Config, job_id: str) -> Path:
    return Path(cfg.files_dir) / "jobs" / job_id


def job_staging_dir(cfg: Config, job_id: str) -> Path:
    return Path(cfg.files_dir) / "jobs" / ".staging" / job_id


def upload_dir(cfg: Config, upload_id: str) -> Path:
    return Path(cfg.files_dir) / "uploads" / upload_id


def remove_tree(path: str | Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def cleanup_staging(cfg: Config) -> None:
    remove_tree(Path(cfg.files_dir) / "jobs" / ".staging")


def cleanup_orphan_job_dirs(cfg: Config, valid_job_ids: set[str]) -> None:
    """Remove promoted attachment dirs left by a crash before DB commit."""
    root = Path(cfg.files_dir) / "jobs"
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.name != ".staging" and child.is_dir() and \
                child.name not in valid_job_ids:
            remove_tree(child)


def validate_upload_names(items: list[dict], uploads: list) -> None:
    """Reject duplicate destinations before the first byte is written."""
    seen: set[str] = set()
    names: list[str] = []
    for item in items:
        if item.get("path"):
            continue
        if item.get("name"):
            names.append(str(item["name"]))
    for upload in uploads:
        names.append(str(getattr(upload, "filename", "") or ""))
    for name in names:
        target = _safe_join(Path.cwd() / ".agent-bridge-name-check", name)
        key = os.path.normcase(str(target.relative_to(
            (Path.cwd() / ".agent-bridge-name-check").resolve())))
        if key in seen:
            raise FileError(f"duplicate upload destination: {name!r}")
        seen.add(key)


def promote_staging(cfg: Config, job_id: str) -> tuple[Path, Path]:
    staging = job_staging_dir(cfg, job_id)
    final = job_dir(cfg, job_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        raise FileError(f"job file destination already exists: {final}")
    if staging.exists():
        os.replace(staging, final)
    return staging, final


def promoted_paths(paths: list[str], staging: Path, final: Path) -> list[str]:
    out = []
    s = staging.resolve()
    for raw in paths:
        p = Path(raw).resolve()
        if p == s or s in p.parents:
            out.append(str(final.resolve() / p.relative_to(s)))
        else:
            out.append(str(p))
    return out


def _write_stream(src, dest: Path, max_bytes: int) -> SavedFile:
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    size = 0
    try:
        with open(dest, "xb") as out:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise FileError(
                        f"file exceeds max_file_mb ({max_bytes // (1 << 20)} MiB)")
                h.update(chunk)
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return SavedFile(name=dest.name, path=str(dest), size=size, sha256=h.hexdigest())


def save_stream(cfg: Config, dest_dir: Path, name: str, src) -> SavedFile:
    target = _safe_join(dest_dir, name)
    saved = _write_stream(src, target, cfg.files_max_file_mb << 20)
    saved.name = name
    return saved


def save_bytes(cfg: Config, dest_dir: Path, name: str, data: bytes) -> SavedFile:
    import io
    return save_stream(cfg, dest_dir, name, io.BytesIO(data))


def save_inline_item(cfg: Config, dest_dir: Path, item: dict) -> str:
    if item.get("path"):
        target = _within(cfg, item["path"])
        if not target.is_file():
            raise FileError(f"remote attachment is not a regular file: {target}")
        return str(target)
    name = item.get("name")
    if not name:
        raise FileError("file item needs path, or name with content_b64/text")
    if "content_b64" in item:
        try:
            data = base64.b64decode(item["content_b64"], validate=True)
        except Exception as e:
            raise FileError(f"bad base64 for {name!r}: {e}")
    elif "text" in item:
        data = (item["text"] or "").encode("utf-8")
    else:
        raise FileError(f"file {name!r} needs content_b64 or text")
    return save_bytes(cfg, dest_dir, name, data).path


def list_files(cfg: Config, dir_: str, glob: str = "*",
               recursive: bool = False) -> list[dict]:
    return list_files_page(cfg, dir_, glob, recursive, 1000, None)[0]


def _file_cursor_encode(relative: str) -> str:
    payload = json.dumps({"after": relative}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _file_cursor_decode(cursor: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        after = value["after"]
        if not isinstance(after, str):
            raise ValueError
        return after
    except Exception as exc:
        raise FileError("invalid file cursor") from exc


def _walk_paths(base: Path, recursive: bool):
    """Scan lazily; callers retain only their bounded result window."""
    def visit(directory: Path):
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    yield path
                    if recursive and entry.is_dir(follow_symlinks=False):
                        yield from visit(path)
        except OSError as exc:
            raise FileError(f"cannot list directory {directory}: {exc}") from exc
    yield from visit(base)


def list_files_page(cfg: Config, dir_: str, glob: str = "*",
                    recursive: bool = False, limit: int = 200,
                    cursor: str | None = None) -> tuple[list[dict], str | None, bool]:
    if not 1 <= int(limit) <= 1000:
        raise FileError("limit must be between 1 and 1000")
    base = _within(cfg, dir_)
    if not base.is_dir():
        raise FileError(f"not a directory: {base}")
    after = _file_cursor_decode(cursor) if cursor else None
    selected: list[tuple[str, Path]] = []
    for path in _walk_paths(base, recursive):
        relative = path.relative_to(base).as_posix()
        if after is not None and relative <= after:
            continue
        match_value = relative if recursive else path.name
        if not fnmatch(match_value, glob):
            continue
        bisect.insort(selected, (relative, path))
        if len(selected) > limit + 1:
            selected.pop()
    has_more = len(selected) > limit
    visible = selected[:limit]
    rows = []
    for _relative, path in visible:
        is_dir = path.is_dir()
        stat = path.stat()
        rows.append({"path": str(path), "is_dir": is_dir,
                     "size": 0 if is_dir else stat.st_size,
                     "mtime": stat.st_mtime})
    next_cursor = _file_cursor_encode(visible[-1][0]) \
        if has_more and visible else None
    return rows, next_cursor, has_more


def open_for_download(cfg: Config, path: str):
    target = _within(cfg, path)
    if not target.is_file():
        raise FileError(f"not a file: {target}")
    return target, target.stat().st_size


def iter_file(path: Path, chunk: int = _CHUNK):
    with open(path, "rb") as source:
        while True:
            data = source.read(chunk)
            if not data:
                break
            yield data
