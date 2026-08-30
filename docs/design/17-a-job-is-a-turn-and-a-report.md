# 17 — A job is a turn *and* a report

**Shipped:** after 0.3.0 — amends [16](16-the-turn-is-the-job.md), which had made
the turn the whole of it
**Scope:** `gateway/db.py`, `gateway/worker.py`, `gateway/server.py`,
`gateway/jobdir.py`, `gateway/config.py`, `gateway/adapters/claude.py`,
`API.md`, `README.md`, `config.example.toml`, both skills.

## What changed, and why it is not a repeat of 11

    queued -> running -> waiting -> succeeded | failed | canceled

16 made a row terminal the moment the turn ended, which fixed a real defect —
every job used to hold itself open waiting for something to call in — but it
accepted a cost: a delegate could end its turn having produced nothing, and the
row read `succeeded`. This makes the deliverable part of the definition. A job is
`succeeded` when its turn has ended **and** `$AB_JOB_DIR/report.md` exists.

That is a post-turn non-terminal state again, so the comparison with
[11](11-a-turn-is-not-a-job.md)'s `awaiting_report` is the thing to be clear
about:

| | `awaiting_report` (11) | `waiting` (17) |
|---|---|---|
| waits on | a call from a compute node the gateway cannot see | a file in a directory the gateway already scans |
| who acts | a batch script, hours later, if it was written correctly | the delegate, which is still running |
| the process | gone | **alive**, and steerable |
| default window | 86400s — a day | 1800s — a grace period |
| on expiry | `report_timeout`: "nobody called in" | `report_missing`: "the deliverable is absent" |

The old one could not be short, because the thing it waited for was hours away.
This one can be, because the delegate is supposed to have written the report
*before* ending its turn: reaching `waiting` at all means it did not, and 30
minutes is time to notice and fix that, not time for the work to happen.

## The load-bearing mechanics

**The result record ends the turn, not the run.** In `direct` mode the child
reads JSON lines from stdin and stays alive after answering; closing stdin is
what ends the run. The adapter used to close it at the result record. Now it
emits `status stage: "turn_end"` and lets the worker decide:

- report already there (the short-job path, and the long-job path with a
  preliminary report) → close stdin, the run winds up, the row goes `succeeded`;
- no report → `mark_waiting`, hold stdin open. The agent is alive: a steer wakes
  it, and it can still write the file.

The sweeper closes that handle when the report appears, which is how the worker
learns to wind up. That is why `pool.steering(job_id)` had to stay published.

**Every backend gets the same rule, not the same mechanism.** `opencode` and the
dispatcher modes have no streaming stdin, so their child exits with the turn and
`turn_end` never fires. The worker applies the identical check at run end: no
report means `waiting`, and the deadline resolves it. The row semantics do not
depend on the backend; only "can the agent still act while waiting" does.

**A `waiting` job releases its session claim.** Reaching the end of `_run_job`
means the process has exited, so holding the claim would block every in-place
resume of that session for the whole grace window. The claim is held only while
the worker is genuinely inside the run — which, in the hold-open case, it is.

**Concurrency went 2 → 16.** A job now holds its worker slot while `waiting`,
because the thread is blocked reading the agent's stdout. Two slots and two slow
reporters would stall the queue. They are subprocesses on a login node, so the
example config says plainly to mind what the box can take.

**`report_deadline` came back.** 16 had marked the column vestigial; it is live
again, for a different deadline. No migration either way, which is the argument
for having left it in place.

**An empty `report.md` does not count.** A zero-byte file is a delegate that
started to write and did not, which is exactly the case worth waiting out.

## The two paths the skills now teach

- **Under an hour**: stay with the work, write the real report, end the turn.
- **An hour or more**: submit, `ab-monitor add`, write a **preliminary** report
  naming the monitor and where the results will land, end the turn. The job
  closes; the watch carries the tail.

Both, or neither is any use: a monitor with no report leaves a job sitting in
`waiting` until it fails, and a report with no monitor loses the ending. So a
monitor's terminal transition is now a full record — `terminal: true`, the label,
the poll command, the last output, when it resolved, how long it was watched, and
the result paths — on the stream of the job that started it, readable months
later without the scheduler's own logs.

## What to be careful with

- **`waiting` is not in `TERMINAL`**, so `ab wait` keeps waiting through it and
  restart recovery leaves those rows alone (`reconcile_startup` fails `running`
  only). A `waiting` row whose gateway restarted has no process behind it and
  will expire on its deadline — correct, if slower than a failure.
- **A second turn refreshes the deadline.** A steer woke the agent, so it is
  working again and the clock should restart.
- **`tests/backend/test_waiting.py`** is the record: both worker paths, the
  predicate, the deadline, the claim release, and that the answer from the turn is
  kept on the row while the report is still coming.
- **One existing test had to be told about this.**
  `test_cancel_during_terminal_commit_cannot_be_falsely_accepted` needs the
  terminal commit to happen, so it now writes a report first. A test that patches
  `finish_job_with_events` and waits for it is a good tripwire for exactly this
  kind of change.
