#!/usr/bin/env python3
"""Compatibility shim. Reporting is a directory now; write to it instead.

`ab-notify` existed because the gateway could not see a compute node, so the
compute node had to call in: it resolved a job id from `$AB_JOB_ID`, a url from
`gateway-endpoint.json`, and a token from the data dir, then tried HTTP, then a
shared-filesystem JSONL drop, then a local one. Every one of those was a way for
a report to be lost, and the first was worse than that -- nothing in the gateway
ever set `$AB_JOB_ID`, so a job whose caller had not pasted the uuid into the
brief could not close itself at all.

Every job is now handed a directory in `$AB_JOB_DIR` and reports by writing
files into it, with no id, url or token involved:

    echo "12/24 sources done" > "$AB_JOB_DIR/progress/010-sources.md"
    echo finished             > "$AB_JOB_DIR/status"
    cp "$RUNS/RESULTS.md"      "$AB_JOB_DIR/report.md"

This shim translates the old flags into exactly those writes so that batch
scripts already on disk keep reporting, and prints what it did. It will be
deleted; `--url`, `--token` and the JSONL fallbacks are already gone, because a
file in a directory the gateway reads needs none of them.

Compute nodes must see `$AB_JOB_DIR` -- it lives under the gateway's data dir, so
put that on the shared filesystem, the same requirement `[messages] dir` had.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

STATUSES = ("queued", "running", "finished", "failed")
_SAFE = re.compile(r"[^a-z0-9._-]+")


def _job_dir(explicit: str | None, job_id: str | None,
             data_dir: str | None) -> Path | None:
    """Where to write. `$AB_JOB_DIR` first, then rebuilt from the old flags."""
    for candidate in (explicit, os.environ.get("AB_JOB_DIR")):
        if candidate:
            return Path(candidate)
    base = data_dir or os.environ.get("AB_DATA_DIR")
    if base and job_id:
        return Path(base) / "reports" / job_id
    return None


def _publish(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ab-notify",
        description="deprecated; write to $AB_JOB_DIR instead")
    parser.add_argument("--status", required=True, choices=STATUSES)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--msg")
    group.add_argument("--msg-file", help="read the message from a file")
    parser.add_argument("--report-id", default=os.environ.get("AB_REPORT_ID"),
                        help="kept as the progress file's name")
    parser.add_argument("--job-id", default=os.environ.get("AB_JOB_ID"))
    parser.add_argument("--job-dir", default=None)
    parser.add_argument("--data-dir", default=os.environ.get("AB_DATA_DIR"))
    return parser


def main(argv=None) -> int:
    args, unknown = build_parser().parse_known_args(argv)
    for flag in unknown:
        # --url/--token/--messages-dir/--timeout: accepted and ignored rather
        # than fatal, so an old script reports instead of dying on argv.
        if flag.startswith("-"):
            print(f"ab-notify: ignoring {flag} (reporting is a directory now)",
                  file=sys.stderr)

    root = _job_dir(args.job_dir, args.job_id, args.data_dir)
    if root is None:
        print("ab-notify: no $AB_JOB_DIR, and no --job-id/--data-dir to rebuild "
              "one from. Nothing was reported.", file=sys.stderr)
        return 2

    text = args.msg or ""
    if args.msg_file:
        try:
            text = Path(args.msg_file).read_text(encoding="utf-8",
                                                 errors="replace")
        except OSError as exc:
            print(f"ab-notify: cannot read --msg-file: {exc}", file=sys.stderr)
            return 2

    written = []
    try:
        if text:
            # `finished`/`failed` carry the deliverable, so they land as the
            # report; progress lands as a milestone under a name derived from
            # --report-id, which is what made a retried step idempotent.
            if args.status in ("finished", "failed"):
                _publish(root / "report.md", text)
                written.append("report.md")
            else:
                stem = _SAFE.sub("-", (args.report_id or "").lower()).strip("-")
                name = f"{stem or int(time.time())}.md"
                _publish(root / "progress" / name, text)
                written.append(f"progress/{name}")
        _publish(root / "status", args.status + "\n")
        written.append("status")
    except OSError as exc:
        print(f"ab-notify: cannot write into {root}: {exc}", file=sys.stderr)
        return 1

    print(f"ab-notify: wrote {', '.join(written)} in {root}")
    print("ab-notify: deprecated — write these files directly; see the worker "
          "skill.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
