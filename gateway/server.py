"""HTTP surface (FastAPI + uvicorn): bearer-authed JSON API, multipart file
upload, streamed download, and SSE.

Routes:
  GET  /health                       -> {"ok": true}                    (no auth)
  GET  /llms.txt | /v1/help          -> agent usage doc                 (no auth)
  GET  /v1/agents                    -> configured/known agents
  GET  /v1/info[?refresh=1]          -> cluster capabilities (cached)
  GET  /v1/sessions?cwd=&agent=      -> session index the dispatcher sees
  POST /v1/jobs                      -> enqueue a job. JSON body, OR multipart
                                        (form field `payload`=JSON + file parts).
                                        `files[]` may be inline/path refs too.
  GET  /v1/jobs                      -> recent jobs
  GET  /v1/jobs/{id}                 -> job row
  POST /v1/jobs/{id}/cancel          -> cancel a queued/running job
  GET  /v1/jobs/{id}/events?after=N  -> SSE (Accept: text/event-stream) or JSON
  POST /v1/files                     -> upload files (JSON inline or multipart);
                                        returns remote paths to reference later
  GET  /v1/files/list?dir=&glob=&recursive=
  GET  /v1/files/content?path=       -> stream a file back (artifacts, CSVs)

Files, cwd, and downloads are sandboxed to allowed_dirs. SSE resumes via
?after=<seq> or Last-Event-ID.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import __version__, files as filemod
from .adapters import build as build_adapter, known_agents
from .bus import Bus
from .cluster import ClusterInfo
from .config import Config
from .db import Database, TERMINAL
from .docs import render_llms_txt
from .files import FileError
from .worker import WorkerPool

_SSE_POLL = 0.3          # seconds between DB polls while streaming
_SSE_HEARTBEAT = 15.0    # comment ping when idle


class Gateway:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.bus = Bus()
        self.pool = WorkerPool(cfg, self.db, self.bus)
        self.cluster = ClusterInfo(cfg.cluster_probe_timeout,
                                   cfg.cluster_env_presence) if cfg.cluster_enabled else None

    def serve_forever(self) -> None:
        uvicorn.run(create_app(self), host=self.cfg.host, port=self.cfg.port,
                    log_level="warning")


def create_app(gw: Gateway) -> FastAPI:
    cfg = gw.cfg

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        gw.pool.start()
        if gw.cluster:
            gw.cluster.start_async()
        print(f"agent-bridge {__version__} listening on "
              f"http://{cfg.host}:{cfg.port}  (db: {cfg.db_path})", flush=True)
        yield
        gw.pool.stop()
        gw.db.close()

    app = FastAPI(title="agent-bridge", version=__version__, lifespan=lifespan)

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        tok = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        if not tok or not hmac.compare_digest(tok, cfg.token):
            raise HTTPException(401, "unauthorized")

    auth = Depends(require_auth)

    # -- public -----------------------------------------------------------
    @app.get("/health")
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/llms.txt")
    @app.get("/v1/help")
    async def help_doc():
        return PlainTextResponse(render_llms_txt(cfg),
                                 media_type="text/markdown; charset=utf-8")

    # -- capabilities -----------------------------------------------------
    @app.get("/v1/agents", dependencies=[auth])
    async def agents():
        return {"configured": sorted(cfg.agents), "known": known_agents(),
                "default": cfg.default_agent}

    @app.get("/v1/info", dependencies=[auth])
    async def info(refresh: bool = False):
        if not gw.cluster:
            raise HTTPException(404, "cluster probing disabled")
        if refresh:
            gw.cluster.refresh_async()
        return gw.cluster.get()

    @app.get("/v1/sessions", dependencies=[auth])
    async def sessions(cwd: str | None = None, agent: str | None = None):
        acfg = cfg.agents.get(agent or cfg.default_agent)
        if not acfg:
            raise HTTPException(400, f"unknown agent '{agent}'")
        infos = await run_in_threadpool(build_adapter(acfg).list_sessions, cwd)
        return {"sessions": [s.to_public() for s in infos]}

    # -- jobs -------------------------------------------------------------
    @app.post("/v1/jobs", dependencies=[auth])
    async def create_job(request: Request):
        _reject_oversize(request)
        spec, uploads = await _parse_submission(request)
        prompt = (spec.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "prompt is required")
        agent = spec.get("agent") or cfg.default_agent
        acfg = cfg.agents.get(agent)
        if not acfg:
            raise HTTPException(400, f"unknown agent '{agent}'")
        try:
            cwd = acfg.resolve_cwd(spec.get("cwd"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        items = spec.get("files") or []
        if (items or uploads) and not cfg.files_enabled:
            raise HTTPException(400, "file attachments are disabled")

        job_id = gw.db.create_job(
            agent=agent, prompt=prompt, cwd=cwd,
            requested_session=spec.get("session"),
            permission_mode=spec.get("permission_mode"), model=spec.get("model"))
        paths = await _save_all(items, uploads, filemod.job_dir(cfg, job_id))
        if paths:
            gw.db.set_job_files(job_id, paths)
        gw.pool.submit(job_id)   # only now is it visible to a worker
        return JSONResponse(status_code=202, content={
            "id": job_id, "status": "queued", "agent": agent, "cwd": cwd,
            "files": paths})

    @app.get("/v1/jobs", dependencies=[auth])
    async def list_jobs():
        return {"jobs": gw.db.list_jobs()}

    @app.get("/v1/jobs/{job_id}", dependencies=[auth])
    async def get_job(job_id: str):
        job = gw.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return job

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[auth])
    async def cancel_job(job_id: str):
        job = gw.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        if job["status"] in TERMINAL:
            return JSONResponse(status_code=409, content={
                "id": job_id, "status": job["status"], "error": "job already finished"})
        was = await run_in_threadpool(gw.pool.cancel, job_id)
        return JSONResponse(status_code=202,
                            content={"id": job_id, "canceling": True, "was": was})

    @app.get("/v1/jobs/{job_id}/events", dependencies=[auth])
    async def job_events(job_id: str, request: Request, after: int = 0):
        job = gw.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        start = _parse_after(after, request.headers.get("last-event-id"))
        if "text/event-stream" not in request.headers.get("accept", ""):
            evs = gw.db.events_after(job_id, start)
            return {"job": job, "events": evs, "terminal": job["status"] in TERMINAL}
        return StreamingResponse(
            _sse_stream(gw, job_id, start, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -- files ------------------------------------------------------------
    @app.post("/v1/files", dependencies=[auth])
    async def upload(request: Request):
        if not cfg.files_enabled:
            raise HTTPException(400, "file uploads are disabled")
        _reject_oversize(request)
        spec, uploads = await _parse_submission(request)
        items = spec.get("files") or []
        upload_id = uuid.uuid4().hex
        dest = filemod.upload_dir(cfg, upload_id)
        paths = await _save_all(items, uploads, dest)
        return {"upload_id": upload_id, "dir": str(dest), "paths": paths}

    @app.get("/v1/files/list", dependencies=[auth])
    async def files_list(dir: str, glob: str = "*", recursive: bool = False):
        try:
            rows = await run_in_threadpool(filemod.list_files, cfg, dir, glob, recursive)
        except FileError as e:
            raise HTTPException(400, str(e))
        return {"dir": dir, "files": rows}

    @app.get("/v1/files/content", dependencies=[auth])
    async def files_content(path: str):
        try:
            p, size = filemod.open_for_download(cfg, path)
        except FileError as e:
            raise HTTPException(400, str(e))
        return StreamingResponse(
            filemod.iter_file(p), media_type="application/octet-stream",
            headers={"Content-Length": str(size),
                     "Content-Disposition": f'attachment; filename="{p.name}"'})

    # -- helpers ----------------------------------------------------------
    def _reject_oversize(request: Request) -> None:
        clen = int(request.headers.get("content-length") or 0)
        if clen and clen > (cfg.files_max_request_mb << 20):
            raise HTTPException(413,
                f"request exceeds max_request_mb ({cfg.files_max_request_mb} MiB); "
                f"for large data use scp into an allowed dir and pass its path")

    async def _save_all(items, uploads, dest) -> list[str]:
        def work():
            out = [filemod.save_inline_item(cfg, dest, it) for it in items]
            for up in uploads:
                out.append(filemod.save_stream(cfg, dest, up.filename, up.file).path)
            return out
        try:
            return await run_in_threadpool(work)
        except FileError as e:
            raise HTTPException(400, str(e))

    return app


async def _parse_submission(request: Request) -> tuple[dict, list]:
    """Return (payload_dict, [UploadFile...]) from JSON or multipart."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        raw = form.get("payload")
        spec = json.loads(raw) if raw else {}
        uploads = [v for _k, v in form.multi_items() if hasattr(v, "filename")]
        return spec, uploads
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    return body, []


async def _sse_stream(gw: Gateway, job_id: str, after: int, request: Request):
    last = after
    idle = 0.0
    while True:
        if await request.is_disconnected():
            return
        evs = await run_in_threadpool(gw.db.events_after, job_id, last)
        for ev in evs:
            yield _sse_format(ev)
            last = ev["seq"]
        if evs:
            idle = 0.0
        job = await run_in_threadpool(gw.db.get_job, job_id)
        if job and job["status"] in TERMINAL:
            tail = await run_in_threadpool(gw.db.events_after, job_id, last)
            for ev in tail:
                yield _sse_format(ev)
            return
        await asyncio.sleep(_SSE_POLL)
        idle += _SSE_POLL
        if idle >= _SSE_HEARTBEAT:
            idle = 0.0
            yield b": ping\n\n"


def _sse_format(ev: dict) -> bytes:
    payload = json.dumps({"seq": ev["seq"], "ts": ev["ts"],
                          "type": ev["type"], "data": ev["data"]})
    return (f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {payload}\n\n").encode()


def _parse_after(after, last_event_id) -> int:
    for src in (last_event_id, after):
        if src is not None:
            try:
                return int(src)
            except (TypeError, ValueError):
                pass
    return int(after or 0)
