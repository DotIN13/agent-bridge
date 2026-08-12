"""Shared stdlib-only transport and operations for the ``ab`` CLI."""
from __future__ import annotations

import concurrent.futures
import json
import ntpath
import os
import posixpath
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Iterator

try:
    from agent_bridge_version import __version__ as CLIENT_VERSION
except ImportError:  # copied client/ directory, without the repository root
    from _version import __version__ as CLIENT_VERSION

TERMINAL = {"succeeded", "failed", "canceled"}
# Short by design: `ab gateways` probes every configured gateway, so this is
# the worst case a listing waits on one dead entry, not a request budget.
PROBE_TIMEOUT = 3.0
# How long `submit` waits for the session id a fresh job will create. Generous
# enough to cover an agent's startup and a short queue, short enough that a
# submit never feels like a wait. Exceeding it is not an error: the job is
# running and the id can still be read off the row.
AWAIT_SESSION_TIMEOUT = 30.0
EVENT_TYPES = {
    "assistant", "thinking", "tool_use", "tool_result", "result", "status",
    "error", "log", "message", "steer",
}


class ConfigError(Exception):
    pass


class GatewayError(RuntimeError):
    pass


class Gateways:
    def __init__(self, cfg: dict) -> None:
        self.gateways: dict[str, dict] = cfg.get("gateways", {}) or {}
        if not self.gateways:
            raise ConfigError("config has no 'gateways'")
        if "default" in cfg:
            self.default = cfg.get("default")
            if not isinstance(self.default, str) or self.default not in self.gateways:
                raise ConfigError(
                    f"configured default gateway {self.default!r} is not in "
                    f"{list(self.gateways)}")
        elif len(self.gateways) == 1:
            self.default = next(iter(self.gateways))
        else:
            raise ConfigError(
                "gateway config has multiple gateways but no default")

    def client(self, name: str | None = None, *,
               require_token: bool = True) -> "Client":
        name = name or self.default
        gw = self.gateways.get(name)
        if not gw:
            raise ConfigError(
                f"unknown gateway '{name}'; configured: {list(self.gateways)}")
        base = (gw.get("base_url") or "").rstrip("/")
        if not base:
            raise ConfigError(f"gateway '{name}' has no base_url")
        return Client(name, base, _token_for(gw, required=require_token))

    def summary(self) -> list[dict]:
        """Configured gateways, from local config only. Reaches nothing."""
        return [{"name": name, "base_url": gw.get("base_url"),
                 "default": name == self.default,
                 "has_token": bool(_token_for(gw, required=False))}
                for name, gw in self.gateways.items()]

    def probe(self, timeout: float = PROBE_TIMEOUT) -> list[dict]:
        """`summary()` plus a real liveness check of every gateway.

        Fanned out concurrently: one unreachable gateway must not make the
        others wait out its timeout. Config order is preserved so the output
        is stable between runs.
        """
        rows = self.summary()
        if not rows:
            return rows
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(rows))) as pool:
            checks = list(pool.map(
                lambda row: probe_gateway(row["base_url"], timeout=timeout), rows))
        return [{**row, **check} for row, check in zip(rows, checks)]


def _token_for(gw: dict, required: bool = True) -> str:
    if gw.get("token"):
        return gw["token"]
    if gw.get("token_env") and os.environ.get(gw["token_env"]):
        return os.environ[gw["token_env"]]
    if gw.get("token_file"):
        path = Path(gw["token_file"]).expanduser()
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    if required:
        raise ConfigError("no token (set token, token_env, or token_file)")
    return ""


def load_gateways(explicit: str | None = None) -> Gateways:
    if explicit and not Path(explicit).expanduser().exists():
        raise ConfigError(f"explicit gateway config not found: {explicit}")
    for candidate in (explicit, os.environ.get("AGENT_BRIDGE_CLIENT_CONFIG"),
                      str(Path.home() / ".config" / "agent-bridge" / "gateways.json"),
                      "gateways.json"):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".toml":
            import tomllib
            cfg = tomllib.loads(text)
        else:
            cfg = json.loads(text)
        return Gateways(cfg)
    raise ConfigError(
        "no gateway config found (tried --config, $AGENT_BRIDGE_CLIENT_CONFIG, "
        "~/.config/agent-bridge/gateways.json, ./gateways.json)")


def _parse(raw: bytes):
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return (raw or b"").decode(errors="replace")


def _unreachable(base: str, path: str, exc: OSError) -> GatewayError:
    return GatewayError(
        f"cannot reach {base}{path}: {getattr(exc, 'reason', exc)} "
        "(is the SSH port-forward up, and the gateway running?)")


def _classify_transport(exc: BaseException) -> tuple[str, str]:
    """Name the failure so the caller knows which thing to go fix.

    Refused and reset look alike in a stack trace and need opposite actions:
    refused means nothing is listening locally (the forward is down), reset
    means the forward is up and the far end is not serving.
    """
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout", "no response before the probe timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "refused", "nothing is listening; SSH forward or gateway is down"
    if isinstance(reason, ConnectionResetError):
        return "reset", "forward is up but the gateway is not serving"
    return "unreachable", str(reason)


def probe_gateway(base_url: str | None, timeout: float = PROBE_TIMEOUT) -> dict:
    """Liveness-check one gateway. Never raises — the result *is* the answer.

    `/health` needs no auth, so a gateway with no token configured is still
    probeable; token presence and reachability are independent facts and are
    reported separately.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return {"state": "no_base_url", "reachable": False,
                "detail": "no base_url configured", "version": None,
                "latency_ms": None}
    started = time.monotonic()
    try:
        with urllib.request.urlopen(
                _request("GET", base, "/health", ""), timeout=timeout) as response:
            payload = _parse(response.read())
    except urllib.error.HTTPError as exc:
        # Something answered, but not with a healthy agent-bridge.
        return {"state": "http_error", "reachable": True,
                "detail": f"HTTP {exc.code} from /health; is this an agent-bridge?",
                "version": None,
                "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except (OSError, urllib.error.URLError) as exc:
        state, detail = _classify_transport(exc)
        return {"state": state, "reachable": False, "detail": detail,
                "version": None, "latency_ms": None}
    latency = round((time.monotonic() - started) * 1000, 1)
    ok = isinstance(payload, dict) and payload.get("ok") is True
    return {
        "state": "up" if ok else "unhealthy",
        "reachable": True,
        "detail": None if ok else f"unexpected /health payload: {payload!r}"[:200],
        "version": payload.get("version") if isinstance(payload, dict) else None,
        "latency_ms": latency,
    }


def _request(method: str, base: str, path: str, token: str, *, body=None,
             accept="application/json", headers=None) -> urllib.request.Request:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        if value is not None:
            req.add_header(key, str(value))
    return req


def http(method: str, base: str, path: str, token: str, body: dict | None = None,
         timeout: float = 60.0, accept: str = "application/json", headers=None):
    req = _request(method, base, path, token, body=body, accept=accept,
                   headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, _parse(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except OSError as exc:
        raise _unreachable(base, path, exc) from exc


def _multipart_filename(name: str) -> str:
    """A conservative quoted-string value for Content-Disposition."""
    if any(ch in name for ch in ("\x00", "\r", "\n")):
        raise GatewayError(f"unsafe upload name: {name!r}")
    return name.replace("\\", "\\\\").replace('"', '\\"')


class _MultipartStream:
    def __init__(self, stream) -> None:
        self.stream = stream

    def __iter__(self):
        self.stream.seek(0)
        while True:
            chunk = self.stream.read(1 << 20)
            if not chunk:
                return
            yield chunk


def http_multipart(base: str, path: str, token: str, payload: dict,
                   files: list[tuple[str, str]], timeout: float = 3600.0,
                   headers=None):
    """Spool multipart with bounded reads, then stream it without RAM copies."""
    boundary = "----agentbridge" + os.urandom(12).hex()
    crlf = b"\r\n"
    spool = tempfile.TemporaryFile()

    def write_header(disposition: str):
        spool.write(f"--{boundary}".encode() + crlf)
        spool.write(disposition.encode("utf-8") + crlf + crlf)

    try:
        write_header('Content-Disposition: form-data; name="payload"')
        spool.write(json.dumps(payload).encode() + crlf)
        for remote_name, local in files:
            quoted = _multipart_filename(remote_name)
            write_header('Content-Disposition: form-data; name="files"; '
                         f'filename="{quoted}"\r\n'
                         'Content-Type: application/octet-stream')
            with open(local, "rb") as source:
                while True:
                    chunk = source.read(1 << 20)
                    if not chunk:
                        break
                    spool.write(chunk)
            spool.write(crlf)
        spool.write(f"--{boundary}--".encode() + crlf)
        length = spool.tell()
        req = urllib.request.Request(
            base + path, data=_MultipartStream(spool), method="POST")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Content-Length", str(length))
        for key, value in (headers or {}).items():
            if value is not None:
                req.add_header(key, str(value))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, _parse(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _parse(exc.read())
        except OSError as exc:
            raise _unreachable(base, path, exc) from exc
    finally:
        spool.close()


def http_download(base: str, token: str, remote_path: str, local_path: str,
                  timeout: float = 3600.0, overwrite: bool = False) -> int:
    """Download through a sibling temp file and atomically publish it."""
    destination = Path(local_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GatewayError(f"cannot create local download directory: {exc}") from exc
    if destination.exists() and not overwrite:
        raise GatewayError(f"refusing to overwrite existing file: {destination}")
    url = base + "/v1/files/content?" + urllib.parse.urlencode({"path": remote_path})
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.",
                                         suffix=".part", dir=destination.parent)
    except OSError as exc:
        raise GatewayError(f"cannot create local download file: {exc}") from exc
    total = 0
    try:
        try:
            response = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            os.close(fd)
            raise GatewayError(
                f"download {remote_path} failed: {exc.code} {_parse(exc.read())}") from exc
        except OSError as exc:
            os.close(fd)
            raise _unreachable(base, "/v1/files/content", exc) from exc
        with response, os.fdopen(fd, "wb") as out:
            while True:
                try:
                    chunk = response.read(1 << 20)
                except OSError as exc:
                    raise _unreachable(base, "/v1/files/content", exc) from exc
                if not chunk:
                    break
                try:
                    out.write(chunk)
                except OSError as exc:
                    raise GatewayError(f"cannot write local download: {exc}") from exc
                total += len(chunk)
            try:
                out.flush()
                os.fsync(out.fileno())
            except OSError as exc:
                raise GatewayError(f"cannot flush local download: {exc}") from exc
        if destination.exists() and not overwrite:
            raise GatewayError(f"refusing to overwrite existing file: {destination}")
        try:
            os.replace(temporary, destination)
        except OSError as exc:
            raise GatewayError(f"cannot publish local download: {exc}") from exc
        return total
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def parse_sse(lines: Iterable[bytes | str], *,
              include_comments: bool = False) -> Iterator[dict]:
    """Parse SSE frames, including comments and multiline ``data:`` fields."""
    event_id = None
    event_name = None
    data_lines: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                text = "\n".join(data_lines)
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise GatewayError(f"invalid SSE JSON payload: {exc}") from exc
                if not isinstance(payload, dict):
                    raise GatewayError("invalid SSE payload: expected JSON object")
                if event_id is not None and "seq" not in payload:
                    try:
                        payload["seq"] = int(event_id)
                    except ValueError:
                        pass
                if event_name is not None and "type" not in payload:
                    payload["type"] = event_name
                yield payload
            event_id = None
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            if include_comments:
                yield {"_comment": line[1:].lstrip()}
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        # Servers should terminate frames with a blank line; accepting EOF is
        # friendlier to proxies which close immediately after the final event.
        yield from parse_sse([f"id: {event_id or ''}\n", f"event: {event_name or ''}\n",
                              *(f"data: {value}\n" for value in data_lines), "\n"],
                             include_comments=include_comments)


class Client:
    def __init__(self, name: str, base: str, token: str) -> None:
        self.name = name
        self.base = base
        self.token = token

    def _get(self, path: str):
        code, data = http("GET", self.base, path, self.token)
        _raise(code, data)
        return data

    # discovery
    def health(self) -> dict:
        """Liveness of *this* gateway, annotated with which one it was.

        The server payload is `{ok, version}` and carries no identity, which
        makes it useless in a multi-gateway script — you cannot tell from the
        response which target answered. The name and URL are added here rather
        than server-side, since only the client knows the local alias.
        """
        payload = self._get("/health")
        if isinstance(payload, dict):
            return {**payload, "gateway": self.name, "base_url": self.base}
        return payload

    def remote_help(self) -> str:
        code, data = http("GET", self.base, "/v1/help", self.token,
                          accept="text/markdown")
        _raise(code, data)
        return data if isinstance(data, str) else json.dumps(data)

    def agents(self) -> dict:
        return self._get("/v1/agents")

    def info(self, refresh: bool = False) -> dict:
        return self._get("/v1/info" + ("?refresh=1" if refresh else ""))

    def session_dirs(self, agent: str | None = None) -> dict:
        """Directories holding sessions. Complete — never a page."""
        query = ("?" + urllib.parse.urlencode({"agent": agent})) if agent else ""
        return self._get("/v1/session-dirs" + query)

    def sessions(self, cwd: str | None = None, agent: str | None = None, *,
                 limit: int = 40, cursor: str | None = None) -> dict:
        """One page of sessions. `cwd` is an exact directory match.

        Paging is by opaque cursor rather than `after=N`: sessions have no
        monotonic sequence, and ordering by timestamp alone would skip or
        repeat rows whenever two share a millisecond.
        """
        query = {key: value for key, value in
                 (("cwd", cwd), ("agent", agent), ("cursor", cursor)) if value}
        query["limit"] = int(limit)
        return self._get("/v1/sessions?" + urllib.parse.urlencode(query))

    def models(self, agent: str | None = None) -> dict:
        query = ("?" + urllib.parse.urlencode({"agent": agent})) if agent else ""
        return self._get("/v1/models" + query)

    def capabilities(self) -> dict:
        return {
            "client": {"version": CLIENT_VERSION,
                       "output_modes": ["human", "json", "jsonl"],
                       "exit_codes": {"success": 0, "local_error": 1,
                                      "invocation": 2, "remote_failure": 3,
                                      "wait_timeout": 4},
                       "streaming": "sse",
                       "operations": ["gateways", "health", "agents", "capabilities",
                                      "info", "models", "sessions", "run", "submit",
                                      "jobs", "job", "wait", "events", "steer",
                                      "cancel", "upload", "download", "ls"]},
            "gateway": self.name,
            "server": self.agents(),
        }

    # jobs
    def submit(self, prompt: str, *, cwd=None, agent=None, model=None,
               session=None, permission_mode=None, files=None, upload=None,
               upload_names=None, title=None, fork=True, include_thinking=False,
               idempotency_key=None) -> dict:
        payload = _job_payload(prompt, cwd, agent, model, session,
                               permission_mode, files, title, fork,
                               include_thinking)
        uploads = _collect_local(upload, None, upload_names)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        if uploads:
            code, data = http_multipart(self.base, "/v1/jobs", self.token,
                                        payload, uploads, headers=headers)
        else:
            code, data = http("POST", self.base, "/v1/jobs", self.token,
                              body=payload, headers=headers)
        _raise(code, data)
        return data

    def await_session(self, accepted: dict, timeout: float = AWAIT_SESSION_TIMEOUT,
                      poll: float = 0.4) -> dict:
        """Fill in the session id a fresh job will create, then return.

        Waits for the *session*, not for the job: the id lands with the agent's
        init record a second or two in, long before any work finishes. Composed
        client-side out of the job row, so no new endpoint is needed.

        Never raises and never hangs. The submission has already succeeded by
        the time this runs, so every outcome here returns the accepted document
        with `session_state` set to what was actually learned:

        - ``ready``   the id is known
        - ``pinned``  the caller named it; nothing to wait for
        - ``failed``  the job went terminal before producing one
        - ``pending`` the timeout won; the job may still be queued or running
        """
        if accepted.get("session"):
            return {**accepted, "session_state": "pinned"}
        job_id = accepted.get("id")
        if not job_id:
            return accepted
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                job = self.get_job(job_id)
            except GatewayError:
                # A transport blip must not turn a successful submit into a
                # failure; report what we know and let the caller re-read.
                return {**accepted, "session_state": "pending"}
            session = job.get("session")
            if session:
                return {**accepted, "session": session, "session_state": "ready",
                        "status": job.get("status", accepted.get("status"))}
            if job.get("status") in TERMINAL:
                # Died before its init record -- a bad model id, a missing agent
                # binary. Stop immediately rather than waiting out the timeout.
                return {**accepted, "session_state": "failed",
                        "status": job.get("status"),
                        "error": job.get("error")}
            if time.monotonic() >= deadline:
                return {**accepted, "session_state": "pending",
                        "status": job.get("status", accepted.get("status"))}
            time.sleep(poll)

    def list_jobs(self, limit: int = 50, cursor: str | None = None) -> dict:
        query = {"limit": int(limit)}
        if cursor:
            query["cursor"] = cursor
        return self._get("/v1/jobs?" + urllib.parse.urlencode(query))

    def get_job(self, job_id: str) -> dict:
        return self._get(f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}")

    def events(self, job_id: str, after: int = 0, limit: int = 500, *,
               tail: int | None = None, until: int | None = None,
               types: Iterable[str] = ()) -> dict:
        """One page of a job's events.

        `tail` reads from the end instead of `after`'s forward paging, and lets
        the server do the filtering so `--type` narrows within the window rather
        than after it. `total`/`first_seq`/`last_seq` describe the whole log, so
        a caller can place its window without probing for the end.
        """
        params: list[tuple[str, str]] = [("legacy", "false")]
        if tail is not None:
            params.append(("tail", str(int(tail))))
        else:
            params += [("after", str(int(after))), ("limit", str(int(limit)))]
        if until is not None:
            params.append(("until", str(int(until))))
        params += [("type", t) for t in types]
        query = urllib.parse.urlencode(params)
        data = self._get(
            f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/events?{query}")
        events = data.get("events", [])
        return {"events": events, "terminal": data.get("terminal"),
                "status": data.get("status") or (data.get("job") or {}).get("status"),
                "next_after": data.get("next_after",
                    events[-1]["seq"] if events else after),
                "has_more": bool(data.get("has_more")),
                "total": data.get("total", len(events)),
                "first_seq": data.get("first_seq"),
                "last_seq": data.get("last_seq")}

    def iter_events(self, job_id: str, after: int = 0, *, types=None,
                    until: int | None = None, read_timeout: float = 30.0,
                    reconnects: int = 2,
                    deadline: float | None = None) -> Iterator[dict]:
        """Yield faithful events from resumable SSE, with JSON-page fallback."""
        cursor = int(after)
        wanted = set(types or [])
        path = f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/events"
        attempts = 0
        while True:
            query = "?" + urllib.parse.urlencode({"after": cursor})
            req = _request("GET", self.base, path + query, self.token,
                           accept="text/event-stream",
                           headers={"Last-Event-ID": cursor})
            try:
                with urllib.request.urlopen(req, timeout=read_timeout) as response:
                    content_type = response.headers.get("Content-Type", "")
                    if "text/event-stream" not in content_type:
                        page = _parse(response.read())
                        if not isinstance(page, dict):
                            raise GatewayError("event endpoint returned neither SSE nor JSON")
                        for event in page.get("events", []):
                            seq = int(event.get("seq", 0))
                            if seq <= cursor:
                                continue
                            cursor = seq
                            if until is not None and seq > until:
                                return
                            if not wanted or event.get("type") in wanted:
                                yield event
                            if until is not None and seq >= until:
                                return
                        if page.get("terminal"):
                            return
                        if deadline is not None and time.monotonic() >= deadline:
                            return
                        time.sleep(0.5)
                        continue
                    received = False
                    for event in parse_sse(response, include_comments=True):
                        if deadline is not None and time.monotonic() >= deadline:
                            return
                        if "_comment" in event:
                            continue
                        seq = int(event.get("seq", 0))
                        if seq <= cursor:
                            continue
                        received = True
                        cursor = seq
                        if until is not None and seq > until:
                            return
                        if not wanted or event.get("type") in wanted:
                            yield event
                        if until is not None and seq >= until:
                            return
                    job = self.get_job(job_id)
                    if job.get("status") in TERMINAL:
                        return
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                    attempts = 0 if received else attempts + 1
                    if attempts > reconnects:
                        raise GatewayError(
                            f"event stream ended while job {job_id} is still "
                            f"{job.get('status')}; reconnect limit exceeded")
                    continue
            except urllib.error.HTTPError as exc:
                data = _parse(exc.read())
                # Old gateways may not honor SSE; their JSON response can still
                # be consumed without a different operation.
                if exc.code in {406, 415}:
                    page = self.events(job_id, cursor)
                    for event in page["events"]:
                        cursor = int(event["seq"])
                        if until is not None and cursor > until:
                            return
                        if not wanted or event.get("type") in wanted:
                            yield event
                        if until is not None and cursor >= until:
                            return
                    if page.get("terminal"):
                        return
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                    time.sleep(0.5)
                    continue
                _raise(exc.code, data)
            except (TimeoutError, socket.timeout):
                # A wait deliberately sets the socket timeout to its remaining
                # deadline. Expiry there is a normal wait timeout, not a broken
                # gateway or exhausted reconnect budget.
                if deadline is not None and time.monotonic() >= deadline:
                    return
                attempts += 1
                if attempts > reconnects:
                    job = self.get_job(job_id)
                    if job.get("status") in TERMINAL:
                        return
                    raise GatewayError(
                        f"event stream timed out while job {job_id} is still "
                        f"{job.get('status')}; reconnect limit exceeded")
            except OSError as exc:
                attempts += 1
                if attempts > reconnects:
                    raise _unreachable(self.base, path, exc) from exc

    def wait(self, job_id: str, *, timeout: float = 900.0, on_event=None,
             types=None, cancel_on_timeout: bool = False) -> dict:
        deadline = time.monotonic() + timeout
        after = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                job = self.get_job(job_id)
                if cancel_on_timeout and job.get("status") not in TERMINAL:
                    self.cancel(job_id)
                    job = self._poll_terminal(job_id, 30.0, on_event, after)
                return {**job, "timed_out_waiting": True}
            for event in self.iter_events(
                    job_id, after, types=types,
                    read_timeout=max(0.1, min(30.0, remaining)), reconnects=2,
                    deadline=deadline):
                after = max(after, int(event.get("seq", 0)))
                if on_event:
                    on_event(event)
                if time.monotonic() >= deadline:
                    break
            job = self.get_job(job_id)
            if job.get("status") in TERMINAL:
                return job

    def _poll_terminal(self, job_id: str, timeout: float, on_event=None,
                       after: int = 0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            page = self.events(job_id, after)
            for event in page["events"]:
                after = int(event["seq"])
                if on_event:
                    on_event(event)
            if page["terminal"]:
                return self.get_job(job_id)
            time.sleep(0.25)
        return self.get_job(job_id)

    def run(self, prompt: str, *, timeout: float = 900.0, on_event=None,
            cancel_on_timeout: bool = False, **submit_kw) -> dict:
        accepted = self.submit(prompt, **submit_kw)
        return self.wait(accepted["id"], timeout=timeout, on_event=on_event,
                         cancel_on_timeout=cancel_on_timeout)

    def cancel(self, job_id: str) -> dict:
        code, data = http("POST", self.base,
                          f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/cancel",
                          self.token)
        _raise(code, data)
        return data

    def steer(self, job_id: str, prompt: str) -> dict:
        code, data = http("POST", self.base,
                          f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/steer",
                          self.token, body={"prompt": prompt})
        _raise(code, data)
        return data

    # files
    def upload_files(self, paths=None, dir=None, names=None) -> dict:
        files = _collect_local(paths, dir, names)
        if not files:
            raise GatewayError("give paths or a dir to upload")
        code, data = http_multipart(self.base, "/v1/files", self.token, {}, files)
        _raise(code, data)
        return data

    def list_files(self, dir: str, glob: str = "*", recursive: bool = False,
                   limit: int = 1000, cursor: str | None = None) -> dict:
        query = {"dir": dir, "glob": glob,
                 "recursive": str(bool(recursive)).lower(), "limit": int(limit)}
        if cursor:
            query["cursor"] = cursor
        return self._get("/v1/files/list?" + urllib.parse.urlencode(query))

    def download_files(self, local_dir: str, paths=None, dir=None, glob="*",
                       recursive=False, *, flatten=False, overwrite=False) -> list[dict]:
        remote = list(paths or [])
        if dir:
            cursor = None
            while True:
                page = self.list_files(dir, glob, recursive, cursor=cursor)
                remote.extend(row["path"] for row in page.get("files", [])
                              if not row.get("is_dir"))
                if not page.get("has_more"):
                    break
                cursor = page.get("next_cursor")
        if not remote:
            raise GatewayError("nothing to download (give paths or a dir)")
        plan = _download_plan(remote, local_dir, source_dir=dir, flatten=flatten)
        for _remote, destination in plan:
            if destination.exists() and not overwrite:
                raise GatewayError(f"refusing to overwrite existing file: {destination}")
        result = []
        for remote_path, destination in plan:
            size = http_download(self.base, self.token, remote_path,
                                 str(destination), overwrite=overwrite)
            result.append({"remote": remote_path, "local": str(destination),
                           "bytes": size})
        return result


def _job_payload(prompt, cwd, agent, model, session, permission_mode, files,
                 title=None, fork=True, include_thinking=False) -> dict:
    body = {"prompt": prompt}
    for key, value in (("cwd", cwd), ("agent", agent), ("model", model),
                       ("session", session), ("permission_mode", permission_mode),
                       ("title", title)):
        if value:
            body[key] = value
    if not fork:
        body["fork"] = False
    if include_thinking:
        body["include_thinking"] = True
    if files:
        body["files"] = [{"path": path} for path in files]
    return body


def _normal_remote_name(name: str) -> str:
    value = name.replace("\\", "/").strip("/")
    if not value or value.startswith("../") or "/../" in f"/{value}/" or "/./" in f"/{value}/":
        raise GatewayError(f"unsafe upload name: {name!r}")
    return value


def _collect_local(paths, directory, names=None) -> list[tuple[str, str]]:
    explicit_names = names or {}
    output: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(remote_name: str, local_name: str) -> None:
        local = Path(local_name).expanduser()
        if local.is_symlink() or not local.is_file():
            raise GatewayError(f"upload is not a regular non-symlink file: {local}")
        if not os.access(local, os.R_OK):
            raise GatewayError(f"upload is not readable: {local}")
        remote = _normal_remote_name(remote_name)
        if remote in seen:
            raise GatewayError(f"duplicate upload destination: {remote}")
        seen.add(remote)
        output.append((remote, str(local)))

    for item in paths or []:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            remote, local = item
        elif isinstance(item, dict):
            local = item.get("path") or item.get("local")
            remote = item.get("name") or item.get("remote") or os.path.basename(local or "")
        else:
            local = str(item)
            remote = explicit_names.get(str(item), os.path.basename(str(item)))
        add(str(remote), str(local))
    if directory:
        root = Path(directory).expanduser()
        if root.is_symlink() or not root.is_dir():
            raise GatewayError(f"upload directory is not a non-symlink directory: {root}")
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs
                       if not (Path(current) / name).is_symlink()]
            for filename in files:
                full = Path(current) / filename
                if full.is_symlink():
                    raise GatewayError(f"refusing directory symlink: {full}")
                add(full.relative_to(root).as_posix(), str(full))
    return output


def _remote_module(path: str):
    return ntpath if "\\" in path and "/" not in path else posixpath


def _download_plan(remote: list[str], local_dir: str, *, source_dir=None,
                   flatten=False) -> list[tuple[str, Path]]:
    raw_root = Path(local_dir).expanduser()
    if raw_root.is_symlink():
        raise GatewayError(f"download destination is a symlink: {raw_root}")
    raw_root.mkdir(parents=True, exist_ok=True)
    root = raw_root.resolve()
    explicit = list(dict.fromkeys(str(path) for path in remote))
    if len(explicit) != len(remote):
        raise GatewayError("duplicate remote download path")
    common = None
    if not flatten and not source_dir and len(explicit) > 1:
        module = _remote_module(explicit[0])
        try:
            common = module.commonpath(explicit)
            if common in explicit:
                common = module.dirname(common)
        except ValueError:
            common = None
    output: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for remote_path in explicit:
        module = _remote_module(remote_path)
        if flatten:
            relative = module.basename(remote_path)
        elif source_dir:
            try:
                relative = module.relpath(remote_path, source_dir)
            except ValueError:
                relative = module.basename(remote_path)
            if relative == ".." or relative.startswith(".." + module.sep):
                relative = module.basename(remote_path)
        elif common:
            relative = module.relpath(remote_path, common)
        else:
            relative = module.basename(remote_path)
        relative = relative.replace("\\", "/")
        pure_parts = [part for part in relative.split("/") if part not in ("", ".")]
        if not pure_parts or any(part == ".." or "\x00" in part for part in pure_parts):
            raise GatewayError(f"unsafe remote download path: {remote_path!r}")
        destination = root.joinpath(*pure_parts)
        resolved = destination.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise GatewayError(f"download path escapes destination: {remote_path!r}") from exc
        key = os.path.normcase(str(resolved))
        if key in seen:
            raise GatewayError(f"download destination collision: {resolved}")
        seen.add(key)
        output.append((remote_path, resolved))
    return output


def _raise(code: int, data) -> None:
    if code < 400:
        return
    message = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or json.dumps(error)
        elif error:
            message = str(error)
        detail = data.get("detail")
        if message is None and isinstance(detail, dict):
            message = detail.get("message") or detail.get("error") or json.dumps(detail)
        elif message is None and detail:
            message = str(detail)
    if message is None:
        message = str(data)
    raise GatewayError(f"gateway returned {code}: {message}")
