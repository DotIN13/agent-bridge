"""The directory a job reports through.

A delegate should not need to know its own job id, the gateway's url, or the
auth token in order to say what happened -- and until this existed it needed all
three. `ab-notify` resolved the job id from `$AB_JOB_ID`, which nothing in the
gateway ever set, so a job whose caller had not pasted the uuid into the prompt
could not close itself (docs/todo/13).

So each job is handed one directory instead, in `$AB_JOB_DIR`:

    $AB_JOB_DIR/
      status                  one word: running | finished | failed
      progress/001-slug.md    a milestone; any name, ingested in name order
      report.md               the deliverable, when it outgrows the turn
      monitors/<id>.json      written by `ab-monitor`, read by the gateway

Every readable file becomes one `message` event on that job's stream, so a
delegate reports with `echo` and `cp` and nothing else. The format is
deliberately files-and-words rather than JSON: a batch script that has to quote
JSON in bash gets it wrong eventually, and this is the path that has to work
when everything else about the run has already gone sideways.

Nothing here writes into the job dir. The gateway creates it and reads it; the
delegate owns its contents.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

STATUS_FILE = "status"
PROGRESS_DIR = "progress"
REPORT_FILE = "report.md"
MONITORS_DIR = "monitors"

#: Statuses a `status` file may name. Anything else is kept verbatim as
#: `unknown` rather than dropped -- a typo should be visible to the caller, not
#: silently equivalent to having written nothing.
STATUSES = ("queued", "running", "finished", "failed")

#: Matches the per-line cap on the JSONL fallback, for the same reason: one
#: report must not be able to exhaust memory or the event row it lands in.
MAX_FILE_BYTES = 64 * 1024

#: A job dir with more files than this is a loop, not a report. The excess is
#: ignored, and the overflow itself is reported as one event.
MAX_FILES = 200


@dataclass(frozen=True)
class Drop:
    """One file found in a job dir, ready to become a `message` event."""

    rel: str            # path relative to the job dir, POSIX separators
    text: str           # content, truncated to MAX_FILE_BYTES
    digest: str         # sha256 of the full bytes, so a rewrite is a new drop
    oversized: bool
    status: str | None  # set only by the status file


#: Beside `messages/` and the file store rather than under either. Not
#: `<data_dir>/jobs/` specifically: the file store already keeps promoted
#: attachments in `<files_dir>/jobs/<job_id>`, and `promote_staging` renames a
#: whole directory into that name and fails if it already exists -- so a
#: deployment that pointed `[files] dir` at the data dir would have the two
#: collide on every job with an attachment.
_ROOT = "reports"


def path_for(data_dir: str | os.PathLike[str], job_id: str) -> Path:
    return Path(data_dir) / _ROOT / job_id


def prepare(data_dir: str | os.PathLike[str], job_id: str) -> Path:
    """Create the directory tree a job reports through, and return its root.

    `progress/` and `monitors/` are created up front so the delegate never has
    to `mkdir -p` first -- one less thing for a brief to have to say, and one
    less way for a milestone drop to fail at the moment it matters.
    """
    root = path_for(data_dir, job_id)
    (root / PROGRESS_DIR).mkdir(parents=True, exist_ok=True)
    (root / MONITORS_DIR).mkdir(parents=True, exist_ok=True)
    return root


def publish(path: str | os.PathLike[str], text: str) -> None:
    """Write a file the way a reader may safely see it: whole, or not at all.

    A reader can scan the directory at any moment, so a partially written
    `status` must never be visible. Same temp-then-rename discipline the file
    store uses for downloads.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def scan(job_dir: str | os.PathLike[str]) -> list[Drop]:
    """Every ingestible file in the job dir, in a stable order.

    Order is by relative path, not mtime: on a shared filesystem mtime is the
    less trustworthy of the two, and a name the delegate chose (`001-`, `002-`)
    is something a brief can ask for and a reader can predict.
    """
    root = Path(job_dir)
    if not root.is_dir():
        return []
    candidates = _candidates(root)
    drops = [_read(root, rel) for rel in candidates[:MAX_FILES]]
    if len(candidates) > MAX_FILES:
        # One event about the overflow, rather than either dropping the excess
        # silently or ingesting a runaway loop's output forever.
        drops.append(Drop(
            rel="…overflow",
            text=(f"job dir holds {len(candidates)} readable files; only the "
                  f"first {MAX_FILES} are ingested"),
            digest=hashlib.sha256(str(len(candidates)).encode()).hexdigest(),
            oversized=False, status=None))
    return drops


def _candidates(root: Path) -> list[str]:
    """Relative paths worth reading, in ingestion order.

    Deliberately narrow. `status` first so a drop written in the same instant as
    a finish is ordered before it; then milestones in name order; then the
    report. Anything else in the directory -- scratch files, a `.tmp` mid-rename,
    the monitors the gateway itself reads -- is not a report and is skipped.
    """
    names: list[str] = []
    if (root / STATUS_FILE).is_file():
        names.append(STATUS_FILE)
    progress = root / PROGRESS_DIR
    if progress.is_dir():
        try:
            entries = sorted(p.name for p in progress.iterdir()
                             if p.is_file() and not p.name.endswith(".tmp"))
        except OSError:
            entries = []
        names += [f"{PROGRESS_DIR}/{name}" for name in entries]
    if (root / REPORT_FILE).is_file():
        names.append(REPORT_FILE)
    return names


def _read(root: Path, rel: str) -> Drop | None:
    path = root / rel
    try:
        with open(path, "rb") as stream:
            head = stream.read(MAX_FILE_BYTES + 1)
            digest = hashlib.sha256()
            digest.update(head)
            oversized = len(head) > MAX_FILE_BYTES
            while True:
                chunk = stream.read(MAX_FILE_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        # A file that cannot be read is worth an event: it is usually a
        # permission mistake in the batch script, and silence would present it
        # as "the delegate never reported".
        return Drop(rel=rel, text=f"could not be read: {exc}",
                    digest=hashlib.sha256(str(exc).encode()).hexdigest(),
                    oversized=False, status="unknown")
    text = head[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    status = _status_word(text) if rel == STATUS_FILE else None
    return Drop(rel=rel, text=text.strip() if rel == STATUS_FILE else text,
                digest=digest.hexdigest(), oversized=oversized, status=status)


def _status_word(text: str) -> str:
    word = text.strip().split()[0].lower() if text.strip() else ""
    return word if word in STATUSES else "unknown"


def job_id_from(job_dir: str | os.PathLike[str]) -> str:
    """The job a directory belongs to.

    `$AB_JOB_DIR` ends in the job id, which is how `ab-monitor` knows which job
    it is registering a monitor for without being told -- the same trick that
    let the job dir replace `$AB_JOB_ID` in the first place.
    """
    return Path(job_dir).name


def monitor_drops(job_dir: str | os.PathLike[str]) -> list[tuple[str, dict]]:
    """Monitor registrations dropped into `monitors/`, as (id, fields).

    Key-value text rather than JSON, for the same reason the rest of the job dir
    is files and words -- this has to be writable from a batch script with a
    heredoc and no quoting rules:

        cat > "$AB_JOB_DIR/monitors/train" <<'EOF'
        poll = sacct -n -X -j 12345 --format=State
        interval = 300
        result = /project/x/runs/RESULTS.md
        EOF

    The file name is the monitor's identity within the job, so re-writing the
    same name updates nothing and registers nothing twice.
    """
    root = Path(job_dir) / MONITORS_DIR
    if not root.is_dir():
        return []
    try:
        names = sorted(p.name for p in root.iterdir()
                       if p.is_file() and not p.name.endswith(".tmp"))
    except OSError:
        return []
    out: list[tuple[str, dict]] = []
    for name in names[:MAX_FILES]:
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fields: dict[str, str] = {}
        for line in text.splitlines()[:50]:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields[key.strip().lower()] = value.strip()
        if fields.get("poll") or fields.get("slurm"):
            out.append((name, fields))
    return out


#: How much of `report.md` rides along on a terminal `status`, as the reason.
REASON_CHARS = 2000


def event_data(drop: Drop, reason: str = "") -> dict:
    """The `message` payload for one drop.

    Shaped like an `ab-notify` report on purpose -- same `status`/`msg` keys, so
    `ab events --type message` reads the same whichever way the report arrived,
    and `_close_awaiting_locked` needs no special case.

    `reason` is the report's own text, passed in when a terminal `status` is
    ingested alongside a `report.md`. Without it a failed job's row reads "batch
    work reported failed" while the actual reason sits in a different event --
    true, but it makes `ab wait` exit 3 with nothing to act on.
    """
    data: dict = {"source": "job_dir", "file": drop.rel}
    if drop.status is not None:
        data["status"] = drop.status
        if drop.status == "unknown" and drop.text:
            data["raw"] = drop.text[:2000]
        if drop.status in ("finished", "failed") and reason:
            data["msg"] = reason[:REASON_CHARS]
        return data
    if drop.oversized:
        data["error"] = f"{drop.rel} exceeded {MAX_FILE_BYTES} bytes; truncated"
    data["msg"] = drop.text
    return data
