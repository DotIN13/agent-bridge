# 15 — Reporting is a directory, and watching is a monitor

**Shipped:** after 0.3.0 — supersedes [11](11-a-turn-is-not-a-job.md)'s default
and restores [07](07-backend-api-contract.md)'s separation
**Scope:** `gateway/jobdir.py`, `gateway/monitors.py`, `gateway/db.py`,
`gateway/server.py`, `gateway/worker.py`, both adapters, `client/ab.py`,
`client/abclient.py`, `client/ab_monitor.py`, `client/ab_notify.py`, both skills.

## The problem, in the order it was found

`ab-notify` resolved its job id from `--job-id` or `$AB_JOB_ID`.
`grep -rn AB_JOB_ID gateway/` matched **nothing**: the adapters spawned the child
with the gateway's inherited environment and appended only an attachments block
to its system prompt. So a delegate learned its own job id only if the caller
happened to paste the uuid into the brief.

Read that against the two defaults it met. `expect_report` defaulted **true**, so
every job parked in `awaiting_report` when its turn ended, and
`report_timeout_sec` defaulted to **86400**. The default path for a worker that
followed `skills/agent-bridge-worker` exactly, and was not hand-fed its uuid,
was therefore: do the work, try to close the job, fail to identify itself, park
for a day, and fail with `report_timeout`. The caller learns only that the
worker went quiet.

Two more costs sat behind that one:

- **The reporter needed a url and a token from a compute node**, with a
  three-tier fallback (HTTP → shared JSONL → local JSONL) and a standing warning
  never to put the token in `--export`. Three delivery paths is three ways for a
  report to be somewhere nobody reads.
- **It merged two lifecycles.** A batch job's fate became the coding-agent job's
  status — the thing 07 decided against and 11 opted back into.

## What shipped

**Reporting is a directory.** Every job is handed
`<data_dir>/reports/<job-id>` in `$AB_JOB_DIR`, created before the agent starts,
with `progress/` and `monitors/` already in it. Files and words, not JSON:

```bash
echo "12/24 sources done" > "$AB_JOB_DIR/progress/020-sources.md"
cp "$RUNS/RESULTS.md"       "$AB_JOB_DIR/report.md"
echo finished             > "$AB_JOB_DIR/status"
```

Every readable file becomes one `message` event through the existing
`external_reports` dedup, keyed by relative path **and** content digest. That
asymmetry is the useful part: rewriting a file with new content reports again,
rewriting it unchanged does not, so `status` can go `running` → `finished` and a
retried step can overwrite its own milestone without piling up duplicates.

**A job is its turn again.** `expect_report` defaults false. The opt-in stays,
unchanged and fully tested, because it is still the only way to make one
`ab wait` cover both the turn and the work it started.

**Work that outlives the turn is a monitor**: its own row, a poll command the
delegate authors, run by the gateway on a timer, resolving hours later without
holding a job open. Only transitions emit events, on the creating job's stream —
post-terminal annotations, exactly as 07 allows.

## The load-bearing decisions

**Why files and not JSONL.** The JSONL fallback already worked, and
`ingest_messages` already parsed it. But it needs valid JSON from bash, and the
one path that has to work when everything else about a run has gone sideways
should not have quoting rules. `echo finished > "$AB_JOB_DIR/status"` has none.

**A job dir cannot end a running turn.** `_close_awaiting_locked` grew a
`parked_only` flag, used only by the job dir. A file in a directory is not the
promise a call is: a delegate may write `finished` about a step and keep working,
and ending its row underneath it would strand a live agent. A parked job is a
different matter — a file is precisely what it is waiting for. The HTTP path
keeps 11's semantics, where a mid-turn finish is a deliberate act.

**Not `<data_dir>/jobs/`.** `files.promote_staging` renames a whole staging
directory into `<files_dir>/jobs/<job_id>` and raises if it exists, so a
deployment that pointed `[files] dir` at the data dir would have collided on
every job with an attachment. `reports/` sits beside `messages/` instead. A test
pins it, because the collision would have appeared only under one config.

**The gateway polls; the delegate authors the command.** `STATUS_MAP` has
Slurm's state names in it the way `cluster.py` runs `sinfo` — the mechanism is
generic, the table is specific. `sacct --format=State` needs no mapping, `echo
finished` works identically, and `--map` covers anything else. **No `squeue`**:
it forgets a job the moment it leaves the queue, so a completed run and a lost
one both read as empty output. An empty or unmapped read is `unknown` and never
moves a monitor, because a failed poll is not news about the work.

**Polling lives in the gateway, not a detached process**, so the state is a row
and survives a restart — the same reasoning that made a parked job survive one.
The cost is that the gateway executes a delegate-authored command on a schedule,
after the job that wrote it has finished. That is the same trust level as a job,
which already runs arbitrary code in `allowed_dirs` under a noninteractive
permission mode, but it is longer-lived, which is what `[monitors]` bounds:
`max_active`, `min_interval_sec`, `poll_timeout_sec`, `max_deadline_sec`, and
`enabled = false` to refuse monitors outright.

**Two doors, one registration path.** `POST /v1/monitors` is the caller's; a
key-value file in `$AB_JOB_DIR/monitors/` is the delegate's, and `ab-monitor`
only writes that file. Both go through `register_monitor`, so the bounds cannot
be true of one and not the other. The file name is the watch's identity, which
is what makes a sweep that re-reads the directory every few seconds register
nothing twice. A refused drop is reported on the job's own event stream — the
only channel the delegate will read.

**`expired` is not `failed`.** A passed deadline means the gateway stopped
watching. `ab monitor` shows that word; the event's report-shaped `status` reads
`failed`, because to a caller waiting on the work it is not good news.

## What to be careful with

- **The sweeper does two jobs on two beats.** Job dirs and monitor polls every
  5s; report-deadline expiry every 60s, as before. It publishes to the bus, so a
  follower streaming since the turn started sees a milestone without
  reconnecting.
- **Nothing reaps `reports/`.** One directory per job, holding text. Bounded per
  job (`MAX_FILES`, `MAX_FILE_BYTES`) and not bounded across jobs.
- **`ab-notify` is a milestone reporter now.** It shipped in this change as a
  shim translating its old flags into job-dir writes, and was rewritten a commit
  later to do one thing: put a note in `progress/`. Dropping `--status` is what
  made it small — nothing needs telling that the work is over, so what is left
  worth saying is what happened along the way. It names the file so milestones
  sort the way they happened, and `--report-id` keeps a retried step to one
  milestone. `--status` and the old transport flags are accepted and ignored
  rather than fatal: an sbatch file already on a compute node cannot be edited in
  lockstep with the gateway, and exiting non-zero under `set -e` would cost the
  run rather than one milestone. It does *not* write `status`, so closing a
  parked `--expect-report` job stays an explicit `echo finished >
  "$AB_JOB_DIR/status"`. Narrowing it to `$AB_JOB_DIR` -- no `--job-id` +
  `--data-dir` rebuild -- then left the `[messages]` JSONL ingest with no writer
  at all, so that went too: the `[messages]` config block, `messages_dir`, and
  `db.ingest_messages` with its own bounds, batching and cross-transport dedup
  identity. A reader nothing writes is worse than nothing, because no test can
  reach it from the outside. `POST /v1/jobs/{ref}/message` remains the HTTP
  equivalent for a caller that wants immediate delivery.
- **`tests/backend/test_expect_report.py`** is the record of what the opt-in
  still guarantees. Its own history is the warning: the suite stayed green the
  last time this default moved, because nothing asserted it.
