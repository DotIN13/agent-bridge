#!/usr/bin/env python3
"""Compute-side batch lifecycle reporter; stdlib only."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid

STATUSES = ("queued", "running", "finished", "failed")


def _data_dirs(data_dir):
    for base in (data_dir, os.environ.get("AB_DATA_DIR"), os.getcwd()):
        if base:
            yield base


def _gateway_url(explicit, data_dir):
    if explicit:
        return explicit, "--url"
    if os.environ.get("AB_URL"):
        return os.environ["AB_URL"], "AB_URL"
    for base in _data_dirs(data_dir):
        path = os.path.join(base, "gateway-endpoint.json")
        try:
            with open(path, encoding="utf-8") as stream:
                info = json.load(stream)
        except (OSError, ValueError):
            continue
        if info.get("url"):
            return info["url"], path
        return "", f"{path} says loopback-only"
    return "", "no --url, AB_URL, or gateway-endpoint.json"


def _token(explicit, data_dir):
    if explicit:
        return explicit
    if os.environ.get("AB_TOKEN"):
        return os.environ["AB_TOKEN"]
    for base in _data_dirs(data_dir):
        try:
            with open(os.path.join(base, ".token"), encoding="utf-8") as stream:
                return stream.read().strip()
        except OSError:
            pass
    return ""


def _append_jsonl(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def _post(url, token, job_id, payload, timeout):
    request = urllib.request.Request(
        f"{url.rstrip('/')}/v1/jobs/{job_id}/message",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def _post_multipart(url, token, job_id, fields, file_path, timeout):
    boundary = "----ab-notify-" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n")
    filename = os.path.basename(file_path)
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n")
    header = "".join(parts).encode("utf-8")
    with open(file_path, "rb") as stream:
        file_bytes = stream.read()
    trailer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = header + file_bytes + trailer
    request = urllib.request.Request(
        f"{url.rstrip('/')}/v1/jobs/{job_id}/message/file",
        data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def build_parser():
    parser = argparse.ArgumentParser(prog="ab-notify")
    parser.add_argument("--status", required=True, choices=STATUSES)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--msg")
    group.add_argument("--msg-file", help="read the message from a file (full content)")
    parser.add_argument("--report-id", default=os.environ.get("AB_REPORT_ID"),
                        help="stable retry-deduplication id")
    parser.add_argument("--job-id", default=os.environ.get("AB_JOB_ID"))
    parser.add_argument("--url")
    parser.add_argument("--token")
    parser.add_argument("--data-dir", default=os.environ.get("AB_DATA_DIR"))
    parser.add_argument("--messages-dir", default=os.environ.get("AB_MESSAGES_DIR"))
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.job_id:
        print("ab-notify: no job id (set AB_JOB_ID or pass --job-id)", file=sys.stderr)
        return 2

    base = {"ts": time.time(), "status": args.status,
            "host": socket.gethostname(),
            "slurm_job_id": os.environ.get("SLURM_JOB_ID")}
    if args.report_id:
        base["report_id"] = args.report_id

    errors = []
    url, source = _gateway_url(args.url, args.data_dir)
    token = _token(args.token, args.data_dir)
    can_http = bool(url and token)
    if not can_http:
        errors.append(
            f"http: no gateway url ({source})" if not url else "http: no token found")

    # HTTP first. --msg-file uploads the file via multipart; --msg sends JSON.
    if can_http:
        try:
            if args.msg_file:
                _post_multipart(url, token, args.job_id, base,
                                args.msg_file, args.timeout)
            else:
                if args.msg:
                    base["msg"] = args.msg
                _post(url, token, args.job_id, base, args.timeout)
            print(f"ab-notify: sent via http ({args.status}) -> {url}")
            return 0
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"http {url}: {exc}")

    # Fallback: JSONL. Inline the message (or file) content into the payload.
    payload = dict(base)
    if args.msg_file:
        try:
            with open(args.msg_file, encoding="utf-8", errors="replace") as stream:
                payload["msg"] = stream.read()
        except OSError as exc:
            print(f"ab-notify: cannot read --msg-file: {exc}", file=sys.stderr)
            return 2
    elif args.msg:
        payload["msg"] = args.msg

    shared = args.messages_dir or (
        os.path.join(args.data_dir, "messages") if args.data_dir else None)
    if shared:
        try:
            _append_jsonl(os.path.join(shared, f"{args.job_id}.jsonl"), payload)
            print(f"ab-notify: wrote shared jsonl ({args.status}) — {shared}")
            for error in errors:
                print(f"ab-notify: fell back, {error}", file=sys.stderr)
            return 0
        except OSError as exc:
            errors.append(f"shared: {exc}")
    else:
        errors.append("shared: no messages dir")

    local = os.path.join(os.environ.get("TMPDIR", "/tmp"),
                         "agent-bridge-messages", f"{args.job_id}.jsonl")
    try:
        _append_jsonl(local, payload)
        print(f"ab-notify: WROTE LOCAL ONLY — {local}", file=sys.stderr)
        print("ab-notify: move this file into <data_dir>/messages/ for ingestion",
              file=sys.stderr)
        for error in errors:
            print(f"ab-notify: {error}", file=sys.stderr)
        return 0
    except OSError as exc:
        errors.append(f"local: {exc}")
    print("ab-notify: ALL WRITE PATHS FAILED", file=sys.stderr)
    for error in errors:
        print(f"  {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
