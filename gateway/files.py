"""File transfer helpers: save uploads, list, and stream downloads — all
sandboxed to the gateway's allowed directories.

Uploads land under files_dir/<subdir>/... (files_dir is itself inside an allowed
dir, so forked agents can read them via --add-dir). Every destination and every
download path is validated to stay inside allowed_dirs; names are sanitized so a
crafted name can't escape via `..` or an absolute path.
"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .config import Config

_CHUNK = 1 << 20  # 1 MiB


@dataclass
class SavedFile:
    name: str      # relative name as provided
    path: str      # absolute path on the node
    size: int
    sha256: str


class FileError(ValueError):
    """Bad request (caller error): traversal, too big, missing content, etc."""


def _within(cfg: Config, path):
    """cfg.within_allowed, but raise FileError (→ 400) instead of ValueError."""
    try:
        return cfg.within_allowed(path)
    except ValueError as e:
        raise FileError(str(e))


def _safe_join(dest_dir: Path, name: str) -> Path:
    """Join a client-provided relative name under dest_dir, rejecting escapes."""
    name = (name or "").strip().lstrip("/")
    if not name:
        raise FileError("empty file name")
    candidate = (dest_dir / name).resolve()
    dest = dest_dir.resolve()
    if candidate != dest and dest not in candidate.parents:
        raise FileError(f"file name escapes destination: {name!r}")
    return candidate


def job_dir(cfg: Config, job_id: str) -> Path:
    return Path(cfg.files_dir) / "jobs" / job_id


def upload_dir(cfg: Config, upload_id: str) -> Path:
    return Path(cfg.files_dir) / "uploads" / upload_id


def _write_stream(src, dest: Path, max_bytes: int) -> SavedFile:
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    size = 0
    with open(dest, "wb") as out:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise FileError(f"file exceeds max_file_mb ({max_bytes // (1<<20)} MiB)")
            h.update(chunk)
            out.write(chunk)
    return SavedFile(name=dest.name, path=str(dest), size=size, sha256=h.hexdigest())


def save_stream(cfg: Config, dest_dir: Path, name: str, src) -> SavedFile:
    """Stream a file-like `src` to dest_dir/name (used for multipart UploadFile)."""
    target = _safe_join(dest_dir, name)
    sf = _write_stream(src, target, cfg.files_max_file_mb << 20)
    sf.name = name
    return sf


def save_bytes(cfg: Config, dest_dir: Path, name: str, data: bytes) -> SavedFile:
    import io
    return save_stream(cfg, dest_dir, name, io.BytesIO(data))


def save_inline_item(cfg: Config, dest_dir: Path, item: dict) -> str:
    """Handle one JSON `files[]` element. Either:
        {"path": "/abs/existing"}                -> validate, return path (no copy)
        {"name": "...", "content_b64": "..."}    -> decode + write
        {"name": "...", "text": "..."}           -> write text
    Returns the absolute path the job can reference.
    """
    if item.get("path"):
        return str(_within(cfg, item["path"]))
    name = item.get("name")
    if not name:
        raise FileError("file item needs 'path', or 'name' with 'content_b64'/'text'")
    if "content_b64" in item:
        try:
            data = base64.b64decode(item["content_b64"], validate=True)
        except Exception as e:
            raise FileError(f"bad base64 for {name!r}: {e}")
    elif "text" in item:
        data = (item["text"] or "").encode("utf-8")
    else:
        raise FileError(f"file {name!r} needs 'content_b64' or 'text'")
    return save_bytes(cfg, dest_dir, name, data).path


def list_files(cfg: Config, dir_: str, glob: str = "*", recursive: bool = False) -> list[dict]:
    base = _within(cfg, dir_)
    if not base.is_dir():
        raise FileError(f"not a directory: {base}")
    it = base.rglob(glob) if recursive else base.glob(glob)
    out = []
    for p in it:
        is_dir = p.is_dir()
        st = p.stat()
        out.append({"path": str(p), "is_dir": is_dir,
                    "size": st.st_size if not is_dir else 0,
                    "mtime": st.st_mtime})
    out.sort(key=lambda d: -d["mtime"])
    return out


def open_for_download(cfg: Config, path: str):
    """Return (abs_path, size) after validating; caller streams it."""
    p = _within(cfg, path)
    if not p.is_file():
        raise FileError(f"not a file: {p}")
    return p, p.stat().st_size


def iter_file(path: Path, chunk: int = _CHUNK):
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk)
            if not data:
                break
            yield data
