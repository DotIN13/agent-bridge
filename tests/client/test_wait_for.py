"""A job has two ends, and `ab wait` has to be able to stop at either.

Since a job parks in `awaiting_report` when its turn succeeds, "the agent
stopped talking" and "the work finished" are separate moments, hours apart on
real batch work. `--for` chooses which one to return at.
"""
from __future__ import annotations

from client import ab, abclient
from client.abclient import (AWAITING_REPORT, _may_have_ended,
                             _report_is_terminal, _wait_reached)

PARKED = {"status": AWAITING_REPORT}
DONE = {"status": "succeeded"}
RUNNING = {"status": "running"}


def test_default_waits_for_the_work_not_the_turn():
    assert _wait_reached(PARKED, False, "both") is False
    assert _wait_reached(DONE, False, "both") is True


def test_for_turn_returns_while_the_work_is_still_out_there():
    assert _wait_reached(PARKED, False, "turn") is True
    assert _wait_reached(RUNNING, False, "turn") is False
    # A job that never parked still ends at its turn.
    assert _wait_reached(DONE, False, "turn") is True


def test_for_report_stops_on_the_report():
    assert _wait_reached(PARKED, False, "report") is False
    assert _wait_reached(PARKED, True, "report") is True


def test_for_report_does_not_hang_on_a_job_that_will_never_send_one():
    """Canceled, failed, or submitted with --no-expect-report.

    Blocking to the timeout and then reporting "still running" about a job that
    finished ten minutes ago would be a lie with a long delay attached.
    """
    assert _wait_reached({"status": "canceled"}, False, "report") is True
    assert _wait_reached(DONE, False, "report") is True


def test_only_a_terminal_report_counts_as_a_report():
    def message(status):
        return {"type": "message", "data": {"status": status}}

    assert _report_is_terminal(message("finished")) is True
    assert _report_is_terminal(message("failed")) is True
    # Progress is why reports exist; it must not end the wait.
    assert _report_is_terminal(message("running")) is False
    assert _report_is_terminal(message("queued")) is False
    assert _report_is_terminal({"type": "assistant", "data": {}}) is False


def test_the_wait_rechecks_as_soon_as_something_could_have_ended():
    """A parked job keeps its stream open, so waiting for it to pause is wrong.

    Without this the row is only consulted after the read window closes, and
    `--for turn` sat through the full 60s timeout on a milestone that had
    already passed one second in.
    """
    assert _may_have_ended(
        {"type": "status", "data": {"stage": "awaiting_report"}}) is True
    assert _may_have_ended({"type": "status", "data": {"stage": "done"}}) is True
    assert _may_have_ended(
        {"type": "message", "data": {"status": "finished"}}) is True
    # Ordinary traffic must not break the stream loop.
    assert _may_have_ended({"type": "assistant", "data": {"text": "hi"}}) is False
    assert _may_have_ended(
        {"type": "status", "data": {"stage": "running"}}) is False


def test_the_flag_reaches_the_client(monkeypatch):
    seen = {}

    class FakeClient:
        def wait(self, job_id, **kwargs):
            seen.update(kwargs)
            return {"id": job_id, "status": "succeeded"}

    monkeypatch.setattr(ab, "_client", lambda _args: FakeClient())
    assert ab.main(["wait", "job-1", "--for", "turn", "--output", "json"]) == 0
    assert seen["until"] == "turn"

    seen.clear()
    assert ab.main(["wait", "job-1", "--output", "json"]) == 0
    assert seen["until"] == "both"


def test_an_unknown_milestone_is_an_invocation_error(capsys):
    import pytest
    with pytest.raises(SystemExit) as exc:
        ab.build_parser().parse_args(["wait", "job-1", "--for", "everything"])
    assert exc.value.code == 2
    assert set(abclient.WAIT_FOR) == {"both", "turn", "report"}
