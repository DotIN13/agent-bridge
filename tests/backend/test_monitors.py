"""Work that outlives the turn is a watch, not a job row held open.

The delegate authors the poll command, because it is what knows what it
submitted; the gateway runs it on a timer and reads the first word. Nothing in
the gateway knows what Slurm is beyond a lookup table, so a command that prints
`finished` is watchable on the same terms as `sacct`.
"""
from __future__ import annotations

import dataclasses
import time

import pytest

from gateway import monitors
from gateway.db import MONITOR_TERMINAL, Database
from gateway.server import MonitorRefused, _as_seconds, register_monitor


def _db(tmp_path):
    return Database(str(tmp_path / "m.db"))


def _tune(gateway, **kw):
    """`Config` is frozen, so a test that needs a bound changes the whole thing."""
    gateway.cfg = dataclasses.replace(gateway.cfg, **kw)


def _state_file(tmp_path, value):
    path = tmp_path / "state"
    path.write_text(value)
    return path


# -- classification --------------------------------------------------------

@pytest.mark.parametrize("output,expected", [
    ("finished", "finished"),
    ("FINISHED\n", "finished"),
    ("COMPLETED", "finished"),          # sacct --format=State
    ("RUNNING", "running"),
    ("PENDING", "queued"),
    ("FAILED", "failed"),
    ("TIMEOUT", "failed"),
    ("OUT_OF_MEMORY", "failed"),
    ("CANCELLED by 4102", "failed"),    # slurm names who cancelled it
    ("", "unknown"),                    # empty read teaches nothing
    ("42", "unknown"),
    ("weather is nice", "unknown"),
])
def test_the_first_word_decides(output, expected):
    assert monitors.classify(output) == expected


def test_a_map_extends_the_table_without_replacing_it():
    spec = "green=finished;amber,red=failed"
    assert monitors.classify("GREEN", spec) == "finished"
    assert monitors.classify("amber", spec) == "failed"
    assert monitors.classify("RUNNING", spec) == "running"   # default survives


def test_a_map_clause_naming_an_unknown_status_is_ignored():
    """A typo in the escape hatch must not invent a status the rest cannot read."""
    assert monitors.classify("green", "green=donezo") == "unknown"


def test_the_slurm_sugar_reads_state_rather_than_the_queue():
    """`squeue` forgets a finished job, so a completed run and a lost one look
    identical. `sacct` keeps the state."""
    cmd = monitors.slurm_poll("12345")
    assert "sacct" in cmd and "squeue" not in cmd
    assert "12345" in cmd


def test_a_shell_injection_attempt_in_a_slurm_id_is_quoted():
    cmd = monitors.slurm_poll("1; rm -rf /")
    assert "'1; rm -rf /'" in cmd


# -- polling ---------------------------------------------------------------

def test_a_poll_reads_the_command_output(tmp_path):
    state = _state_file(tmp_path, "RUNNING\n")
    row = {"id": "m1", "poll_cmd": f"cat {state}", "map_spec": ""}
    assert monitors.poll(row, timeout=5) == ("running", "RUNNING")


def test_a_broken_poll_command_is_unknown_rather_than_failed(tmp_path):
    """A command we cannot run says nothing about the work it was watching."""
    row = {"id": "m1", "poll_cmd": "definitely-not-a-command-xyz", "map_spec": ""}
    status, _detail = monitors.poll(row, timeout=5)
    assert status == "unknown"


# -- lifecycle -------------------------------------------------------------

def test_a_monitor_runs_to_its_terminal_status(tmp_path):
    db = _db(tmp_path)
    row = db.create_monitor(monitor_id="m1", job_id=None, poll_cmd="true",
                            interval_sec=1)
    assert row["status"] == "queued"

    changed = db.record_poll("m1", "running", "RUNNING")
    assert changed["status"] == "running"

    changed = db.record_poll("m1", "finished", "COMPLETED")
    assert changed["status"] == "finished"
    assert changed["finished_at"] is not None
    assert db.record_poll("m1", "failed", "FAILED") is None, \
        "a resolved monitor cannot be reopened by a later poll"
    db.close()


def test_only_a_change_of_status_is_worth_an_event(tmp_path):
    """The sweep runs every few seconds for hours; a caller wants the
    transitions, not a heartbeat."""
    db = _db(tmp_path)
    db.create_monitor(monitor_id="m1", job_id=None, poll_cmd="true",
                      interval_sec=1)
    assert db.record_poll("m1", "running", "RUNNING") is not None
    assert db.record_poll("m1", "running", "RUNNING") is None
    assert db.record_poll("m1", "running", "RUNNING") is None
    db.close()


def test_an_unknown_poll_never_moves_a_monitor(tmp_path):
    db = _db(tmp_path)
    db.create_monitor(monitor_id="m1", job_id=None, poll_cmd="true",
                      interval_sec=1)
    db.record_poll("m1", "running", "RUNNING")
    assert db.record_poll("m1", "unknown", "the poll broke") is None
    assert db.monitor("m1")["status"] == "running"
    assert db.monitor("m1")["detail"] == "the poll broke"   # still recorded
    db.close()


def test_a_due_monitor_becomes_due_again_only_after_its_interval(tmp_path):
    db = _db(tmp_path)
    now = time.time()
    db.create_monitor(monitor_id="m1", job_id=None, poll_cmd="true",
                      interval_sec=300, now=now)
    assert [m["id"] for m in db.due_monitors(now)] == ["m1"]
    db.record_poll("m1", "running", "RUNNING", now=now)
    assert db.due_monitors(now) == []
    assert [m["id"] for m in db.due_monitors(now + 301)] == ["m1"]
    db.close()


def test_a_terminal_monitor_is_never_polled_again(tmp_path):
    db = _db(tmp_path)
    now = time.time()
    db.create_monitor(monitor_id="m1", job_id=None, poll_cmd="true",
                      interval_sec=1, now=now)
    db.record_poll("m1", "finished", "COMPLETED", now=now)
    assert db.due_monitors(now + 3600) == []
    db.close()


def test_a_deadline_expires_the_watch_and_says_that_is_what_happened(tmp_path):
    """`expired` rather than `failed`: we stopped watching, which is not a
    claim about the work."""
    db = _db(tmp_path)
    now = time.time()
    db.create_monitor(monitor_id="m1", job_id=None, poll_cmd="true",
                      interval_sec=1, deadline=now - 1, now=now - 10)

    expired = db.expire_monitors(now)
    assert [m["status"] for m in expired] == ["expired"]
    assert db.monitor("m1")["status"] == "expired"
    assert "expired" in MONITOR_TERMINAL
    assert db.expire_monitors(now) == [], "expiry is not repeated"
    db.close()


def test_a_monitor_without_a_deadline_keeps_watching(tmp_path):
    db = _db(tmp_path)
    db.create_monitor(monitor_id="m1", job_id=None, poll_cmd="true",
                      interval_sec=1)
    assert db.expire_monitors(time.time() + 10 ** 6) == []
    db.close()


def test_cancelling_is_idempotent(tmp_path):
    db = _db(tmp_path)
    db.create_monitor(monitor_id="m1", job_id=None, poll_cmd="true",
                      interval_sec=1)
    assert db.close_monitor("m1", "canceled")["status"] == "canceled"
    assert db.close_monitor("m1", "canceled") is None
    db.close()


def test_registering_the_same_id_twice_changes_nothing(tmp_path):
    """A monitor dropped as a file in the job dir is re-read on every sweep, so
    the second registration is the normal case, not an error."""
    db = _db(tmp_path)
    first = db.create_monitor(monitor_id="job:train", job_id="job",
                              poll_cmd="true", interval_sec=60)
    again = db.create_monitor(monitor_id="job:train", job_id="job",
                              poll_cmd="something else", interval_sec=1)
    assert first is not None and again is None
    assert db.monitor("job:train")["poll_cmd"] == "true"
    db.close()


def test_a_monitor_survives_a_restart(tmp_path):
    """The row is the state, which is the reason polling lives in the gateway
    rather than in a detached process."""
    path = str(tmp_path / "m.db")
    db = Database(path)
    db.create_monitor(monitor_id="m1", job_id=None, poll_cmd="true",
                      interval_sec=1)
    db.record_poll("m1", "running", "RUNNING")
    db.close()

    again = Database(path)
    assert again.monitor("m1")["status"] == "running"
    assert [m["id"] for m in again.due_monitors(time.time() + 60)] == ["m1"]
    again.close()


# -- registration bounds --------------------------------------------------

def test_the_interval_floor_and_deadline_ceiling_are_applied(gateway):
    _tune(gateway, monitors_min_interval_sec=30, monitors_max_deadline_sec=100)
    row = register_monitor(gateway, {"poll": "true", "interval_sec": 1,
                                     "deadline_sec": 10 ** 6})
    assert row["interval_sec"] == 30
    assert row["deadline"] <= time.time() + 100 + 1


def test_the_active_bound_refuses_rather_than_queues(gateway):
    _tune(gateway, monitors_max_active=1)
    register_monitor(gateway, {"poll": "true"})
    with pytest.raises(MonitorRefused):
        register_monitor(gateway, {"poll": "true"})


def test_a_disabled_gateway_refuses(gateway):
    _tune(gateway, monitors_enabled=False)
    with pytest.raises(MonitorRefused):
        register_monitor(gateway, {"poll": "true"})


def test_the_slurm_shorthand_expands_at_registration(gateway):
    row = register_monitor(gateway, {"slurm": "12345"})
    assert "sacct" in row["poll_cmd"] and "12345" in row["poll_cmd"]


@pytest.mark.parametrize("text,seconds", [
    ("300", 300), ("90s", 90), ("15m", 900), ("12h", 43200), ("2d", 172800),
    ("", None), ("soon", None), (None, None),
])
def test_durations_are_written_the_way_people_say_them(text, seconds):
    assert _as_seconds(text) == seconds


# -- through the API ------------------------------------------------------

def test_a_monitor_reports_its_transitions_on_the_job_stream(client, auth, gateway,
                                                             tmp_path):
    """The job is usually finished by the time its monitor resolves, so the
    transitions land as post-terminal annotations on the same message stream a
    caller already reads."""
    from gateway.server import _poll_monitors

    job = client.post("/v1/jobs", json={"prompt": "submit the sweep"},
                      headers=auth).json()["id"]
    state = _state_file(tmp_path, "PENDING\n")
    created = client.post("/v1/monitors", headers=auth, json={
        "poll": f"cat {state}", "job": job, "label": "sweep",
        "interval_sec": 1, "result_paths": ["/project/x/RESULTS.md"]})
    assert created.status_code == 201, created.text
    monitor = created.json()["id"]

    state.write_text("RUNNING\n")
    _poll_monitors(gateway)
    state.write_text("COMPLETED\n")
    _poll_monitors(gateway, now=time.time() + 3600)   # past the interval

    assert client.get(f"/v1/monitors/{monitor}",
                      headers=auth).json()["status"] == "finished"
    events = client.get(f"/v1/jobs/{job}/events?after=0&type=message",
                        headers=auth).json()["events"]
    statuses = [e["data"]["monitor_status"] for e in events]
    assert statuses == ["queued", "running", "finished"]
    assert events[-1]["data"]["result_paths"] == ["/project/x/RESULTS.md"]
    assert events[-1]["data"]["status"] == "finished"


def test_the_listing_filters_by_job_and_liveness(client, auth, gateway):
    job = client.post("/v1/jobs", json={"prompt": "one"},
                      headers=auth).json()["id"]
    client.post("/v1/monitors", headers=auth,
                json={"poll": "true", "job": job, "label": "mine"})
    other = client.post("/v1/monitors", headers=auth,
                        json={"poll": "true", "label": "unattached"}).json()
    client.post(f"/v1/monitors/{other['id']}/cancel", headers=auth)

    page = client.get(f"/v1/monitors?job={job}", headers=auth).json()
    assert [m["label"] for m in page["monitors"]] == ["mine"]

    live = client.get("/v1/monitors?active=true", headers=auth).json()
    assert [m["label"] for m in live["monitors"]] == ["mine"]
    done = client.get("/v1/monitors?active=false", headers=auth).json()
    assert [m["label"] for m in done["monitors"]] == ["unattached"]


def test_cancelling_over_http_is_idempotent(client, auth):
    monitor = client.post("/v1/monitors", headers=auth,
                          json={"poll": "true"}).json()["id"]
    first = client.post(f"/v1/monitors/{monitor}/cancel", headers=auth)
    second = client.post(f"/v1/monitors/{monitor}/cancel", headers=auth)
    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "canceled"


def test_a_monitor_needs_something_to_poll(client, auth):
    refused = client.post("/v1/monitors", headers=auth, json={"label": "empty"})
    assert refused.status_code == 422, refused.text


def test_poll_and_slurm_are_mutually_exclusive(client, auth):
    refused = client.post("/v1/monitors", headers=auth,
                          json={"poll": "true", "slurm": "1"})
    assert refused.status_code == 422


def test_an_unknown_status_filter_is_a_typed_error(client, auth):
    refused = client.get("/v1/monitors?status=donezo", headers=auth)
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid_request"


def test_a_missing_monitor_is_a_typed_404(client, auth):
    missing = client.get("/v1/monitors/nope", headers=auth)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_the_exhausted_bound_is_a_typed_conflict(client, auth, gateway):
    _tune(gateway, monitors_max_active=1)
    client.post("/v1/monitors", headers=auth, json={"poll": "true"})
    refused = client.post("/v1/monitors", headers=auth, json={"poll": "true"})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "monitors_exhausted"


def test_a_delegate_can_register_a_monitor_with_a_heredoc(client, auth, gateway):
    """No CLI, no token, no job id: a file in the directory it was handed.

    This is the path that has to work when the delegate cannot reach the
    gateway over HTTP, which on a shared node is not hypothetical.
    """
    from gateway import jobdir
    from gateway.server import _adopt_monitor_drops

    job = client.post("/v1/jobs", json={"prompt": "submit the sweep"},
                      headers=auth).json()["id"]
    root = jobdir.prepare(gateway.cfg.data_dir, job)
    jobdir.publish(root / "monitors" / "train", (
        "poll = echo RUNNING\n"
        "interval = 15m\n"
        "deadline = 12h\n"
        "result = /project/x/RESULTS.md\n"
        "# a comment, and a blank line\n\n"))

    _adopt_monitor_drops(gateway, job)
    _adopt_monitor_drops(gateway, job)            # every sweep re-reads it

    page = client.get(f"/v1/monitors?job={job}", headers=auth).json()
    assert len(page["monitors"]) == 1, "re-reading the drop must not duplicate it"
    monitor = page["monitors"][0]
    assert monitor["label"] == "train"
    assert monitor["poll_cmd"] == "echo RUNNING"
    assert monitor["interval_sec"] == 900
    assert monitor["result_paths"] == ["/project/x/RESULTS.md"]
    assert jobdir.job_id_from(root) == job


def test_a_refused_drop_tells_the_delegate_on_its_own_stream(client, auth, gateway):
    from gateway import jobdir
    from gateway.server import _adopt_monitor_drops

    _tune(gateway, monitors_max_active=0)
    job = client.post("/v1/jobs", json={"prompt": "submit"},
                      headers=auth).json()["id"]
    root = jobdir.prepare(gateway.cfg.data_dir, job)
    jobdir.publish(root / "monitors" / "train", "poll = true\n")

    _adopt_monitor_drops(gateway, job)

    events = client.get(f"/v1/jobs/{job}/events?after=0&type=message",
                        headers=auth).json()["events"]
    assert any("not registered" in (e["data"].get("error") or "")
               for e in events)


def test_a_drop_without_a_poll_is_not_a_monitor(tmp_path):
    from gateway import jobdir

    root = jobdir.prepare(tmp_path, "job-1")
    jobdir.publish(root / "monitors" / "notes", "label = just a note\n")
    assert jobdir.monitor_drops(root) == []
