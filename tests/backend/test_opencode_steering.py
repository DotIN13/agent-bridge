"""Steering an opencode job, which is not steering a claude job.

claude reads JSON lines from stdin for the life of the turn, so a steer is a
write into a live pipe. `opencode run` reads its whole prompt from stdin and
closes it *before* the turn starts, and the server it talks to is in-process
behind a fetch shim with no listener — so there is nothing to write to and
nothing to connect to.

What opencode has instead is a steering verb on its HTTP API:
`POST /api/session/<id>/prompt` with `delivery: "steer" | "queue"`. Reaching it
means the run must be attached to a server with a port, so a steerable job gets
a private `opencode serve` on loopback and runs with `--attach` (docs/design/18).

The stub below stands in for both halves of the `opencode` binary: `serve`
announces a port and answers the v2 routes, `run` streams a session id and then
blocks until the test releases it — which is what makes "mid-turn" testable.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from gateway.adapters.base import Event, JobSpec, SteerError, Steering
from gateway.adapters.opencode import OpenCodeAdapter
from gateway.config import AgentConfig

STUB = r'''#!/usr/bin/env python3
"""Enough of `opencode` to test the steering channel, and nothing more."""
import base64, json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ["FAKE_OC_LOG"]


def log(record):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


class Handler(BaseHTTPRequestHandler):
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return None
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return None

    def _reply(self, code, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def log_message(self, *a):
        pass

    def do_GET(self):
        log({"method": "GET", "path": self.path,
             "auth": self.headers.get("Authorization", "")})
        if self.path == "/api/health" and not os.environ.get("FAKE_OC_NO_V2"):
            return self._reply(200, {"status": "ok"})
        return self._reply(404, {"_tag": "NotFound", "message": "no such route"})

    def do_POST(self):
        body = self._body()
        log({"method": "POST", "path": self.path, "body": body,
             "auth": self.headers.get("Authorization", ""),
             "directory": self.headers.get("x-opencode-directory", "")})
        parts = self.path.strip("/").split("/")
        if os.environ.get("FAKE_OC_REFUSE") and parts[-1] == "prompt":
            return self._reply(409, {"_tag": "ConflictError",
                                     "message": "session is compacting"})
        if parts[-1] == "prompt":
            return self._reply(200, {"data": {
                "admittedSeq": 7, "promotedSeq": 8, "id": "msg_stub",
                "sessionID": parts[-2], "prompt": (body or {}).get("prompt"),
                "delivery": (body or {}).get("delivery") or "queue",
                "timeCreated": 0}})
        if parts[-1] == "interrupt":
            return self._reply(204)
        return self._reply(404, {"_tag": "NotFound", "message": self.path})


def serve():
    if os.environ.get("FAKE_OC_SILENT"):
        time.sleep(30)
        return
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(f"opencode server listening on http://127.0.0.1:{httpd.server_port}",
          flush=True)
    httpd.serve_forever()


def run():
    log({"argv": sys.argv[1:]})
    sys.stdin.read()
    print(json.dumps({"type": "step_start", "sessionID": "ses_stub"}), flush=True)
    release = os.environ.get("FAKE_OC_RELEASE")
    deadline = time.time() + 20
    while release and not os.path.exists(release) and time.time() < deadline:
        time.sleep(0.02)
    print(json.dumps({"type": "text", "part": {"text": "done"}}), flush=True)
    print(json.dumps({"type": "step_finish", "part": {"cost": 0.5}}), flush=True)


if sys.argv[1:2] == ["serve"]:
    serve()
else:
    run()
'''


@pytest.fixture
def opencode(tmp_path, monkeypatch):
    """The stub binary, a log to read it back from, and a release latch."""
    binary = tmp_path / "fake-opencode"
    binary.write_text(STUB, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.jsonl"
    release = tmp_path / "release"
    monkeypatch.setenv("FAKE_OC_LOG", str(log))
    monkeypatch.setenv("FAKE_OC_RELEASE", str(release))
    return SimpleStub(binary=binary, log=log, release=release, cwd=tmp_path)


class SimpleStub:
    def __init__(self, binary, log, release, cwd):
        self.binary, self.log, self.release, self.cwd = binary, log, release, cwd

    def calls(self) -> list[dict]:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in
                self.log.read_text(encoding="utf-8").splitlines() if line]

    def posts(self, suffix: str) -> list[dict]:
        return [c for c in self.calls()
                if c.get("method") == "POST" and c["path"].endswith(suffix)]

    def argv(self) -> list[str]:
        for call in self.calls():
            if "argv" in call:
                return call["argv"]
        return []


def _cfg(stub, **over) -> AgentConfig:
    fields = dict(
        name="opencode", bin=str(stub.binary), dispatch_mode="direct",
        permission_mode="auto", model="", default_cwd=str(stub.cwd),
        allowed_dirs=(str(stub.cwd),), timeout_sec=0, max_sessions_in_index=5,
        models=("anthropic/claude",))
    fields.update(over)
    return AgentConfig(**fields)


def _spec(stub, **over) -> JobSpec:
    steer = Steering()
    fields = dict(job_id="j1", prompt="do the thing", cwd=str(stub.cwd),
                  requested_session=None, permission_mode=None, model=None,
                  steer=steer)
    fields.update(over)
    return JobSpec(**fields)


def _run(adapter, spec):
    """Run the adapter on a thread, so the test can steer while it is mid-turn."""
    events: list[Event] = []
    out: dict = {}

    def go():
        out["result"] = adapter.run(spec, events.append)

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, events, out


def _wait(predicate, timeout=20.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_steerable_job_attaches_to_a_server_of_its_own(opencode):
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode)
    thread, events, out = _run(adapter, spec)
    _wait(lambda: opencode.argv(), what="`opencode run` to start")

    argv = opencode.argv()
    assert "--attach" in argv, argv
    base = argv[argv.index("--attach") + 1]
    assert base.startswith("http://127.0.0.1:"), base
    # The credential never goes on the command line: argv is world-readable
    # through /proc on a shared host. It rides in the environment instead,
    # which `opencode run --attach` reads when `--password` is absent.
    assert "--password" not in argv and "--username" not in argv
    opencode.release.touch()
    thread.join(20)
    assert out["result"].ok, out["result"].error
    logged = [e.data for e in events if e.type == "log"]
    assert any(d.get("opencode_server") == base for d in logged), logged
    assert not any("password" in json.dumps(e.data) for e in events)


def test_a_steer_lands_as_a_steer_delivery_with_a_receipt(opencode):
    """The point of the whole exercise: mid-turn, into the running session."""
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode)
    thread, events, out = _run(adapter, spec)
    _wait(lambda: spec.steer.available and any(
        e.type == "status" and e.data.get("stage") == "step" for e in events),
        what="the session id to arrive")

    spec.steer.send("stop and summarise")

    posts = opencode.posts("/prompt")
    assert len(posts) == 1, posts
    assert posts[0]["path"] == "/api/session/ses_stub/prompt"
    assert posts[0]["body"] == {"prompt": {"text": "stop and summarise"},
                                "delivery": "steer"}
    assert posts[0]["auth"].startswith("Basic ")
    assert posts[0]["directory"], "the server needs the job's directory"

    steers = [e.data for e in events if e.type == "steer"]
    assert steers == [{"text": "stop and summarise", "source": "opencode",
                       "delivery": "steer", "message_id": "msg_stub",
                       "admitted_seq": 7, "promoted_seq": 8}]
    opencode.release.touch()
    thread.join(20)


def test_a_steer_before_the_session_id_is_known_says_to_retry(opencode):
    """A fresh session only names itself in the records it streams back, so the
    first second of a job has a channel but no address."""
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode)
    server, why = adapter._server_for(spec, str(opencode.cwd), lambda e: None)
    assert server is not None, why
    try:
        res = type("R", (), {"session": None})()
        adapter._bind_steering(spec.steer, server, str(opencode.cwd), res,
                               lambda e: None)
        with pytest.raises(SteerError) as exc:
            spec.steer.send("too soon")
        assert "session id yet" in str(exc.value)
    finally:
        server.stop()


def test_a_refused_steer_carries_opencode_s_own_words(opencode, monkeypatch):
    monkeypatch.setenv("FAKE_OC_REFUSE", "1")
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode)
    thread, events, out = _run(adapter, spec)
    _wait(lambda: spec.steer.available and any(
        e.type == "status" and e.data.get("stage") == "step" for e in events),
        what="the session id to arrive")

    with pytest.raises(SteerError) as exc:
        spec.steer.send("stop")
    assert "ConflictError" in str(exc.value)
    assert "compacting" in str(exc.value), "the server's message, not ours"
    opencode.release.touch()
    thread.join(20)


def test_an_interrupt_goes_to_the_interrupt_route(opencode):
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode)
    thread, events, out = _run(adapter, spec)
    _wait(lambda: spec.steer.available and any(
        e.type == "status" and e.data.get("stage") == "step" for e in events),
        what="the session id to arrive")

    spec.steer.interrupt()

    assert [c["path"] for c in opencode.posts("/interrupt")] == \
        ["/api/session/ses_stub/interrupt"]
    opencode.release.touch()
    thread.join(20)


def test_a_directory_attachment_runs_unattached_and_says_why(opencode):
    """`--attach` refuses a local directory outright, and a job losing its work
    to gain steering is the wrong trade."""
    folder = opencode.cwd / "corpus"
    folder.mkdir()
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode, files=(str(folder),))
    thread, events, out = _run(adapter, spec)
    _wait(lambda: opencode.argv(), what="`opencode run` to start")

    argv = opencode.argv()
    assert "--attach" not in argv, argv
    assert not spec.steer.available
    assert "is a directory" in spec.steer.unavailable_reason
    assert any(e.data.get("steering") == "unavailable" for e in events
               if e.type == "log")
    opencode.release.touch()
    thread.join(20)


def test_an_oversized_attachment_runs_unattached_too(opencode, monkeypatch):
    from gateway.adapters import opencode as mod
    monkeypatch.setattr(mod, "ATTACH_MAX_BYTES", 16)
    big = opencode.cwd / "big.bin"
    big.write_bytes(b"x" * 64)
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode, files=(str(big),))
    server, why = adapter._server_for(spec, str(opencode.cwd), lambda e: None)
    assert server is None
    assert "larger than" in why


def test_steering_off_is_advertised_and_explained(opencode):
    adapter = OpenCodeAdapter(_cfg(opencode, steering=False))
    assert adapter.capabilities()["steering"] is False
    spec = _spec(opencode)
    server, why = adapter._server_for(spec, str(opencode.cwd), lambda e: None)
    assert server is None
    assert "steering = true" in why, why


def test_the_dispatcher_modes_do_not_claim_steering(opencode):
    adapter = OpenCodeAdapter(_cfg(opencode, dispatch_mode="agent_exec"))
    assert adapter.capabilities()["steering"] is False


def test_a_server_without_the_v2_api_is_not_attached(opencode, monkeypatch):
    """An older opencode takes `--attach` and would then refuse every steer,
    which is worse than not attaching: attaching costs the file limits."""
    monkeypatch.setenv("FAKE_OC_NO_V2", "1")
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode)
    server, why = adapter._server_for(spec, str(opencode.cwd), lambda e: None)
    assert server is None
    assert "no opencode server could be started" in why


def test_a_server_that_never_announces_a_port_does_not_fail_the_job(
        opencode, monkeypatch):
    monkeypatch.setenv("FAKE_OC_SILENT", "1")
    from gateway.adapters import opencode as mod
    monkeypatch.setattr(mod, "SERVE_WAIT_SEC", 1.0)
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode)
    thread, events, out = _run(adapter, spec)
    opencode.release.touch()
    thread.join(30)
    assert out["result"].ok, out["result"].error
    assert "--attach" not in opencode.argv()
    assert not spec.steer.available


def test_the_job_s_own_server_is_stopped_when_the_run_ends(opencode):
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode)
    thread, events, out = _run(adapter, spec)
    _wait(lambda: opencode.argv(), what="`opencode run` to start")
    base = opencode.argv()[opencode.argv().index("--attach") + 1]
    opencode.release.touch()
    thread.join(20)

    import urllib.error
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises((OSError, urllib.error.URLError)):
        opener.open(base + "/api/health", timeout=3)


def test_a_configured_server_is_used_instead_of_a_private_one(
        opencode, monkeypatch):
    """One server the operator runs, rather than one process per job."""
    import subprocess
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "hunter2")
    proc = subprocess.Popen([str(opencode.binary), "serve"],
                            stdout=subprocess.PIPE, text=True,
                            env=dict(os.environ))
    try:
        line = proc.stdout.readline()
        base = line.strip().split()[-1]
        adapter = OpenCodeAdapter(_cfg(opencode, server_url=base))
        spec = _spec(opencode)
        server, why = adapter._server_for(spec, str(opencode.cwd),
                                          lambda e: None)
        assert server is not None, why
        assert server.base == base
        assert server.proc is None, "not ours, so not ours to stop"
        server.stop()
        assert proc.poll() is None, "stopping someone else's server is not ours to do"
    finally:
        proc.terminate()
        proc.wait(10)


def test_the_steer_request_never_goes_through_a_proxy(opencode, monkeypatch):
    """A loopback call swallowed by an ambient HTTPS_PROXY is the failure mode
    this environment hands you for free."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    adapter = OpenCodeAdapter(_cfg(opencode))
    spec = _spec(opencode)
    server, why = adapter._server_for(spec, str(opencode.cwd), lambda e: None)
    assert server is not None, why
    server.stop()


def test_the_202_says_what_this_backend_does_with_the_message(client, auth,
                                                              gateway):
    """The route used to promise "at its next tool boundary" to everyone, which
    describes claude's pipe and not opencode's admission."""
    created = client.post("/v1/jobs", json={"prompt": "go"}, headers=auth)
    job = created.json()["id"]
    gateway.db.mark_running(job)

    pipe = Steering()
    pipe.bind(_Sink())
    gateway.pool.steers[job] = pipe
    note = client.post(f"/v1/jobs/{job}/steer", json={"prompt": "left a bit"},
                       headers=auth).json()["note"]
    assert note == "the agent picks this up at its next tool boundary"

    remote = Steering()
    remote.bind_remote(send=lambda text: None, note="opencode admits this")
    gateway.pool.steers[job] = remote
    note = client.post(f"/v1/jobs/{job}/steer", json={"prompt": "left a bit"},
                       headers=auth).json()["note"]
    assert note == "opencode admits this"


def test_a_job_that_never_had_a_channel_names_both_backends(client, auth,
                                                            gateway):
    created = client.post("/v1/jobs", json={"prompt": "go"}, headers=auth)
    job = created.json()["id"]
    gateway.db.mark_running(job)
    gateway.pool.steers[job] = Steering()

    refused = client.post(f"/v1/jobs/{job}/steer", json={"prompt": "hello"},
                          headers=auth)
    assert refused.status_code == 409
    message = refused.json()["error"]["message"]
    assert "claude" in message and "opencode" in message, message


class _Sink:
    """A stdin that records what was written, for the pipe half."""

    def __init__(self):
        self.written: list[str] = []
        self.closed = False

    def write(self, text):
        self.written.append(text)

    def flush(self):
        return None

    def close(self):
        self.closed = True


def test_the_pipe_half_still_writes_claude_s_stream_json():
    """`Steering` grew a second way to be bound; the first must be untouched —
    every claude job's prompt goes through this path, not just its steers."""
    sink = _Sink()
    steer = Steering()
    steer.bind(sink)
    steer.send("left a bit")
    steer.interrupt()

    prompt, interrupt = (json.loads(line) for line in sink.written)
    assert prompt == {"type": "user", "parent_tool_use_id": None,
                      "message": {"role": "user", "content": [
                          {"type": "text", "text": "left a bit"}]}}
    assert interrupt["type"] == "control_request"
    assert interrupt["request"] == {"subtype": "interrupt"}
    assert steer.note == "the agent picks this up at its next tool boundary"

    steer.close()
    assert sink.closed, "closing stdin is what ends a streaming-input run"
    assert not steer.available
    with pytest.raises(SteerError) as exc:
        steer.send("too late")
    assert "already ended" in str(exc.value)


def test_a_broken_pipe_is_read_as_a_stale_row():
    class Broken(_Sink):
        def write(self, text):
            raise OSError("EPIPE")

    steer = Steering()
    steer.bind(Broken())
    with pytest.raises(SteerError) as exc:
        steer.send("anyone there")
    assert "no longer accepting input" in str(exc.value)
    assert not steer.available, "a failed write is the liveness probe"


def test_the_server_password_travels_in_the_environment_not_on_argv(opencode):
    """argv is world-readable through /proc; a shared HPC login node is exactly
    where that matters."""
    from gateway.adapters.opencode import _Server, _run_env

    spec = _spec(opencode, job_dir=str(opencode.cwd / "reports" / "j1"))
    server = _Server("http://127.0.0.1:1", "s3cret")
    env = _run_env(spec, server)
    assert env["OPENCODE_SERVER_PASSWORD"] == "s3cret"
    assert env["OPENCODE_SERVER_USERNAME"] == "opencode"
    assert env["AB_JOB_DIR"].endswith("j1"), "and the job dir still gets through"
    # No server, no credential -- and no environment invented for a job that
    # had none.
    assert _run_env(_spec(opencode), None) is None
