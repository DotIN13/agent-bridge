from __future__ import annotations

from pathlib import Path

from gateway.bus import Bus
from gateway.config import AgentConfig, Config
from gateway.db import Database
from gateway.server import _content_disposition
from gateway.worker import WorkerPool


def test_attachment_failure_and_collision_leave_no_job(client, auth, gateway):
    duplicate = client.post("/v1/jobs", headers=auth, json={
        "prompt": "files",
        "files": [
            {"name": "same.txt", "text": "one"},
            {"name": "same.txt", "text": "two"},
        ]})
    assert duplicate.status_code == 400
    assert duplicate.json()["error"]["code"] == "file_error"
    assert gateway.db.list_jobs() == []

    invalid = client.post("/v1/jobs", headers=auth, json={
        "prompt": "files",
        "files": [{"name": "bad.bin", "content_b64": "not-base64"}]})
    assert invalid.status_code == 400
    assert gateway.db.list_jobs() == []
    jobs_dir = Path(gateway.cfg.files_dir) / "jobs"
    assert not jobs_dir.exists() or not [p for p in jobs_dir.iterdir()
                                         if p.name != ".staging"]


def test_remote_attachment_reference_must_be_regular_file(
        client, auth, gateway):
    allowed = Path(gateway.cfg.agents["claude"].default_cwd)
    for path in (allowed / "missing.txt", allowed):
        response = client.post("/v1/jobs", headers=auth, json={
            "prompt": "attach", "files": [{"path": str(path)}]})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "file_error"
    assert gateway.db.list_jobs() == []


def test_empty_upload_rejected_and_file_listing_paged(client, auth, gateway):
    empty = client.post("/v1/files", headers=auth, json={"files": []})
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "empty_upload"

    allowed = Path(gateway.cfg.agents["claude"].default_cwd)
    for name in ("a.txt", "b.txt", "c.txt"):
        (allowed / name).write_text(name)
    first = client.get(
        f"/v1/files/list?dir={allowed}&limit=2", headers=auth).json()
    assert len(first["files"]) == 2
    assert first["has_more"] is True
    assert str(allowed) not in first["next_cursor"]
    second = client.get(
        "/v1/files/list", headers=auth, params={
            "dir": str(allowed), "limit": 2,
            "cursor": first["next_cursor"]}).json()
    assert len(second["files"]) == 1
    assert second["has_more"] is False
    invalid_cursor = client.get("/v1/files/list", headers=auth, params={
        "dir": str(allowed), "cursor": "not-a-cursor"})
    assert invalid_cursor.status_code == 400


def test_upload_items_and_download_headers_are_strict(client, auth, gateway):
    unknown = client.post("/v1/files", headers=auth, json={
        "files": [{"name": "x.txt", "text": "x", "oops": True}]})
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "validation_error"

    hostile = client.post("/v1/files", headers=auth, json={
        "files": [{"name": "bad\r\nname.txt", "text": "x"}]})
    assert hostile.status_code == 400

    allowed = Path(gateway.cfg.agents["claude"].default_cwd)
    target = allowed / "café.txt"
    target.write_text("safe")
    response = client.get("/v1/files/content", headers=auth,
                          params={"path": str(target)})
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "filename*=UTF-8''" in disposition
    assert "%C3%A9" in disposition

    # Quotes are invalid in Windows filenames, so exercise header escaping
    # directly to keep this contract test portable.
    assert "%22" in _content_disposition('quote" café.txt')


def test_restart_reconciles_running_and_returns_queued(tmp_path):
    db = Database(str(tmp_path / "recovery.db"))
    queued = db.create_job(
        agent="claude", prompt="queued", cwd=str(tmp_path),
        session=None, permission_mode=None, model=None)
    running = db.create_job(
        agent="claude", prompt="running", cwd=str(tmp_path),
        session=None, permission_mode=None, model=None)
    db.mark_running(running)
    db.append_event(running, "status", {"stage": "running"})

    recovered = db.reconcile_startup()
    assert recovered == [queued]
    row = db.get_job(running)
    assert row["status"] == "failed"
    assert "restarted" in row["error"]
    events = db.events_after(running, 0)
    assert [event["seq"] for event in events] == sorted(
        event["seq"] for event in events)
    assert events[-1]["data"]["reason"] == "gateway_restarted"
    count = len(events)
    assert db.reconcile_startup() == [queued]
    assert len(db.events_after(running, 0)) == count
    db.close()


def test_worker_shutdown_joins_before_database_close(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    agent = AgentConfig(
        name="claude", bin="claude", dispatch_mode="direct",
        permission_mode="bypassPermissions", model="",
        default_cwd=str(allowed), allowed_dirs=(str(allowed),),
        timeout_sec=0, max_sessions_in_index=1, models=())
    cfg = Config(
        host="127.0.0.1", port=0, token="x", concurrency=2,
        db_path=str(tmp_path / "worker.db"), data_dir=str(tmp_path), files_dir=str(tmp_path / "files"),
        cluster_enabled=False, agents={"claude": agent})
    db = Database(cfg.db_path)
    pool = WorkerPool(cfg, db, Bus())
    pool.start()
    pool.stop(timeout=2)
    assert pool._threads == []
    # The connection is still usable; lifespan owns the later close.
    assert db.list_jobs() == []
    db.close()
