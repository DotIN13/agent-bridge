"""A delegate reports by writing files, with no id, url or token.

`ab-notify` needed `$AB_JOB_ID`, which nothing in the gateway ever set, so a job
whose caller had not pasted its uuid into the prompt could not close itself. The
job dir replaces that: the gateway hands the agent one directory and reads
whatever appears in it.
"""
from __future__ import annotations

from pathlib import Path

from gateway import jobdir
from gateway.adapters.base import JobSpec, child_env, job_dir_note
from gateway.db import Database


def _db(tmp_path):
    return Database(str(tmp_path / "j.db"))


def _job(db):
    return db.create_job(
        agent="claude", prompt="run the sweep", cwd=None, session=None,
        permission_mode=None, model=None)


def _messages(db, job):
    return [e for e in db.events_after(job, 0, limit=500)
            if e["type"] == "message"]


def test_a_progress_drop_becomes_one_message_event(tmp_path):
    db, job = _db(tmp_path), None
    job = _job(db)
    root = jobdir.prepare(tmp_path, job)
    jobdir.publish(root / "progress" / "010-sources.md", "12/24 sources done")

    rows = db.ingest_job_dir(job, str(root))
    assert len(rows) == 1
    events = _messages(db, job)
    assert len(events) == 1
    assert events[0]["data"]["msg"] == "12/24 sources done"
    assert events[0]["data"]["file"] == "progress/010-sources.md"
    assert events[0]["data"]["source"] == "job_dir"
    db.close()


def test_scanning_twice_does_not_report_twice(tmp_path):
    """The sweeper runs on a timer, so re-reading is the normal case."""
    db = _db(tmp_path)
    job = _job(db)
    root = jobdir.prepare(tmp_path, job)
    jobdir.publish(root / "progress" / "001-start.md", "server up")

    assert len(db.ingest_job_dir(job, str(root))) == 1
    assert db.ingest_job_dir(job, str(root)) == []
    assert len(_messages(db, job)) == 1
    db.close()


def test_rewriting_a_file_with_new_content_reports_again(tmp_path):
    """Dedup is path *and* content, which is what lets a retried step overwrite
    its own milestone and still be heard."""
    db = _db(tmp_path)
    job = _job(db)
    root = jobdir.prepare(tmp_path, job)

    jobdir.publish(root / "progress" / "sources.md", "12/24 done")
    db.ingest_job_dir(job, str(root))
    jobdir.publish(root / "progress" / "sources.md", "24/24 done")
    db.ingest_job_dir(job, str(root))

    assert [e["data"]["msg"] for e in _messages(db, job)] == \
        ["12/24 done", "24/24 done"]
    db.close()


def test_an_oversized_file_is_truncated_and_says_so(tmp_path):
    db = _db(tmp_path)
    job = _job(db)
    root = jobdir.prepare(tmp_path, job)
    jobdir.publish(root / "report.md", "x" * (jobdir.MAX_FILE_BYTES + 500))

    db.ingest_job_dir(job, str(root))
    data = _messages(db, job)[0]["data"]
    assert len(data["msg"]) == jobdir.MAX_FILE_BYTES
    assert "exceeded" in data["error"]
    db.close()


def test_a_half_written_file_is_not_ingested(tmp_path):
    """`publish` renames into place, so a `.tmp` is mid-write by definition."""
    root = jobdir.prepare(tmp_path, "job-1")
    (root / "progress" / "003-partial.md.tmp").write_text("half a li")
    assert jobdir.scan(root) == []


def test_the_scan_is_ordered_by_name_with_status_first(tmp_path):
    root = jobdir.prepare(tmp_path, "job-1")
    jobdir.publish(root / "progress" / "020-b.md", "second")
    jobdir.publish(root / "progress" / "010-a.md", "first")
    jobdir.publish(root / "report.md", "the report")
    jobdir.publish(root / "status", "finished")

    assert [d.rel for d in jobdir.scan(root)] == [
        "status", "progress/010-a.md", "progress/020-b.md", "report.md"]


def test_a_runaway_job_dir_is_bounded_and_says_so(tmp_path):
    root = jobdir.prepare(tmp_path, "job-1")
    for index in range(jobdir.MAX_FILES + 5):
        jobdir.publish(root / "progress" / f"{index:04d}.md", str(index))

    drops = jobdir.scan(root)
    assert len(drops) == jobdir.MAX_FILES + 1
    assert drops[-1].rel == "…overflow"
    assert "only the first" in drops[-1].text


def test_a_missing_job_dir_is_not_an_error(tmp_path):
    db = _db(tmp_path)
    job = _job(db)
    assert db.ingest_job_dir(job, str(tmp_path / "nope")) == []
    db.close()


def test_the_agent_is_told_where_to_write_and_gets_it_in_its_environment():
    """Both halves, or a delegate has the directory and not the convention."""
    spec = JobSpec(job_id="j1", prompt="p", cwd="/tmp", requested_session=None,
                   permission_mode=None, model=None, job_dir="/data/reports/j1")
    note = job_dir_note(spec)
    assert "/data/reports/j1" in note
    assert "ab-notify --msg" in note
    assert "report.md is written" in note
    assert child_env(spec)["AB_JOB_DIR"] == "/data/reports/j1"


def test_a_job_without_a_dir_says_nothing_about_one():
    spec = JobSpec(job_id="j1", prompt="p", cwd="/tmp", requested_session=None,
                   permission_mode=None, model=None)
    assert job_dir_note(spec) == ""
    assert child_env(spec) is None


def test_the_job_dir_cannot_collide_with_the_attachment_store(tmp_path):
    """`files.promote_staging` renames a directory into `<files_dir>/jobs/<id>`
    and fails if it exists, so the report dir must not be able to land there
    even when an operator points `[files] dir` at the data dir."""
    from gateway import files as filemod
    from gateway.config import Config

    cfg = Config(host="127.0.0.1", port=1, token="t", concurrency=1,
                 db_path=str(tmp_path / "g.db"), data_dir=str(tmp_path), files_dir=str(tmp_path),
                 files_enabled=True, cluster_enabled=False, agents={},
                 notes_path=str(tmp_path / "gateway.md"))
    assert Path(filemod.job_dir(cfg, "j1")) != jobdir.path_for(tmp_path, "j1")


def test_the_worker_creates_the_dir_and_hands_it_to_the_adapter(tmp_path, monkeypatch):
    """The wiring todo/13 found missing: a job that is never told where to
    write cannot report, however good the convention is."""
    from gateway import worker as workermod
    from gateway.adapters.base import RunResult
    from gateway.bus import Bus
    from gateway.config import AgentConfig, Config

    seen = {}

    class FakeAdapter:
        def run(self, spec, emit):
            seen["job_dir"] = spec.job_dir
            return RunResult(ok=True, result="done")

    monkeypatch.setattr(workermod, "build_adapter", lambda cfg: FakeAdapter())
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    agent = AgentConfig(
        name="claude", bin="claude", dispatch_mode="direct",
        permission_mode="bypassPermissions", model="", default_cwd=str(allowed),
        allowed_dirs=(str(allowed),), timeout_sec=0, max_sessions_in_index=5,
        models=("claude-test",))
    cfg = Config(host="127.0.0.1", port=1, token="t", concurrency=1,
                 db_path=str(tmp_path / "w.db"), data_dir=str(tmp_path), files_dir=str(tmp_path / "f"),
                 files_enabled=True, cluster_enabled=False,
                 agents={"claude": agent},
                 notes_path=str(tmp_path / "gateway.md"))
    db = Database(cfg.db_path)
    pool = workermod.WorkerPool(cfg, db, Bus())
    job = db.create_job(agent="claude", prompt="go", cwd=str(allowed),
                        session=None, permission_mode=None, model=None)

    pool._run_job(job)

    expected = jobdir.path_for(tmp_path, job)
    assert seen["job_dir"] == str(expected)
    assert (expected / "progress").is_dir(), "progress/ exists before the agent asks"
    assert (expected / "monitors").is_dir()
    db.close()


def test_a_drop_shows_up_on_the_event_stream_over_http(client, auth, gateway):
    """End to end through the read path a caller actually uses."""
    created = client.post("/v1/jobs", json={"prompt": "run the sweep"},
                          headers=auth)
    assert created.status_code == 202, created.text
    job = created.json()["id"]

    root = jobdir.prepare(gateway.cfg.data_dir, job)
    jobdir.publish(root / "progress" / "001-up.md", "server up")

    events = client.get(f"/v1/jobs/{job}/events?after=0&type=message",
                        headers=auth).json()["events"]
    assert [e["data"]["msg"] for e in events] == ["server up"]


def test_a_status_file_is_an_ordinary_drop(tmp_path):
    """It used to name a job's state, back when a row could park waiting to be
    closed. The turn's end decides that now, so the word means nothing to the
    gateway — a delegate that writes one out of habit is simply heard."""
    db = _db(tmp_path)
    job = _job(db)
    db.mark_running(job)
    root = jobdir.prepare(tmp_path, job)
    jobdir.publish(root / "status", "finished")

    db.ingest_job_dir(job, str(root))

    data = _messages(db, job)[0]["data"]
    assert data["file"] == "status"
    assert data["msg"] == "finished"
    assert "status" not in data, "a drop must not claim to be a state"
    assert db.get_job(job)["status"] == "running", "and must not move the row"
    db.close()
