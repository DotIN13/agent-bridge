"""The local daemon's HTTP surface, and the page it serves.

This is not the gateway. The gateway runs on the cluster; this runs on the
laptop, holds the ssh forwards open, and lets a browser drive them.

Two things follow from what it can do, and both are enforced rather than
documented:

* **It executes a command from a config file the UI can edit.** So the bind is
  loopback unless forced, a bearer token is always required (generated if the
  operator did not supply one), and the command itself is argv-only from a small
  program allowlist (`config.validate_ssh`). Without those three, a web page on
  any tab would be a shell on this machine.
* **It relays secrets.** A password or a Duo passcode arrives as a request body,
  goes straight to the child's pty, and is never stored, echoed, or logged. The
  one endpoint that takes one says so in its name.

No CORS is configured, so a page from another origin can send a request but
cannot read the reply -- and without the token it gets a 401 anyway.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .config import ConfigError, Store
from .supervisor import Supervisor
from .tunnel import TunnelError
from .ui import PAGE

#: How long an SSE connection goes without saying anything before it sends a
#: keepalive. Short enough that a proxy or a sleeping laptop does not hold a dead
#: socket open forever.
SSE_IDLE_SEC = 15.0

#: One wait inside that. `Supervisor.wait_for_event` is a blocking call on a
#: threadpool thread, so the idle window cannot be one long wait: a browser that
#: navigates away would park a thread for the whole of it, and a page that
#: reconnects a few times would exhaust the pool. A second at a time also lets
#: the generator notice the disconnect and stop.
SSE_POLL_SEC = 1.0


class Error(Exception):
    """A typed failure, shaped like the gateway's so a client can read both."""

    def __init__(self, status: int, code: str, message: str,
                 detail: dict | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.detail = detail or {}


class Tickets:
    """Single-use, short-lived credentials for the event stream.

    `EventSource` cannot send an `Authorization` header, and the real token in a
    query string would sit in browser history and in any log that records a url.
    So the page trades the token for one of these, which is good for exactly one
    connection and expires whether or not it is used.

    In memory only: a restart invalidating them all is the correct behaviour, not
    a bug -- the page just asks for another.
    """

    TTL_SEC = 30.0

    def __init__(self) -> None:
        self._live: dict[str, float] = {}

    def issue(self) -> str:
        self._expire()
        ticket = secrets.token_urlsafe(18)
        self._live[ticket] = time.time() + self.TTL_SEC
        return ticket

    def redeem(self, ticket: str) -> bool:
        self._expire()
        if not ticket:
            return False
        return self._live.pop(ticket, None) is not None

    def _expire(self) -> None:
        now = time.time()
        for key in [key for key, until in self._live.items() if until <= now]:
            self._live.pop(key, None)


class GatewayBody(BaseModel):
    base_url: str = Field(..., description="http(s) url the CLI will connect to")
    ssh: str | list[str] | None = Field(
        None, description="argv or a quoted command line; no shell")
    autostart: bool | None = None
    token: str | None = None
    token_env: str | None = None
    token_file: str | None = None


class AnswerBody(BaseModel):
    """A prompt's answer. Possibly a password; treated as one either way."""

    text: str = Field(..., max_length=4096)


def create_app(sup: Supervisor, token: str) -> FastAPI:
    app = FastAPI(title="agent-bridge tunnels", docs_url=None, redoc_url=None)
    scheme = HTTPBearer(auto_error=False)

    def require(creds: HTTPAuthorizationCredentials | None = Depends(scheme)):
        # `compare_digest` because this is reachable from any page in the
        # browser: a timing oracle on a loopback port is a short walk.
        if creds is None or not secrets.compare_digest(
                creds.credentials or "", token):
            raise HTTPException(401, "bad or missing bearer token")

    auth = Depends(require)

    tickets = Tickets()

    @app.exception_handler(Error)
    async def _typed(_request: Request, exc: Error):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message,
                               **({"detail": exc.detail} if exc.detail else {})}})

    @app.get("/", response_class=HTMLResponse)
    async def page():
        # The token is *not* embedded here: the page reads it from the url
        # fragment, which browsers do not send to the server and proxies do not
        # log. Serving it in the body would put it in every cache along the way.
        return HTMLResponse(PAGE, headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy":
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'",
            "Referrer-Policy": "no-referrer"})

    @app.get("/v1/state", dependencies=[auth])
    async def state():
        rows = await run_in_threadpool(sup.rows)
        return {"config_path": str(sup.store.path),
                "writable": sup.store.writable,
                "default": sup.store.default,
                "programs": list(sup.store.programs),
                "gateways": rows,
                "at": time.time()}

    @app.post("/v1/tunnels/{name}/up", dependencies=[auth], status_code=202)
    async def up(name: str):
        await _act(sup.up, name)
        return {"name": name, "wanted": True}

    @app.post("/v1/tunnels/{name}/down", dependencies=[auth], status_code=202)
    async def down(name: str):
        await _act(sup.down, name)
        return {"name": name, "wanted": False}

    @app.post("/v1/tunnels/{name}/restart", dependencies=[auth],
              status_code=202)
    async def restart(name: str):
        await _act(sup.restart, name)
        return {"name": name, "wanted": True}

    @app.post("/v1/tunnels/{name}/answer", dependencies=[auth],
              status_code=202)
    async def answer(name: str, body: AnswerBody):
        """Answer whatever ssh is asking. The body may be a secret.

        Nothing about it is retained: it is written to the pty and dropped. The
        response says only that it was delivered.
        """
        await _act(lambda n: sup.answer(n, body.text), name)
        return {"name": name, "delivered": True}

    @app.get("/v1/tunnels/{name}/output", dependencies=[auth])
    async def output(name: str, after: int = 0):
        tunnel = sup.tunnel(name)
        if tunnel is None:
            raise Error(404, "not_a_tunnel",
                        f"gateway {name!r} has no ssh command")
        lines = tunnel.lines(after)
        return {"name": name,
                "lines": [{"seq": l.seq, "at": l.at, "text": l.text,
                           "kind": l.kind} for l in lines],
                "last_seq": lines[-1].seq if lines else after}

    @app.post("/v1/events/ticket", dependencies=[auth])
    async def events_ticket():
        """A single-use, 30-second credential for the event stream.

        `EventSource` cannot send an `Authorization` header, and putting the
        real token in a query string would leave it in browser history. So the
        page trades the token for a ticket that is good for one connection and
        expires whether or not it is used.
        """
        return {"ticket": tickets.issue(), "expires_in": int(Tickets.TTL_SEC)}

    @app.get("/v1/events")
    async def events(request: Request, after: int = 0, ticket: str = ""):
        if not tickets.redeem(ticket):
            raise Error(401, "bad_ticket",
                        "the event stream needs a fresh ticket from "
                        "POST /v1/events/ticket")

        async def stream():
            cursor, quiet = after, 0.0
            while not await request.is_disconnected():
                batch = await run_in_threadpool(
                    sup.wait_for_event, cursor, SSE_POLL_SEC)
                if batch:
                    quiet = 0.0
                    for event in batch:
                        cursor = event["seq"]
                        yield (f"id: {event['seq']}\n"
                               f"data: {json.dumps(event)}\n\n")
                    continue
                quiet += SSE_POLL_SEC
                if quiet >= SSE_IDLE_SEC:
                    quiet = 0.0
                    yield ": keepalive\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store",
                                          "X-Accel-Buffering": "no"})

    # -- reading the gateway through the tunnel ---------------------------
    #
    # The browser never gets a gateway's bearer token: it asks the daemon, which
    # already resolves `token`/`token_env`/`token_file` the way the CLI does and
    # is the only process that can reach the forwarded port anyway.
    #
    # Deliberately a handful of named read-only endpoints rather than a path
    # proxy. An open proxy on loopback would let any page in the browser submit
    # jobs, cancel them, or read files through the gateway; these four cannot do
    # anything but look.
    def _remote(name: str):
        from client.abclient import Gateways
        entry = sup.entry(name)
        if entry is None:
            raise Error(404, "unknown_gateway", f"unknown gateway {name!r}")
        try:
            return Gateways(sup.store.document).client(name)
        except Exception as exc:
            raise Error(400, "gateway_unusable", str(exc)) from exc

    async def _read(name: str, call):
        from client.abclient import GatewayError
        client = _remote(name)
        try:
            return await run_in_threadpool(call, client)
        except GatewayError as exc:
            # The overwhelmingly likely cause is the tunnel, so say so here
            # rather than making the page guess from a 500.
            raise Error(502, "gateway_unreachable", str(exc),
                        {"gateway": name}) from exc

    @app.get("/v1/gateways/{name}/jobs", dependencies=[auth])
    async def remote_jobs(name: str, limit: int = 50, cursor: str | None = None):
        return await _read(name, lambda c: c.list_jobs(limit=limit, cursor=cursor))

    @app.get("/v1/gateways/{name}/jobs/{job_id}", dependencies=[auth])
    async def remote_job(name: str, job_id: str):
        return await _read(name, lambda c: c.get_job(job_id))

    @app.get("/v1/gateways/{name}/jobs/{job_id}/events", dependencies=[auth])
    async def remote_events(name: str, job_id: str, after: int = 0,
                            tail: int = 0, limit: int = 300):
        def call(client):
            if tail:
                return client.events(job_id, tail=tail, limit=limit)
            return client.events(job_id, after=after, limit=limit)
        return await _read(name, call)

    @app.get("/v1/gateways/{name}/monitors", dependencies=[auth])
    async def remote_monitors(name: str, job: str | None = None):
        return await _read(name, lambda c: c.list_monitors(job=job))

    @app.put("/v1/gateways/{name}", dependencies=[auth])
    async def put_gateway(name: str, body: GatewayBody):
        def work():
            fields = body.model_dump(exclude_none=True)
            entry = sup.store.put(name, fields)
            sup.reload()
            return entry.public()
        return {"gateway": await _config(work)}

    @app.delete("/v1/gateways/{name}", dependencies=[auth])
    async def delete_gateway(name: str):
        def work():
            sup.store.delete(name)
            sup.reload()
            return {"deleted": name}
        return await _config(work)

    @app.post("/v1/gateways/{name}/default", dependencies=[auth])
    async def make_default(name: str):
        def work():
            sup.store.set_default(name)
            return {"default": name}
        return await _config(work)

    async def _act(fn, name: str):
        try:
            await run_in_threadpool(fn, name)
        except TunnelError as exc:
            raise Error(409, "tunnel_unavailable", str(exc),
                        {"name": name}) from exc

    async def _config(work):
        try:
            return await run_in_threadpool(work)
        except ConfigError as exc:
            raise Error(400, "bad_config", str(exc)) from exc
        except OSError as exc:
            raise Error(500, "cannot_write", f"{sup.store.path}: {exc}") from exc

    return app


def resolve_token(explicit: str | None) -> str:
    """The daemon's bearer token: given, from the environment, or fresh.

    Generated rather than optional. An unauthenticated port that can rewrite and
    run an ssh command is not a convenience, and "I'll set one later" is how it
    stays unset.
    """
    import os
    if explicit:
        return explicit
    from_env = os.environ.get("AGENT_BRIDGE_UI_TOKEN")
    if from_env:
        return from_env
    return secrets.token_urlsafe(24)
