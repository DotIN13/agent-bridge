#!/usr/bin/env python3
"""agent-bridge MCP server (stdio) — thin wrapper over abclient.

Exposes the same operations as the `ab` CLI as MCP tools, for clients that
prefer MCP over shelling out. Pure stdlib. See abclient.py for config/transport.

Install (Claude Code):
    claude mcp add agent-bridge -- \
        python3 /path/to/client/agent_bridge_mcp.py --config /path/to/gateways.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from abclient import ConfigError, GatewayError, load_gateways  # noqa: E402

SERVER_NAME = "agent-bridge-mcp"
SERVER_VERSION = "0.2.0"
DEFAULT_PROTOCOL = "2025-06-18"


def log(*a):
    print("[agent-bridge-mcp]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Tools (delegate to abclient.Client)
# --------------------------------------------------------------------------
class Tools:
    def __init__(self, gws) -> None:
        self.gws = gws

    def _c(self, args):
        return self.gws.client(args.get("gateway"))

    def registry(self) -> dict:
        gw = {"type": "string", "description": "gateway name; omit for the default"}
        job = {
            "prompt": {"type": "string", "description": "task to run"},
            "cwd": {"type": "string", "description": "abs working dir within allowed_dirs"},
            "agent": {"type": "string"}, "model": {"type": "string"},
            "session": {"type": "string"}, "permission_mode": {"type": "string"},
            "upload": {"type": "array", "items": {"type": "string"},
                       "description": "LOCAL files to upload with the job"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "REMOTE paths to attach"},
            "gateway": gw,
        }
        return {
            "list_gateways": ({"type": "object", "properties": {}}, self.list_gateways),
            "cluster_info": ({"type": "object", "properties": {
                "gateway": gw, "refresh": {"type": "boolean"}}}, self.cluster_info),
            "list_sessions": ({"type": "object", "properties": {
                "gateway": gw, "cwd": {"type": "string"}}}, self.list_sessions),
            "submit_job": ({"type": "object", "properties": job,
                            "required": ["prompt"]}, self.submit_job),
            "get_job": ({"type": "object", "properties": {
                "gateway": gw, "job_id": {"type": "string"}},
                "required": ["job_id"]}, self.get_job),
            "job_events": ({"type": "object", "properties": {
                "gateway": gw, "job_id": {"type": "string"},
                "after": {"type": "integer"}}, "required": ["job_id"]}, self.job_events),
            "cancel_job": ({"type": "object", "properties": {
                "gateway": gw, "job_id": {"type": "string"}},
                "required": ["job_id"]}, self.cancel_job),
            "run_prompt": ({"type": "object", "properties": {
                **job, "timeout_sec": {"type": "number"}},
                "required": ["prompt"]}, self.run_prompt),
            "upload_files": ({"type": "object", "properties": {
                "gateway": gw, "paths": {"type": "array", "items": {"type": "string"}},
                "dir": {"type": "string"}}}, self.upload_files),
            "download_files": ({"type": "object", "properties": {
                "gateway": gw, "paths": {"type": "array", "items": {"type": "string"}},
                "dir": {"type": "string"}, "glob": {"type": "string"},
                "recursive": {"type": "boolean"},
                "local_dir": {"type": "string"}}, "required": ["local_dir"]},
                self.download_files),
            "list_remote_files": ({"type": "object", "properties": {
                "gateway": gw, "dir": {"type": "string"}, "glob": {"type": "string"},
                "recursive": {"type": "boolean"}}, "required": ["dir"]},
                self.list_remote_files),
        }

    def descriptions(self) -> dict:
        return {
            "list_gateways": "List configured gateways and the default.",
            "cluster_info": "A gateway's host/CPU/RAM, GPUs, Slurm partitions + GPU "
                            "inventory, allocation balance.",
            "list_sessions": "List the agent sessions a gateway can fork.",
            "submit_job": "Submit a prompt; return a job id immediately (no wait).",
            "get_job": "Fetch a job's status/result.",
            "job_events": "Fetch a job's event log incrementally (`after` = last seq).",
            "cancel_job": "Cancel a queued/running job.",
            "run_prompt": "Submit a prompt and WAIT for the result. `upload` sends "
                          "local files, `files` attaches remote paths.",
            "upload_files": "Upload local files to a gateway -> remote paths.",
            "download_files": "Fetch remote files (artifacts) to a local dir.",
            "list_remote_files": "List files under a remote dir.",
        }

    # -- impls --
    def list_gateways(self, a):
        return {"gateways": self.gws.summary(), "default": self.gws.default}

    def cluster_info(self, a):
        c = self._c(a); return {"gateway": c.name, **c.info(bool(a.get("refresh")))}

    def list_sessions(self, a):
        c = self._c(a); return {"gateway": c.name, **c.sessions(a.get("cwd"))}

    def submit_job(self, a):
        c = self._c(a)
        return {"gateway": c.name, **c.submit(
            a["prompt"], cwd=a.get("cwd"), agent=a.get("agent"), model=a.get("model"),
            session=a.get("session"), permission_mode=a.get("permission_mode"),
            files=a.get("files"), upload=a.get("upload"))}

    def get_job(self, a):
        c = self._c(a); return {"gateway": c.name, **c.get_job(a["job_id"])}

    def job_events(self, a):
        c = self._c(a); return {"gateway": c.name, **c.events(a["job_id"], int(a.get("after") or 0))}

    def cancel_job(self, a):
        c = self._c(a); return {"gateway": c.name, **c.cancel(a["job_id"])}

    def run_prompt(self, a):
        c = self._c(a)
        job = c.run(a["prompt"], cwd=a.get("cwd"), agent=a.get("agent"),
                    model=a.get("model"), session=a.get("session"),
                    permission_mode=a.get("permission_mode"),
                    files=a.get("files"), upload=a.get("upload"),
                    timeout=float(a.get("timeout_sec") or 900))
        return {"gateway": c.name, "job_id": job.get("id"), "status": job.get("status"),
                "result": job.get("result"), "error": job.get("error"),
                "chosen_session": job.get("chosen_session"),
                "forked_session": job.get("forked_session"),
                "cost_usd": job.get("cost_usd"),
                "timed_out_waiting": job.get("timed_out_waiting")}

    def upload_files(self, a):
        c = self._c(a); return {"gateway": c.name,
                                **c.upload_files(a.get("paths"), a.get("dir"))}

    def download_files(self, a):
        c = self._c(a)
        return {"gateway": c.name, "downloaded": c.download_files(
            a["local_dir"], paths=a.get("paths"), dir=a.get("dir"),
            glob=a.get("glob", "*"), recursive=bool(a.get("recursive")))}

    def list_remote_files(self, a):
        c = self._c(a); return {"gateway": c.name, **c.list_files(
            a["dir"], a.get("glob", "*"), bool(a.get("recursive")))}


# --------------------------------------------------------------------------
# MCP stdio server (JSON-RPC 2.0, newline-delimited)
# --------------------------------------------------------------------------
class MCPServer:
    def __init__(self, tools: Tools) -> None:
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
                result = {"protocolVersion": msg.get("params", {}).get(
                    "protocolVersion") or DEFAULT_PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}
            elif method in ("notifications/initialized", "notifications/cancelled"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._tool_list()}
            elif method == "tools/call":
                result = self._call(msg.get("params", {}))
            elif method in ("resources/list", "prompts/list"):
                result = {"resources": []} if method[0] == "r" else {"prompts": []}
            else:
                if is_notification:
                    return None
                return _err(mid, -32601, f"method not found: {method}")
        except Exception as e:
            if is_notification:
                return None
            return _err(mid, -32603, str(e))
        return None if is_notification else {"jsonrpc": "2.0", "id": mid, "result": result}

    def _tool_list(self):
        out = []
        for name, (schema, _fn) in self._reg.items():
            s = dict(schema); s.setdefault("type", "object")
            s.setdefault("additionalProperties", False)
            out.append({"name": name, "description": self._desc.get(name, name),
                        "inputSchema": s})
        return out

    def _call(self, params):
        name = params.get("name")
        entry = self._reg.get(name)
        if not entry:
            return _tool_err(f"unknown tool: {name}")
        try:
            result = entry[1](params.get("arguments") or {})
        except (ConfigError, GatewayError) as e:
            return _tool_err(str(e))
        except Exception as e:
            return _tool_err(f"{type(e).__name__}: {e}")
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_err(message):
    return {"content": [{"type": "text", "text": f"ERROR: {message}"}], "isError": True}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="agent-bridge MCP server (stdio)")
    ap.add_argument("--config", "-c", help="path to gateways.json")
    args = ap.parse_args(argv)
    try:
        gws = load_gateways(args.config)
    except ConfigError as e:
        log("config error:", e)
        return 2
    log(f"ready; gateways: {[g['name'] for g in gws.summary()]} (default: {gws.default})")
    MCPServer(Tools(gws)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
