"""A listed session must be one you can resume into (todo 05).

Claude Code writes a transcript for every subagent, but records the subagent's
turns inline in the *parent*, so the child file keeps only `ai-title` /
`agent-name` metadata. It has a session id and a title and looks resumable;
resuming it lands in an empty conversation. opencode grows the same class of row
from sessions created but never used.

Ten of 106 rows here were that, and three of 1522 on the opencode side.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from gateway import sessions
from gateway.adapters.opencode import OpenCodeAdapter
from gateway.config import AgentConfig


def _real(projects, folder, cwd, session_id, mtime):
    """A transcript with an actual conversation in it."""
    d = projects / folder
    d.mkdir(exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "ai-title", "aiTitle": "meta"}) + "\n"
        + json.dumps({"type": "user", "cwd": cwd,
                      "message": {"role": "user", "content": "hello"}}) + "\n",
        encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _stub(projects, folder, session_id, mtime, title="a subagent"):
    """A metadata-only transcript: exactly what a subagent leaves behind."""
    d = projects / folder
    d.mkdir(exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "ai-title", "aiTitle": title}) + "\n"
        + json.dumps({"type": "agent-name", "agentName": title}) + "\n",
        encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(sessions, "_PROJECTS", projects)
    monkeypatch.setattr(sessions, "_DIR_CWD", {})
    monkeypatch.setattr(sessions, "_HAS_MSG", {})
    return projects


def test_a_stub_is_not_listed_and_does_not_inflate_the_count(store):
    _real(store, "D--proj", r"D:\proj", "aaaaaaaa-0000-0000-0000-000000000000", 1_000)
    _stub(store, "D--proj", "bbbbbbbb-0000-0000-0000-000000000000", 2_000)

    page = sessions.scan(cwd=r"D:\proj")
    assert page.total == 1
    assert [s.session_id for s in page.sessions] == [
        "aaaaaaaa-0000-0000-0000-000000000000"]


def test_stubs_are_dropped_before_the_limit_not_after(store):
    """The whole point: filtering after the slice frees no slots.

    The stubs are newest, so a post-slice filter would hand back 2 rows for a
    limit of 10 while 10 real sessions waited behind them.
    """
    for i in range(10):
        _real(store, "D--proj", r"D:\proj",
              f"{i:08d}-0000-0000-0000-000000000000", mtime=1_000 + i)
    for i in range(8):
        _stub(store, "D--proj", f"{i:08d}-9999-9999-9999-999999999999",
              mtime=5_000 + i)

    page = sessions.scan(limit=10, cwd=r"D:\proj")
    assert page.total == 10
    assert len(page.sessions) == 10
    assert all(s.messages for s in page.sessions)


def test_a_directory_of_only_stubs_leaves_the_dirs_view(store):
    _real(store, "D--real", r"D:\real", "aaaaaaaa-0000-0000-0000-000000000000", 1_000)
    _stub(store, "D--ghost", "bbbbbbbb-0000-0000-0000-000000000000", 9_000)
    # The ghost folder records no cwd either, but the point stands even when one
    # does: nothing there can be resumed, so offering it is a false lead.
    assert [d.cwd for d in sessions.list_dirs()] == [r"D:\real"]


def test_the_two_views_agree_when_stubs_are_present(store):
    for i in range(4):
        _real(store, "D--proj", r"D:\proj",
              f"{i:08d}-0000-0000-0000-000000000000", mtime=1_000 + i)
    for i in range(3):
        _stub(store, "D--proj", f"{i:08d}-9999-9999-9999-999999999999",
              mtime=5_000 + i)

    dirs = {d.cwd: d for d in sessions.list_dirs()}
    assert dirs[r"D:\proj"].sessions == sessions.scan(cwd=r"D:\proj").total == 4


def test_the_dirs_view_points_at_a_real_session_not_a_newer_stub(store):
    _real(store, "D--proj", r"D:\proj", "aaaaaaaa-0000-0000-0000-000000000000", 1_000)
    _stub(store, "D--proj", "bbbbbbbb-0000-0000-0000-000000000000", mtime=9_000)
    # The stub is newest. `latest_session_id` is a resume handle, so it must
    # skip past it rather than offer the freshest useless thing.
    d = sessions.list_dirs()[0]
    assert d.latest_session_id == "aaaaaaaa-0000-0000-0000-000000000000"
    assert d.last_active == 1_000


def test_stubs_piled_on_top_cannot_hide_a_whole_directory(store):
    """A folder's cwd is read from its transcripts, and stubs record none.

    Probing only the newest few files raw means enough fresh subagent stubs
    resolve the folder to no directory at all -- and every real session in it
    disappears from both views together. The largest possible loss in this
    index, from the most ordinary cause.
    """
    _real(store, "D--proj", r"D:\proj", "aaaaaaaa-0000-0000-0000-000000000000", 1_000)
    for i in range(6):
        _stub(store, "D--proj", f"{i:08d}-9999-9999-9999-999999999999",
              mtime=5_000 + i)

    assert [d.cwd for d in sessions.list_dirs()] == [r"D:\proj"]
    assert sessions.scan(cwd=r"D:\proj").total == 1


def test_find_still_resolves_a_stub_by_explicit_id(store):
    _stub(store, "D--proj", "bbbbbbbb-0000-0000-0000-000000000000", 2_000)
    # Listing is a recommendation; a lookup by id is an instruction. Reporting
    # "no such session" about a file that exists sends the caller somewhere
    # worse than telling the truth about an empty one.
    info = sessions.find("bbbbbbbb-0000-0000-0000-000000000000")
    assert info is not None
    assert info.messages == 0


def test_a_stub_that_gains_a_conversation_starts_being_listed(store):
    path = _stub(store, "D--proj", "bbbbbbbb-0000-0000-0000-000000000000", 1_000)
    _real(store, "D--proj", r"D:\proj", "aaaaaaaa-0000-0000-0000-000000000000", 1_000)
    assert sessions.scan(cwd=r"D:\proj").total == 1

    # The emptiness answer is cached against mtime; a file that gains messages
    # gains an mtime with them, so the entry has to invalidate itself.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "cwd": r"D:\proj",
                             "message": {"role": "user", "content": "now real"}}) + "\n")
    os.utime(path, (3_000, 3_000))
    assert sessions.scan(cwd=r"D:\proj").total == 2


# -- opencode ------------------------------------------------------------


@pytest.fixture
def oc(tmp_path, monkeypatch):
    """A store shaped like opencode's: sessions in one table, messages in another."""
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT,"
        "  time_updated INTEGER, time_archived INTEGER);"
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT);")
    rows = [("ses_real1", "D:/proj", "one", 3_000), ("ses_real2", "D:/proj", "two", 2_000),
            ("ses_empty", "D:/proj", "never used", 9_000),
            ("ses_ghost", "D:/ghost", "only session here", 8_000)]
    con.executemany(
        "INSERT INTO session (id, directory, title, time_updated, time_archived)"
        " VALUES (?,?,?,?,NULL)", rows)
    con.executemany("INSERT INTO message (id, session_id) VALUES (?,?)",
                    [("m1", "ses_real1"), ("m2", "ses_real2")])
    con.commit()
    con.close()

    monkeypatch.setattr(OpenCodeAdapter, "_db_path", lambda self: db)
    return OpenCodeAdapter(AgentConfig(
        name="opencode", bin="opencode", dispatch_mode="direct",
        permission_mode="auto", model="", default_cwd=".", allowed_dirs=(),
        timeout_sec=60, max_sessions_in_index=40))


def test_opencode_skips_sessions_with_no_messages(oc):
    page = oc.list_sessions(cwd="D:/proj")
    assert page.total == 2
    assert {s.session_id for s in page.sessions} == {"ses_real1", "ses_real2"}


def test_opencode_dirs_drop_a_directory_of_only_empty_sessions(oc):
    dirs = {d.cwd: d for d in oc.list_dirs()}
    assert set(dirs) == {"D:/proj"}          # D:/ghost held one unused session
    assert dirs["D:/proj"].sessions == 2
    # The empty session is the newest, so this also pins that the dirs view
    # does not advertise it as the thing to continue.
    assert dirs["D:/proj"].latest_session_id == "ses_real1"
