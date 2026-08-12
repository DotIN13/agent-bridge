from __future__ import annotations

from datetime import datetime

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


def test_a_report_carrying_a_timestamp_is_accepted(tmp_path):
    """`ab-notify` always sends an epoch `ts`, and with `--report-id`.

    That combination raised ValueError -> HTTP 500: one line rewrote `ts` to ISO
    before hashing, and the report branch then re-derived its own timestamp by
    calling float() on the string that line had just written. The recommended
    invocation was the broken one. Every test above omits `ts`, which is why
    they stayed green.
    """
    db = Database(str(tmp_path / "events.db"))
    job = make_job(db)
    row = db.add_message(job, {"status": "running", "msg": "12/24",
                               "report_id": "p12", "ts": 1786500000.0})
    assert row["duplicate"] is False
    event = db.events_after(job, 0)[0]
    # Timestamped from the payload, not from arrival, and published as ISO.
    assert event["ts"] == 1786500000.0
    assert event["data"]["ts"].startswith("2026-")
    db.close()


def test_a_timestamped_report_keeps_one_identity_across_transports(tmp_path):
    """The fallback path has to normalise too, or `ts` splits the dedup key."""
    db = Database(str(tmp_path / "events.db"))
    job = make_job(db)
    payload = {"status": "running", "report_id": "both-ways", "ts": 1786500000.0}
    first = db.add_message(job, payload)
    messages = tmp_path / "messages"
    messages.mkdir()
    (messages / f"{job}.jsonl").write_text(json.dumps(payload) + "\n")
    assert db.ingest_messages(job, str(messages)) == 0
    assert len(db.events_after(job, 0)) == 1
    assert db.events_after(job, 0)[0]["seq"] == first["seq"]
    db.close()


def test_normalising_a_report_twice_changes_nothing(tmp_path):
    """Idempotent, so an already-ISO `ts` survives and re-ingestion is safe."""
    db = Database(str(tmp_path / "events.db"))
    job = make_job(db)
    first = db.add_message(job, {"status": "running", "report_id": "iso",
                                 "ts": 1786500000.0})
    stored = db.events_after(job, 0)[0]["data"]["ts"]
    # Feed the stored (ISO) form back in: same identity, same row, same instant.
    again = db.add_message(job, {"status": "running", "report_id": "iso",
                                 "ts": stored})
    assert again["seq"] == first["seq"]
    assert again["duplicate"] is True
    assert db.events_after(job, 0)[0]["ts"] == 1786500000.0
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


def test_page_reports_the_shape_of_the_whole_log(client, auth, gateway):
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "shape"}).json()
    jid = accepted["id"]
    for index in range(7):
        gateway.db.append_event(jid, "log", {"index": index})
    page = client.get(f"/v1/jobs/{jid}/events?limit=2&legacy=false",
                      headers=auth).json()
    # Without these a caller cannot tell how far it is from the end, which is
    # what forced blind forward paging.
    assert (page["total"], page["first_seq"], page["last_seq"]) == (7, 1, 7)


def test_tail_reads_from_the_end_in_chronological_order(client, auth, gateway):
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "tail"}).json()
    jid = accepted["id"]
    for index in range(9):
        gateway.db.append_event(jid, "log", {"index": index})
    page = client.get(f"/v1/jobs/{jid}/events?tail=3&legacy=false",
                      headers=auth).json()
    # Selected descending, returned ascending: the response shape must not
    # depend on which end was read.
    assert [row["seq"] for row in page["events"]] == [7, 8, 9]
    assert page["total"] == 9


def test_tail_filters_types_inside_the_window(client, auth, gateway):
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "tailtype"}).json()
    jid = accepted["id"]
    gateway.db.append_event(jid, "result", {"text": "early"})
    for index in range(6):
        gateway.db.append_event(jid, "log", {"index": index})
    page = client.get(f"/v1/jobs/{jid}/events?tail=3&type=result&legacy=false",
                      headers=auth).json()
    # Filtering after the limit would return nothing here: the last three
    # events are all logs.
    assert [row["type"] for row in page["events"]] == ["result"]


def test_tail_and_after_cannot_be_combined(client, auth, gateway):
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "conflict"}).json()
    jid = accepted["id"]
    response = client.get(f"/v1/jobs/{jid}/events?tail=2&after=1", headers=auth)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_event_timestamps_are_iso_and_carry_elapsed(client, auth, gateway):
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "stamps"}).json()
    jid = accepted["id"]
    gateway.db.append_event(jid, "log", {"index": 0})
    gateway.db.append_event(jid, "log", {"index": 1})
    rows = client.get(f"/v1/jobs/{jid}/events?legacy=false",
                      headers=auth).json()["events"]
    first, last = rows[0], rows[-1]
    # `ts` is published as ISO, not an epoch float -- no bare number reaches a
    # reader, and the offset keeps local time unambiguous.
    assert isinstance(first["ts"], str)
    datetime.fromisoformat(first["ts"])
    assert ("+" in first["ts"][10:] or "-" in first["ts"][10:])
    # Durations stay numeric: they are arithmetic, not timestamps.
    assert first["elapsed"] == 0.0
    assert first["elapsed_hms"] == "+00:00:00"
    assert last["elapsed"] >= 0.0


def test_every_published_timestamp_is_iso(client, auth, gateway):
    """No epoch float should reach a caller from any surface."""
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "sweep"}).json()
    jid = accepted["id"]
    gateway.db.add_message(jid, {"status": "running", "msg": "hi", "ts": 1786490297.5})

    detail = client.get(f"/v1/jobs/{jid}", headers=auth).json()
    for field in ("created_at", "started_at", "finished_at", "last_event_at"):
        assert not isinstance(detail.get(field), (int, float)), field
        if detail.get(field) is not None:
            datetime.fromisoformat(detail[field])

    summary = client.get("/v1/jobs?limit=1", headers=auth).json()["jobs"][0]
    assert isinstance(summary["created_at"], str)

    events = client.get(f"/v1/jobs/{jid}/events?legacy=false",
                        headers=auth).json()["events"]
    message = [e for e in events if e["type"] == "message"][0]
    # The report payload is an opaque passthrough, so its own `ts` is the one
    # place a bare epoch could still surface.
    assert isinstance(message["data"]["ts"], str)
    datetime.fromisoformat(message["data"]["ts"])


def test_submit_reports_the_session_it_can_already_know(client, auth):
    pinned = client.post("/v1/jobs", headers=auth,
                         json={"prompt": "pin", "session": "sess-1",
                               "fork": False}).json()
    assert (pinned["session"], pinned["session_state"]) == ("sess-1", "pinned")
    fresh = client.post("/v1/jobs", headers=auth, json={"prompt": "new"}).json()
    # A fresh run has no id until its init record; say so rather than omit it.
    assert (fresh["session"], fresh["session_state"]) == (None, "pending")


def test_job_row_exposes_one_canonical_session(client, auth, gateway):
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "canon", "session": "asked",
                                 "fork": False}).json()
    jid = accepted["id"]
    detail = client.get(f"/v1/jobs/{jid}", headers=auth).json()
    assert detail["session"] == "asked"          # falls back before init
    gateway.db.finish_job(jid, status="succeeded", forked_session="wrote")
    detail = client.get(f"/v1/jobs/{jid}", headers=auth).json()
    assert detail["session"] == "wrote"          # the id actually written wins


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

    def paused_finish(job_id, fields, events, **kwargs):
        entered.set()
        assert release.wait(2)
        return original_finish(job_id, fields, events, **kwargs)

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
