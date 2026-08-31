"""`ab-monitor` writes a file; `ab monitors` reads the gateway.

The asymmetry is the design. The delegate runs on the gateway host and drops a
registration into the directory it was already handed -- no job id, no url, no
token, and it works when HTTP does not. The caller is on a laptop and talks to
the API like everything else.

Both halves stay in one file even though the code is now split across `worker/`
and `client/`, because the asymmetry *is* the subject: a test for one half that
cannot see the other stops being able to state it.
"""
from __future__ import annotations

import pytest

from client import ab
from worker import monitor as ab_monitor


def _add(tmp_path, *argv):
    assert ab_monitor.main(["add", "--job-dir", str(tmp_path), *argv]) == 0
    return sorted((tmp_path / "monitors").iterdir())


def test_a_slurm_watch_is_written_as_readable_key_values(tmp_path, capsys):
    files = _add(tmp_path, "--slurm", "12345", "--label", "Nightly Train",
                 "--interval", "15m", "--result", "/project/x/RESULTS.md")
    assert [p.name for p in files] == ["nightly-train"]
    body = files[0].read_text()
    assert "poll = sacct -n -X -j 12345 --format=State" in body
    assert "interval = 15m" in body
    assert "result = /project/x/RESULTS.md" in body
    assert "squeue" not in body        # squeue forgets a finished job
    assert "watching" in capsys.readouterr().out


def test_the_label_is_the_identity_so_re_registering_is_not_a_duplicate(tmp_path):
    _add(tmp_path, "--slurm", "1", "--label", "train")
    files = _add(tmp_path, "--slurm", "2", "--label", "train")
    assert len(files) == 1, "the same label is the same watch"
    assert "-j 2 " in files[0].read_text()


def test_two_unlabelled_watches_are_two_watches(tmp_path):
    _add(tmp_path, "--poll", "true")
    assert len(_add(tmp_path, "--poll", "true")) == 2


def test_a_watch_is_published_whole_or_not_at_all(tmp_path):
    """The gateway may scan mid-write, so nothing lands under a live name."""
    files = _add(tmp_path, "--poll", "true", "--label", "x")
    assert not any(p.name.endswith(".tmp") for p in files)


def test_result_paths_repeat(tmp_path):
    files = _add(tmp_path, "--poll", "true", "--label", "x",
                 "--result", "/a", "--result", "/b")
    assert "result = /a,/b" in files[0].read_text()


def test_a_shell_metacharacter_in_a_slurm_id_is_quoted(tmp_path):
    files = _add(tmp_path, "--slurm", "1; rm -rf /", "--label", "x")
    assert "'1; rm -rf /'" in files[0].read_text()


def test_without_a_job_dir_it_says_so_rather_than_guessing(monkeypatch):
    monkeypatch.delenv("AB_JOB_DIR", raising=False)
    with pytest.raises(SystemExit) as exc:
        ab_monitor.main(["add", "--poll", "true"])
    assert "AB_JOB_DIR" in str(exc.value)


def test_the_job_dir_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AB_JOB_DIR", str(tmp_path))
    assert ab_monitor.main(["add", "--poll", "true", "--label", "x"]) == 0
    assert (tmp_path / "monitors" / "x").is_file()


def test_a_watch_needs_something_to_poll(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ab_monitor.main(["add", "--job-dir", str(tmp_path)])
    assert exc.value.code == 2


def test_poll_and_slurm_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ab_monitor.main(["add", "--job-dir", str(tmp_path),
                         "--poll", "true", "--slurm", "1"])
    assert exc.value.code == 2


# -- the caller's side ----------------------------------------------------

def test_the_client_defaults_to_live_watches_only():
    args = ab.build_parser().parse_args(["monitors"])
    assert args.all is False
    assert ab.build_parser().parse_args(["monitors", "--all"]).all is True


def test_cancelling_a_watch_is_explicit():
    args = ab.build_parser().parse_args(["monitor", "m1"])
    assert args.cancel is False
    assert ab.build_parser().parse_args(["monitor", "m1", "--cancel"]).cancel


def test_waiting_on_a_watch_stops_at_a_terminal_status(monkeypatch):
    from client import abclient

    rows = iter([{"id": "m1", "status": "queued"},
                 {"id": "m1", "status": "running"},
                 {"id": "m1", "status": "finished"}])
    client = abclient.Client.__new__(abclient.Client)
    monkeypatch.setattr(client, "get_monitor", lambda _id: next(rows),
                        raising=False)
    monkeypatch.setattr(abclient.time, "sleep", lambda _s: None)

    row = client.wait_monitor("m1", timeout=60, poll_interval=0)
    assert row["status"] == "finished"
    assert "timed_out_waiting" not in row


def test_a_wait_that_times_out_leaves_the_watch_running(monkeypatch):
    from client import abclient

    client = abclient.Client.__new__(abclient.Client)
    monkeypatch.setattr(client, "get_monitor",
                        lambda _id: {"id": "m1", "status": "running"},
                        raising=False)
    monkeypatch.setattr(abclient.time, "sleep", lambda _s: None)

    row = client.wait_monitor("m1", timeout=0, poll_interval=0)
    assert row["timed_out_waiting"] is True
    assert row["status"] == "running"
