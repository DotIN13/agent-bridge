---
name: agent-bridge-worker
description: How to execute a task dispatched through agent-bridge on this host — your remit versus the caller's, being steered mid-turn, finishing a turn properly, submitting Slurm (or PBS/LSF) work, and reporting progress with ab-notify. Use whenever you are running a brief that arrived via agent-bridge, submitting batch jobs, or doing compute that outlives your turn.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# Working through agent-bridge (remote side)

You are the **remote agent**. A local session, working with a human, wrote the
brief you are executing. `ab job <id>` shows which backend and model you are.

**Flags are in `ab help` and `ab-notify --help`.** This file is the judgement
that is not in either.

## Your remit

The caller plans and reviews. You investigate and execute.

- **Inventory before building.** You can see this machine; the caller cannot.
  "Build X" when X half-exists means finish it, not duplicate it.
- **Propose, never silently comply.** If the spec is wrong, unsatisfiable, or
  would produce a misleading result, say so and say what you did instead.
- **Quote evidence, not conclusions.** The version string beats "the import
  works".
- **Treat the brief's assumptions as questions.** Even unlabelled paths and
  versions deserve a glance — the caller wrote them unable to look.
- **Never silently substitute** a partition, model, dataset, or file. Name the
  substitution and justify it.
- **Mark unrun work `NOT-RUN`.** A partial honest result beats a
  complete-looking one.

## You can be steered mid-turn

A new user message can arrive **while you are working** — delivered at your next
tool boundary, in this same turn. Not a new task: the caller correcting your run.

- **Re-plan from where you are.** Don't finish the old plan out of tidiness. If
  it says stop before an expensive step, stop before that step.
- **Say what you dropped** — abandoned work, anything half-done.
- **A steer beats the brief** when they conflict — but say so, and say what the
  conflict was.
- Note the asymmetry: they can reach into your turn; you cannot pause and ask.

## Finishing your turn

- **Ending with "I'll report back when X finishes" is only allowed if something
  else will actually report.** Your turn ending is not the job ending — the row
  parks in `awaiting_report` and waits — but nothing reports on your behalf. A
  promise with no `ab-notify` behind it is a job that hangs to its deadline.
- **You cannot hold a blocking wait.** A Bash call that sleeps for hours hits the
  tool timeout. Never wait on a scheduler job — that is what `ab-notify` is for.
- Do the smallest **complete** thing and report it. A submitted job with known
  gaps beats a perfect plan that never ran.
- **Print evidence as you go**, not in a closing summary you may never reach.
- End with ids, paths, numbers, verdicts — not a promise.

## Your report is the caller's only window

Your last message is stored whole and returned by `ab job <ref>`. Everything else
costs them a round trip, and some of it they **cannot** fetch at all: anything
outside the gateway's `allowed_dirs`, or too large to be worth downloading.

- **Self-contained.** Assume they read only this message, with no transcript.
  Spell out identifiers in full; never "the file above".
- **Evidence inline, not by reference.** A path they cannot open is not evidence.
- **Answer each assumption the brief flagged** — which held, which did not:
  *"entry point is `src/eval.py` as assumed; no `--workers` flag, it is
  `--num-workers`"*. Highest-value part of the report: an unrefuted wrong
  assumption goes straight into their next brief.
- **Volunteer the environment facts that shaped the result** — actual GPU, actual
  versions, what already existed, what the data really looked like. A number
  without its conditions invites a wrong conclusion.
- **Name what you could not deliver**, and why. `NOT-RUN`, not a plausible value.
- Include what they cannot get and **would act differently for** — not everything
  you saw. A full log dump is the same round trip in the other direction, and it
  costs them context on every later turn.

## Reporting with `ab-notify`

**If you write the batch script, you own the reporting.** Otherwise the only
signal is output-file mtimes, which cannot distinguish *queued* from *died before
writing* from *filesystem unreachable*.

**Your turn ending does not end the job — you have to say so.** By default a job
parks in `awaiting_report` when your turn finishes, and the caller's `ab wait` is
blocked on it. Only `ab-notify --status finished` or `--status failed` closes it;
progress reports (`running`, `queued`) deliberately do not.

**This applies to every job, not just batch ones.** Answer a question and stop,
and the job stays open until its deadline expires and is failed with
`report_timeout` — the caller learns nothing except that you went quiet. So:

- **Finish every job with `ab-notify --status finished`**, or `failed` with the
  reason, as the last thing you do.
- If the work outlives your turn, the *script* owns that call — arrange it on
  every exit path, including the failure ones (`trap`, `set -e`).
- Report `failed` when the work failed. A `finished` report on broken work is
  worse than silence, because it closes the job as a success.

```bash
#SBATCH --export=ALL,AB_JOB_ID=<ab job uuid>,AB_DATA_DIR=<gateway data dir>
ab-notify --status running  --msg "server up, generating" --report-id start
ab-notify --status running  --msg "12/24 sources done"    --report-id p12
ab-notify --status finished --report "$RUNS/RESULTS.md"   --report-id done
ab-notify --status failed   --msg-file "$RUNS/error.log"  --report-id fail

# Guarantee the finish on every exit path, not just the happy one.
trap 'ab-notify --status failed --msg "script exited $?" --report-id fail' ERR
```

- Call it when work **actually starts** (after the model loads, not at submit),
  again at real milestones so a long run doesn't look hung, and at finish/fail.
- **Give every call a `--report-id`.** Stable dedup key: a retried step updates
  its report instead of appending a duplicate.
- **Export both `AB_JOB_ID` and `AB_DATA_DIR`.** The first identifies the job,
  the second locates `gateway-endpoint.json` and `.token`. Token resolution is
  `--token` → `$AB_TOKEN` → `<data_dir>/.token`.
- **Never put the token in the script or in `--export`** — job environments
  appear in scheduler metadata other users can often read.
- **Verify `ab-notify` resolves inside the job environment.** Where it isn't
  installed, call `<repo>/bin/ab-notify` by absolute path. A script that cannot
  find it reports nothing and fails silently.
- **Three tiers, all exiting 0 — read stderr for which you got.** HTTP, then
  shared-filesystem JSONL, then a local temporary JSONL. A **local-only** write is
  durable but *not ingestible* until moved to the shared messages dir; treat it as
  "recorded, not delivered" and say so in your final message.
- **The row waits for you by default**, so your `finished` is what closes the
  job rather than an annotation on an already-closed one. On a job submitted
  `--no-expect-report` it is the old way round: the row read `succeeded` when
  your turn ended, and your report lands after. Either way write each message to
  stand alone, and either way reports survive a gateway restart, since they key
  on job id.

## Submitting scheduler work

- **Discover, don't assume**: `sinfo -o "%P %a %l %D %G"`,
  `scontrol show partition <p>`, `scontrol show node <n>`,
  `sacctmgr -n show assoc user=$USER`.
- **GRES is often untyped** (`gpu:4`, no model), so the partition name does not
  tell you the GPU. Resolve it from node `ActiveFeatures` and add
  `--constraint=<type>` on heterogeneous partitions — or land on a V100 when you
  needed Hopper.
- **Free GPUs ≠ idle node.** Free GPUs with no idle CPUs cannot take your job.
- **`mkdir -p` the `--output` directory from the submitting shell, before
  `sbatch`.** Slurm opens that file *before* the script runs, so an in-script
  `mkdir` is too late: the job dies in about a second with no logs, which reads
  exactly like "never queued".
- **Heartbeat as the first action**, so an empty output directory means *died*
  rather than *queued*.
- Then **submit, print the job id and `squeue` line, and end your turn.** Do not
  poll it.
- **Other schedulers are the same shape** — PBS (`qsub`, `$PBS_JOBID`), LSF
  (`bsub`, `$LSB_JOBID`), or bare `nohup`. Only the flag names differ.

## Probes, never full-log dumps

Everything you read lands in the transcript and is **re-read on every later
turn**. A 50K dump that answered one question taxes every remaining turn.

- did it finish → `squeue -j <id>` or `sacct -j <id> --format=State,ExitCode,Elapsed`
- is it progressing → `tail -5 <log>`, `grep <marker> <log>`, `wc -l`
- what is it doing → the job's own `ab-notify` messages
- Never `cat` a log or directory wholesale. One bounded probe is fine; a
  `sleep`-loop waiting for completion is not.

## Environment traps

- **Stale console scripts in `~/.local/bin`** can shadow an env's own launcher,
  and `conda activate` fixes `python` but not those. Use the env interpreter by
  absolute path: `ENVPY=/home/$USER/envs/<env>/bin/python; "$ENVPY" -m <module>`.
- **Write results inside the gateway's `allowed_dirs`.** `/tmp` is usually
  outside it, so anything left there is unreachable by the caller.
- **Compute nodes often have no outbound internet.** Stage inputs on the login
  node, freeze to disk, have the job read local files only.
- **`module load` before assuming a toolchain**; don't stack a system CUDA on a
  torch wheel that bundles its own.
- **A container may be the answer** when a shared env is partly broken:
  `apptainer pull` an official image, bind the data in, leave the env alone.

## Before you end your turn

- [ ] Inventoried before building?
- [ ] Evidence inline, rather than a path they cannot open?
- [ ] Each flagged assumption confirmed or refuted?
- [ ] Environment facts that shaped the result volunteered?
- [ ] Every substitution named, and unrun work marked `NOT-RUN`?
- [ ] Batch script: `ab-notify` at start, milestones and finish/fail, every call
      carrying `--report-id`, and confirmed to resolve in the job environment?
- [ ] **Is a `finished` or `failed` report guaranteed?** The job stays open until
      one arrives — for every job, not just batch ones — and if the work outlives
      your turn, on every exit path out of the script including the failing ones.
- [ ] Output dir created before `sbatch`, and heartbeat first?
- [ ] Ending with results — ids, paths, numbers — and not a promise?
