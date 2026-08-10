from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from gateway.adapters.base import Cancellation, RunResult
from gateway.bus import Bus
from gateway.config import AgentConfig, Config
from gateway.db import Database, ReportConflict
from gateway.worker import WorkerPool


def make_job(db: Database, title="event-job") -> str:
    return db.create_job(
        agent="claude", prompt=title, cwd="/tmp", requested_session=None,
        permission_mode=None, model=None, title=title)


def test_migration_allocates_above_every_historical_sequence(tmp_path):
    path = tmp_path / "events.db"
    db = Database(str(path))
    job = make_job(db)
    db.close()
    conn = sqlite3.connect(path)
    for seq in (1, 1_000_000, 2_000_000, 1_000_000_000):
        conn.execute(
            "INSERT INTO events(job_id,seq,ts,type,data) VALUES (?,?,?,?,?)",
            (job, seq, 1.0, "log", "{}"))
    conn.execute("DELETE FROM event_counters WHERE job_id=?", (job,))
    conn.commit()
    conn.close()

    db = Database(str(path))
    row = db.append_event(job, "message", {"after": "bands"})
    assert row["seq"] == 1_000_000_001
    db.close()


def test_allocator_is_unique_and_monotonic_under_threads(tmp_path):
    db = Database(str(tmp_path / "events.db"))
    job = make_job(db)
    rows = []
    lock = threading.Lock()

    def append(index):
        row = db.append_event(job, "log", {"index": index})
        with lock:
            rows.append(row)

    threads = [threading.Thread(target=append, args=(index,))
               for index in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seqs = sorted(row["seq"] for row in rows)
    assert seqs == list(range(1, 31))
    db.close()


def test_report_deduplication_and_conflict(tmp_path):
    db = Database(str(tmp_path / "events.db"))
    job = make_job(db)
    first = db.add_message(job, {"status": "running", "report_id": "r1"})
    duplicate = db.add_message(job, {"status": "running", "report_id": "r1"})
    assert duplicate["seq"] == first["seq"]
    assert duplicate["duplicate"] is True
    with pytest.raises(ReportConflict):
        db.add_message(job, {"status": "failed", "report_id": "r1"})
    assert len(db.events_after(job, 0)) == 1
    db.close()


def test_report_id_deduplicates_across_http_and_file_fallback(tmp_path):
    db = Database(str(tmp_path / "events.db"))
    job = make_job(db)
    payload = {"status": "running", "report_id": "cross-transport"}
    first = db.add_message(job, payload)
    messages = tmp_path / "messages"
    messages.mkdir()
    (messages / f"{job}.jsonl").write_text(json.dumps(payload) + "\n")
    assert db.ingest_messages(job, str(messages)) == 0
    assert len(db.events_after(job, 0)) == 1
    assert db.events_after(job, 0)[0]["seq"] == first["seq"]
    db.close()


def test_filesystem_reports_are_streamed_bounded_and_nonobjects_are_safe(
        client, auth, gateway):
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "fallback reports"}).json()
    job = accepted["id"]
    messages = Path(gateway.cfg.messages_dir)
    messages.mkdir(exist_ok=True)
    lines = ["[]", "null", '"text"', "{not-json"]
    lines.extend(json.dumps({"status": "running", "index": index})
                 for index in range(300))
    lines.append(json.dumps({"status": "running", "msg": "x" * 70000}))
    (messages / f"{job}.jsonl").write_text("\n".join(lines) + "\n")

    assert gateway.db.ingest_messages(job, str(messages)) == len(lines)
    events = gateway.db.events_after(job, 0, limit=1000)
    assert len(events) == len(lines)
    assert all(isinstance(event["data"], dict) for event in events)
    assert events[0]["data"]["error"] == "report line must be a JSON object"
    assert events[-1]["data"]["error"].startswith("report line exceeded")
    assert len(events[-1]["data"]["raw"]) <= 2000
    assert gateway.db.ingest_messages(job, str(messages)) == 0
    assert client.get(f"/v1/jobs/{job}", headers=auth).status_code == 200
    assert client.get(f"/v1/jobs/{job}/events?limit=500",
                      headers=auth).status_code == 200


def test_event_page_is_bounded_and_has_cursor(client, auth, gateway):
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "events"}).json()
    jid = accepted["id"]
    for index in range(3):
        gateway.db.append_event(jid, "log", {"index": index})
    page = client.get(
        f"/v1/jobs/{jid}/events?limit=2&legacy=false", headers=auth).json()
    assert [row["seq"] for row in page["events"]] == [1, 2]
    assert page["next_after"] == 2
    assert page["has_more"] is True
    assert page["job"] is None
    second = client.get(
        f"/v1/jobs/{jid}/events?after=2&limit=2&legacy=false",
        headers=auth).json()
    assert [row["seq"] for row in second["events"]] == [3]
    assert second["has_more"] is False


def test_queued_start_and_cancel_compare_and_set_race(tmp_path):
    db = Database(str(tmp_path / "race.db"))
    job = make_job(db)
    barrier = threading.Barrier(3)
    outcomes = {}

    def start():
        barrier.wait()
        outcomes["start"] = db.start_queued_job(
            job, {"stage": "running", "agent": "claude"})

    def cancel():
        barrier.wait()
        outcomes["cancel"] = db.cancel_queued_job(job)

    threads = [threading.Thread(target=start), threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sum(value is not None for value in outcomes.values()) == 1
    state = db.get_job(job)["status"]
    assert state in {"running", "canceled"}
    assert db.events_after(job, 0)[-1]["data"]["stage"] in {"running", "done"}
    db.close()


def test_terminal_fields_and_final_events_commit_atomically(tmp_path):
    path = tmp_path / "terminal.db"
    writer = Database(str(path))
    observer = Database(str(path))
    job = make_job(writer)
    assert writer.start_queued_job(job, {"stage": "running"}) is not None
    entered = threading.Event()
    release = threading.Event()
    original = writer._append_event_locked

    def paused_append(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    writer._append_event_locked = paused_append
    thread = threading.Thread(target=lambda: writer.finish_job_with_events(
        job, {"status": "succeeded", "result": "ok"},
        [("status", {"stage": "done", "status": "succeeded"})]))
    thread.start()
    assert entered.wait(2)
    assert observer.get_job(job)["status"] == "running"
    assert observer.events_after(job, 0)[-1]["data"]["stage"] == "running"
    release.set()
    thread.join()
    assert observer.get_job(job)["status"] == "succeeded"
    assert observer.events_after(job, 0)[-1]["data"]["stage"] == "done"
    writer.close()
    observer.close()


def test_cancel_during_terminal_commit_cannot_be_falsely_accepted(
        tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    agent = AgentConfig(
        name="claude", bin="claude", dispatch_mode="direct",
        permission_mode="bypassPermissions", model="",
        default_cwd=str(allowed), allowed_dirs=(str(allowed),),
        timeout_sec=0, max_sessions_in_index=1, models=())
    cfg = Config(
        host="127.0.0.1", port=0, token="x", concurrency=1,
        db_path=str(tmp_path / "race.db"), data_dir=str(tmp_path),
        messages_dir=str(tmp_path / "messages"),
        files_dir=str(tmp_path / "files"), cluster_enabled=False,
        agents={"claude": agent})
    db = Database(cfg.db_path)
    job = db.create_job(
        agent="claude", prompt="race", cwd=str(allowed),
        requested_session=None, permission_mode=None, model=None)
    pool = WorkerPool(cfg, db, Bus())

    class Adapter:
        def run(self, _spec, _emit):
            return RunResult(ok=True, result="done")

    monkeypatch.setattr("gateway.worker.build_adapter", lambda _cfg: Adapter())
    entered = threading.Event()
    release = threading.Event()
    original_finish = db.finish_job_with_events

    def paused_finish(job_id, fields, events):
        entered.set()
        assert release.wait(2)
        return original_finish(job_id, fields, events)

    monkeypatch.setattr(db, "finish_job_with_events", paused_finish)
    worker = threading.Thread(target=pool._run_job, args=(job,))
    worker.start()
    assert entered.wait(2)
    outcome = {}
    canceler = threading.Thread(
        target=lambda: outcome.setdefault("status", pool.cancel(job)))
    canceler.start()
    assert canceler.is_alive()  # serialized behind terminal persistence
    release.set()
    worker.join(2)
    canceler.join(2)

    assert db.get_job(job)["status"] == "succeeded"
    assert outcome["status"] == "succeeded"
    assert job not in pool._cancel_requested
    assert job not in pool._cancels
    db.close()


def test_cancel_marks_token_before_slow_interrupt_work():
    """Completion cannot commit success while cancel() is signaling processes."""
    pool = object.__new__(WorkerPool)
    pool._lock = threading.Lock()
    pool._cancels = {}
    pool._cancel_requested = set()
    token = Cancellation()
    pool._cancels["job"] = token
    entered = threading.Event()
    release = threading.Event()

    def slow_interrupt():
        entered.set()
        assert release.wait(2)

    token.cancel = slow_interrupt
    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault("status", pool.cancel("job")))
    thread.start()
    assert entered.wait(2)
    with pool._lock:
        assert token.cancelled() is True
    release.set()
    thread.join(2)
    assert outcome["status"] == "running"


def test_sse_replays_post_terminal_annotations_on_reconnect(client, auth, gateway):
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "stream"}).json()
    jid = accepted["id"]
    first = gateway.db.append_event(jid, "assistant", {"text": "done"})
    gateway.db.finish_job(jid, status="succeeded", result="done")
    gateway.db.append_event(jid, "status", {"stage": "done", "status": "succeeded"})
    report = gateway.db.add_message(
        jid, {"status": "finished", "report_id": "late"})
    response = client.get(
        f"/v1/jobs/{jid}/events?after={first['seq']}",
        headers={**auth, "Accept": "text/event-stream"})
    assert response.status_code == 200
    assert f"id: {report['seq']}" in response.text
    assert "event: message" in response.text
