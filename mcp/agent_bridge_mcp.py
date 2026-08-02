#!/usr/bin/env python3
"""agent-bridge MCP server (local machine, stdio).

Bridges a local Claude Code (or any MCP client) to one or more agent-bridge
gateways. You configure the gateways once; the MCP exposes tools to submit
prompts, wait for results, inspect sessions, and read each cluster's capabilities
— routed to whichever gateway you name.

Pure stdlib. Speaks MCP over stdio (newline-delimited JSON-RPC 2.0). Runs on your
laptop; reach each gateway over its SSH port-forward (e.g. http://localhost:8787).

Install (Claude Code):
    claude mcp add agent-bridge -- \
        python3 /path/to/agent_bridge_mcp.py --config /path/to/gateways.json

Config: see gateways.example.json. Discovery order:
    --config ARG  >  $AGENT_BRIDGE_MCP_CONFIG  >
    ~/.config/agent-bridge/gateways.json  >  ./gateways.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER_NAME = "agent-bridge-mcp"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-06-18"
TERMINAL = {"succeeded", "failed", "canceled"}


def log(*a):
    """Diagnostics MUST go to stderr; stdout is the JSON-RPC channel."""
    print("[agent-bridge-mcp]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
class Gateways:
    def __init__(self, cfg: dict) -> None:
        self.default = cfg.get("default")
        self.gateways: dict[str, dict] = cfg.get("gateways", {}) or {}
        if not self.gateways:
            raise ValueError("config has no 'gateways'")
        if not self.default or self.default not in self.gateways:
            self.default = next(iter(self.gateways))

    def resolve(self, name: str | None) -> tuple[str, str, str]:
        """Return (name, base_url, token) for a gateway."""
        name = name or self.default
        gw = self.gateways.get(name)
        if not gw:
            raise ValueError(
                f"unknown gateway '{name}'; configured: {list(self.gateways)}")
        base = gw.get("base_url", "").rstrip("/")
        if not base:
            raise ValueError(f"gateway '{name}' has no base_url")
        return name, base, _token_for(gw)

    def summary(self) -> list[dict]:
        return [
            {"name": n, "base_url": g.get("base_url"),
             "default": (n == self.default),
             "has_token": bool(_token_for(g, required=False))}
            for n, g in self.gateways.items()
        ]


def _token_for(gw: dict, required: bool = True) -> str:
    if gw.get("token"):
        return gw["token"]
    if gw.get("token_env"):
        v = os.environ.get(gw["token_env"], "")
        if v:
            return v
    if gw.get("token_file"):
        p = Path(gw["token_file"]).expanduser()
        if p.exists():
            return p.read_text().strip()
    if required:
        raise ValueError(
            "no token for gateway (set token, token_env, or token_file)")
    return ""


def load_config(explicit: str | None) -> Gateways:
    candidates = [
        explicit,
        os.environ.get("AGENT_BRIDGE_MCP_CONFIG"),
        str(Path.home() / ".config" / "agent-bridge" / "gateways.json"),
        "gateways.json",
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser()
        if not p.exists():
            continue
        text = p.read_text()
        if p.suffix == ".toml":
            import tomllib
            cfg = tomllib.loads(text)
        else:
            cfg = json.loads(text)
        log(f"loaded config {p}")
        return Gateways(cfg)
    raise FileNotFoundError(
        "no gateway config found (tried --config, $AGENT_BRIDGE_MCP_CONFIG, "
        "~/.config/agent-bridge/gateways.json, ./gateways.json)")


# --------------------------------------------------------------------------
# HTTP to gateways
# --------------------------------------------------------------------------
def http(method: str, base: str, path: str, token: str,
         body: dict | None = None, timeout: float = 30.0,
         accept: str = "application/json") -> tuple[int, object]:
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach {url}: {e.reason} "
                           f"(is the SSH port-forward up?)") from e


def _parse(raw: bytes):
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return (raw or b"").decode(errors="replace")


def http_multipart(base: str, path: str, token: str, payload: dict,
                   files: list[tuple[str, str]], timeout: float = 300.0):
    """POST multipart/form-data: one `payload` field (JSON) + file parts.
    `files` is a list of (field_filename, local_path)."""
    boundary = "----agentbridge" + os.urandom(12).hex()
    crlf = b"\r\n"
    body = bytearray()

    def part_header(disp):
        body.extend(f"--{boundary}".encode() + crlf)
        body.extend(disp.encode() + crlf + crlf)

    part_header('Content-Disposition: form-data; name="payload"')
    body.extend(json.dumps(payload).encode() + crlf)
    for fname, local in files:
        with open(local, "rb") as fh:
            data = fh.read()
        part_header(f'Content-Disposition: form-data; name="files"; '
                    f'filename="{fname}"\r\nContent-Type: application/octet-stream')
        body.extend(data + crlf)
    body.extend(f"--{boundary}--".encode() + crlf)

    req = urllib.request.Request(base + path, data=bytes(body), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())


def http_download(base: str, path: str, token: str, local_path: str,
                  timeout: float = 300.0) -> int:
    """Stream GET /v1/files/content to a local file. Returns bytes written."""
    url = base + "/v1/files/content?" + urllib.parse.urlencode({"path": path})
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    os.makedirs(os.path.dirname(os.path.abspath(local_path)) or ".", exist_ok=True)
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as r, open(local_path, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
    return total


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
class Tools:
    def __init__(self, gws: Gateways) -> None:
        self.gws = gws

    # each tool: (schema dict, callable(args)->result)
    def registry(self) -> dict:
        gw_prop = {"type": "string",
                   "description": "gateway name (see list_gateways); "
                                  "omit to use the default"}
        job_fields = {
            "prompt": {"type": "string", "description": "task to run"},
            "cwd": {"type": "string",
                    "description": "absolute working dir; must be within the "
                                   "gateway's allowed_dirs"},
            "agent": {"type": "string", "description": "agent backend (default: claude)"},
            "model": {"type": "string", "description": "model alias/id (optional)"},
            "session": {"type": "string", "description": "session_id hint (optional)"},
            "permission_mode": {"type": "string", "description": "optional override"},
            "upload": {"type": "array", "items": {"type": "string"},
                       "description": "LOCAL file paths to upload with the job; the "
                                      "agent gets their remote paths as attachments"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "REMOTE paths (already on the gateway) to attach"},
            "gateway": gw_prop,
        }
        gw_only = {"type": "object", "properties": {"gateway": gw_prop}}
        return {
            "list_gateways": (
                {"type": "object", "properties": {}},
                self.list_gateways),
            "cluster_info": (
                {"type": "object", "properties": {
                    "gateway": gw_prop,
                    "refresh": {"type": "boolean",
                                "description": "force a re-probe"}}},
                self.cluster_info),
            "list_sessions": (
                {"type": "object", "properties": {
                    "gateway": gw_prop,
                    "cwd": {"type": "string",
                            "description": "prefer sessions under this dir"}}},
                self.list_sessions),
            "submit_job": (
                {"type": "object", "properties": job_fields,
                 "required": ["prompt"]},
                self.submit_job),
            "get_job": (
                {"type": "object", "properties": {
                    "gateway": gw_prop,
                    "job_id": {"type": "string"}},
                 "required": ["job_id"]},
                self.get_job),
            "job_events": (
                {"type": "object", "properties": {
                    "gateway": gw_prop,
                    "job_id": {"type": "string"},
                    "after": {"type": "integer",
                              "description": "return only events with seq > this "
                                             "(default 0); pass the last seq you "
                                             "saw to page incrementally"}},
                 "required": ["job_id"]},
                self.job_events),
            "cancel_job": (
                {"type": "object", "properties": {
                    "gateway": gw_prop,
                    "job_id": {"type": "string"}},
                 "required": ["job_id"]},
                self.cancel_job),
            "run_prompt": (
                {"type": "object",
                 "properties": {**job_fields,
                                "poll_interval_sec": {"type": "number"},
                                "timeout_sec": {"type": "number",
                                                "description": "client wait cap "
                                                "(default 900); does not cancel "
                                                "the job"}},
                 "required": ["prompt"]},
                self.run_prompt),
            "upload_files": (
                {"type": "object", "properties": {
                    "gateway": gw_prop,
                    "paths": {"type": "array", "items": {"type": "string"},
                              "description": "local file paths to upload"},
                    "dir": {"type": "string",
                            "description": "local dir to upload recursively "
                                           "(preserves relative structure)"}}},
                self.upload_files),
            "download_files": (
                {"type": "object", "properties": {
                    "gateway": gw_prop,
                    "paths": {"type": "array", "items": {"type": "string"},
                              "description": "remote file paths to fetch"},
                    "dir": {"type": "string", "description": "remote dir to fetch from"},
                    "glob": {"type": "string", "description": "glob when using dir (default *)"},
                    "recursive": {"type": "boolean"},
                    "local_dir": {"type": "string",
                                  "description": "local destination dir (required)"}},
                 "required": ["local_dir"]},
                self.download_files),
            "list_remote_files": (
                {"type": "object", "properties": {
                    "gateway": gw_prop,
                    "dir": {"type": "string"},
                    "glob": {"type": "string"},
                    "recursive": {"type": "boolean"}},
                 "required": ["dir"]},
                self.list_remote_files),
        }

    def descriptions(self) -> dict:
        return {
            "list_gateways": "List the configured agent-bridge gateways and "
                             "which is the default.",
            "cluster_info": "Get a gateway's machine/cluster capabilities "
                            "(host, CPU/RAM, GPUs, Slurm partitions + GPU "
                            "inventory, allocation balance). Use before "
                            "choosing where to run heavy work.",
            "list_sessions": "List the agent sessions a gateway can fork.",
            "submit_job": "Submit a prompt to a gateway and return immediately "
                          "with a job id (does not wait). Use get_job to poll.",
            "get_job": "Fetch a job's current status/result from a gateway.",
            "job_events": "Fetch a job's event log incrementally (progress: "
                          "assistant text, tool calls, result). Pass `after` = "
                          "the last seq you saw to stream in chunks by polling.",
            "cancel_job": "Cancel a queued or running job (kills the agent "
                          "process on the gateway).",
            "run_prompt": "Submit a prompt to a gateway and WAIT for the result "
                          "(polls to completion). The normal way to run a task. "
                          "Pass `upload` (local files) to send inputs, `files` "
                          "(remote paths) to attach existing ones.",
            "upload_files": "Upload local files to a gateway (returns remote "
                            "paths). Use `upload` on submit_job/run_prompt to do "
                            "it in one call; use this to stage files for reuse.",
            "download_files": "Fetch remote files (artifacts, result CSVs) from a "
                              "gateway to a local dir. Give `paths`, or `dir`+`glob`.",
            "list_remote_files": "List files under a remote dir on a gateway "
                                 "(to discover artifacts before downloading).",
        }

    # --- implementations ---
    def list_gateways(self, args):
        return {"gateways": self.gws.summary(), "default": self.gws.default}

    def cluster_info(self, args):
        name, base, token = self.gws.resolve(args.get("gateway"))
        q = "?refresh=1" if args.get("refresh") else ""
        code, data = http("GET", base, "/v1/info" + q, token)
        _raise_http(code, data)
        return {"gateway": name, **(data if isinstance(data, dict) else {"info": data})}

    def list_sessions(self, args):
        name, base, token = self.gws.resolve(args.get("gateway"))
        path = "/v1/sessions"
        if args.get("cwd"):
            path += "?" + urllib.parse.urlencode({"cwd": args["cwd"]})
        code, data = http("GET", base, path, token)
        _raise_http(code, data)
        return {"gateway": name, **(data if isinstance(data, dict) else {"data": data})}

    def _submit(self, args):
        """POST a job (multipart if there are local uploads, else JSON)."""
        name, base, token = self.gws.resolve(args.get("gateway"))
        payload = _job_payload(args)
        uploads = [(os.path.basename(p), p) for p in (args.get("upload") or [])]
        if uploads:
            code, data = http_multipart(base, "/v1/jobs", token, payload, uploads)
        else:
            code, data = http("POST", base, "/v1/jobs", token, body=payload)
        _raise_http(code, data)
        return name, base, token, data

    def submit_job(self, args):
        name, _base, _token, data = self._submit(args)
        return {"gateway": name, **(data if isinstance(data, dict) else {"data": data})}

    def get_job(self, args):
        name, base, token = self.gws.resolve(args.get("gateway"))
        code, data = http("GET", base, f"/v1/jobs/{args['job_id']}", token)
        _raise_http(code, data)
        return {"gateway": name, **(data if isinstance(data, dict) else {"data": data})}

    def job_events(self, args):
        name, base, token = self.gws.resolve(args.get("gateway"))
        after = int(args.get("after") or 0)
        path = f"/v1/jobs/{args['job_id']}/events?after={after}"
        code, data = http("GET", base, path, token, accept="application/json")
        _raise_http(code, data)
        if not isinstance(data, dict):
            return {"gateway": name, "data": data}
        events = data.get("events", [])
        last = events[-1]["seq"] if events else after
        return {"gateway": name, "status": (data.get("job") or {}).get("status"),
                "terminal": data.get("terminal"), "next_after": last,
                "events": events}

    def cancel_job(self, args):
        name, base, token = self.gws.resolve(args.get("gateway"))
        code, data = http("POST", base, f"/v1/jobs/{args['job_id']}/cancel", token)
        _raise_http(code, data)
        return {"gateway": name, **(data if isinstance(data, dict) else {"data": data})}

    def upload_files(self, args):
        name, base, token = self.gws.resolve(args.get("gateway"))
        files = _collect_local(args)   # list of (remote_name, local_path)
        if not files:
            raise RuntimeError("give `paths` (files) or `dir` (a local directory)")
        code, data = http_multipart(base, "/v1/files", token, {}, files)
        _raise_http(code, data)
        return {"gateway": name, **(data if isinstance(data, dict) else {"data": data})}

    def download_files(self, args):
        name, base, token = self.gws.resolve(args.get("gateway"))
        local_dir = args["local_dir"]
        remote = list(args.get("paths") or [])
        if args.get("dir"):
            q = urllib.parse.urlencode({"dir": args["dir"], "glob": args.get("glob", "*"),
                                        "recursive": str(bool(args.get("recursive"))).lower()})
            code, listing = http("GET", base, "/v1/files/list?" + q, token)
            _raise_http(code, listing)
            remote += [f["path"] for f in listing.get("files", [])]
        if not remote:
            raise RuntimeError("nothing to download (give `paths` or `dir`)")
        saved = []
        for rp in remote:
            local = os.path.join(local_dir, os.path.basename(rp))
            n = http_download(base, rp, token, local)
            saved.append({"remote": rp, "local": local, "bytes": n})
        return {"gateway": name, "downloaded": saved}

    def list_remote_files(self, args):
        name, base, token = self.gws.resolve(args.get("gateway"))
        q = urllib.parse.urlencode({"dir": args["dir"], "glob": args.get("glob", "*"),
                                    "recursive": str(bool(args.get("recursive"))).lower()})
        code, data = http("GET", base, "/v1/files/list?" + q, token)
        _raise_http(code, data)
        return {"gateway": name, **(data if isinstance(data, dict) else {"data": data})}

    def run_prompt(self, args):
        name, base, token, data = self._submit(args)
        job_id = data["id"]
        interval = float(args.get("poll_interval_sec") or 2.0)
        deadline = time.monotonic() + float(args.get("timeout_sec") or 900.0)
        while True:
            code, job = http("GET", base, f"/v1/jobs/{job_id}", token)
            _raise_http(code, job)
            if job.get("status") in TERMINAL:
                return {"gateway": name, "job_id": job_id,
                        "status": job["status"], "result": job.get("result"),
                        "error": job.get("error"),
                        "chosen_session": job.get("chosen_session"),
                        "forked_session": job.get("forked_session"),
                        "cost_usd": job.get("cost_usd")}
            if time.monotonic() > deadline:
                return {"gateway": name, "job_id": job_id,
                        "status": job.get("status"), "timed_out_waiting": True,
                        "note": "job still running on the gateway; poll get_job"}
            time.sleep(interval)


def _job_payload(args: dict) -> dict:
    body = {"prompt": args["prompt"]}
    for k in ("cwd", "agent", "model", "session", "permission_mode"):
        if args.get(k):
            body[k] = args[k]
    if args.get("files"):
        body["files"] = [{"path": p} for p in args["files"]]
    return body


def _collect_local(args: dict) -> list[tuple[str, str]]:
    """(remote_name, local_path) pairs from `paths` (basenames) or `dir` (relpaths)."""
    out: list[tuple[str, str]] = []
    for p in args.get("paths") or []:
        out.append((os.path.basename(p), p))
    d = args.get("dir")
    if d:
        for root, _dirs, fnames in os.walk(d):
            for fn in fnames:
                full = os.path.join(root, fn)
                out.append((os.path.relpath(full, d), full))
    return out


def _raise_http(code: int, data):
    if code >= 400:
        msg = data.get("error") if isinstance(data, dict) else str(data)
        raise RuntimeError(f"gateway returned {code}: {msg}")


# --------------------------------------------------------------------------
# MCP stdio server (JSON-RPC 2.0, newline-delimited)
# --------------------------------------------------------------------------
class MCPServer:
    def __init__(self, tools: Tools) -> None:
        self.tools = tools
        self._reg = tools.registry()
        self._desc = tools.descriptions()

    def run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = self.handle(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

    def handle(self, msg: dict):
        method = msg.get("method")
        mid = msg.get("id")
        is_notification = "id" not in msg
        try:
            if method == "initialize":
                result = self._initialize(msg.get("params", {}))
            elif method in ("notifications/initialized", "notifications/cancelled"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._tool_list()}
            elif method == "tools/call":
                result = self._tools_call(msg.get("params", {}))
            elif method in ("resources/list", "prompts/list"):
                result = {"resources": []} if method.startswith("resources") \
                    else {"prompts": []}
            else:
                if is_notification:
                    return None
                return _err(mid, -32601, f"method not found: {method}")
        except Exception as e:
            if is_notification:
                log("error handling notification:", e)
                return None
            return _err(mid, -32603, str(e))
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _initialize(self, params: dict) -> dict:
        proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return {
            "protocolVersion": proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _tool_list(self) -> list[dict]:
        out = []
        for name, (schema, _fn) in self._reg.items():
            schema = dict(schema)
            schema.setdefault("type", "object")
            schema.setdefault("additionalProperties", False)
            out.append({"name": name,
                        "description": self._desc.get(name, name),
                        "inputSchema": schema})
        return out

    def _tools_call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        entry = self._reg.get(name)
        if not entry:
            return _tool_error(f"unknown tool: {name}")
        _schema, fn = entry
        try:
            result = fn(args)
        except Exception as e:
            return _tool_error(str(e))
        text = result if isinstance(result, str) else json.dumps(result, indent=2)
        return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": f"ERROR: {message}"}],
            "isError": True}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="agent-bridge MCP server (stdio)")
    ap.add_argument("--config", "-c", help="path to gateways.json (or .toml)")
    args = ap.parse_args(argv)
    try:
        gws = load_config(args.config)
    except Exception as e:
        log("config error:", e)
        return 2
    log(f"ready; gateways: {[g['name'] for g in gws.summary()]} "
        f"(default: {gws.default})")
    MCPServer(Tools(gws)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
