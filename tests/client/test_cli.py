from __future__ import annotations

import json

import pytest

from client import ab, abclient


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

    def events(self, job_id, after=0, limit=500, *, tail=None, until=None,
               types=()):
        # No history: `--follow` primes from a tail before streaming, and this
        # fake's events all arrive live through iter_events below. Tests that
        # exercise priming supply their own page (see FakeHistoryClient).
        return {"events": [], "terminal": False, "status": self.status,
                "next_after": 0, "has_more": False,
                "total": 0, "first_seq": None, "last_seq": None}

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


class _AwaitClient(abclient.Client):
    """Exercises `await_session` without a gateway: `get_job` is scripted."""

    def __init__(self, rows):
        super().__init__("t", "http://127.0.0.1:0", "tok")
        self.rows = list(rows)
        self.calls = 0

    def get_job(self, job_id, **kwargs):
        self.calls += 1
        return self.rows[min(self.calls - 1, len(self.rows) - 1)]


def test_await_session_returns_the_id_once_it_appears():
    client = _AwaitClient([
        {"status": "running", "session": None},
        {"status": "running", "session": "sess-9"},
    ])
    out = client.await_session({"id": "job-1", "status": "queued"},
                               timeout=5, poll=0)
    assert (out["session"], out["session_state"]) == ("sess-9", "ready")


def test_await_session_does_not_wait_for_a_pinned_target():
    client = _AwaitClient([{"status": "running", "session": "pinned-1"}])
    out = client.await_session(
        {"id": "job-1", "session": "pinned-1", "session_state": "pinned"})
    assert out["session_state"] == "pinned"
    assert client.calls == 0          # nothing to wait for; no round trip


def test_await_session_stops_when_the_job_dies_before_init():
    # The failure mode the timeout must not paper over: no session will ever
    # arrive, so waiting out 30s would be pure latency.
    client = _AwaitClient([{"status": "failed", "session": None,
                            "error": "boom"}])
    out = client.await_session({"id": "job-1", "status": "queued"},
                               timeout=30, poll=0)
    assert out["session_state"] == "failed"
    assert out["error"] == "boom"
    assert client.calls == 1


def test_await_session_timeout_is_not_a_failure():
    client = _AwaitClient([{"status": "queued", "session": None}])
    out = client.await_session({"id": "job-1", "status": "queued"},
                               timeout=0, poll=0)
    # Still queued behind other work: pending, and the submission stands.
    assert out["session_state"] == "pending"
    assert out["id"] == "job-1"


class FakeHistoryClient(FakeClient):
    """A job that already has events, so priming has something to replay."""

    def __init__(self, status="succeeded"):
        super().__init__(status)
        self.events_calls = []

    def events(self, job_id, after=0, limit=500, *, tail=None, until=None,
               types=()):
        self.events_calls.append(
            {"after": after, "limit": limit, "tail": tail, "until": until,
             "types": tuple(types)})
        rows = [{"seq": 8, "ts": 8.0, "type": "assistant", "data": {"text": "older"}},
                {"seq": 9, "ts": 9.0, "type": "assistant", "data": {"text": "recent"}}]
        return {"events": rows, "terminal": False, "status": self.status,
                "next_after": 9, "has_more": True,
                "total": 9, "first_seq": 1, "last_seq": 9}


def test_events_defaults_to_a_tail_not_the_first_page(monkeypatch):
    client = FakeHistoryClient()
    monkeypatch.setattr(ab, "_client", lambda _args: client)
    assert ab.main(["events", "job-1", "--output", "json"]) == 0
    assert client.events_calls[0]["tail"] == ab.DEFAULT_TAIL
    assert client.events_calls[0]["after"] == 0


def test_events_after_opts_out_of_the_tail(monkeypatch):
    client = FakeHistoryClient()
    monkeypatch.setattr(ab, "_client", lambda _args: client)
    assert ab.main(["events", "job-1", "--after", "3", "--output", "json"]) == 0
    assert client.events_calls[0]["tail"] is None
    assert client.events_calls[0]["after"] == 3


def test_events_tail_conflicts_with_after(monkeypatch):
    monkeypatch.setattr(ab, "_client", lambda _args: FakeHistoryClient())
    with pytest.raises(SystemExit) as exc:
        ab.main(["events", "job-1", "--tail", "5", "--after", "3"])
    assert exc.value.code == ab.EXIT_INVOCATION


def test_after_zero_is_the_documented_escape_hatch(monkeypatch):
    """`--after 0` must read top-down, and it is the boundary value.

    `--after` used to default to 0, so "absent" and "explicitly zero" were the
    same value and this -- the one opt-out design/08 offered for making `events`
    tail by default -- silently returned a tail instead. Every test above used
    `--after 3`, which is why the suite stayed green.
    """
    client = FakeHistoryClient()
    monkeypatch.setattr(ab, "_client", lambda _args: client)
    assert ab.main(["events", "job-1", "--after", "0", "--output", "json"]) == 0
    assert client.events_calls[0]["tail"] is None
    assert client.events_calls[0]["after"] == 0


def test_tail_conflicts_with_after_zero_too(monkeypatch):
    monkeypatch.setattr(ab, "_client", lambda _args: FakeHistoryClient())
    with pytest.raises(SystemExit) as exc:
        ab.main(["events", "job-1", "--tail", "5", "--after", "0"])
    assert exc.value.code == ab.EXIT_INVOCATION


def test_follow_after_zero_replays_instead_of_priming(monkeypatch):
    """Following with `--after 0` asks for the whole log, not a short tail."""
    client = FakeHistoryClient()
    monkeypatch.setattr(ab, "_client", lambda _args: client)
    assert ab.main(["events", "job-1", "-f", "--after", "0",
                    "--output", "jsonl"]) == 0
    primed = [c for c in client.events_calls
              if c["tail"] == ab.FOLLOW_PRIME_TAIL]
    assert not primed


def test_events_passes_type_filter_to_the_server(monkeypatch):
    client = FakeHistoryClient()
    monkeypatch.setattr(ab, "_client", lambda _args: client)
    assert ab.main(["events", "job-1", "--tail", "3", "--type", "result",
                    "--output", "json"]) == 0
    # Filtering must reach the server so it applies within the tail window;
    # filtering client-side would make `--type result --tail 3` return nothing.
    assert client.events_calls[0]["types"] == ("result",)
    assert client.events_calls[0]["tail"] == 3


def test_follow_primes_from_a_tail_then_streams(monkeypatch, capsys):
    client = FakeHistoryClient()
    monkeypatch.setattr(ab, "_client", lambda _args: client)
    assert ab.main(["events", "job-1", "--follow", "--output", "jsonl"]) == 0
    assert client.events_calls[0]["tail"] == ab.FOLLOW_PRIME_TAIL
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    seqs = [row["event"]["seq"] for row in rows if row["kind"] == "event"]
    # The primed history precedes the live stream, in order and without a
    # full replay from seq 0.
    assert seqs[:2] == [8, 9]


def test_follow_failure_flag_applies_when_until_is_reached(monkeypatch):
    monkeypatch.setattr(ab, "_client", lambda _args: FakeClient("failed"))
    with pytest.raises(SystemExit) as exc:
        ab.main(["events", "job-1", "--follow", "--until", "1",
                 "--fail-on-job-failure", "--output", "jsonl"])
    assert exc.value.code == ab.EXIT_REMOTE


def test_snapshot_queries_do_not_fail_by_default(monkeypatch):
    monkeypatch.setattr(ab, "_client", lambda _args: FakeClient("failed"))
    assert ab.main(["job", "job-1", "--output", "json"]) == 0
