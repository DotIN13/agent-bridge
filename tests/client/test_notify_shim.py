"""`ab-notify` is a shim over the job dir now, and must not break a live script.

An sbatch file already on disk on five deployments cannot be edited by this
commit, so the old flags keep working and translate into the writes a delegate
would make by hand.
"""
from __future__ import annotations

from pathlib import Path

from client import ab_notify


def _run(tmp_path, *argv, expect=0):
    assert ab_notify.main([*argv, "--job-dir", str(tmp_path)]) == expect


def test_a_progress_call_becomes_a_milestone_named_after_its_report_id(tmp_path):
    """`--report-id` was the retry-dedup key; as a file name it still is."""
    _run(tmp_path, "--status", "running", "--msg", "12/24 done",
         "--report-id", "p12")
    assert (tmp_path / "progress" / "p12.md").read_text() == "12/24 done"
    assert (tmp_path / "status").read_text().strip() == "running"


def test_a_retried_progress_call_overwrites_rather_than_piling_up(tmp_path):
    for text in ("12/24 done", "18/24 done"):
        _run(tmp_path, "--status", "running", "--msg", text, "--report-id", "p")
    assert (tmp_path / "progress" / "p.md").read_text() == "18/24 done"
    assert len(list((tmp_path / "progress").iterdir())) == 1


def test_a_finish_carries_the_deliverable_into_the_report(tmp_path):
    results = tmp_path / "RESULTS.md"
    results.write_text("# numbers\n42\n")
    _run(tmp_path, "--status", "finished", "--msg-file", str(results))
    assert (tmp_path / "report.md").read_text() == "# numbers\n42\n"
    assert (tmp_path / "status").read_text().strip() == "finished"


def test_a_failure_says_failed_and_keeps_the_reason(tmp_path):
    _run(tmp_path, "--status", "failed", "--msg", "CUDA OOM at step 200")
    assert (tmp_path / "status").read_text().strip() == "failed"
    assert "CUDA OOM" in (tmp_path / "report.md").read_text()


def test_a_status_only_call_still_reports(tmp_path):
    _run(tmp_path, "--status", "finished")
    assert (tmp_path / "status").read_text().strip() == "finished"
    assert not (tmp_path / "report.md").exists()


def test_the_old_flags_are_ignored_rather_than_fatal(tmp_path, capsys):
    """An old script passes --url/--token/--timeout; dying on argv would turn a
    reporting change into a lost report."""
    _run(tmp_path, "--status", "running", "--msg", "up",
         "--url", "http://localhost:8787", "--token", "secret",
         "--timeout", "10", "--messages-dir", "/shared/messages")
    assert (tmp_path / "status").read_text().strip() == "running"
    err = capsys.readouterr().err
    assert "--url" in err and "--token" in err


def test_the_directory_is_rebuilt_from_the_old_job_id_and_data_dir(tmp_path):
    """The sbatch line in the README exports AB_JOB_ID and AB_DATA_DIR, so a
    script that never learned about AB_JOB_DIR still lands in the right place."""
    assert ab_notify.main(["--status", "finished", "--job-id", "job-1",
                           "--data-dir", str(tmp_path)]) == 0
    assert (tmp_path / "reports" / "job-1" / "status").read_text().strip() == \
        "finished"


def test_with_nothing_to_write_to_it_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AB_JOB_DIR", raising=False)
    monkeypatch.delenv("AB_DATA_DIR", raising=False)
    assert ab_notify.main(["--status", "finished"]) == 2
    assert "Nothing was reported" in capsys.readouterr().err


def test_it_publishes_whole_or_not_at_all(tmp_path):
    _run(tmp_path, "--status", "running", "--msg", "x", "--report-id", "p")
    assert not any(p.name.endswith(".tmp")
                   for p in Path(tmp_path).rglob("*"))
