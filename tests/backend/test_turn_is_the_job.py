"""The turn is the job. Nothing a delegate writes ends a row.

This default moved three times. It started as "the turn is the job", became
`expect_report` defaulting *on* so a job parked in `awaiting_report` until
something called in (design/11), then opt-in (design/15), and is now gone
(design/16): holding a row open made a caller's mistake — a brief that never
arranged a report — indistinguishable from work still running, and cost a day
per occurrence at the old deadline.

What replaced it: work that outlives a turn is a monitor with its own row.
"""
from __future__ import annotations

import sys
from pathlib import Path

from gateway.api_models import JobCreate
from gateway.db import TERMINAL, Database

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))
from abclient import _job_payload  # noqa: E402


def _job(db):
    return db.create_job(
        agent="claude", prompt="submit the sweep", cwd=None,
        session=None, permission_mode=None, model=None)


def _succeed(db, job, **kw):
    return db.finish_job_with_events(
        job, {"status": "succeeded", "result": "submitted as 12345"},
        [("status", {"stage": "done", "status": "succeeded"})], **kw)


def test_a_job_is_terminal_when_its_turn_ends(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    job = _job(db)
    _succeed(db, job)
    row = db.get_job(job)
    assert row["status"] == "succeeded"
    assert row["status"] in TERMINAL
    assert row["finished_at"] is not None
    assert row["result"] == "submitted as 12345"
    db.close()


def test_no_path_reaches_awaiting_report(tmp_path):
    """The status is gone from the vocabulary, not merely unreachable by
    default. A row that could still park would keep `ab wait` blocking on
    something nobody sends."""
    db = Database(str(tmp_path / "j.db"))
    job = _job(db)
    _succeed(db, job)
    statuses = {db.get_job(job)["status"]}
    for _ in range(2):                       # a second terminal write is a no-op
        _succeed(db, job)
        statuses.add(db.get_job(job)["status"])
    assert statuses == {"succeeded"}
    assert not hasattr(db, "expire_awaiting_reports")
    db.close()


def test_a_failed_turn_fails_the_job(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    job = _job(db)
    db.finish_job_with_events(
        job, {"status": "failed", "error": "claude exited 1"},
        [("status", {"stage": "done", "status": "failed"})])
    row = db.get_job(job)
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    db.close()


def test_the_api_refuses_expect_report_rather_than_ignoring_it(client, auth):
    """A caller asking to be waited for, and silently not being, is the
    substitution design/03 rules out. So it is a typed error that names the
    replacement."""
    refused = client.post("/v1/jobs", headers=auth,
                          json={"prompt": "x", "expect_report": True})
    assert refused.status_code == 400
    body = refused.json()["error"]
    assert body["code"] == "expect_report_removed"
    assert "monitor" in body["message"]


def test_expect_report_false_is_still_accepted(client, auth):
    """An old script that spelled out the default keeps working."""
    accepted = client.post("/v1/jobs", headers=auth,
                           json={"prompt": "x", "expect_report": False})
    assert accepted.status_code == 202


def test_the_dto_keeps_the_field_only_so_the_refusal_can_name_it():
    assert JobCreate(prompt="x").expect_report is False
    assert JobCreate(prompt="x", expect_report=True).expect_report is True


def test_the_cli_no_longer_sends_it():
    assert "expect_report" not in _job_payload(
        "p", None, None, None, None, None, None)


def test_an_http_report_annotates_and_does_not_move_the_row(tmp_path):
    """`/message` was able to end a running or parked job. A job ends when its
    turn does, so a report is an annotation — the caller has `cancel` if it
    wants to end one."""
    db = Database(str(tmp_path / "j.db"))
    job = _job(db)
    db.mark_running(job)

    row = db.add_message(job, {"status": "finished", "msg": "24/24",
                               "report_id": "done"})

    assert "closing_event" not in row
    assert db.get_job(job)["status"] == "running"
    db.close()


def test_a_terminal_report_after_the_job_ended_is_still_recorded(tmp_path):
    """Post-terminal annotations stay allowed — design/07's rule, and how a
    monitor reports hours later."""
    db = Database(str(tmp_path / "j.db"))
    job = _job(db)
    _succeed(db, job)

    db.add_message(job, {"status": "finished", "msg": "the batch landed"})

    messages = [e for e in db.events_after(job, 0, limit=100)
                if e["type"] == "message"]
    assert [m["data"]["msg"] for m in messages] == ["the batch landed"]
    assert db.get_job(job)["status"] == "succeeded"
    db.close()


def test_the_reporter_is_optional_at_startup(tmp_path, monkeypatch, capsys):
    """Startup once exited 2 without `ab-notify`, because a parked job could
    never be closed without it."""
    from gateway import __main__ as entry

    cfg = tmp_path / "config.toml"
    cfg.write_text('[server]\nhost = "127.0.0.1"\nport = 8123\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    served = []
    monkeypatch.setattr(entry.Gateway, "serve_forever",
                        lambda self: served.append(True))
    monkeypatch.setattr(entry, "find_ab_notify", lambda: None)

    assert entry.main(["--config", str(cfg)]) == 0
    assert served == [True]
    assert "AB_JOB_DIR" in capsys.readouterr().err
