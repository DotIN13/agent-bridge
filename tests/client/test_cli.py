from __future__ import annotations

import json

import pytest

from client import ab


def test_globals_work_before_and_after_command():
    before = ab.build_parser().parse_args(["--json", "jobs"])
    after = ab.build_parser().parse_args(["jobs", "--json"])
    assert ab._mode(before) == "json"
    assert ab._mode(after) == "json"


def test_output_conflict_and_local_validation():
    args = ab.build_parser().parse_args(["--json", "jobs", "--output", "jsonl"])
    with pytest.raises(SystemExit) as exc:
        ab._validate(args)
    assert exc.value.code == ab.EXIT_INVOCATION

    args = ab.build_parser().parse_args(["submit", "hello", "--no-fork"])
    with pytest.raises(SystemExit) as exc:
        ab._validate(args)
    assert exc.value.code == ab.EXIT_INVOCATION


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_wait_timeouts_must_be_finite(value):
    parser = ab.build_parser()
    for command in (["run", "hello", "--timeout", value],
                    ["wait", "job", "--timeout", value],
                    ["job", "job", "--wait", "--timeout", value]):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(command)
        assert exc.value.code == ab.EXIT_INVOCATION


def test_event_range_and_type_validation():
    parser = ab.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["events", "x", "--type", "not-an-event"])
    args = parser.parse_args(["events", "x", "--after", "5", "--until", "4"])
    with pytest.raises(SystemExit) as exc:
        ab._validate(args)
    assert exc.value.code == ab.EXIT_INVOCATION


def test_help_and_version_are_discoverable(capsys):
    parser = ab.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    assert ab.CLIENT_VERSION in capsys.readouterr().out
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    output = capsys.readouterr().out
    for command in ("health", "agents", "capabilities", "wait", "events"):
        assert command in output


class FakeClient:
    def __init__(self, status="succeeded", timeout=False):
        self.status = status
        self.timeout = timeout
        self.cancelled = False

    def submit(self, prompt, **kwargs):
        return {"id": "job-1", "status": "queued", "title": "test"}

    def wait(self, job_id, timeout=900, on_event=None, cancel_on_timeout=False, **kwargs):
        if on_event:
            on_event({"seq": 1, "ts": 1.0, "type": "assistant",
                      "data": {"text": "hello"}})
        return {"id": job_id, "status": self.status, "result": "hello",
                "error": None, "timed_out_waiting": self.timeout}

    def get_job(self, job_id):
        return {"id": job_id, "status": self.status, "result": "hello",
                "error": None}

    def iter_events(self, job_id, after=0, **kwargs):
        yield {"seq": 1, "ts": 1.0, "type": "assistant",
               "data": {"text": "hello"}}
        yield {"seq": 2, "ts": 2.0, "type": "status",
               "data": {"stage": "done", "status": self.status}}


def test_json_run_is_one_faithful_document(monkeypatch, capsys):
    monkeypatch.setattr(ab, "_client", lambda _args: FakeClient())
    assert ab.main(["--json", "run", "hello", "--stream"]) == 0
    captured = capsys.readouterr()
    value = json.loads(captured.out)
    assert value["id"] == "job-1"
    assert value["result"] == "hello"
    assert captured.out.count("{") == 1


def test_jsonl_run_is_typed_and_parseable(monkeypatch, capsys):
    monkeypatch.setattr(ab, "_client", lambda _args: FakeClient())
    assert ab.main(["run", "hello", "--output", "jsonl"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["kind"] for row in rows] == ["event", "terminal"]
    assert rows[0]["job_id"] == "job-1"


def test_jsonl_follow_honors_type_filter(monkeypatch, capsys):
    monkeypatch.setattr(ab, "_client", lambda _args: FakeClient())
    assert ab.main(["events", "job-1", "--follow", "--output", "jsonl",
                    "--type", "assistant"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["kind"] for row in rows] == ["event", "terminal"]
    assert rows[0]["event"]["type"] == "assistant"


@pytest.mark.parametrize("status,timeout,code", [
    ("failed", False, ab.EXIT_REMOTE),
    ("canceled", False, ab.EXIT_REMOTE),
    ("running", True, ab.EXIT_TIMEOUT),
])
def test_blocking_exit_contract(monkeypatch, status, timeout, code):
    monkeypatch.setattr(ab, "_client", lambda _args: FakeClient(status, timeout))
    with pytest.raises(SystemExit) as exc:
        ab.main(["wait", "job-1", "--output", "json"])
    assert exc.value.code == code


def test_follow_failure_flag_applies_when_until_is_reached(monkeypatch):
    monkeypatch.setattr(ab, "_client", lambda _args: FakeClient("failed"))
    with pytest.raises(SystemExit) as exc:
        ab.main(["events", "job-1", "--follow", "--until", "1",
                 "--fail-on-job-failure", "--output", "jsonl"])
    assert exc.value.code == ab.EXIT_REMOTE


def test_snapshot_queries_do_not_fail_by_default(monkeypatch):
    monkeypatch.setattr(ab, "_client", lambda _args: FakeClient("failed"))
    assert ab.main(["job", "job-1", "--output", "json"]) == 0
