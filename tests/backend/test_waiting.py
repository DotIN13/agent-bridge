"""queued -> running -> waiting -> succeeded | failed | canceled.

A job is finished when its turn has ended **and** its report is written. Between
those two moments it is `waiting`, and — unlike the `awaiting_report` this
replaces — the agent process is still alive: the gateway is waiting on a file the
delegate is expected to write, not on a call from a machine it cannot see.
"""
from __future__ import annotations

import time

from gateway import jobdir
from gateway.db import TERMINAL, WAITING, Database


def _db(tmp_path):
    return Database(str(tmp_path / "j.db"))


def _running(db):
    job = db.create_job(agent="claude", prompt="go", cwd=None, session=None,
                        permission_mode=None, model=None)
    db.mark_running(job)
    return job


def _events(db, job, kind="status"):
    return [e for e in db.events_after(job, 0, limit=500) if e["type"] == kind]


# -- the state itself -----------------------------------------------------

def test_waiting_is_not_terminal(tmp_path):
    """`ab wait` keeps waiting, and restart recovery leaves it alone."""
    assert WAITING not in TERMINAL


def test_a_turn_that_ends_without_a_report_waits(tmp_path):
    db = _db(tmp_path)
    job = _running(db)

    row = db.mark_waiting(job, deadline=time.time() + 1800)

    assert db.get_job(job)["status"] == WAITING
    assert row["data"]["stage"] == "waiting"
    assert "report.md" in row["data"]["detail"]
    assert db.get_job(job)["finished_at"] is None, "it has not finished"
    db.close()


def test_the_report_landing_finishes_the_job(tmp_path):
    db = _db(tmp_path)
    job = _running(db)
    db.mark_waiting(job)

    rows = db.finish_reported(job)

    assert [r["data"]["reason"] for r in rows] == ["report_written"]
    row = db.get_job(job)
    assert row["status"] == "succeeded"
    assert row["finished_at"] is not None
    db.close()


def test_finishing_is_only_from_waiting(tmp_path):
    """Whoever gets there first wins; the loser is a no-op rather than a
    second terminal transition."""
    db = _db(tmp_path)
    job = _running(db)
    db.mark_waiting(job)
    assert db.finish_reported(job) != []
    assert db.finish_reported(job) == []
    db.close()


def test_a_missing_report_fails_at_the_deadline(tmp_path):
    """The deliverable is the point of the job, so its absence is a failure
    rather than a footnote."""
    db = _db(tmp_path)
    job = _running(db)
    now = time.time()
    db.mark_waiting(job, deadline=now - 1)

    assert db.expire_waiting(now) == [job]

    row = db.get_job(job)
    assert row["status"] == "failed"
    assert "report.md" in row["error"]
    assert [e["data"].get("code") for e in _events(db, job, "error")] == \
        ["report_missing"]
    assert db.expire_waiting(now) == [], "expiry is not repeated"
    db.close()


def test_no_deadline_waits_indefinitely(tmp_path):
    """`report_wait_sec = 0` means exactly that."""
    db = _db(tmp_path)
    job = _running(db)
    db.mark_waiting(job, deadline=None)
    assert db.expire_waiting(time.time() + 10 ** 6) == []
    assert db.get_job(job)["status"] == WAITING
    db.close()


def test_a_second_turn_refreshes_the_deadline(tmp_path):
    """A steer woke it and it is working again, so the clock restarts."""
    db = _db(tmp_path)
    job = _running(db)
    db.mark_waiting(job, deadline=100.0)
    db.mark_waiting(job, deadline=10 ** 12)
    assert db.expire_waiting(time.time()) == []
    db.close()


def test_the_turns_answer_is_kept_while_waiting(tmp_path):
    """`ab job` should show what the agent said even before the report lands."""
    db = _db(tmp_path)
    job = _running(db)
    db.save_result_fields(job, {"status": "succeeded", "result": "submitted 12345",
                                "cost_usd": 0.4})
    db.mark_waiting(job)

    row = db.get_job(job)
    assert row["result"] == "submitted 12345"
    assert row["cost_usd"] == 0.4
    assert row["status"] == WAITING, "the fields must not carry a status in"
    db.close()


def test_a_waiting_job_is_swept_for_its_report(tmp_path):
    db = _db(tmp_path)
    job = _running(db)
    db.mark_waiting(job)
    assert [r["id"] for r in db.jobs_with_open_dirs()] == [job]
    assert [r["status"] for r in db.jobs_with_open_dirs()] == [WAITING]
    db.close()


# -- the predicate --------------------------------------------------------

def test_a_report_must_have_something_in_it(tmp_path):
    """A zero-byte report.md is a delegate that started to write and did not,
    which is the case worth waiting out rather than accepting."""
    root = jobdir.prepare(tmp_path, "job-1")
    assert not jobdir.has_report(root)
    (root / "report.md").write_text("")
    assert not jobdir.has_report(root)
    jobdir.publish(root / "report.md", "the answer")
    assert jobdir.has_report(root)


def test_a_missing_directory_has_no_report(tmp_path):
    assert not jobdir.has_report(tmp_path / "nope")


# -- through the worker ---------------------------------------------------

def _pool(tmp_path, monkeypatch, adapter, **cfg_kw):
    from gateway import worker as workermod
    from gateway.bus import Bus
    from gateway.config import AgentConfig, Config

    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)
    agent = AgentConfig(
        name="claude", bin="claude", dispatch_mode="direct",
        permission_mode="bypassPermissions", model="", default_cwd=str(allowed),
        allowed_dirs=(str(allowed),), timeout_sec=0, max_sessions_in_index=5,
        models=())
    cfg = Config(host="127.0.0.1", port=1, token="t", concurrency=1,
                 db_path=str(tmp_path / "w.db"), data_dir=str(tmp_path),
                 files_dir=str(tmp_path / "f"), files_enabled=True,
                 cluster_enabled=False, agents={"claude": agent},
                 notes_path=str(tmp_path / "gateway.md"), **cfg_kw)
    monkeypatch.setattr(workermod, "build_adapter", lambda _cfg: adapter)
    db = Database(cfg.db_path)
    return workermod.WorkerPool(cfg, db, Bus()), db


def test_a_turn_that_wrote_its_report_finishes_without_waiting(tmp_path, monkeypatch):
    from gateway.adapters.base import RunResult

    class Adapter:
        def run(self, spec, emit):
            jobdir.publish(jobdir.path_for(tmp_path, spec.job_id) / "report.md",
                           "what I did")
            return RunResult(ok=True, result="done")

    pool, db = _pool(tmp_path, monkeypatch, Adapter())
    job = db.create_job(agent="claude", prompt="go", cwd=None, session=None,
                        permission_mode=None, model=None)
    pool._run_job(job)
    assert db.get_job(job)["status"] == "succeeded"
    db.close()


def test_a_turn_with_no_report_lands_in_waiting(tmp_path, monkeypatch):
    """The path a backend whose child exits with its turn takes: the same rule,
    applied at run end instead of at the result record."""
    from gateway.adapters.base import RunResult

    class Adapter:
        def run(self, _spec, _emit):
            return RunResult(ok=True, result="submitted 12345")

    pool, db = _pool(tmp_path, monkeypatch, Adapter(), report_wait_sec=1800)
    job = db.create_job(agent="claude", prompt="go", cwd=None, session=None,
                        permission_mode=None, model=None)
    pool._run_job(job)

    row = db.get_job(job)
    assert row["status"] == WAITING
    assert row["result"] == "submitted 12345", "the answer is kept"
    assert row["report_deadline"] is not None
    db.close()


def test_a_failed_turn_needs_no_report(tmp_path, monkeypatch):
    """Nothing to wait for: the turn did not get far enough to have a
    deliverable, and waiting would only delay the news."""
    from gateway.adapters.base import RunResult

    class Adapter:
        def run(self, _spec, _emit):
            return RunResult(ok=False, error="claude exited 1")

    pool, db = _pool(tmp_path, monkeypatch, Adapter())
    job = db.create_job(agent="claude", prompt="go", cwd=None, session=None,
                        permission_mode=None, model=None)
    pool._run_job(job)
    assert db.get_job(job)["status"] == "failed"
    db.close()


def test_the_turn_end_event_closes_the_run_when_the_report_is_there(tmp_path,
                                                                   monkeypatch):
    """The claude path: the result record ends the *turn*, and the worker closes
    the agent's stdin only once the report exists."""
    from gateway.adapters.base import Event, RunResult, Steering

    closed = []

    class FakeSteering(Steering):
        def close(self):
            closed.append(True)

    class Adapter:
        def run(self, spec, emit):
            jobdir.publish(jobdir.path_for(tmp_path, spec.job_id) / "report.md",
                           "done")
            emit(Event("status", {"stage": "turn_end"}))
            return RunResult(ok=True, result="done")

    pool, db = _pool(tmp_path, monkeypatch, Adapter())
    monkeypatch.setattr("gateway.worker.Steering", FakeSteering)
    job = db.create_job(agent="claude", prompt="go", cwd=None, session=None,
                        permission_mode=None, model=None)

    pool._run_job(job)

    assert closed, "the worker has to close stdin, or the read loop never ends"
    assert db.get_job(job)["status"] == "succeeded"
    db.close()


def test_the_turn_end_event_holds_the_run_open_without_a_report(tmp_path,
                                                               monkeypatch):
    from gateway.adapters.base import Event, RunResult, Steering

    closed = []

    class FakeSteering(Steering):
        def close(self):
            closed.append(True)

    at_turn_end = {}

    class Adapter:
        def run(self, _spec, emit):
            emit(Event("status", {"stage": "turn_end", "result": "submitted",
                                  "cost_usd": 0.2}))
            # Snapshot here: in the real flow the adapter is *blocked* reading
            # stdout at this point, and only unblocks when someone closes the
            # handle. Checking after `run` returns would instead catch the
            # worker's own unconditional close on the way out.
            at_turn_end["closed"] = list(closed)
            return RunResult(ok=True, result="submitted")

    pool, db = _pool(tmp_path, monkeypatch, Adapter(), report_wait_sec=1800)
    monkeypatch.setattr("gateway.worker.Steering", FakeSteering)
    job = db.create_job(agent="claude", prompt="go", cwd=None, session=None,
                        permission_mode=None, model=None)

    pool._run_job(job)

    assert at_turn_end["closed"] == [], \
        "the agent stays alive so it can still write its report"
    row = db.get_job(job)
    assert row["status"] == WAITING
    assert row["result"] == "submitted", \
        "`ab job` is read while waiting, so the turn's answer has to be there"
    assert row["cost_usd"] == 0.2
    db.close()


def test_a_waiting_job_does_not_hold_its_session_claim(tmp_path, monkeypatch):
    """Reaching the end of `_run_job` means the process has exited, so holding
    the claim would block every later resume of that session for the whole
    grace window."""
    from gateway.adapters.base import Event, RunResult

    class Adapter:
        def run(self, _spec, emit):
            emit(Event("status", {"session_id": "ses-1"}))
            return RunResult(ok=True, result="submitted", session="ses-1")

    pool, db = _pool(tmp_path, monkeypatch, Adapter(), report_wait_sec=1800)
    job = db.create_job(agent="claude", prompt="go", cwd=None, session=None,
                        permission_mode=None, model=None)
    pool._run_job(job)

    assert db.get_job(job)["status"] == WAITING
    assert pool.claimant("ses-1") is None
    db.close()


def test_a_later_save_does_not_blank_what_an_earlier_one_kept(tmp_path):
    """Both ends of a held-open run save fields: the turn's answer when the turn
    ends, then the RunResult when the run winds up half an hour later. The
    second carries `None` for anything it does not know."""
    db = _db(tmp_path)
    job = _running(db)
    db.save_result_fields(job, {"result": "the answer", "cost_usd": 0.2})
    db.save_result_fields(job, {"result": None, "cost_usd": None,
                                "session": "ses-9"})

    row = db.get_job(job)
    assert row["result"] == "the answer"
    assert row["cost_usd"] == 0.2
    assert row["session"] == "ses-9"
    db.close()
