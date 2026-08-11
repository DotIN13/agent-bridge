"""A resumed session runs in its own recorded directory (docs/todo/03).

The live path is easy to check by hand; the fallbacks are not, and every one of
them keeps the old behaviour rather than failing a resume, so a silent
regression here would look exactly like the bug being fixed.
"""
from __future__ import annotations

from gateway.adapters.base import Event, resume_cwd
from gateway.config import AgentConfig


def _cfg(tmp_path):
    allowed = tmp_path / "projects"
    (allowed / "molly").mkdir(parents=True)
    (allowed / "bridge").mkdir(parents=True)
    return AgentConfig(
        name="claude", bin="claude", dispatch_mode="direct",
        permission_mode="bypassPermissions", model="",
        default_cwd=str(allowed / "bridge"), allowed_dirs=(str(allowed),),
        timeout_sec=0, max_sessions_in_index=5, models=())


def _collect():
    events: list[Event] = []
    return events, events.append


def test_recorded_cwd_wins_over_the_requested_one(tmp_path):
    cfg = _cfg(tmp_path)
    session_home = str(tmp_path / "projects" / "molly")
    events, emit = _collect()
    result = resume_cwd(cfg, "sess-1", session_home,
                        str(tmp_path / "projects" / "bridge"), emit)
    assert result == session_home
    # Substituting a directory the caller did not name must be visible.
    status = [e for e in events if e.type == "status"]
    assert len(status) == 1
    assert status[0].data["cwd_source"] == "session"
    assert status[0].data["replaced"].endswith("bridge")


def test_no_event_when_the_cwd_is_already_right(tmp_path):
    cfg = _cfg(tmp_path)
    home = str(tmp_path / "projects" / "molly")
    events, emit = _collect()
    assert resume_cwd(cfg, "sess-1", home, home, emit) == home
    assert [e for e in events if e.type == "status"] == []


def test_session_without_a_recorded_cwd_keeps_the_fallback(tmp_path):
    cfg = _cfg(tmp_path)
    fallback = str(tmp_path / "projects" / "bridge")
    for recorded in (None, ""):
        events, emit = _collect()
        assert resume_cwd(cfg, "sess-1", recorded, fallback, emit) == fallback
        assert any(e.data.get("cwd_source") == "fallback" for e in events)


def test_cwd_outside_allowed_dirs_falls_back_rather_than_escaping(tmp_path):
    cfg = _cfg(tmp_path)
    fallback = str(tmp_path / "projects" / "bridge")
    outside = str(tmp_path / "elsewhere")
    events, emit = _collect()
    # A session recorded somewhere the gateway does not permit must not become a
    # way to escape allowed_dirs, and must not fail the resume either.
    assert resume_cwd(cfg, "sess-1", outside, fallback, emit) == fallback
    assert any("not usable here" in (e.data.get("reason") or "") for e in events)


def test_find_is_not_limited_by_the_index_window(tmp_path, monkeypatch):
    # `find` must not be built on the bounded scan: a session outside the window
    # would read as absent and silently fall back to a default, which is the bug.
    from gateway import sessions

    projects = tmp_path / "projects"
    (projects / "D--x-old").mkdir(parents=True)
    target = projects / "D--x-old" / "aaaaaaaa-0000-0000-0000-000000000001.jsonl"
    target.write_text('{"type":"user","cwd":"D:\\\\x\\\\old",'
                      '"message":{"role":"user","content":"hi"}}\n',
                      encoding="utf-8")
    for index in range(30):                     # bury it under newer transcripts
        noise = projects / "D--x-new"
        noise.mkdir(exist_ok=True)
        (noise / f"bbbbbbbb-0000-0000-0000-0000000000{index:02d}.jsonl").write_text(
            '{"type":"user","cwd":"D:\\\\x\\\\new",'
            '"message":{"role":"user","content":"hi"}}\n', encoding="utf-8")
    monkeypatch.setattr(sessions, "_PROJECTS", projects)

    assert sessions.find("aaaaaaaa-0000-0000-0000-000000000001").cwd == "D:\\x\\old"
    assert sessions.find("no-such-session") is None
