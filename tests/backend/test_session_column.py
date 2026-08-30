"""One session column, written while the job is still running.

There used to be three -- `requested_session`, `chosen_session` and
`forked_session` -- from the dispatcher modes, where an agent chose which
session to use and the three were genuinely different things. Under `direct`
dispatch only one of them is ever written, and a caller had to be told which of
the three to read.
"""
from __future__ import annotations

import sqlite3

from gateway.adapters.base import Event, RunResult
from gateway.bus import Bus
from gateway.config import AgentConfig, Config
from gateway.db import Database
from gateway.worker import WorkerPool


def make_config(tmp_path) -> Config:
    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)
    agent = AgentConfig(
        name="claude", bin="claude", dispatch_mode="direct",
        permission_mode="bypassPermissions", model="",
        default_cwd=str(allowed), allowed_dirs=(str(allowed),),
        timeout_sec=0, max_sessions_in_index=1, models=())
    return Config(
        host="127.0.0.1", port=0, token="x", concurrency=1,
        db_path=str(tmp_path / "gw.db"), data_dir=str(tmp_path),
        files_dir=str(tmp_path / "files"), cluster_enabled=False,
        agents={"claude": agent})


def test_a_running_job_says_which_session_it_is_in(tmp_path, monkeypatch):
    """The id is known from the first thing the agent says, not at the end.

    It used to be written only in the terminal commit, so the column was empty
    for the whole life of a run -- which is exactly when somebody is looking.
    """
    cfg = make_config(tmp_path)
    db = Database(cfg.db_path)
    job = db.create_job(agent="claude", prompt="go", cwd=str(tmp_path / "allowed"),
                        session=None, permission_mode=None, model=None)
    pool = WorkerPool(cfg, db, Bus())

    seen: list[str | None] = []

    class Adapter:
        def run(self, _spec, emit):
            # What an adapter does on its very first record.
            emit(Event("status", {"session_id": "ses_live"}))
            seen.append(db.get_job(job)["session"])
            return RunResult(ok=True, result="done", session="ses_live")

    monkeypatch.setattr("gateway.worker.build_adapter", lambda _cfg: Adapter())
    pool._run_job(job)

    assert seen == ["ses_live"], "the row was still empty while the job ran"
    assert db.get_job(job)["session"] == "ses_live"
    db.close()


def test_a_run_that_raises_keeps_the_session_it_had(tmp_path, monkeypatch):
    """The failure path has no RunResult to read, so the live write is the only
    thing standing between a crash and losing the id."""
    cfg = make_config(tmp_path)
    db = Database(cfg.db_path)
    job = db.create_job(agent="claude", prompt="go", cwd=str(tmp_path / "allowed"),
                        session=None, permission_mode=None, model=None)
    pool = WorkerPool(cfg, db, Bus())

    class Adapter:
        def run(self, _spec, emit):
            emit(Event("status", {"session_id": "ses_doomed"}))
            raise RuntimeError("the agent fell over")

    monkeypatch.setattr("gateway.worker.build_adapter", lambda _cfg: Adapter())
    pool._run_job(job)

    row = db.get_job(job)
    assert row["status"] == "failed"
    assert row["session"] == "ses_doomed"
    db.close()


def test_the_caller_s_own_session_is_there_before_the_run_starts(tmp_path):
    """A pinned session is the answer until the run reports a better one, so a
    queued job is not blank either."""
    cfg = make_config(tmp_path)
    db = Database(cfg.db_path)
    job = db.create_job(agent="claude", prompt="go", cwd=str(tmp_path / "allowed"),
                        session="ses_pinned", permission_mode=None, model=None)
    assert db.get_job(job)["session"] == "ses_pinned"
    db.close()


def test_three_old_columns_become_one_and_are_not_served(tmp_path):
    """An existing database keeps its rows and loses its ambiguity."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, agent TEXT NOT NULL,
            prompt TEXT NOT NULL, cwd TEXT, requested_session TEXT,
            chosen_session TEXT, forked_session TEXT, permission_mode TEXT,
            model TEXT, result TEXT, error TEXT, cost_usd REAL,
            created_at REAL NOT NULL, started_at REAL, finished_at REAL);
    """)
    rows = [
        # forked wins over chosen wins over requested, as the old computed
        # property decided.
        ("all-three", "req", "chose", "forked", "forked"),
        ("no-fork", "req", "chose", None, "chose"),
        ("request-only", "req", None, None, "req"),
        ("none-at-all", None, None, None, None),
    ]
    for jid, req, chosen, forked, _ in rows:
        conn.execute(
            "INSERT INTO jobs (id,status,agent,prompt,requested_session,"
            "chosen_session,forked_session,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (jid, "succeeded", "claude", "p", req, chosen, forked, 1.0))
    conn.commit()
    conn.close()

    db = Database(str(path))
    served = {j["id"]: j for j in db.list_jobs(10)}
    db.close()

    for jid, _, _, _, want in rows:
        assert served[jid].get("session") == want, jid
        for dead in ("requested_session", "chosen_session", "forked_session"):
            assert dead not in served[jid], f"{dead} is still on the wire"

    # The old columns stay on disk: dropping one wants a recent SQLite and
    # cannot be undone, and nothing reads them any more.
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    conn.close()
    assert "session" in cols
    assert "forked_session" in cols


def test_migrating_twice_does_not_undo_the_first(tmp_path):
    """A gateway restarts; the run that happened in between must survive it."""
    cfg = make_config(tmp_path)
    db = Database(cfg.db_path)
    job = db.create_job(agent="claude", prompt="go", cwd=str(tmp_path / "allowed"),
                        session=None, permission_mode=None, model=None)
    db.set_job_session(job, "ses_written")
    db.close()

    again = Database(cfg.db_path)
    assert again.get_job(job)["session"] == "ses_written"
    again.close()


def test_the_api_serves_a_running_job_s_session(client, auth, gateway):
    """What `ab submit --await-session` polls.

    It reads `GET /v1/jobs/{ref}` every 400ms waiting for `session` to appear,
    on the documented understanding that "the id lands with the agent's init
    record a second or two in". That was only true of the event stream: the row
    it actually polls was not written until the job reached a terminal state, so
    the wait ran to its 30s timeout and told the caller to go and look it up.
    """
    job = gateway.db.create_job(
        agent="claude", prompt="go", cwd=gateway.cfg.agents["claude"].default_cwd,
        session=None, permission_mode=None, model=None)
    gateway.db.mark_running(job)
    gateway.db.set_job_session(job, "ses_midflight")

    body = client.get(f"/v1/jobs/{job}", headers=auth).json()
    assert body["status"] == "running"
    assert body["session"] == "ses_midflight"

    listed = client.get("/v1/jobs", headers=auth).json()["jobs"]
    assert [j["session"] for j in listed if j["id"] == job] == ["ses_midflight"]


def test_the_session_id_is_taken_from_the_result_when_no_init_record_arrived():
    """The init record is where this normally comes from. A run whose row says
    null is one the caller cannot follow up with `--session`, and the alignment
    turn the client skill prescribes depends on reading it back — so take it
    from whichever record carries it."""
    from gateway.adapters.base import RunResult
    from gateway.adapters.claude import ClaudeAdapter
    from gateway.config import AgentConfig

    adapter = ClaudeAdapter(AgentConfig(
        name="claude", bin="claude", dispatch_mode="direct",
        permission_mode="bypassPermissions", model="", default_cwd="/tmp",
        allowed_dirs=("/tmp",), timeout_sec=0, max_sessions_in_index=5,
        models=()))
    res = RunResult(ok=False)
    adapter._handle_record(
        {"type": "result", "subtype": "success", "result": "done",
         "session_id": "aaaaaaaa-0000-0000-0000-000000000000",
         "is_error": False}, lambda _event: None, res, False)
    assert res.session == "aaaaaaaa-0000-0000-0000-000000000000"


def test_an_init_record_still_wins_over_the_result():
    """Order matters: init arrives first and is authoritative, so a later
    result must not overwrite it (a nested run's id would be wrong here)."""
    from gateway.adapters.base import RunResult
    from gateway.adapters.claude import ClaudeAdapter
    from gateway.config import AgentConfig

    adapter = ClaudeAdapter(AgentConfig(
        name="claude", bin="claude", dispatch_mode="direct",
        permission_mode="bypassPermissions", model="", default_cwd="/tmp",
        allowed_dirs=("/tmp",), timeout_sec=0, max_sessions_in_index=5,
        models=()))
    res = RunResult(ok=False)
    events = []
    adapter._handle_record(
        {"type": "system", "subtype": "init",
         "session_id": "11111111-0000-0000-0000-000000000000"},
        events.append, res, False)
    adapter._handle_record(
        {"type": "result", "result": "done",
         "session_id": "22222222-0000-0000-0000-000000000000"},
        events.append, res, False)
    assert res.session == "11111111-0000-0000-0000-000000000000"
