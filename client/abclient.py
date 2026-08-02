"""Shared agent-bridge client: gateway config + HTTP transport + a high-level
Client. Used by both the `ab` CLI and the MCP server, so there's one source of
truth for talking to gateways. Pure stdlib.

Config (gateways.json): a `default` name plus per-gateway `base_url` and a token
(`token`, or `token_env`, or `token_file`). Discovery order:
    explicit path  >  $AGENT_BRIDGE_MCP_CONFIG  >
    ~/.config/agent-bridge/gateways.json  >  ./gateways.json
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TERMINAL = {"succeeded", "failed", "canceled"}


class ConfigError(Exception):
    pass


class GatewayError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
class Gateways:
    def __init__(self, cfg: dict) -> None:
        self.default = cfg.get("default")
        self.gateways: dict[str, dict] = cfg.get("gateways", {}) or {}
        if not self.gateways:
            raise ConfigError("config has no 'gateways'")
        if not self.default or self.default not in self.gateways:
            self.default = next(iter(self.gateways))

    def client(self, name: str | None = None) -> "Client":
        name = name or self.default
        gw = self.gateways.get(name)
        if not gw:
            raise ConfigError(
                f"unknown gateway '{name}'; configured: {list(self.gateways)}")
        base = (gw.get("base_url") or "").rstrip("/")
        if not base:
            raise ConfigError(f"gateway '{name}' has no base_url")
        return Client(name, base, _token_for(gw))

    def summary(self) -> list[dict]:
        return [{"name": n, "base_url": g.get("base_url"),
                 "default": n == self.default,
                 "has_token": bool(_token_for(g, required=False))}
                for n, g in self.gateways.items()]


def _token_for(gw: dict, required: bool = True) -> str:
    if gw.get("token"):
        return gw["token"]
    if gw.get("token_env") and os.environ.get(gw["token_env"]):
        return os.environ[gw["token_env"]]
    if gw.get("token_file"):
        p = Path(gw["token_file"]).expanduser()
        if p.exists():
            return p.read_text().strip()
    if required:
        raise ConfigError("no token (set token, token_env, or token_file)")
    return ""


def load_gateways(explicit: str | None = None) -> Gateways:
    for c in (explicit, os.environ.get("AGENT_BRIDGE_MCP_CONFIG"),
              str(Path.home() / ".config" / "agent-bridge" / "gateways.json"),
              "gateways.json"):
        if not c:
            continue
        p = Path(c).expanduser()
        if not p.exists():
            continue
        text = p.read_text()
        cfg = _load_toml(text) if p.suffix == ".toml" else json.loads(text)
        return Gateways(cfg)
    raise ConfigError(
        "no gateway config found (tried --config, $AGENT_BRIDGE_MCP_CONFIG, "
        "~/.config/agent-bridge/gateways.json, ./gateways.json)")


def _load_toml(text: str) -> dict:
    import tomllib
    return tomllib.loads(text)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------
def _parse(raw: bytes):
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return (raw or b"").decode(errors="replace")


def _unreachable(base: str, path: str, e: OSError) -> "GatewayError":
    """A transport-level failure. Covers both a refused connect (URLError, which
    carries .reason) and a connection dropped mid-response — the signature of a
    live `ssh -L` whose far end has died, which raises a bare OSError."""
    return GatewayError(f"cannot reach {base}{path}: {getattr(e, 'reason', e)} "
                        f"(is the SSH port-forward up, and the gateway running?)")


def http(method: str, base: str, path: str, token: str, body: dict | None = None,
         timeout: float = 60.0, accept: str = "application/json"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except OSError as e:
        raise _unreachable(base, path, e) from e


def http_multipart(base: str, path: str, token: str, payload: dict,
                   files: list[tuple[str, str]], timeout: float = 3600.0):
    """POST multipart: a `payload` JSON field + file parts (field name `files`).
    `files` is [(remote_filename, local_path)]."""
    boundary = "----agentbridge" + os.urandom(12).hex()
    crlf = b"\r\n"
    body = bytearray()

    def hdr(disp: str):
        body.extend(f"--{boundary}".encode() + crlf)
        body.extend(disp.encode() + crlf + crlf)

    hdr('Content-Disposition: form-data; name="payload"')
    body.extend(json.dumps(payload).encode() + crlf)
    for fname, local in files:
        with open(local, "rb") as fh:
            data = fh.read()
        hdr(f'Content-Disposition: form-data; name="files"; filename="{fname}"'
            f'\r\nContent-Type: application/octet-stream')
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
    except OSError as e:
        raise _unreachable(base, path, e) from e


def http_download(base: str, token: str, remote_path: str, local_path: str,
                  timeout: float = 3600.0) -> int:
    url = base + "/v1/files/content?" + urllib.parse.urlencode({"path": remote_path})
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    os.makedirs(os.path.dirname(os.path.abspath(local_path)) or ".", exist_ok=True)
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, \
                open(local_path, "wb") as out:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
    except urllib.error.HTTPError as e:
        raise GatewayError(f"download {remote_path} failed: "
                           f"{e.code} {_parse(e.read())}") from e
    except OSError as e:
        raise _unreachable(base, "/v1/files/content", e) from e
    return total


# --------------------------------------------------------------------------
# High-level client
# --------------------------------------------------------------------------
class Client:
    def __init__(self, name: str, base: str, token: str) -> None:
        self.name = name
        self.base = base
        self.token = token

    def _get(self, path: str):
        code, data = http("GET", self.base, path, self.token)
        _raise(code, data)
        return data

    # -- capabilities --
    def info(self, refresh: bool = False) -> dict:
        return self._get("/v1/info" + ("?refresh=1" if refresh else ""))

    def sessions(self, cwd: str | None = None) -> dict:
        q = ("?" + urllib.parse.urlencode({"cwd": cwd})) if cwd else ""
        return self._get("/v1/sessions" + q)

    def models(self, agent: str | None = None) -> dict:
        q = ("?" + urllib.parse.urlencode({"agent": agent})) if agent else ""
        return self._get("/v1/models" + q)

    # -- jobs --
    def submit(self, prompt: str, *, cwd=None, agent=None, model=None,
               session=None, permission_mode=None, files=None, upload=None) -> dict:
        payload = _job_payload(prompt, cwd, agent, model, session,
                               permission_mode, files)
        uploads = [(os.path.basename(p), p) for p in (upload or [])]
        if uploads:
            code, data = http_multipart(self.base, "/v1/jobs", self.token,
                                        payload, uploads)
        else:
            code, data = http("POST", self.base, "/v1/jobs", self.token, body=payload)
        _raise(code, data)
        return data

    def get_job(self, job_id: str) -> dict:
        return self._get(f"/v1/jobs/{job_id}")

    def events(self, job_id: str, after: int = 0) -> dict:
        data = self._get(f"/v1/jobs/{job_id}/events?after={int(after)}")
        evs = data.get("events", [])
        return {"events": evs, "terminal": data.get("terminal"),
                "status": (data.get("job") or {}).get("status"),
                "next_after": evs[-1]["seq"] if evs else after}

    def cancel(self, job_id: str) -> dict:
        code, data = http("POST", self.base, f"/v1/jobs/{job_id}/cancel", self.token)
        _raise(code, data)
        return data

    def run(self, prompt: str, *, poll_interval: float = 1.5, timeout: float = 900,
            on_event=None, **submit_kw) -> dict:
        """Submit and wait; calls on_event(ev) for each event if given."""
        job = self.submit(prompt, **submit_kw)
        job_id = job["id"]
        after = 0
        deadline = time.monotonic() + timeout
        while True:
            ev = self.events(job_id, after)
            for e in ev["events"]:
                if on_event:
                    on_event(e)
                after = e["seq"]
            if ev["terminal"]:
                return self.get_job(job_id)
            if time.monotonic() > deadline:
                return {**self.get_job(job_id), "timed_out_waiting": True}
            time.sleep(poll_interval)

    # -- files --
    def upload_files(self, paths=None, dir=None) -> dict:
        files = _collect_local(paths, dir)
        if not files:
            raise GatewayError("give paths or a dir to upload")
        code, data = http_multipart(self.base, "/v1/files", self.token, {}, files)
        _raise(code, data)
        return data

    def list_files(self, dir: str, glob: str = "*", recursive: bool = False) -> dict:
        q = urllib.parse.urlencode({"dir": dir, "glob": glob,
                                    "recursive": str(bool(recursive)).lower()})
        return self._get("/v1/files/list?" + q)

    def download_files(self, local_dir: str, paths=None, dir=None, glob="*",
                       recursive=False) -> list[dict]:
        remote = list(paths or [])
        if dir:
            remote += [f["path"] for f in
                       self.list_files(dir, glob, recursive).get("files", [])]
        if not remote:
            raise GatewayError("nothing to download (give paths or a dir)")
        out = []
        for rp in remote:
            local = os.path.join(local_dir, os.path.basename(rp))
            n = http_download(self.base, self.token, rp, local)
            out.append({"remote": rp, "local": local, "bytes": n})
        return out


def _job_payload(prompt, cwd, agent, model, session, permission_mode, files) -> dict:
    body = {"prompt": prompt}
    for k, v in (("cwd", cwd), ("agent", agent), ("model", model),
                 ("session", session), ("permission_mode", permission_mode)):
        if v:
            body[k] = v
    if files:
        body["files"] = [{"path": p} for p in files]
    return body


def _collect_local(paths, dir) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in paths or []:
        out.append((os.path.basename(p), p))
    if dir:
        for root, _dirs, fnames in os.walk(dir):
            for fn in fnames:
                full = os.path.join(root, fn)
                out.append((os.path.relpath(full, dir), full))
    return out


def _raise(code: int, data) -> None:
    if code >= 400:
        # FastAPI's HTTPException serialises to {"detail": ...}; a few handlers
        # return {"error": ...}. Fall back to the raw body so a message is never
        # swallowed into "None".
        if isinstance(data, dict):
            msg = data.get("detail") or data.get("error") or json.dumps(data)
        else:
            msg = str(data)
        raise GatewayError(f"gateway returned {code}: {msg}")
