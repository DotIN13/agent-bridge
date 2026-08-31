"""Typed FastAPI HTTP surface for agent-bridge."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import sys
import time
import uuid
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from . import __version__, files as filemod, jobdir, monitors as monmod
from .adapters import build as build_adapter, known_agents
from .adapters.base import SteerError
from .api_models import (
    ERROR_RESPONSES, EVENT_TYPES, AgentDescription, AgentsResponse,
    CancelResponse, EventsPage, FileItem, FilesPage, JobAccepted, JobCreate,
    JobDetail, JobsPage, MessageRequest, MessageResponse, ModelsResponse,
    MonitorCreate, MonitorDetail, MonitorPage,
    SessionDirsResponse, SessionsResponse, SteerRequest, SteerResponse,
    UploadResponse, iso_local,
)
from .bus import Bus
from .cluster import ClusterInfo
from .config import Config
from .db import (Database, IdempotencyConflict, ReportConflict, TERMINAL,
                 WAITING, derive_title)
from .docs import render_llms_txt
from .files import FileError
from .notes import NotesStore
from .worker import WorkerPool

_SSE_POLL = 0.3
_SSE_HEARTBEAT = 15.0


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 details: dict | None = None) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def _error_content(code: str, message: str,
                   details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


async def _parse_message_request(request: Request) -> MessageRequest:
    """Parse a report message from a JSON body or a multipart file upload."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        parsed: dict = {}
        for key, value in form.items():
            if isinstance(value, UploadFile):
                if key == "file":
                    content = await value.read()
                    parsed["msg"] = content.decode("utf-8", errors="replace")
                continue
            parsed[key] = value
        return MessageRequest.model_validate(parsed)
    return MessageRequest.model_validate(await request.json())


def _inline_model_schema(model) -> dict:
    """Return a standalone schema whose references resolve in-place."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.rsplit("/", 1)[-1]
            merged = dict(definitions[name])
            merged.update({key: item for key, item in value.items()
                           if key != "$ref"})
            return expand(merged)
        return {key: expand(item) for key, item in value.items()
                if key != "$defs"}

    return expand(schema)


def _content_disposition(name: str) -> str:
    fallback = "".join(
        char if char.isascii() and (char.isalnum() or char in "._-") else "_"
        for char in name).strip("._") or "download"
    encoded = urllib.parse.quote(name, safe="")
    return (f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{encoded}")


class Gateway:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.bus = Bus()
        self.pool = WorkerPool(cfg, self.db, self.bus)
        self.cluster = ClusterInfo(
            cfg.cluster_probe_timeout, cfg.cluster_env_presence
        ) if cfg.cluster_enabled else None
        # What the owner knows and the probes cannot discover (gateway/notes.py).
        self.notes = NotesStore(cfg.notes_path, cfg.notes_max_bytes)

    def publish_endpoint(self) -> dict:
        loopback = self.cfg.host in ("127.0.0.1", "localhost", "::1", "")
        info = {
            "bound": self.cfg.host,
            "port": self.cfg.port,
            "fqdn": socket.getfqdn(),
            "url": None if loopback else
                f"http://{socket.getfqdn()}:{self.cfg.port}",
        }
        if loopback:
            info["note"] = (
                "bound to loopback; compute-node reports use shared storage")
        path = Path(self.cfg.data_dir) / "gateway-endpoint.json"
        try:
            path.write_text(json.dumps(info, indent=2) + "\n")
        except OSError as e:
            print(f"warning: could not write {path}: {e}", file=sys.stderr)
        print(f"endpoint for compute nodes: {info['url'] or 'NONE (loopback)'}",
              flush=True)
        return info

    def serve_forever(self) -> None:
        self.publish_endpoint()
        uvicorn.run(create_app(self), host=self.cfg.host, port=self.cfg.port,
                    log_level="warning")


class MonitorRefused(ValueError):
    """A watch the gateway will not take: disabled, or over the active bound."""


def register_monitor(gw: Gateway, spec: dict, *,
                     monitor_id: str | None = None,
                     job_id: str | None = None) -> dict | None:
    """Create one monitor from a validated spec. None if it already existed.

    One path for both doors -- the HTTP route and a file dropped in a job dir --
    so the bounds, the interval floor and the deadline ceiling cannot be true of
    one and not the other.
    """
    cfg = gw.cfg
    if not cfg.monitors_enabled:
        raise MonitorRefused("monitors are disabled on this gateway")
    if gw.db.count_active_monitors() >= cfg.monitors_max_active:
        raise MonitorRefused(
            f"this gateway is already watching {cfg.monitors_max_active} things; "
            f"cancel one before adding another")
    poll = spec.get("poll") or monmod.slurm_poll(spec["slurm"])
    interval = float(spec.get("interval_sec") or cfg.monitors_default_interval_sec)
    interval = max(interval, cfg.monitors_min_interval_sec)
    deadline = None
    requested = spec.get("deadline_sec")
    ceiling = cfg.monitors_max_deadline_sec
    if requested or ceiling:
        span = min(float(requested or ceiling), ceiling) if ceiling else float(requested)
        deadline = time.time() + span
    return gw.db.create_monitor(
        monitor_id=monitor_id or str(uuid.uuid4()),
        job_id=job_id or spec.get("job"),
        poll_cmd=poll, interval_sec=interval, deadline=deadline,
        label=spec.get("label") or "", map_spec=spec.get("map") or "",
        note=spec.get("note") or "",
        result_paths=list(spec.get("result_paths") or ()))


def _monitor_event(gw: Gateway, row: dict) -> None:
    """Say what a monitor did, on the stream of the job that created it.

    Post-terminal annotation, exactly as design/07 allows: the job is usually
    finished by the time its monitor resolves, and `ab events <job> --type
    message` stays the one progress log rather than growing a second one.
    """
    if not row.get("job_id"):
        return
    data = {"source": "monitor", "monitor": row["id"],
            "status": _MONITOR_REPORT_STATUS.get(row["status"], "running"),
            "monitor_status": row["status"], "msg": monmod.summary(row)}
    if row.get("result_paths"):
        data["result_paths"] = row["result_paths"]
    if row["status"] in monmod.TERMINAL:
        # The record of how the long task actually ended, on the stream of the
        # job that started it: what was watched, what it last read, when it
        # resolved, and where the results are. A caller reading
        # `ab events <job> --type message` months later has the whole story
        # without the scheduler's own logs.
        data["terminal"] = True
        data["label"] = row.get("label") or ""
        data["poll_cmd"] = row["poll_cmd"]
        data["detail"] = row.get("detail") or ""
        data["finished_at"] = iso_local(row["finished_at"]) \
            if row.get("finished_at") else None
        data["watched_for_sec"] = round(
            (row["finished_at"] or time.time()) - row["created_at"], 1)
    try:
        event = gw.db.append_event(row["job_id"], "message", data)
    except Exception as exc:                          # a deleted job, say
        print(f"warning: monitor event failed: {exc}", file=sys.stderr)
        return
    gw.bus.publish(row["job_id"], event)


#: A monitor's status as an `ab-notify`-shaped report status, so a reader that
#: already filters on `status` sees the same words from both channels. `expired`
#: reports as `failed` deliberately: to a caller waiting on the work, "we
#: stopped watching" is not good news, and `monitor_status` keeps the precise
#: word for anyone who cares.
_MONITOR_REPORT_STATUS = {"queued": "queued", "running": "running",
                          "finished": "finished", "failed": "failed",
                          "expired": "failed", "canceled": "failed"}


def _adopt_monitor_drops(gw: Gateway, job_id: str) -> None:
    """Register monitors a delegate dropped as files in its job dir."""
    job_dir = jobdir.path_for(gw.cfg.data_dir, job_id)
    for name, fields in jobdir.monitor_drops(job_dir):
        spec = {"poll": fields.get("poll"), "slurm": fields.get("slurm"),
                "label": fields.get("label") or name,
                "map": fields.get("map"), "note": fields.get("note"),
                "interval_sec": _as_seconds(fields.get("interval")),
                "deadline_sec": _as_seconds(fields.get("deadline")),
                "result_paths": [p for p in (fields.get("result") or "").split(",")
                                 if p.strip()]}
        try:
            row = register_monitor(gw, spec, monitor_id=f"{job_id}:{name}",
                                   job_id=job_id)
        except (MonitorRefused, ValueError, KeyError) as exc:
            # The drop is the delegate's only feedback channel, so a refusal has
            # to land where it will read it rather than in the gateway's log.
            gw.db.append_event(job_id, "message", {
                "source": "monitor", "file": f"monitors/{name}",
                "error": f"monitor not registered: {exc}"})
            continue
        if row:
            _monitor_event(gw, row)


def _as_seconds(value: str | None) -> float | None:
    """`300`, `90s`, `15m`, `12h`, `2d` -> seconds. Unparseable -> None.

    A batch script writes durations the way a person says them; refusing `12h`
    and demanding 43200 is the kind of friction that gets a monitor left
    unregistered.
    """
    text = (value or "").strip().lower()
    if not text:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    scale = units.get(text[-1], 1)
    number = text[:-1] if text[-1] in units else text
    try:
        return float(number) * scale
    except ValueError:
        return None


def _ingest_and_settle(gw: Gateway, job_id: str) -> list[dict]:
    """Ingest on a read, and finish a `waiting` job whose report has landed.

    The finish belongs here rather than only in the sweeper because a read is the
    moment somebody is asking. Ingesting without settling let a `GET /v1/jobs/<id>`
    return `waiting` in a body whose own event list already carried `report.md` --
    the response contradicting itself, for up to one sweep interval. Idempotent:
    the row moves once, from `waiting` only.
    """
    rows = _ingest_external(gw, job_id)
    row = gw.db.get_job(job_id)
    if row and row.get("status") == WAITING:
        _finish_if_reported(gw, job_id)
    return rows


def _ingest_external(gw: Gateway, job_id: str) -> list[dict]:
    """Pull in everything a job reported outside its own event stream.

    One channel: the job dir. Idempotent and keyed, so a second pass inserts
    nothing, and cheap enough to run on every read. Returns the rows it
    inserted so a live-stream sweeper can publish them.

    There used to be a second channel here -- a shared-filesystem JSONL drop,
    for a compute node that could not reach the gateway over HTTP. Nothing has
    written it since `ab-notify` became a job-dir reporter, and a reader with no
    writer is worse than nothing: it was a whole ingestion path, with its own
    bounds and dedup identity, that no test could exercise from the outside.
    """
    job_dir = str(jobdir.path_for(gw.cfg.data_dir, job_id))
    rows = gw.db.ingest_job_dir(job_id, job_dir)
    if gw.cfg.monitors_enabled:
        # Here rather than only in the sweeper: a read is the other moment a
        # pending registration can be noticed, and adoption is idempotent.
        _adopt_monitor_drops(gw, job_id)
    return rows


def _poll_monitors(gw: Gateway, now: float | None = None) -> None:
    """One round of every monitor that is due, plus deadline expiry.

    Runs the delegate's command, records the outcome, and emits an event only
    when the status actually changed -- a five-second sweep must not write an
    event every five seconds for the eight hours a training run takes.

    `now` is injectable so a test can advance past an interval without either
    sleeping or reaching into the connection.
    """
    for row in gw.db.due_monitors(now):
        status, detail = monmod.poll(row, gw.cfg.monitors_poll_timeout_sec)
        changed = gw.db.record_poll(row["id"], status, detail, now=now)
        if changed:
            _monitor_event(gw, changed)
    for expired in gw.db.expire_monitors(now):
        _monitor_event(gw, expired)


def _finish_if_reported(gw: Gateway, job_id: str) -> None:
    """A `waiting` job whose report has landed is finished, and answered.

    Also what ends the *run*: the agent is still alive with its stdin held open
    (design/17), so closing that handle is how the worker learns to wind up. A
    backend whose child already exited has no handle, and the row is all there
    is to close.
    """
    job_dir = jobdir.path_for(gw.cfg.data_dir, job_id)
    if not jobdir.has_report(job_dir):
        return
    # Into the row as well as onto the stream (design/23). Before the status
    # change, so a reader that sees `succeeded` never sees it without the answer.
    reported = jobdir.read_report(job_dir)
    if reported:
        gw.db.save_result_fields(job_id, {"result": reported})
    rows = gw.db.finish_reported(job_id)
    if not rows:
        return
    for row in rows:
        gw.bus.publish(job_id, row)
    steering = gw.pool.steering(job_id)
    if steering is not None:
        try:
            steering.close()
        except Exception:                              # already gone
            pass
    gw.bus.close(job_id)


def _expire_waiting(gw: Gateway) -> None:
    """Give up on a job that ended its turn and never wrote its report."""
    for job_id in gw.db.expire_waiting():
        steering = gw.pool.steering(job_id)
        if steering is not None:
            try:
                steering.close()
            except Exception:
                pass
        gw.bus.close(job_id)


async def _sweep_reports(gw: Gateway, interval: float = 5.0) -> None:
    """Notice what a delegate wrote, and move the monitors along.

    Not read-triggered only: a follower that has been streaming since the turn
    started never issues another read, so a milestone drop would sit unseen
    until it reconnected. The job-dir scan is a handful of stats over at most
    200 rows, so it can run this often -- which is also how quickly a `waiting`
    job notices its report and finishes.
    """
    while True:
        try:
            for row in await run_in_threadpool(gw.db.jobs_with_open_dirs):
                for event in await run_in_threadpool(
                        _ingest_external, gw, row["id"]):
                    gw.bus.publish(row["id"], event)
                if row.get("status") == WAITING:
                    await run_in_threadpool(_finish_if_reported, gw, row["id"])
            await run_in_threadpool(_expire_waiting, gw)
            if gw.cfg.monitors_enabled:
                await run_in_threadpool(_poll_monitors, gw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # never kill the loop
            print(f"warning: report sweep failed: {exc}", file=sys.stderr)
        await asyncio.sleep(interval)


def create_app(gw: Gateway) -> FastAPI:
    cfg = gw.cfg

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        filemod.cleanup_staging(cfg)
        filemod.cleanup_orphan_job_dirs(cfg, gw.db.job_ids())
        await run_in_threadpool(gw.pool.start)
        if gw.cluster:
            gw.cluster.start_async()
        sweeper = asyncio.create_task(_sweep_reports(gw))
        print(f"agent-bridge {__version__} listening on "
              f"http://{cfg.host}:{cfg.port}  (db: {cfg.db_path})", flush=True)
        yield
        sweeper.cancel()
        await run_in_threadpool(gw.pool.stop)
        gw.db.close()

    app = FastAPI(title="agent-bridge", version=__version__, lifespan=lifespan)
    security = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def bounded_request_body(request: Request, call_next):
        """Enforce limits for chunked bodies too, not only Content-Length."""
        if request.method not in {"POST", "PUT", "PATCH"}:
            return await call_next(request)
        maximum_mb = 1 if request.url.path.endswith("/message") else \
            cfg.files_max_request_mb
        maximum = maximum_mb << 20
        original_receive = request._receive
        seen = 0

        async def receive():
            nonlocal seen
            message = await original_receive()
            seen += len(message.get("body", b""))
            if seen > maximum:
                raise ApiError(413, "payload_too_large",
                               f"request exceeds {maximum_mb} MiB")
            return message

        request._receive = receive
        try:
            return await call_next(request)
        except ApiError as exc:
            return JSONResponse(status_code=exc.status, content=_error_content(
                exc.code, exc.message, exc.details))

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=_error_content(
            exc.code, exc.message, exc.details))

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_request: Request,
                                         exc: RequestValidationError):
        details = json.loads(json.dumps(exc.errors(), default=str))
        return JSONResponse(status_code=422, content=_error_content(
            "validation_error", "request validation failed", {"errors": details}))

    @app.exception_handler(HTTPException)
    async def http_error_handler(_request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code") or detail.get("error") or "http_error")
            message = str(detail.get("message") or detail.get("error") or detail)
            details = {key: value for key, value in detail.items()
                       if key not in {"code", "message", "error"}} or None
        else:
            code = "http_error"
            message = str(detail)
            details = None
        return JSONResponse(status_code=exc.status_code,
                            content=_error_content(code, message, details))

    def require_auth(credentials: HTTPAuthorizationCredentials | None =
                     Depends(security)) -> None:
        token = credentials.credentials if credentials and \
            credentials.scheme.lower() == "bearer" else ""
        if not token or not hmac.compare_digest(token, cfg.token):
            raise ApiError(401, "unauthorized", "missing or invalid bearer token")

    auth = Depends(require_auth)

    def resolve_job(ref: str) -> dict:
        job = gw.db.get_job(ref)
        if job:
            return job
        for kind, finder in (("title", gw.db.find_jobs_by_title),
                             ("id-prefix", gw.db.find_jobs_by_prefix)):
            matches = finder(ref)
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise ApiError(409, "ambiguous_reference",
                               f"{kind} '{ref}' is ambiguous",
                               {"matches": [{
                                   "id": item["id"], "title": item.get("title"),
                                   "status": item.get("status"),
                                   "created_at": item.get("created_at")}
                                   for item in matches]})
        raise ApiError(404, "not_found",
                       f"no job matching id, title, or id-prefix '{ref}'")

    @app.get("/health", response_model=dict[str, Any])
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/llms.txt", response_class=PlainTextResponse)
    @app.get("/v1/help", response_class=PlainTextResponse)
    async def help_doc():
        return PlainTextResponse(render_llms_txt(cfg),
                                 media_type="text/markdown; charset=utf-8")

    @app.get("/v1/agents", dependencies=[auth],
             response_model=AgentsResponse, responses=ERROR_RESPONSES)
    async def agents():
        rows = []
        for name in sorted(cfg.agents):
            agent_cfg = cfg.agents[name]
            adapter = build_adapter(agent_cfg)
            rows.append(AgentDescription(
                name=name,
                default_model=agent_cfg.model or None,
                models=list(agent_cfg.models),
                default_cwd=agent_cfg.default_cwd,
                capabilities=adapter.capabilities()))
        return AgentsResponse(
            configured=sorted(cfg.agents), known=known_agents(),
            default=cfg.default_agent, agents=rows,
            features={"files": cfg.files_enabled,
                      "cluster_info": cfg.cluster_enabled,
                      "event_stream": "sse"})

    @app.get("/v1/models", dependencies=[auth],
             response_model=ModelsResponse, responses=ERROR_RESPONSES)
    async def models(agent: str | None = None):
        agent_cfg = cfg.agents.get(agent or cfg.default_agent)
        if not agent_cfg:
            raise ApiError(400, "unknown_agent", f"unknown agent '{agent}'")
        return {"agent": agent_cfg.name, "models": list(agent_cfg.models),
                "default": agent_cfg.model or None}

    @app.get("/v1/info", dependencies=[auth], response_model=dict[str, Any],
             responses=ERROR_RESPONSES)
    async def info(refresh: bool = False):
        """What this machine is, and what its owner says about it.

        Two kinds of knowledge in one answer, deliberately. Above: what the
        probes measured — hostname, GPUs, scheduler. Below, under `notes`: the
        markdown file on this host, which is where the things no probe can
        discover live — which account to charge, which filesystem is full,
        which env has which package. A caller that reads one has read the
        other, which is the point: nobody thinks to ask for local conventions
        they do not know exist.
        """
        # `[cluster] enabled = false` used to 404 this whole route, which took
        # the notes with it -- and the notes are the half a probe cannot
        # produce, configured separately, on a gateway whose operator has
        # already gone to the trouble of writing them. So probing off means
        # fewer keys, not a missing document.
        probed = gw.cluster.get() if gw.cluster else {"cluster_enabled": False}
        if refresh and gw.cluster:
            gw.cluster.refresh_async()
        doc = gw.notes.read()
        return {**probed,
                # ISO with the offset, like every other timestamp this API
                # publishes — a bare epoch float has never reached a caller and
                # should not start here.
                "notes": {"text": doc.text,
                          "updated_at": iso_local(doc.updated_at) if doc.updated_at else None,
                          # The path, so an agent asked to update the notes
                          # knows which file to open.
                          "path": gw.notes.path}}

    @app.get("/v1/session-dirs", dependencies=[auth],
             response_model=SessionDirsResponse, responses=ERROR_RESPONSES)
    async def session_dirs(agent: str | None = None):
        """Directories that hold sessions — the "where is there work" view.

        Returned whole. Its size is bounded by how many projects exist rather
        than by a page window, so unlike the old flat session list it cannot
        quietly omit a project just because another one has been busy.
        """
        agent_cfg = cfg.agents.get(agent or cfg.default_agent)
        if not agent_cfg:
            raise ApiError(400, "unknown_agent", f"unknown agent '{agent}'")
        dirs = await run_in_threadpool(build_adapter(agent_cfg).list_dirs)
        return {"dirs": [d.to_public() for d in dirs], "total": len(dirs)}

    @app.get("/v1/sessions", dependencies=[auth],
             response_model=SessionsResponse, responses=ERROR_RESPONSES)
    async def sessions(cwd: str | None = None, agent: str | None = None,
                       limit: int = Query(40, ge=1, le=200),
                       cursor: str | None = None):
        """Sessions, newest first. `cwd` is an **exact** directory match.

        Paged rather than truncated: `total` is the real size of the selection,
        so a short page is visibly a page and never a silent sample.
        """
        agent_cfg = cfg.agents.get(agent or cfg.default_agent)
        if not agent_cfg:
            raise ApiError(400, "unknown_agent", f"unknown agent '{agent}'")
        try:
            page = await run_in_threadpool(
                build_adapter(agent_cfg).list_sessions, cwd, limit, cursor)
        except ValueError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc
        return {"sessions": [s.to_public() for s in page.sessions],
                "total": page.total, "next_cursor": page.next_cursor,
                "has_more": page.next_cursor is not None}

    @app.post(
        "/v1/jobs", dependencies=[auth], response_model=JobAccepted,
        status_code=202, responses=ERROR_RESPONSES,
        openapi_extra={"requestBody": {"required": True, "content": {
            "application/json": {"schema": _inline_model_schema(JobCreate)},
            "multipart/form-data": {"schema": {"type": "object",
                "properties": {"payload": {"type": "string"},
                               "files": {"type": "array", "items": {
                                   "type": "string", "format": "binary"}}}}}}}})
    async def create_job(request: Request,
                         idempotency_key: str | None = Header(
                             default=None, alias="Idempotency-Key",
                             min_length=1, max_length=200)):
        _reject_oversize(request, cfg)
        raw, uploads = await _parse_submission(request)
        try:
            spec = JobCreate.model_validate(raw)
        except ValidationError as exc:
            raise ApiError(400, "validation_error", "invalid job submission",
                           {"errors": json.loads(exc.json())}) from exc
        if spec.expect_report:
            # Refused rather than ignored: a caller asking to be waited for and
            # silently not being is the substitution design/03 rules out. The
            # field is still on the DTO so this message can explain itself.
            raise ApiError(
                400, "expect_report_removed",
                "expect_report is gone: a job ends when its turn ends. Watch "
                "work that outlives the turn with a monitor (POST /v1/monitors, "
                "or `ab-monitor add` from inside the job) and wait on that.")
        agent_name = spec.agent or cfg.default_agent
        agent_cfg = cfg.agents.get(agent_name)
        if not agent_cfg:
            raise ApiError(400, "unknown_agent",
                           f"unknown agent '{agent_name}'")
        try:
            cwd = agent_cfg.resolve_cwd(spec.cwd)
        except ValueError as exc:
            raise ApiError(400, "cwd_not_allowed", str(exc)) from exc
        if (spec.files or uploads) and not cfg.files_enabled:
            raise ApiError(400, "files_disabled", "file attachments are disabled")
        if not spec.fork:
            holder = gw.pool.claimant(spec.session or "")
            if holder:
                raise ApiError(409, "session_busy",
                    f"session {spec.session} is being written by a running job",
                    {"session": spec.session, "held_by": holder,
                     "steer_ref": f"/v1/jobs/{holder}/steer"})

        request_hash = await _submission_hash(spec, uploads)
        if idempotency_key:
            try:
                replay = gw.db.idempotency_lookup(
                    "jobs", idempotency_key, request_hash)
            except IdempotencyConflict as exc:
                raise ApiError(409, "idempotency_conflict", str(exc)) from exc
            if replay:
                _status, body = replay
                body["replayed"] = True
                return JSONResponse(status_code=202, content=body,
                    headers={"Location": f"/v1/jobs/{body['id']}"})

        job_id = str(uuid.uuid4())
        items = [item.model_dump(exclude_none=True) for item in spec.files]
        paths: list[str] = []
        promoted = False
        persisted = False
        try:
            paths, promoted = await _save_job_files(
                cfg, job_id, items, uploads)
            title = (spec.title or "").strip() or derive_title(spec.prompt)
            response = {
                "id": job_id, "status": "queued", "agent": agent_name,
                "cwd": cwd, "title": title, "fork": spec.fork,
                "include_thinking": spec.include_thinking, "files": paths,
                "replayed": False,
                "session": spec.session,
                "session_state": "pinned" if spec.session else "pending"}
            job_data = dict(
                job_id=job_id, agent=agent_name, prompt=spec.prompt, cwd=cwd,
                session=spec.session,
                permission_mode=spec.permission_mode, model=spec.model,
                title=title, fork=spec.fork,
                include_thinking=spec.include_thinking, files=paths)
            if idempotency_key:
                try:
                    response, created = gw.db.create_job_idempotent(
                        scope="jobs", key=idempotency_key,
                        request_hash=request_hash, response=response, job=job_data)
                except IdempotencyConflict as exc:
                    raise ApiError(409, "idempotency_conflict", str(exc)) from exc
                if not created:
                    if promoted:
                        filemod.remove_tree(filemod.job_dir(cfg, job_id))
                    return JSONResponse(status_code=202, content=response,
                        headers={"Location": f"/v1/jobs/{response['id']}"})
                persisted = True
            else:
                gw.db.create_job(**job_data)
                persisted = True
            gw.pool.submit(response["id"])
            return JSONResponse(status_code=202, content=response,
                headers={"Location": f"/v1/jobs/{response['id']}"})
        except ApiError:
            if promoted and not persisted:
                filemod.remove_tree(filemod.job_dir(cfg, job_id))
            raise
        except (FileError, OSError, ValueError) as exc:
            filemod.remove_tree(filemod.job_staging_dir(cfg, job_id))
            if promoted and not persisted:
                filemod.remove_tree(filemod.job_dir(cfg, job_id))
            raise ApiError(400, "file_error", str(exc)) from exc
        except Exception:
            filemod.remove_tree(filemod.job_staging_dir(cfg, job_id))
            if promoted and not persisted:
                filemod.remove_tree(filemod.job_dir(cfg, job_id))
            raise

    @app.get("/v1/jobs", dependencies=[auth], response_model=JobsPage,
             responses=ERROR_RESPONSES)
    async def list_jobs(limit: int = Query(50, ge=1, le=200),
                        cursor: str | None = None):
        try:
            rows, next_cursor, has_more = gw.db.list_jobs_page(limit, cursor)
        except ValueError as exc:
            raise ApiError(400, "invalid_cursor", str(exc)) from exc
        return {"jobs": rows, "next_cursor": next_cursor,
                "has_more": has_more}

    async def _store_message(job: dict, message: MessageRequest) -> dict:
        data = message.model_dump(exclude_none=True)
        try:
            row = await run_in_threadpool(
                gw.db.add_message, job["id"], data, message.report_id)
        except ReportConflict as exc:
            raise ApiError(409, "report_id_conflict", str(exc)) from exc
        if not row.get("duplicate"):
            gw.bus.publish(job["id"], row)
        return {"id": job["id"], "seq": row["seq"],
                "duplicate": bool(row.get("duplicate"))}

    # -- monitors ---------------------------------------------------------
    @app.post("/v1/monitors", dependencies=[auth], status_code=201,
              response_model=MonitorDetail, responses=ERROR_RESPONSES)
    async def create_monitor(spec: MonitorCreate):
        """Watch something that outlives a turn.

        The usual caller is the delegate itself, from the gateway host, right
        before it ends its turn -- `ab-monitor add`. A client can register one
        too, which is the path a laptop takes for work it started by hand.
        """
        if spec.job:
            spec.job = resolve_job(spec.job)["id"]
        try:
            row = await run_in_threadpool(
                register_monitor, gw, spec.model_dump(exclude_none=True))
        except MonitorRefused as exc:
            raise ApiError(409, "monitors_exhausted", str(exc)) from exc
        if row is None:                                  # id collision only
            raise ApiError(409, "monitor_exists", "that monitor id is taken")
        await run_in_threadpool(_monitor_event, gw, row)
        return row

    @app.get("/v1/monitors", dependencies=[auth], response_model=MonitorPage,
             responses=ERROR_RESPONSES)
    async def list_monitors(job: str | None = Query(None),
                            status: str | None = Query(None),
                            active: bool | None = Query(None),
                            limit: int = Query(50, ge=1, le=200),
                            cursor: str | None = Query(None)):
        job_id = resolve_job(job)["id"] if job else None
        if status and status not in monmod.STATUSES:
            raise ApiError(400, "invalid_request",
                           f"unknown status '{status}'; expected one of "
                           f"{', '.join(monmod.STATUSES)}")
        try:
            rows, next_cursor, has_more = gw.db.list_monitors(
                job_id=job_id, status=status, active=active,
                limit=limit, cursor=cursor)
        except ValueError as exc:
            raise ApiError(400, "invalid_cursor", str(exc)) from exc
        return {"monitors": rows, "next_cursor": next_cursor,
                "has_more": has_more}

    def resolve_monitor(monitor_id: str) -> dict:
        row = gw.db.monitor(monitor_id)
        if row is None:
            raise ApiError(404, "not_found", f"no monitor '{monitor_id}'")
        return row

    @app.get("/v1/monitors/{monitor_id}", dependencies=[auth],
             response_model=MonitorDetail, responses=ERROR_RESPONSES)
    async def get_monitor(monitor_id: str):
        return resolve_monitor(monitor_id)

    @app.post("/v1/monitors/{monitor_id}/cancel", dependencies=[auth],
              response_model=MonitorDetail, responses=ERROR_RESPONSES)
    async def cancel_monitor(monitor_id: str):
        """Stop watching. Idempotent: cancelling a resolved monitor returns it.

        Cancelling a *watch* says nothing about the work it was watching, which
        keeps running -- the gateway never had a handle on it to begin with.
        """
        row = resolve_monitor(monitor_id)
        closed = await run_in_threadpool(
            gw.db.close_monitor, monitor_id, "canceled", "canceled by request")
        if closed is None:
            return row
        await run_in_threadpool(_monitor_event, gw, closed)
        return closed

    @app.post("/v1/jobs/{job_id}/message", dependencies=[auth],
              response_model=MessageResponse, responses=ERROR_RESPONSES)
    async def add_message(job_id: str, message: MessageRequest,
                          request: Request):
        _reject_oversize(request, cfg, maximum_mb=1)
        job = resolve_job(job_id)
        return await _store_message(job, message)

    @app.post("/v1/jobs/{job_id}/message/file", dependencies=[auth],
              response_model=MessageResponse, responses=ERROR_RESPONSES)
    async def add_message_file(job_id: str, request: Request):
        _reject_oversize(request, cfg, maximum_mb=1)
        job = resolve_job(job_id)
        message = await _parse_message_request(request)
        return await _store_message(job, message)

    @app.post("/v1/jobs/{job_id}/steer", dependencies=[auth],
              response_model=SteerResponse, status_code=202,
              responses=ERROR_RESPONSES)
    async def steer_job(job_id: str, body: SteerRequest):
        job = resolve_job(job_id)
        jid = job["id"]
        if job["status"] in TERMINAL:
            raise ApiError(409, "job_terminal", "job already finished",
                           {"id": jid, "status": job["status"]})
        handle = gw.pool.steering(jid)
        if handle is None or not handle.available:
            reason = handle.unavailable_reason if handle else \
                "job is not running on this gateway"
            raise ApiError(409, "steering_unavailable", reason,
                           {"id": jid, "status": job["status"]})
        try:
            await run_in_threadpool(handle.send, body.prompt)
        except SteerError as exc:
            raise ApiError(409, "steering_unavailable", str(exc)) from exc
        # The note is the handle's, not this route's: claude takes a steer at
        # its next tool boundary, opencode admits it and promotes it into the
        # running turn, and a response that says the first about the second is
        # simply wrong.
        return {"id": jid, "delivered": True, "note": handle.note}

    @app.get("/v1/jobs/{job_id}", dependencies=[auth],
             response_model=JobDetail, responses=ERROR_RESPONSES)
    async def get_job(job_id: str):
        job = resolve_job(job_id)
        await run_in_threadpool(_ingest_and_settle, gw, job["id"])
        return gw.db.get_job(job["id"])

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[auth],
              response_model=CancelResponse, status_code=202,
              responses={200: {"model": CancelResponse,
                               "description": "Already canceled"},
                         **ERROR_RESPONSES})
    async def cancel_job(job_id: str):
        job = resolve_job(job_id)
        jid = job["id"]
        if job["status"] == "canceled":
            return JSONResponse(status_code=200,
                content={"id": jid, "status": "canceled",
                         "already_terminal": True})
        if job["status"] in TERMINAL:
            raise ApiError(409, "job_terminal", "job already finished",
                           {"id": jid, "status": job["status"]})
        was = await run_in_threadpool(gw.pool.cancel, jid)
        if was == "canceled":
            return JSONResponse(status_code=200,
                content={"id": jid, "status": "canceled",
                         "already_terminal": True})
        if was in {"succeeded", "failed"}:
            raise ApiError(409, "job_terminal", "job already finished",
                           {"id": jid, "status": was})
        return {"id": jid, "canceling": True, "was": was,
                "status": "canceling"}

    @app.get("/v1/jobs/{job_id}/events", dependencies=[auth],
             response_model=EventsPage,
             responses={200: {"description": "Event page or live stream",
                 "content": {
                     "application/json": {"schema": {
                         "$ref": "#/components/schemas/EventsPage"}},
                     "text/event-stream": {"schema": {"type": "string"}}}},
                 **ERROR_RESPONSES})
    async def job_events(job_id: str, request: Request,
                         after: int = Query(0, ge=0),
                         limit: int = Query(500, ge=1, le=1000),
                         tail: int | None = Query(None, ge=1, le=1000),
                         until: int | None = Query(None, ge=1),
                         type: list[str] | None = Query(None),
                         legacy: bool = True):
        job = resolve_job(job_id)
        jid = job["id"]
        # Settling here too: this response carries `status` and `terminal`, so a
        # page that lists `report.md` and calls the job `waiting` is the same
        # self-contradiction the job read had.
        await run_in_threadpool(_ingest_and_settle, gw, jid)
        start = _parse_after(after, request.headers.get("last-event-id"))
        if tail is not None and after:
            # Anchoring from both ends at once has no single sensible reading;
            # make the caller pick rather than guessing which they meant.
            raise ApiError(400, "invalid_request",
                           "tail and after cannot be combined; choose one end")
        if type:
            unknown = sorted(set(type) - EVENT_TYPES)
            if unknown:
                raise ApiError(400, "invalid_request",
                               f"unknown event type(s): {', '.join(unknown)}",
                               {"known": sorted(EVENT_TYPES)})
        if "text/event-stream" not in request.headers.get("accept", ""):
            bounds = gw.db.event_bounds(jid)
            if tail is not None:
                visible = gw.db.events_tail(jid, tail, until_seq=until,
                                            types=tuple(type or ()))
                # A tail is anchored at the end, so there is nothing after it to
                # page to; `has_more` describes older events it skipped.
                has_more = bool(visible) and visible[0]["seq"] > (bounds["first_seq"] or 0)
            else:
                events = gw.db.events_after(jid, start, limit + 1)
                has_more = len(events) > limit
                visible = events[:limit]
                if until is not None:
                    visible = [e for e in visible if e["seq"] <= until]
                if type:
                    keep = set(type)
                    visible = [e for e in visible if e["type"] in keep]
            first_ts = bounds["first_ts"]
            if first_ts is not None:
                for event in visible:
                    event["elapsed"] = round(event["ts"] - first_ts, 3)
            current = gw.db.get_job(jid)
            return {
                "job": current if legacy else None,
                "events": visible, "status": current["status"],
                "terminal": current["status"] in TERMINAL,
                "next_after": visible[-1]["seq"] if visible else start,
                "has_more": has_more,
                "total": bounds["total"],
                "first_seq": bounds["first_seq"],
                "last_seq": bounds["last_seq"]}
        return StreamingResponse(
            _sse_stream(gw, jid, start, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post(
        "/v1/files", dependencies=[auth], response_model=UploadResponse,
        responses=ERROR_RESPONSES,
        openapi_extra={"requestBody": {"required": True, "content": {
            "application/json": {"schema": {"type": "object",
                "properties": {"files": {"type": "array", "items":
                    _inline_model_schema(FileItem)}}, "required": ["files"],
                "additionalProperties": False}},
            "multipart/form-data": {"schema": {"type": "object"}}}}})
    async def upload(request: Request):
        if not cfg.files_enabled:
            raise ApiError(400, "files_disabled", "file uploads are disabled")
        _reject_oversize(request, cfg)
        raw, uploads = await _parse_submission(request)
        raw_items = raw.get("files") or []
        if set(raw) - {"files"}:
            raise ApiError(400, "validation_error",
                           "upload body accepts only the files field")
        if not isinstance(raw_items, list):
            raise ApiError(400, "validation_error", "files must be an array")
        try:
            items = [FileItem.model_validate(item).model_dump(exclude_none=True)
                     for item in raw_items]
        except ValidationError as exc:
            raise ApiError(400, "validation_error", "invalid file item",
                           {"errors": json.loads(exc.json())}) from exc
        if not items and not uploads:
            raise ApiError(400, "empty_upload", "give at least one file")
        upload_id = uuid.uuid4().hex
        dest = filemod.upload_dir(cfg, upload_id)
        try:
            filemod.validate_upload_names(items, uploads)
            paths = await _save_all(cfg, items, uploads, dest)
        except (FileError, OSError, ValueError) as exc:
            filemod.remove_tree(dest)
            raise ApiError(400, "file_error", str(exc)) from exc
        return {"upload_id": upload_id, "dir": str(dest), "paths": paths}

    @app.get("/v1/files/list", dependencies=[auth], response_model=FilesPage,
             responses=ERROR_RESPONSES)
    async def files_list(dir: str, glob: str = "*", recursive: bool = False,
                         limit: int = Query(200, ge=1, le=1000),
                         cursor: str | None = None):
        try:
            rows, next_cursor, has_more = await run_in_threadpool(
                filemod.list_files_page, cfg, dir, glob, recursive, limit, cursor)
        except FileError as exc:
            raise ApiError(400, "file_error", str(exc)) from exc
        return {"dir": dir, "files": rows, "next_cursor": next_cursor,
                "has_more": has_more}

    @app.get("/v1/files/content", dependencies=[auth],
             responses={200: {"description": "File bytes", "content": {
                 "application/octet-stream": {"schema": {
                     "type": "string", "format": "binary"}}}},
                 **ERROR_RESPONSES})
    async def files_content(path: str):
        try:
            target, size = filemod.open_for_download(cfg, path)
        except FileError as exc:
            raise ApiError(400, "file_error", str(exc)) from exc
        return StreamingResponse(
            filemod.iter_file(target), media_type="application/octet-stream",
            headers={"Content-Length": str(size),
                     "Content-Disposition": _content_disposition(target.name)})

    return app


def _reject_oversize(request: Request, cfg: Config,
                     maximum_mb: int | None = None) -> None:
    maximum = maximum_mb or cfg.files_max_request_mb
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError as exc:
        raise ApiError(400, "invalid_content_length", "invalid Content-Length") from exc
    if length and length > (maximum << 20):
        raise ApiError(413, "payload_too_large",
                       f"request exceeds {maximum} MiB")


async def _parse_submission(request: Request) -> tuple[dict, list]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception as exc:
            raise ApiError(400, "invalid_multipart",
                           "invalid multipart form data") from exc
        raw_payload = None
        uploads = []
        for key, value in form.multi_items():
            if key == "payload":
                if raw_payload is not None:
                    raise ApiError(400, "validation_error",
                                   "multipart requires exactly one payload field")
                if isinstance(value, UploadFile) or not isinstance(value, str):
                    raise ApiError(400, "validation_error",
                                   "multipart payload must be a text field")
                raw_payload = value
            elif key == "files":
                if not isinstance(value, UploadFile):
                    raise ApiError(400, "validation_error",
                                   "multipart files fields must be file parts")
                uploads.append(value)
            else:
                raise ApiError(400, "validation_error",
                               f"unknown multipart field: {key}")
        if raw_payload is None:
            raise ApiError(400, "validation_error",
                           "multipart requires exactly one text payload field")
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ApiError(400, "invalid_json", "invalid multipart payload JSON") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "validation_error", "payload must be a JSON object")
        return payload, uploads
    try:
        body = await request.json()
    except Exception as exc:
        raise ApiError(400, "invalid_json", "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "validation_error", "body must be a JSON object")
    return body, []


async def _submission_hash(spec: JobCreate, uploads: list) -> str:
    def work() -> str:
        upload_rows = []
        for upload in uploads:
            stream = upload.file
            position = stream.tell()
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
            stream.seek(position)
            upload_rows.append({"filename": upload.filename,
                                "sha256": digest.hexdigest()})
        semantic = {"payload": spec.model_dump(exclude_none=True),
                    "uploads": upload_rows}
        return hashlib.sha256(json.dumps(
            semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return await run_in_threadpool(work)


async def _save_all(cfg: Config, items: list[dict], uploads: list,
                    dest: Path) -> list[str]:
    def work():
        paths = [filemod.save_inline_item(cfg, dest, item) for item in items]
        for upload in uploads:
            paths.append(filemod.save_stream(
                cfg, dest, upload.filename, upload.file).path)
        return paths
    return await run_in_threadpool(work)


async def _save_job_files(cfg: Config, job_id: str, items: list[dict],
                          uploads: list) -> tuple[list[str], bool]:
    if not items and not uploads:
        return [], False
    staging = filemod.job_staging_dir(cfg, job_id)
    final = filemod.job_dir(cfg, job_id)
    filemod.remove_tree(staging)
    try:
        filemod.validate_upload_names(items, uploads)
        staged = await _save_all(cfg, items, uploads, staging)
        old, new = filemod.promote_staging(cfg, job_id)
        return filemod.promoted_paths(staged, old, new), True
    except Exception:
        filemod.remove_tree(staging)
        filemod.remove_tree(final)
        raise


async def _sse_stream(gw: Gateway, job_id: str, after: int,
                      request: Request):
    last = after
    idle = 0.0
    while True:
        if await request.is_disconnected():
            return
        events = await run_in_threadpool(
            gw.db.events_after, job_id, last, 500)
        for event in events:
            yield _sse_format(event)
            last = event["seq"]
        if events:
            idle = 0.0
            if len(events) == 500:
                continue
        job = await run_in_threadpool(gw.db.get_job, job_id)
        if job and job["status"] in TERMINAL:
            while True:
                tail = await run_in_threadpool(
                    gw.db.events_after, job_id, last, 500)
                if not tail:
                    break
                for event in tail:
                    yield _sse_format(event)
                    last = event["seq"]
            return
        await asyncio.sleep(_SSE_POLL)
        idle += _SSE_POLL
        if idle >= _SSE_HEARTBEAT:
            idle = 0.0
            yield b": ping\n\n"


def _sse_format(event: dict) -> bytes:
    payload = json.dumps({
        "seq": event["seq"], "ts": event["ts"],
        "type": event["type"], "data": event["data"]})
    return (f"id: {event['seq']}\nevent: {event['type']}\n"
            f"data: {payload}\n\n").encode()


def _parse_after(after: int, last_event_id: str | None) -> int:
    if last_event_id is None:
        return after
    try:
        value = int(last_event_id)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_event_cursor",
                       "Last-Event-ID must be an integer") from exc
    if value < 0:
        raise ApiError(400, "invalid_event_cursor",
                       "Last-Event-ID must be non-negative")
    return value
