"""Operator notes: one markdown file the gateway serves to every client.

Everything else a gateway advertises is configured (`/v1/agents`), probed
(`/v1/info`) or generated (`/llms.txt`). None of those can hold what a person
knows and a machine cannot discover — which account to charge, which partition
has the GPUs, which filesystem is nearly full. That knowledge otherwise lives in
one operator's head and is rediscovered, expensively, by every caller.

One file rather than a directory of them. The gateway runs on a login node its
owner already has a shell on, so the notes should be editable with an editor at
two in the morning as readily as through the API; a single `notes.md` is a
document a person can hold in their head, and it keeps the API to read, replace
and append.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Notes:
    """The document, and enough about it to judge and to write it safely."""

    text: str
    updated_at: float | None   # mtime, or None when the file is not there yet


class NotesStore:
    """Reads the document. Nothing here writes it.

    Deliberately: the gateway runs where the file lives, so the ways to change
    it already exist and are better than an endpoint — an agent with file tools
    edits it in place, `/v1/files` uploads it, or its owner opens an editor over
    ssh. A write API would be a fourth way to do what three things already do,
    and the only one that could clobber the other three.
    """

    def __init__(self, path: str, max_bytes: int) -> None:
        self._path = Path(path)
        self._max = max_bytes

    @property
    def path(self) -> str:
        return str(self._path)

    def read(self) -> Notes:
        """Whole-file, every call. The file is small and edited out of band, so
        a cache would only be a way to serve yesterday's answer."""
        try:
            text = self._path.read_text(encoding="utf-8")[: self._max]
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            # Absent and empty are the same thing to a reader; a gateway with
            # nothing to say should not look like one that is broken.
            return Notes(text="", updated_at=None)
        except OSError:
            # Unreadable is reported as empty on purpose. These notes ride on
            # `/v1/info`, and a stray permission or a path that turned out to
            # be a directory must not take the endpoint that answers "what is
            # this machine" down with it.
            return Notes(text="", updated_at=None)
        return Notes(text=text, updated_at=mtime)

