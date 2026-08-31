#!/usr/bin/env python3
"""Register a watch on work that outlives the turn; stdlib only.

Run by the delegate on the gateway host, usually as the last thing it does
before ending its turn:

    ab-monitor add --slurm 12345 --result "$RUNS/RESULTS.md" --note "8h train"

It writes a file into `$AB_JOB_DIR/monitors/` and exits. The gateway picks it up
on its next sweep and starts polling.

Writing a file rather than calling the API is the whole point. `ab-notify`, which
this replaces, needed a job id (which nothing set), a url and a token, with a
three-tier fallback for when the compute node could not reach the gateway -- and
every one of those was a way for a report to be lost. A file in a directory the
gateway already reads needs none of them, works when HTTP does not, and the job
id comes from the directory's own name.

There is nothing here a shell cannot do, and that is deliberate: this tool is a
convenience over

    cat > "$AB_JOB_DIR/monitors/train" <<'EOF'
    poll = sacct -n -X -j 12345 --format=State
    interval = 15m
    EOF

not a dependency. A delegate that cannot find `ab-monitor` should write the file.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
import uuid
from pathlib import Path

_SAFE_NAME = re.compile(r"[^a-z0-9._-]+")


def _job_dir(explicit: str | None) -> Path:
    for candidate in (explicit, os.environ.get("AB_JOB_DIR")):
        if candidate:
            return Path(candidate)
    raise SystemExit(
        "ab-monitor: no job dir. Pass --job-dir, or run this inside a job, "
        "where $AB_JOB_DIR is set.")


def _name(label: str | None) -> str:
    """A file name that is also the watch's identity within the job.

    Derived from the label so re-running the same registration updates nothing
    and registers nothing twice; a uuid when there is no label, because two
    unlabelled watches on one job are two watches.
    """
    slug = _SAFE_NAME.sub("-", (label or "").strip().lower()).strip("-")
    return slug[:60] or f"watch-{uuid.uuid4().hex[:8]}"


def _fields(args) -> list[str]:
    poll = args.poll
    if args.slurm:
        poll = f"sacct -n -X -j {shlex.quote(args.slurm)} --format=State"
    lines = [f"poll = {poll}"]
    for key, value in (("label", args.label), ("interval", args.interval),
                       ("deadline", args.deadline), ("map", args.map),
                       ("note", args.note)):
        if value:
            lines.append(f"{key} = {value}")
    if args.result:
        lines.append("result = " + ",".join(args.result))
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ab-monitor",
        description="watch work that outlives an agent turn")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="register a watch")
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument("--slurm", metavar="JOBID",
                        help="watch a Slurm job by id (reads sacct, not squeue: "
                             "squeue forgets a job once it leaves the queue)")
    source.add_argument("--poll", metavar="CMD",
                        help="a command whose first word of output is the "
                             "status; plain words (running/finished/failed) and "
                             "Slurm state names are both understood")
    add.add_argument("--label", help="short name, also the watch's identity")
    add.add_argument("--interval", help="how often to poll: 300, 90s, 15m, 12h")
    add.add_argument("--deadline", help="stop watching after: 12h, 2d")
    add.add_argument("--map", help="extra words: 'GREEN=finished;RED=failed'")
    add.add_argument("--note", help="what this is, for whoever reads it later")
    add.add_argument("--result", action="append", metavar="PATH",
                     help="a file the caller will want when this finishes "
                          "(repeatable)")
    add.add_argument("--job-dir", help="defaults to $AB_JOB_DIR")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    root = _job_dir(args.job_dir) / "monitors"
    name = _name(args.label)
    try:
        root.mkdir(parents=True, exist_ok=True)
        target = root / name
        # Same publish discipline as the rest of the job dir: the gateway may
        # read this directory at any moment, so a half-written registration must
        # never be visible.
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text("\n".join(_fields(args)) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        print(f"ab-monitor: cannot write the watch: {exc}", file=sys.stderr)
        return 1
    job = Path(root).parent.name
    print(f"ab-monitor: watching, as {job}:{name}")
    print(f"  the caller sees it with: ab monitors --job {job}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
