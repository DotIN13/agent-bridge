"""HTTP surface: bearer-authed JSON API + SSE.

Routes:
  GET  /healthz                      -> {"ok": true}                (no auth)
  GET  /v1/agents                    -> configured/known agents
  GET  /v1/sessions?cwd=&agent=      -> session index the dispatcher sees
  POST /v1/jobs                      -> enqueue {prompt, agent?, cwd?, session?,
                                        model?, permission_mode?} -> 202 {id}
  GET  /v1/jobs                      -> recent jobs
  GET  /v1/jobs/{id}                 -> job row
  GET  /v1/jobs/{id}/events?after=N  -> SSE stream (Accept: text/event-stream)
                                        or one-shot JSON of events after N.

SSE resumability: pass ?after=<seq> or the Last-Event-ID header to replay only
newer events, then join the live stream.
"""
from __future__ import annotations

import json
import hmac
import queue as _queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import __version__
from .adapters import build as build_adapter, known_agents
from .bus import Bus, is_end
from .docs import render_llms_txt
from .config import Config
from .db import Database, TERMINAL
from .worker import WorkerPool

_HEARTBEAT_SEC = 15.0


class Gateway:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.bus = Bus()
        self.pool = WorkerPool(cfg, self.db, self.bus)

    def serve_forever(self) -> None:
        self.pool.start()
        handler = _make_handler(self)
        httpd = ThreadingHTTPServer((self.cfg.host, self.cfg.port), handler)
        httpd.daemon_threads = True
        print(f"agent-bridge {__version__} listening on "
              f"http://{self.cfg.host}:{self.cfg.port}  (db: {self.cfg.db_path})",
              flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.pool.stop()
            self.db.close()


def _make_handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"agent-bridge/{__version__}"
        protocol_version = "HTTP/1.1"

        # -- helpers ------------------------------------------------------
        def _authed(self) -> bool:
            got = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not got.startswith(prefix):
                return False
            return hmac.compare_digest(got[len(prefix):], gw.cfg.token)

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, code: int, text: str,
                  ctype: str = "text/markdown; charset=utf-8") -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if not n:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return {}

        def log_message(self, fmt, *args):  # quieter default logging
            return

        # -- dispatch -----------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            if path == "/healthz":
                return self._json(200, {"ok": True, "version": __version__})
            if path in ("/llms.txt", "/v1/help"):
                return self._text(200, render_llms_txt(gw.cfg))
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})

            if path == "/v1/agents":
                return self._json(200, {
                    "configured": sorted(gw.cfg.agents),
                    "known": known_agents(),
                    "default": gw.cfg.default_agent,
                })
            if path == "/v1/sessions":
                return self._sessions(qs)
            if path == "/v1/jobs":
                return self._json(200, {"jobs": gw.db.list_jobs()})
            if path.startswith("/v1/jobs/"):
                rest = path[len("/v1/jobs/"):]
                if rest.endswith("/events"):
                    return self._events(rest[:-len("/events")], qs)
                return self._get_job(rest)
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            if urlparse(self.path).path == "/v1/jobs":
                return self._create_job()
            return self._json(404, {"error": "not found"})

        # -- handlers -----------------------------------------------------
        def _sessions(self, qs):
            agent = (qs.get("agent") or [gw.cfg.default_agent])[0]
            cfg = gw.cfg.agents.get(agent)
            if not cfg:
                return self._json(400, {"error": f"unknown agent '{agent}'"})
            cwd = (qs.get("cwd") or [None])[0]
            infos = build_adapter(cfg).list_sessions(cwd_filter=cwd)
            return self._json(200, {"sessions": [s.to_public() for s in infos]})

        def _create_job(self):
            body = self._read_body()
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                return self._json(400, {"error": "prompt is required"})
            agent = body.get("agent") or gw.cfg.default_agent
            cfg = gw.cfg.agents.get(agent)
            if not cfg:
                return self._json(400, {"error": f"unknown agent '{agent}'"})
            try:
                cwd = cfg.resolve_cwd(body.get("cwd"))
            except ValueError as e:
                return self._json(400, {"error": str(e)})

            perm = body.get("permission_mode")
            job_id = gw.db.create_job(
                agent=agent, prompt=prompt, cwd=cwd,
                requested_session=body.get("session"),
                permission_mode=perm, model=body.get("model"),
            )
            gw.pool.submit(job_id)
            return self._json(202, {"id": job_id, "status": "queued",
                                    "agent": agent, "cwd": cwd})

        def _get_job(self, job_id):
            job = gw.db.get_job(job_id)
            if not job:
                return self._json(404, {"error": "job not found"})
            return self._json(200, job)

        def _events(self, job_id, qs):
            job = gw.db.get_job(job_id)
            if not job:
                return self._json(404, {"error": "job not found"})
            after = _parse_after(qs, self.headers.get("Last-Event-ID"))

            accept = self.headers.get("Accept", "")
            if "text/event-stream" not in accept:
                # one-shot poll
                evs = gw.db.events_after(job_id, after)
                return self._json(200, {"job": job, "events": evs,
                                        "terminal": job["status"] in TERMINAL})
            return self._sse(job_id, after)

        def _sse(self, job_id, after):
            # Subscribe BEFORE reading backlog to avoid a gap between the two.
            sub = gw.bus.subscribe(job_id)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last = after
            try:
                # 1) replay persisted backlog
                for ev in gw.db.events_after(job_id, after):
                    self._sse_write(ev)
                    last = ev["seq"]
                # if job already finished and backlog drained, close cleanly
                fresh = gw.db.get_job(job_id)
                if fresh and fresh["status"] in TERMINAL:
                    if not gw.db.events_after(job_id, last):
                        return
                # 2) live stream
                while True:
                    try:
                        item = sub.get(timeout=_HEARTBEAT_SEC)
                    except _queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if is_end(item):
                        break
                    if item["seq"] <= last:
                        continue
                    self._sse_write(item)
                    last = item["seq"]
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                gw.bus.unsubscribe(job_id, sub)

        def _sse_write(self, ev):
            payload = json.dumps({"seq": ev["seq"], "ts": ev["ts"],
                                  "type": ev["type"], "data": ev["data"]})
            chunk = (f"id: {ev['seq']}\n"
                     f"event: {ev['type']}\n"
                     f"data: {payload}\n\n").encode()
            self.wfile.write(chunk)
            self.wfile.flush()

    return Handler


def _parse_after(qs, last_event_id) -> int:
    for src in ((qs.get("after") or [None])[0], last_event_id):
        if src is not None:
            try:
                return int(src)
            except (TypeError, ValueError):
                pass
    return 0
