#!/usr/bin/env python3
"""Report a milestone from inside a job; stdlib only.

One job, one verb: put a message where the caller will see it while the work is
still going.

    ab-notify --msg "server up, generating"
    ab-notify --msg-file "$RUNS/step-3.log" --report-id step-3

It writes a file into `$AB_JOB_DIR/progress/`, which the gateway ingests as one
`message` event on this job's stream. That is all it does, and it is a
convenience rather than a dependency -- the same milestone is

    echo "server up, generating" > "$AB_JOB_DIR/progress/010-up.md"

What it no longer has is `--status`. Reporting a *status* used to be its point:
it resolved a job id, a url and a token, then tried HTTP, a shared JSONL drop and
a local one, so that `--status finished` could close a job parked in
`awaiting_report`. A job now ends when its turn ends, and long-running work is a
monitor with its own lifecycle (`ab-monitor`), so nothing needs to be told that
the work is over. What remains worth saying is what happened along the way.

`--status` is therefore accepted and ignored rather than fatal, with a note
naming the remedy: an sbatch file already on a compute node cannot be edited in
lockstep with the gateway, and exiting non-zero under `set -e` would cost the
whole run rather than one milestone.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from pathlib import Path

_SAFE = re.compile(r"[^a-z0-9._-]+")

#: A milestone is a note, not a log. Bounded here so an accidental
#: `--msg-file some.tar` is refused locally rather than truncated by the gateway.
MAX_BYTES = 64 * 1024


def _job_dir(explicit: str | None, job_id: str | None,
             data_dir: str | None) -> Path | None:
    """Where to write. `$AB_JOB_DIR` first, then rebuilt from the old flags.

    The rebuild is what keeps `#SBATCH --export=ALL,AB_JOB_ID=…,AB_DATA_DIR=…`
    working: those two are what older batch scripts carry, and they are enough to
    name the directory.
    """
    for candidate in (explicit, os.environ.get("AB_JOB_DIR")):
        if candidate:
            return Path(candidate)
    base = data_dir or os.environ.get("AB_DATA_DIR")
    if base and job_id:
        return Path(base) / "reports" / job_id
    return None


def _name(report_id: str | None) -> str:
    """The milestone's file name, which is also its identity.

    With `--report-id`, a stable name: a retried step overwrites its own
    milestone instead of adding a second one, which is what that flag was always
    for. Without one, a timestamp that sorts the way it happened -- milestones
    are ingested in name order -- plus four random hex, because two parallel
    steps reporting in the same second are two milestones and must not collide.
    """
    slug = _SAFE.sub("-", (report_id or "").strip().lower()).strip("-")
    if slug:
        return f"{slug[:60]}.md"
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    return f"{stamp}-{uuid.uuid4().hex[:4]}.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ab-notify", description="report a milestone from inside a job")
    body = parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--msg", help="the milestone, inline")
    body.add_argument("--msg-file", help="the milestone, read from a file")
    parser.add_argument("--report-id", default=os.environ.get("AB_REPORT_ID"),
                        help="stable name for this milestone; a retry with the "
                             "same one overwrites instead of adding another")
    parser.add_argument("--job-dir", help="defaults to $AB_JOB_DIR")
    parser.add_argument("--job-id", default=os.environ.get("AB_JOB_ID"),
                        help="with --data-dir, rebuilds the job dir path")
    parser.add_argument("--data-dir", default=os.environ.get("AB_DATA_DIR"))
    # Accepted and ignored. Every call is a milestone now; see the module
    # docstring for why this is not an error.
    parser.add_argument("--status", help=argparse.SUPPRESS)
    return parser


def main(argv=None) -> int:
    args, unknown = build_parser().parse_known_args(argv)
    for flag in unknown:
        if flag.startswith("-"):
            print(f"ab-notify: ignoring {flag} — a milestone needs no url or "
                  f"token now, only $AB_JOB_DIR", file=sys.stderr)
    if args.status:
        remedy = ("write it yourself: echo %s > \"$AB_JOB_DIR/status\""
                  % args.status) if args.status in ("finished", "failed") else \
            "progress is what this reports"
        print(f"ab-notify: --status {args.status} is ignored; every call is a "
              f"milestone now ({remedy})", file=sys.stderr)

    root = _job_dir(args.job_dir, args.job_id, args.data_dir)
    if root is None:
        print("ab-notify: no $AB_JOB_DIR, and no --job-id/--data-dir to rebuild "
              "one from. Nothing was reported.", file=sys.stderr)
        return 2

    text = args.msg or ""
    if args.msg_file:
        try:
            raw = Path(args.msg_file).read_bytes()
        except OSError as exc:
            print(f"ab-notify: cannot read --msg-file: {exc}", file=sys.stderr)
            return 2
        if len(raw) > MAX_BYTES:
            print(f"ab-notify: --msg-file is larger than {MAX_BYTES} bytes; a "
                  f"milestone is a note. Copy the file to "
                  f"\"$AB_JOB_DIR/report.md\" instead, or point --msg-file at an "
                  f"excerpt.", file=sys.stderr)
            return 2
        text = raw.decode("utf-8", errors="replace")

    target = root / "progress" / _name(args.report_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # The gateway may scan this directory at any moment, so a half-written
        # milestone must never be visible under a live name.
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        print(f"ab-notify: cannot write the milestone: {exc}", file=sys.stderr)
        return 1

    print(f"ab-notify: reported {target.parent.name}/{target.name} in {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
