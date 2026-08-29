"""Watching work that outlives the turn that started it.

A coding-agent turn cannot sit for eight hours waiting on a scheduler, and a job
row held open for that long turns "still running" and "the delegate forgot to
report" into the same state. So the long tail is a *monitor*: its own row, with
its own lifecycle, created by the delegate and polled by the gateway.

The delegate authors the poll command, because it is the one that knows what it
submitted:

    poll = sacct -n -X -j 12345 --format=State

The gateway runs it on a timer and reads the first word of its output. Nothing
here knows what Slurm is -- `STATUS_MAP` is a lookup table with Slurm's state
names in it, the same way `cluster.py` runs `sinfo` without the probe set being
Slurm-specific. A command that prints `finished` works just as well, which is
what makes a background process, a file appearing, or a REST poll wrapped in
`curl` equally watchable.

Deliberately no `squeue` in the default sugar: `squeue` forgets a job the moment
it leaves the queue, so a finished run and a lost one look identical (empty
output). `sacct` keeps the state, and an empty read stays `unknown` rather than
being guessed either way.
"""
from __future__ import annotations

import re
import shlex

from .cluster import _run

#: Terminal for a monitor. `expired` is its own outcome on purpose: a deadline
#: passing means we stopped watching, which is not the same claim as the work
#: having failed.
TERMINAL = {"finished", "failed", "expired", "canceled"}

STATUSES = ("queued", "running", "finished", "failed", "expired", "canceled",
            "unknown")

#: First word of the poll output -> monitor status. Plain words so a command can
#: just say what happened; Slurm state names so `sacct --format=State` needs no
#: mapping at all. Unlisted output is `unknown`, which never transitions.
STATUS_MAP: dict[str, str] = {
    # what a command can say directly
    "queued": "queued", "pending": "queued", "waiting": "queued",
    "running": "running", "active": "running", "started": "running",
    "finished": "finished", "done": "finished", "complete": "finished",
    "ok": "finished", "success": "finished",
    "failed": "failed", "error": "failed", "fail": "failed",
    # Slurm, as reported by sacct/squeue --format=State
    "configuring": "queued", "resizing": "queued", "requeued": "queued",
    "suspended": "running", "completing": "running", "signaling": "running",
    "completed": "finished",
    "cancelled": "failed", "canceled": "failed", "timeout": "failed",
    "node_fail": "failed", "out_of_memory": "failed", "boot_fail": "failed",
    "deadline": "failed", "preempted": "failed", "revoked": "failed",
    "special_exit": "failed",
}

#: Slurm decorates a cancelled state with who did it ("CANCELLED by 12345").
_WORD = re.compile(r"[A-Za-z_]+")

#: One poll's output is a status, not a log. Enough to see why, bounded so a
#: chatty command cannot fill the row or the event.
MAX_DETAIL = 2000


def slurm_poll(job_id: str) -> str:
    """The command `--slurm 12345` expands to. Sugar, not a special case."""
    return f"sacct -n -X -j {shlex.quote(str(job_id))} --format=State"


def parse_map(spec: str | None) -> dict[str, str]:
    """`PENDING,RUNNING=running;COMPLETED=finished` on top of the defaults.

    An escape hatch for a scheduler or service whose words are not in the table,
    not a language the delegate is expected to learn: leaving it unset is the
    normal case.
    """
    table = dict(STATUS_MAP)
    for clause in (spec or "").split(";"):
        clause = clause.strip()
        if not clause or "=" not in clause:
            continue
        words, _, status = clause.partition("=")
        status = status.strip().lower()
        if status not in STATUSES:
            continue
        for word in words.split(","):
            word = word.strip().lower()
            if word:
                table[word] = status
    return table


def classify(output: str, spec: str | None = None) -> str:
    """The status a poll's output means, or `unknown` if it means nothing.

    First word only, lower-cased. `unknown` covers an empty read, a command that
    failed, and a word nobody mapped -- all three are "we did not learn
    anything", and none of them should move a monitor.
    """
    match = _WORD.search(output or "")
    if not match:
        return "unknown"
    return parse_map(spec).get(match.group(0).lower(), "unknown")


def poll(row: dict, timeout: float) -> tuple[str, str]:
    """Run one monitor's command. Returns (status, detail).

    Never raises: `cluster._run` already turns a missing binary, a timeout and a
    crash into `(False, text)`, and a monitor that cannot poll has to keep its
    row and its deadline rather than disappearing.
    """
    ok, text = _run(row["poll_cmd"], timeout, shell=True)
    detail = (text or "").strip()[:MAX_DETAIL]
    if not ok and not detail:
        return "unknown", "poll command produced no output and failed"
    status = classify(detail, row.get("map_spec"))
    return status, detail


def summary(row: dict) -> str:
    """One line for the event stream and `ab monitors`."""
    label = row.get("label") or row["id"][:8]
    detail = (row.get("detail") or "").splitlines()
    tail = f" ({detail[0][:120]})" if detail else ""
    return f"monitor {label}: {row['status']}{tail}"
