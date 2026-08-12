# 11 — A turn is not a job

**Shipped:** after 0.3.0. Partly supersedes
[07's batch lifecycle decision](07-backend-api-contract.md).
**Scope:** `gateway/db.py`, `gateway/worker.py`, `gateway/server.py`,
`gateway/config.py`, `gateway/__main__.py`, `client/ab.py`,
`client/abclient.py`, both skills.

## Problem

A job's status described the **agent's turn**, and a turn that submits to a
scheduler ends in seconds. So a batch submission read `succeeded` the moment the
agent stopped talking — hours before the work it started actually ran, and
regardless of whether that work then succeeded, failed, or never started.

`ab wait` inherited the same meaning: it waited for the turn, which is almost
never the thing the caller wanted to wait for.

## What shipped

`POST /v1/jobs` takes `expect_report: true` (`ab submit --expect-report`). Such a
job parks in **`awaiting_report`** when its turn succeeds instead of going
terminal, and only `ab-notify --status finished|failed` closes it.

| | |
|---|---|
| `finished` | job → `succeeded`, `finished_at` = when the work ended |
| `failed` | job → `failed`, the report's `msg` becomes the error |
| `running`, `queued` | progress; the job stays parked |
| nothing, ever | `report_timeout` after `worker.report_timeout_sec` |

`ab wait` then blocks on the actual work with no new flag, which is the whole
point: the fix belongs in what the status *means*, not in a second verb.

## The decisions inside it

**Opt-in, not inferred.** The gateway could guess from a `running` report
arriving before turn end. It does not: a job that hangs forever because the
gateway inferred something is far worse than one that finishes early, and 07 was
right that the two status machines must not merge *silently*. `--expect-report`
is the caller saying so.

**A new status, not `running`.** The obvious cheap move is to leave the row
`running`, and it is wrong. Three separate things read `running` as "an agent is
alive on this": steering, the `session_busy` gate, and restart recovery. A parked
job has no process, so it must be distinguishable — the row is open, but nothing
is running.

**Only a successful turn parks.** If the turn failed or was canceled there is no
reason to believe anything was submitted, and waiting for a report nobody will
send is worse than reporting the failure now.

**The claim and the slot are released anyway.** `_release_locked` already ran on
every path, which is what makes the new status safe: the session goes back to
idle and can be resumed, and the worker slot frees, even though the row stays
open. Had it been coupled to terminality, one parked job would have pinned its
session against every later resume.

**Restart recovery leaves parked jobs alone.** It selects `status='running'`,
and an `awaiting_report` row has no local process to have lost — its scheduler
work is still out there and will report whenever it lands. Failing those on
restart would destroy exactly the state this status exists to keep. That
exclusion is now load-bearing and says so in the docstring.

**A deadline, because silence is not success.** Without one, a scheduler job that
dies quietly leaves the row open forever and `ab wait` blocks on a report nobody
will send. Default 86400s; `0` means wait indefinitely, which is a real choice
and not a default. Swept on a timer rather than at read time, so a job nobody
polls still reaches a terminal state.

**Both transports close the job.** The first cut only closed on the HTTP path.
On a cluster that is the *wrong* one to pick: compute nodes routinely cannot
reach the gateway, so `ab-notify` drops the report on shared storage, and a job
closed only over HTTP would have waited out its whole deadline with the finish
sitting in a file on disk. Found by running it, not by reading it — the loopback
gateway published no URL, so the live test took the fallback path by accident.

**A report cannot reopen a terminal job.** Only parked rows are closed by a
report. Moving a row that already finished would break the monotonicity `wait`
and SSE depend on, and a follow that had closed would never learn.

**The gateway refuses to start without `ab-notify`.** A job that parks can only
be closed by the reporter, so a gateway that cannot resolve it hands out jobs
that can never finish — and it fails at the far end, hours later, on a compute
node, as silence. `shutil.which`, falling back to `bin/ab-notify` in the
checkout, so a source checkout is a valid install. It prints the path it
resolved.

## Verified end to end

Against a real gateway on an isolated port, not just in unit tests:

```
submit --expect-report   -> status awaiting_report, finished_at null,
                            the turn's own result preserved
ab wait (3s)             -> exit 4, still running
gateway restarted        -> still parked, not failed as stale
ab-notify --status finished (fell back to shared jsonl, loopback gateway)
                         -> succeeded, reason "batch_report"
ab wait                  -> exit 0
```

The restart is proved by the *reason*: the job closed as `batch_report`, not
`gateway_restarted`.

## Left open

The turn's result and the batch's result now share one `result` field, and the
turn's wins because it was written first. For a job whose real output is a file
named in the finish report, `ab job` shows "submitted as 12345" rather than
anything about the run. Worth a `report` field on the job row if that becomes
annoying in practice.
