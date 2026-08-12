"""The session index is two views, and neither may lose a session (todo 04).

The old shape parsed the newest `limit * 3` transcripts globally and only then
ranked by cwd, so a quiet project vanished whenever a busy one filled the
window. These build exactly that situation and assert it no longer happens.
"""
from __future__ import annotations

import json

import pytest

from gateway import sessions


def _write(projects, folder, cwd, session_id, mtime, *, messages=1):
    d = projects / folder
    d.mkdir(exist_ok=True)
    path = d / f"{session_id}.jsonl"
    rows = [{"type": "ai-title", "aiTitle": "meta"}]          # stub first, as real ones do
    for i in range(messages):
        rows.append({"type": "user", "cwd": cwd,
                     "message": {"role": "user", "content": f"hello {i}"}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    import os
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(sessions, "_PROJECTS", projects)
    monkeypatch.setattr(sessions, "_DIR_CWD", {})
    return projects


def test_a_quiet_directory_survives_a_busy_one(store):
    # One noisy project newer than everything, far exceeding any window.
    for i in range(200):
        _write(store, "D--busy", r"D:\busy", f"{i:08d}-0000-0000-0000-000000000000",
               mtime=9_000_000 + i)
    for i in range(3):
        _write(store, "D--quiet", r"D:\quiet", f"{i:08d}-1111-1111-1111-111111111111",
               mtime=1_000_000 + i)

    page = sessions.scan(limit=40, cwd=r"D:\quiet")
    assert page.total == 3
    assert len(page.sessions) == 3
    assert {s.cwd for s in page.sessions} == {r"D:\quiet"}


def test_cwd_is_an_exact_match_not_a_prefix(store):
    _write(store, "D--proj", r"D:\proj", "aaaaaaaa-0000-0000-0000-000000000000", 1_000)
    _write(store, "D--proj-sub", r"D:\proj\sub",
           "bbbbbbbb-0000-0000-0000-000000000000", 2_000)
    # A sub-project keeps its own index, so a count means what it says.
    assert sessions.scan(cwd=r"D:\proj").total == 1
    assert sessions.scan(cwd=r"D:\proj\sub").total == 1


def test_paths_match_across_separator_and_case(store):
    _write(store, "D--proj", r"D:\proj", "aaaaaaaa-0000-0000-0000-000000000000", 1_000)
    # opencode records forward slashes for the same directory; Windows also
    # differs in case. Both must find it.
    assert sessions.scan(cwd="D:/proj").total == 1
    assert sessions.scan(cwd=r"d:\PROJ").total == 1


def test_unresumable_ids_are_not_advertised(store):
    _write(store, "D--proj", r"D:\proj", "aaaaaaaa-0000-0000-0000-000000000000", 1_000)
    orphan = store / "D--proj" / (
        "bbbbbbbb-0000-0000-0000-000000000000.orphaned-1786-abc.jsonl")
    orphan.write_text('{"type":"user","cwd":"D:\\\\proj",'
                      '"message":{"role":"user","content":"x"}}\n', encoding="utf-8")
    page = sessions.scan(cwd=r"D:\proj")
    # Its stem is not a session id anything can resume, so it must not inflate
    # the count or appear as a resumable row.
    assert page.total == 1
    assert [s.session_id for s in page.sessions] == [
        "aaaaaaaa-0000-0000-0000-000000000000"]


def test_dirs_view_is_complete_and_counts_are_real(store):
    for i in range(200):
        _write(store, "D--busy", r"D:\busy", f"{i:08d}-0000-0000-0000-000000000000",
               mtime=9_000_000 + i)
    for i in range(3):
        _write(store, "D--quiet", r"D:\quiet", f"{i:08d}-1111-1111-1111-111111111111",
               mtime=1_000_000 + i)

    dirs = {d.cwd: d for d in sessions.list_dirs()}
    # Both projects present, however lopsided the activity.
    assert set(dirs) == {r"D:\busy", r"D:\quiet"}
    assert dirs[r"D:\busy"].sessions == 200
    assert dirs[r"D:\quiet"].sessions == 3
    assert dirs[r"D:\busy"].latest_title


def test_metadata_only_folders_are_skipped(store):
    d = store / "D--stub"
    d.mkdir()
    (d / "cccccccc-0000-0000-0000-000000000000.jsonl").write_text(
        json.dumps({"type": "ai-title", "aiTitle": "no cwd here"}) + "\n",
        encoding="utf-8")
    # No transcript records a cwd, so the folder maps to no directory and there
    # is nothing to offer a caller.
    assert sessions.list_dirs() == []


def test_cursor_walks_every_session_exactly_once(store):
    for i in range(25):
        _write(store, "D--proj", r"D:\proj",
               f"{i:08d}-0000-0000-0000-000000000000", mtime=1_000 + i)
    seen, cursor, pages = [], None, 0
    while True:
        page = sessions.scan(limit=7, cwd=r"D:\proj", cursor=cursor)
        seen += [s.session_id for s in page.sessions]
        cursor = page.next_cursor
        pages += 1
        if not cursor or pages > 20:
            break
    assert len(seen) == 25
    assert len(set(seen)) == 25          # no repeats, no skips across pages


def test_bad_cursor_is_rejected(store):
    with pytest.raises(ValueError):
        sessions.scan(cwd=r"D:\proj", cursor="not-a-cursor")
