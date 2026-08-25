#!/usr/bin/env python3
"""Minimal client: submit a prompt and stream results over SSE. Stdlib only.

    python3 client_example.py "list the python files in this repo"
    AGENT_BRIDGE_TOKEN=... AGENT_BRIDGE_URL=http://localhost:8787 \
        python3 client_example.py "..." --cwd /project/jevans/tzhang3/agent-bridge
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def _req(url, token, method="GET", body=None, stream=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    if stream:
        req.add_header("Accept", "text/event-stream")
    return urllib.request.urlopen(req)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--cwd")
    ap.add_argument("--agent", default="claude")
    ap.add_argument("--url", default=os.environ.get("AGENT_BRIDGE_URL", "http://localhost:8787"))
    ap.add_argument("--token", default=os.environ.get("AGENT_BRIDGE_TOKEN", ""))
    args = ap.parse_args()
    if not args.token:
        sys.exit("set AGENT_BRIDGE_TOKEN or pass --token")

    body = {"prompt": args.prompt, "agent": args.agent}
    if args.cwd:
        body["cwd"] = args.cwd
    resp = _req(f"{args.url}/v1/jobs", args.token, "POST", body)
    job = json.loads(resp.read())
    job_id = job["id"]
    print(f"# job {job_id} ({job['agent']} @ {job['cwd']})", file=sys.stderr)

    stream = _req(f"{args.url}/v1/jobs/{job_id}/events", args.token, stream=True)
    etype, buf = None, []
    for raw in stream:
        line = raw.decode(errors="replace").rstrip("\n")
        if line.startswith("event:"):
            etype = line[6:].strip()
        elif line.startswith("data:"):
            buf.append(line[5:].strip())
        elif line == "":
            if buf:
                _render(etype, json.loads("".join(buf)))
            etype, buf = None, []


def _render(etype, ev):
    d = ev.get("data", {})
    if etype == "assistant":
        sys.stdout.write(d.get("text", "")); sys.stdout.flush()
    elif etype == "tool_use":
        print(f"\n[tool: {d.get('name')}] {json.dumps(d.get('input'))[:200]}", file=sys.stderr)
    elif etype == "status":
        print(f"[status {d}]", file=sys.stderr)
    elif etype == "result":
        print("\n\n=== RESULT ===")
        print(d.get("text", ""))
        print(f"(session={d.get('session')} cost=${d.get('cost_usd')})",
              file=sys.stderr)
    elif etype == "error":
        print(f"\n[ERROR] {d.get('message')}", file=sys.stderr)


if __name__ == "__main__":
    main()
