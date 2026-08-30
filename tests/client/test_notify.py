"""`ab-notify` reports one milestone, and nothing else.

Reporting a *status* used to be its point — it resolved a job id, a url and a
token so `--status finished` could close a parked job. A job ends when its turn
ends now, and long work is a monitor, so what is left worth saying is what
happened along the way.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from client import ab_notify


def _run(tmp_path, *argv, expect=0):
    assert ab_notify.main([*argv, "--job-dir", str(tmp_path)]) == expect


def _milestones(tmp_path):
    return sorted(p.name for p in (tmp_path / "progress").iterdir())


def test_a_message_becomes_one_milestone(tmp_path):
    _run(tmp_path, "--msg", "server up, generating")
    names = _milestones(tmp_path)
    assert len(names) == 1
    assert (tmp_path / "progress" / names[0]).read_text() == "server up, generating"


def test_a_report_id_is_the_milestone_name(tmp_path):
    _run(tmp_path, "--msg", "12/24 done", "--report-id", "sources")
    assert _milestones(tmp_path) == ["sources.md"]


def test_a_retry_with_the_same_report_id_overwrites(tmp_path):
    """What that flag was always for: a retried step reports once, not twice."""
    for text in ("12/24 done", "18/24 done"):
        _run(tmp_path, "--msg", text, "--report-id", "sources")
    assert _milestones(tmp_path) == ["sources.md"]
    assert (tmp_path / "progress" / "sources.md").read_text() == "18/24 done"


def test_unnamed_milestones_never_collide(tmp_path):
    """Two parallel steps reporting in the same second are two milestones."""
    for _ in range(3):
        _run(tmp_path, "--msg", "step done")
    assert len(_milestones(tmp_path)) == 3


def test_unnamed_milestones_sort_the_way_they_happened(tmp_path, monkeypatch):
    """Ingestion is in name order, so the name has to carry the order."""
    stamps = iter(["20260830T090000", "20260830T101500", "20260830T110000"])
    monkeypatch.setattr(ab_notify.time, "strftime",
                        lambda *_a, **_k: next(stamps))
    for _ in range(3):
        _run(tmp_path, "--msg", "step")
    assert _milestones(tmp_path) == sorted(_milestones(tmp_path))
    assert _milestones(tmp_path)[0].startswith("20260830T090000")


def test_a_file_can_carry_the_milestone(tmp_path):
    note = tmp_path / "step.log"
    note.write_text("last 20 lines of the fit\n")
    _run(tmp_path, "--msg-file", str(note), "--report-id", "fit")
    assert (tmp_path / "progress" / "fit.md").read_text() == \
        "last 20 lines of the fit\n"


def test_a_whole_log_is_refused_rather_than_truncated(tmp_path, capsys):
    """A milestone is a note. Silently posting the first 64k of a tarball is
    worse than saying so."""
    fat = tmp_path / "train.log"
    fat.write_bytes(b"x" * (ab_notify.MAX_BYTES + 1))
    _run(tmp_path, "--msg-file", str(fat), expect=2)
    assert "report.md" in capsys.readouterr().err
    assert not (tmp_path / "progress").exists() or not _milestones(tmp_path)


def test_status_is_ignored_and_names_the_remedy(tmp_path, capsys):
    """An sbatch file on a compute node cannot be edited in lockstep with the
    gateway, and exiting non-zero under `set -e` would cost the run rather than
    one milestone. So it reports, and says what to do instead."""
    _run(tmp_path, "--status", "finished", "--msg", "24/24 done")
    assert len(_milestones(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "--status finished is ignored" in err
    assert '"$AB_JOB_DIR/status"' in err


def test_no_status_file_is_written(tmp_path):
    _run(tmp_path, "--status", "finished", "--msg", "done")
    assert not (tmp_path / "status").exists()
    assert not (tmp_path / "report.md").exists()


def test_the_old_transport_flags_are_ignored_rather_than_fatal(tmp_path, capsys):
    _run(tmp_path, "--msg", "up", "--url", "http://localhost:8787",
         "--token", "secret", "--timeout", "10")
    assert len(_milestones(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "--url" in err and "--token" in err


def test_without_a_job_dir_it_says_where_it_expects_to_run(tmp_path, monkeypatch,
                                                           capsys):
    """No discovery and no fallback: $AB_JOB_DIR or it does not run."""
    monkeypatch.delenv("AB_JOB_DIR", raising=False)
    assert ab_notify.main(["--msg", "up"]) == 2
    err = capsys.readouterr().err
    assert "Nothing was reported" in err
    assert "gateway host" in err


def test_the_job_dir_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AB_JOB_DIR", str(tmp_path))
    assert ab_notify.main(["--msg", "up"]) == 0
    assert len(_milestones(tmp_path)) == 1


def test_a_milestone_needs_something_to_say(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ab_notify.main(["--job-dir", str(tmp_path)])
    assert exc.value.code == 2


def test_msg_and_msg_file_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ab_notify.main(["--job-dir", str(tmp_path), "--msg", "a",
                        "--msg-file", "b"])
    assert exc.value.code == 2


def test_it_publishes_whole_or_not_at_all(tmp_path):
    _run(tmp_path, "--msg", "up", "--report-id", "p")
    assert not any(p.name.endswith(".tmp") for p in Path(tmp_path).rglob("*"))
