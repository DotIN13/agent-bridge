"""`expect_report` parks a job until the work it started reports back.

The turn and the job were the same thing, so a batch submission looked
`succeeded` the moment the agent stopped talking -- hours before the work it
started actually ran. `expect_report` parks the row in `awaiting_report` until a
terminal report closes it.

It is now **opt-in**. Holding every row open by default made a caller's mistake
(a brief that never arranged a report) indistinguishable from work still
running, and cost a day per occurrence at the default deadline; long-running
external work is a monitor with its own lifecycle instead (docs/todo/15). What
this module pins is that the opt-in still works, in every way it used to.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys

from gateway.api_models import JobCreate
from gateway.db import AWAITING_REPORT, TERMINAL, Database

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))
from abclient import _job_payload  # noqa: E402


def _job(db, *, expect_report=False):
    return db.create_job(
        agent="claude", prompt="submit the sweep", cwd=None,
        session=None, permission_mode=None, model=None,
        expect_report=expect_report)


def _succeed(db, job, **kw):
    return db.finish_job_with_events(
        job, {"status": "succeeded", "result": "submitted as 12345"},
        [("status", {"stage": "done", "status": "succeeded"})], **kw)


def test_a_job_that_opted_out_finishes_with_its_turn(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    job = _job(db)                                 # db layer defaults off
    _succeed(db, job)
    row = db.get_job(job)
    assert row["status"] == "succeeded"
    assert row["finished_at"] is not None
    db.close()


def test_the_api_does_not_expect_a_report_by_default():
    """The turn is the job unless the caller says otherwise.

    The whole suite stayed green the last time this default moved, because
    nothing asserted what an API-submitted job does at the end of its turn.
    These pin the contract in both directions.
    """
    assert JobCreate(prompt="x").expect_report is False
    assert JobCreate(prompt="x", expect_report=True).expect_report is True


def test_the_cli_sends_the_opt_in_explicitly():
    """The server default is off, so `--expect-report` has to be transmitted.

    Omitting a true value would silently mean "the turn is the whole job" --
    the opposite of what the caller asked for.
    """
    assert "expect_report" not in _job_payload(
        "p", None, None, None, None, None, None)
    assert _job_payload("p", None, None, None, None, None, None,
                        expect_report=True)["expect_report"] is True


def test_an_api_submitted_job_finishes_with_its_turn_by_default(client, auth, gateway):
    """The whole way through: DTO default, job row, and the end of the turn.

    Asserting the DTO alone would not have caught a server that passed a
    hardcoded true into `create_job`, which is the shape of the bug this
    default's history is made of.
    """
    created = client.post("/v1/jobs", json={"prompt": "answer a question"},
                          headers=auth)
    job = created.json()["id"]
    assert created.json()["expect_report"] is False

    _succeed(gateway.db, job, report_timeout_sec=3600)

    row = client.get(f"/v1/jobs/{job}", headers=auth).json()
    assert row["status"] == "succeeded"
    assert row["finished_at"] is not None


def test_expect_report_parks_instead_of_finishing(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    job = _job(db, expect_report=True)
    _succeed(db, job)
    row = db.get_job(job)
    assert row["status"] == AWAITING_REPORT
    assert row["status"] not in TERMINAL          # so `ab wait` keeps waiting
    assert row["finished_at"] is None             # it has not finished
    assert row["result"] == "submitted as 12345"  # the turn's result is kept
    db.close()


def test_a_finished_report_closes_the_job(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    job = _job(db, expect_report=True)
    _succeed(db, job)
    db.add_message(job, {"status": "finished", "report": "/runs/RESULTS.md",
                         "report_id": "done", "ts": 1786500000.0})
    row = db.get_job(job)
    assert row["status"] == "succeeded"
    assert row["finished_at"] == 1786500000.0     # when the work ended
    db.close()


def test_a_failed_report_fails_the_job_and_says_why(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    job = _job(db, expect_report=True)
    _succeed(db, job)
    db.add_message(job, {"status": "failed", "msg": "CUDA OOM on node 7",
                         "report_id": "fail"})
    row = db.get_job(job)
    assert row["status"] == "failed"
    assert "CUDA OOM" in (row["error"] or "")
    db.close()


def test_progress_reports_leave_the_job_parked(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    job = _job(db, expect_report=True)
    _succeed(db, job)
    for i in range(3):
        db.add_message(job, {"status": "running", "msg": f"{i}/3",
                             "report_id": f"p{i}"})
    assert db.get_job(job)["status"] == AWAITING_REPORT
    db.close()


def test_a_report_cannot_reopen_a_job_that_already_finished(tmp_path):
    """Only parked jobs are closed by a report.

    A job that never declared `expect_report` finished when its turn did, and
    moving a terminal row again would break the monotonicity `wait` and SSE
    rely on -- a follow that already closed would never learn.
    """
    db = Database(str(tmp_path / "j.db"))
    job = _job(db)                                 # no expect_report
    _succeed(db, job)
    db.add_message(job, {"status": "failed", "msg": "late", "report_id": "x"})
    row = db.get_job(job)
    assert row["status"] == "succeeded"
    db.close()


def test_a_failed_turn_does_not_wait_for_a_report(tmp_path):
    """If the turn failed there is no reason to believe anything was submitted."""
    db = Database(str(tmp_path / "j.db"))
    job = _job(db, expect_report=True)
    db.finish_job_with_events(
        job, {"status": "failed", "error": "sbatch refused the job"},
        [("status", {"stage": "done", "status": "failed"})])
    assert db.get_job(job)["status"] == "failed"
    db.close()


def test_a_report_that_never_comes_expires(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    job = _job(db, expect_report=True)
    _succeed(db, job, report_timeout_sec=3600)
    row = db.get_job(job)
    assert row["report_deadline"] is not None

    assert db.expire_awaiting_reports(now=row["report_deadline"] - 1) == []
    assert db.get_job(job)["status"] == AWAITING_REPORT

    assert db.expire_awaiting_reports(now=row["report_deadline"] + 1) == [job]
    closed = db.get_job(job)
    assert closed["status"] == "failed"
    assert "no ab-notify report" in (closed["error"] or "")
    # The reason is on the stream too, so a follower learns why it ended.
    reasons = [e["data"].get("code") or e["data"].get("reason")
               for e in db.events_after(job, 0)]
    assert "report_timeout" in reasons
    db.close()


def test_no_deadline_means_wait_indefinitely(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    job = _job(db, expect_report=True)
    _succeed(db, job, report_timeout_sec=0)
    assert db.get_job(job)["report_deadline"] is None
    assert db.expire_awaiting_reports(now=1e12) == []
    assert db.get_job(job)["status"] == AWAITING_REPORT
    db.close()


def test_a_parked_job_survives_a_gateway_restart(tmp_path):
    """The batch work is still out there; only `running` rows lost a process."""
    path = str(tmp_path / "j.db")
    db = Database(path)
    parked = _job(db, expect_report=True)
    _succeed(db, parked)
    running = _job(db)
    db.mark_running(running)
    db.close()

    db = Database(path)
    db.reconcile_startup()
    assert db.get_job(parked)["status"] == AWAITING_REPORT
    assert db.get_job(running)["status"] == "failed"
    db.close()


def test_the_closing_status_reaches_the_stream(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    job = _job(db, expect_report=True)
    _succeed(db, job)
    row = db.add_message(job, {"status": "finished", "report_id": "done"})
    # The server publishes this and closes the bus; without it a follow that
    # has been open since the turn ended would hang.
    assert row["closing_event"] is not None
    assert row["closing_event"]["data"]["status"] == "succeeded"
    assert row["closing_event"]["data"]["reason"] == "batch_report"
    db.close()


def test_a_missing_reporter_no_longer_refuses_to_start(tmp_path, monkeypatch, capsys):
    """Startup used to exit 2 without `ab-notify`, because a parked job could
    never be closed without it. Every job now gets `$AB_JOB_DIR` and closes
    itself with `echo`, so a missing reporter is worth saying and not worth
    refusing to boot over.

    The config has to be valid, or `main` returns 2 for the missing file and the
    test passes without reaching the guard at all.
    """
    from gateway import __main__ as entry

    cfg = tmp_path / "config.toml"
    cfg.write_text('[server]\nhost = "127.0.0.1"\nport = 8123\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))

    served = []
    monkeypatch.setattr(entry.Gateway, "serve_forever",
                        lambda self: served.append(True))

    # Resolvable: startup proceeds all the way to serving.
    monkeypatch.setattr(entry, "find_ab_notify", lambda: "/usr/bin/ab-notify")
    assert entry.main(["--config", str(cfg)]) == 0
    assert served == [True]

    # Not resolvable: still serves, and says where reporting goes instead.
    served.clear()
    capsys.readouterr()
    monkeypatch.setattr(entry, "find_ab_notify", lambda: None)
    assert entry.main(["--config", str(cfg)]) == 0
    assert served == [True]
    assert "AB_JOB_DIR" in capsys.readouterr().err


def test_ab_notify_resolves_from_the_checkout_when_not_on_path(monkeypatch):
    """The repo ships bin/ab-notify, so a source checkout is a valid install."""
    from gateway import __main__ as entry

    monkeypatch.setattr(entry.shutil, "which", lambda _name: None)
    resolved = entry.find_ab_notify()
    assert resolved and resolved.endswith("ab-notify")
