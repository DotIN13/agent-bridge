"""The directory a job reports through.

A delegate should not need to know its own job id, the gateway's url, or the
auth token in order to say what happened -- and until this existed it needed all
three. `ab-notify` resolved the job id from `$AB_JOB_ID`, which nothing in the
gateway ever set, so a job whose caller had not pasted the uuid into the prompt
could not close itself (docs/todo/13).

So each job is handed one directory instead, in `$AB_JOB_DIR`:

    $AB_JOB_DIR/
      progress/001-slug.md    a milestone, written by `ab-notify`
      report.md               the deliverable, and the job's result
      monitors/<name>         key-values, written by `ab-monitor`

Every readable file becomes one `message` event on that job's stream. Two of the
three names have a tool that writes them -- `ab-notify` for a milestone,
`ab-monitor` for a watch -- and a brief should name the tool rather than the
path: one thing to remember, and the sort order, the size bound and the naming
are then somebody else's problem. `report.md` is the delegate's own `cp` or
heredoc, because it is a document rather than an event.

The format is deliberately files-and-words rather than JSON: a batch script that
has to quote JSON in bash gets it wrong eventually, and this is the path that has
to work when everything else about the run has already gone sideways.

Nothing here writes into the job dir. The gateway creates it and reads it; the
delegate owns its contents.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

PROGRESS_DIR = "progress"
REPORT_FILE = "report.md"
MONITORS_DIR = "monitors"

#: A `status` file is read like any other: one more milestone. It used to name
#: a job's state, back when a row could park and wait to be closed; the turn's
#: end decides that now (design/16), so the word means nothing to the gateway
#: and a delegate that writes one out of habit is simply heard.
STATUS_FILE = "status"

#: One report must not be able to exhaust memory or the event row it lands in.
#: `ab-notify` refuses a larger `--msg-file` up front for the same reason.
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


#: Beside the file store rather than under it. Not `<data_dir>/jobs/`
#: specifically: the file store already keeps promoted
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
            oversized=False))
    return drops


def _candidates(root: Path) -> list[str]:
    """Relative paths worth reading, in ingestion order.

    Deliberately narrow: `status` if present, then milestones in name order,
    then the report. Anything else in the directory -- scratch files, a `.tmp`
    mid-rename, the monitors the gateway itself reads -- is not a report and is
    skipped.
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
                    oversized=False)
    text = head[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    return Drop(rel=rel, text=text.strip() if rel == STATUS_FILE else text,
                digest=digest.hexdigest(), oversized=oversized)


def read_report(job_dir: str | os.PathLike[str]) -> str | None:
    """The deliverable's text, or ``None`` when there is not one yet.

    This is the job's *result* (design/23), not merely one more milestone: it is
    written into the `result` column so `ab job <ref>` prints it, alongside the
    `message` event the same file lands as. Two channels for one file, because
    they answer different questions -- the event stream says *when* the work
    reported, and the row says *what the answer was* without reading a stream.

    Bounded by the same `MAX_FILE_BYTES` as ingestion. A report is a document,
    not an artifact store: the paths it names are how the big things travel.
    """
    report = Path(job_dir) / REPORT_FILE
    try:
        with open(report, "rb") as stream:
            head = stream.read(MAX_FILE_BYTES)
    except OSError:
        return None
    text = head.decode("utf-8", errors="replace").strip()
    return text or None


def has_report(job_dir: str | os.PathLike[str]) -> bool:
    """Is the deliverable there?

    A job is finished when its turn has ended *and* it has written its report,
    so this is the predicate the lifecycle turns on (design/17). Emptiness
    counts as absent: a zero-byte `report.md` is a delegate that started to
    write and did not, which is exactly the case worth waiting out.
    """
    report = Path(job_dir) / REPORT_FILE
    try:
        return report.is_file() and report.stat().st_size > 0
    except OSError:
        return False


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


def event_data(drop: Drop) -> dict:
    """The `message` payload for one drop.

    `file` and `msg`, and nothing that claims to be a state: a drop is something
    the delegate said, not something the gateway acts on.
    """
    data: dict = {"source": "job_dir", "file": drop.rel}
    if drop.oversized:
        data["error"] = f"{drop.rel} exceeded {MAX_FILE_BYTES} bytes; truncated"
    data["msg"] = drop.text
    return data
