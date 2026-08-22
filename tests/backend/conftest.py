from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gateway.bus import Bus
from gateway.config import AgentConfig, Config
from gateway.db import Database
from gateway.notes import NotesStore
from gateway.server import create_app


class FakePool:
    def __init__(self):
        self.submitted = []
        self.started = False
        self.stopped = False
        self.cancelled = []
        self.claims = {}
        self.steers = {}

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def submit(self, job_id):
        self.submitted.append(job_id)

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return "queued"

    def claimant(self, session_id):
        return self.claims.get(session_id)

    def steering(self, job_id):
        return self.steers.get(job_id)


@pytest.fixture
def gateway(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    files = tmp_path / "files"
    files.mkdir()
    messages = tmp_path / "messages"
    messages.mkdir()
    agent = AgentConfig(
        name="claude", bin="claude", dispatch_mode="direct",
        permission_mode="bypassPermissions", model="claude-test",
        default_cwd=str(allowed), allowed_dirs=(str(allowed),),
        timeout_sec=0, max_sessions_in_index=5,
        models=("claude-test",))
    cfg = Config(
        host="127.0.0.1", port=8787, token="test-token", concurrency=1,
        db_path=str(tmp_path / "gateway.db"), data_dir=str(tmp_path),
        messages_dir=str(messages), files_dir=str(files),
        files_enabled=True, cluster_enabled=False, agents={"claude": agent},
        notes_path=str(tmp_path / "gateway.md"))
    gw = SimpleNamespace(
        cfg=cfg, db=Database(cfg.db_path), bus=Bus(),
        pool=FakePool(), cluster=None,
        notes=NotesStore(cfg.notes_path, cfg.notes_max_bytes))
    yield gw
    try:
        gw.db.close()
    except Exception:
        pass


@pytest.fixture
def client(gateway):
    with TestClient(create_app(gateway)) as test_client:
        yield test_client


@pytest.fixture
def auth():
    return {"Authorization": "Bearer test-token"}
