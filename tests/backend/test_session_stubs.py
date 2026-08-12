"""A listed session must be one you can resume into (todo 05, then 10).

Three ways a transcript exists without anything having happened in it:

- **Subagent files.** Claude Code writes one per subagent but records its turns
  inline in the *parent*, so the child keeps only `ai-title`/`agent-name`.
- **Slash-command residue.** `/login` or `/resume` stores the caveat, the
  command and its stdout as three separate `user` records, so counting records
  says 3 messages while the session holds no prompt and no reply.
- **opencode sessions** created and never used.

10, 11 and 3 rows respectively on the store these were measured against. Each
looks resumable from the outside and lands somewhere empty.
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


def _slash_only(projects, folder, cwd, session_id, mtime, command="/resume"):
    """What `/login` or `/resume` leaves behind: three user records, no prompt.

    The caveat, the command and its stdout are each stored as a `user` record,
    so this counts as 3 messages while holding nothing anyone can continue.
    """
    d = projects / folder
    d.mkdir(exist_ok=True)
    path = d / f"{session_id}.jsonl"
    rows = [
        {"type": "user", "cwd": cwd, "message": {"role": "user", "content":
            "<local-command-caveat>Caveat: The messages below were generated by "
            "the user while running local commands.</local-command-caveat>"}},
        {"type": "user", "cwd": cwd, "message": {"role": "user", "content":
            f"<command-name>{command}</command-name><command-args></command-args>"}},
        {"type": "user", "cwd": cwd, "message": {"role": "user", "content":
            "<local-command-stdout>Login successful</local-command-stdout>"}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_slash_command_residue_is_not_a_session(store):
    _real(store, "D--proj", r"D:\proj", "aaaaaaaa-0000-0000-0000-000000000000", 1_000)
    _slash_only(store, "D--proj", r"D:\proj",
                "bbbbbbbb-0000-0000-0000-000000000000", 2_000)
    # 3 `user` records and not one of them a prompt. Counting records rather
    # than content would list this and label it "(no prompt captured)".
    page = sessions.scan(cwd=r"D:\proj")
    assert page.total == 1
    assert [s.session_id for s in page.sessions] == [
        "aaaaaaaa-0000-0000-0000-000000000000"]


def test_a_command_that_does_real_work_is_kept(store):
    """The escape hatch: prose is not the only evidence a session happened.

    A custom slash command can drive real work with no human text at all. None
    exist on the store this was measured against, so nothing here would have
    caught it -- which is exactly why it is pinned.
    """
    d = store / "D--proj"
    d.mkdir(exist_ok=True)
    path = d / "cccccccc-0000-0000-0000-000000000000.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in [
        {"type": "user", "cwd": r"D:\proj", "message": {"role": "user", "content":
            "<command-name>/deploy</command-name>"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "make deploy"}}]}},
    ]), encoding="utf-8")
    os.utime(path, (2_000, 2_000))

    page = sessions.scan(cwd=r"D:\proj")
    assert [s.session_id for s in page.sessions] == [
        "cccccccc-0000-0000-0000-000000000000"]


def test_dirs_title_is_the_first_human_prompt_not_a_command_wrapper(store):
    d = store / "D--proj"
    d.mkdir()
    path = d / "aaaaaaaa-0000-0000-0000-000000000000.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in [
        {"type": "user", "cwd": r"D:\proj", "message": {"role": "user", "content":
            "<local-command-caveat>Caveat: …</local-command-caveat>"}},
        {"type": "user", "cwd": r"D:\proj", "message": {"role": "user", "content":
            "<command-name>/clear</command-name>"}},
        {"type": "user", "cwd": r"D:\proj",
         "message": {"role": "user", "content": "fix the parser"}},
    ]), encoding="utf-8")
    os.utime(path, (1_000, 1_000))

    assert sessions.list_dirs()[0].latest_title == "fix the parser"


def test_non_ascii_titles_survive_the_index(store):
    """Transcripts are UTF-8; the reader must say so.

    `open(path, "r")` uses the locale encoding, which on Windows is cp1252, so
    every CJK title came back double-encoded -- the gateway served `c3a4 c2bd
    c2a0` where the transcript held `e4 bd a0` (你). Every reader here now names
    the encoding explicitly.
    """
    d = store / "D--proj"
    d.mkdir()
    path = d / "aaaaaaaa-0000-0000-0000-000000000000.jsonl"
    path.write_text(
        json.dumps({"type": "user", "cwd": r"D:\proj",
                    "message": {"role": "user", "content": "你先了解一下 café 🎉"}},
                   ensure_ascii=False) + "\n",
        encoding="utf-8")
    os.utime(path, (1_000, 1_000))

    assert sessions.scan(cwd=r"D:\proj").sessions[0].title == "你先了解一下 café 🎉"
    assert sessions.list_dirs()[0].latest_title == "你先了解一下 café 🎉"


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
