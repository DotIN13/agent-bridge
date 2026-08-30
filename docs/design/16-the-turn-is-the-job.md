# 16 — The turn is the job, unconditionally

**Shipped:** after 0.3.0 — supersedes [11](11-a-turn-is-not-a-job.md) and closes
the question [15](15-reporting-is-a-directory-and-watching-is-a-monitor.md) left
open
**Scope:** `gateway/db.py`, `gateway/worker.py`, `gateway/server.py`,
`gateway/config.py`, `gateway/api_models.py`, `gateway/jobdir.py`,
`gateway/adapters/base.py`, `client/ab.py`, `client/abclient.py`,
`client/ab_notify.py`, `config.example.toml`, `API.md`, `README.md`, both skills.

## The default moved three times; this is the end of it

1. **The turn was the job.** A batch submission read `succeeded` the moment the
   agent stopped talking — hours before the work it started ran.
2. **[11](11-a-turn-is-not-a-job.md) added `expect_report`**, defaulting *on*: a
   job parked in `awaiting_report` when its turn ended, and only a terminal
   report closed it. That merged two lifecycles, which
   [07](07-backend-api-contract.md) had deliberately kept apart.
3. **[15](15-reporting-is-a-directory-and-watching-is-a-monitor.md) reversed the
   default** and kept the opt-in, because it was still the only way to make one
   `ab wait` cover both the turn and the work.
4. **This removes the opt-in.**

What made step 4 safe is that the reasons for 2 all now have better answers.
Progress arrives from `$AB_JOB_DIR` whether or not a row is open. The deliverable
is the final message, plus `report.md` when it outgrows a turn. And the long tail
is a monitor with its own row, its own poll and `ab monitor <id> --wait` —
watched by the gateway rather than promised by an agent.

What was left of `expect_report` was a second, weaker lifecycle for the same
work: a row held open, a `report_deadline`, an `expire_awaiting_reports` sweep,
and a `report_timeout` failure meaning *nobody called in* rather than *the work
failed*. Held open by default, it made a caller's mistake — a brief that never
arranged a report — indistinguishable from work still running, and cost
`report_timeout_sec` (a day) per occurrence.

## The `status` file was already inert

`_close_awaiting_locked(..., parked_only=True)` was the only reader, and it acted
only on a *parked* row. So on the default path, `echo finished >
"$AB_JOB_DIR/status"` already changed nothing about the job. What remained was a
vocabulary — `STATUSES`, `_status_word`, `unknown` plus `raw`, the
`report.md`-as-reason attachment — feeding a mechanism being deleted.

So `status` keeps being *read*, as an ordinary drop: `{"source": "job_dir",
"file": "status", "msg": "finished"}`. A delegate that writes one out of habit is
still heard; the word simply means nothing to the gateway. A job's own status is
decided where it always should have been — `queued` → `running` →
`succeeded`/`failed`/`canceled`, from the turn.

## The decisions worth keeping

**The turn wins, and the delegate cannot veto.** A turn that ends cleanly is a
succeeded job even if its *work* failed. The alternative — reading `status` once
at turn end and letting `failed` override — was considered and rejected as one
more state machine for one more edge. A delegate that must fail its own job can
still end its turn badly (`is_error` or a non-zero exit, which both adapters map
to `failed`), and a caller can still `POST /cancel`.

**The accepted cost, stated so nobody rediscovers it as a bug.** `ab wait` exits
0 on a job whose report says `NOT-RUN`. The signal lives in the report now, which
makes the still-open worker-skill half of [todo/14](../todo/14-the-prompt-contract-both-ways.md)
— a failure in the report's first sentence, a claim of done resting on observed
output — load-bearing rather than merely good practice. Take it next.

**Refused, not ignored.** `expect_report: true` is a typed `400
expect_report_removed` naming monitors. A caller asking to be waited for and
silently not being is the substitution [03](03-direct-mode-resumes-in-wrong-cwd.md)
rules out; the field stays on the DTO so the error can explain itself rather than
reading as a schema complaint. `--expect-report` and `--for` are gone from the
CLI, where an unknown flag is already loud.

**The columns stay.** `jobs.expect_report` and `jobs.report_deadline` are
vestigial and commented as such. Dropping a column in SQLite is a table rebuild,
old rows carry real values, and `_migrate_locked` already tolerates columns
nothing reads.

## What to be careful with

- **`ab wait` has one end.** `_wait_reached`, `WAIT_FOR`, `_report_is_terminal`
  and the `until` parameter are gone; `_may_have_ended` now only re-checks on a
  `stage: "done"` status event.
- **`add_message` is an annotation, always.** It used to be able to close a
  running or parked job — that was 11's mid-turn finish. `cancel` is the verb for
  ending a job now.
- **The sweeper has one beat.** It scans job dirs and polls monitors every 5s;
  the 60s expiry beat went with the deadlines.
- **`tests/backend/test_turn_is_the_job.py`** (was `test_expect_report.py`) is
  the record. It pins that no path reaches `awaiting_report`, that the refusal is
  typed, and that a report annotates without moving a row. The module's own
  history is the warning: the suite stayed green the first time this default
  moved, because nothing asserted it.
