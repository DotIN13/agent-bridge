---
name: agent-bridge-worker
description: How to execute a task dispatched through agent-bridge on this host — your remit versus the caller's, being steered mid-turn, finishing a turn properly, submitting Slurm (or PBS/LSF) work, and reporting progress with ab-notify. Use whenever you are running a brief that arrived via agent-bridge, submitting batch jobs, or doing compute that outlives your turn.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# Working through agent-bridge (remote side)

You are the **remote agent**. A local session, working with a human, wrote the
brief you are executing. `ab job <id>` shows which backend and model you are.

## Your remit

The caller plans and reviews. You investigate and execute.

- **Inventory before building.** Check what exists — modules, scripts, data,
  prior runs. You can see this machine; the caller cannot. "Build X" when X
  half-exists means finish it, not duplicate it.
- **Propose.** If the spec is wrong, unsatisfiable, or would produce a
  misleading result, say so and say what you did instead. Never silently comply
  with something broken.
- **Quote evidence, not conclusions.** Paste the output that supports the claim.
  "The import works" is worth less than the version string.
- **Never silently substitute** a different partition, model, dataset, or file.
  Name the substitution and justify it.
- **Mark unrun work `NOT-RUN`.** A partial honest result beats a
  complete-looking one.

## You can be steered mid-turn

A new user message can arrive **while you are working** — sent with `ab steer`,
delivered at your next tool boundary, in this same turn. Not a new task, not a
new session: the caller watching your run and correcting it.

- **Re-plan from where you are.** Don't finish the old plan out of tidiness. If
  it says stop before an expensive step, stop before that step.
- **Say what you dropped** — abandoned work, anything half-done.
- **A steer beats the brief** when they conflict — but say so, and say what the
  conflict was.

Note the asymmetry: the caller can reach into your turn; you cannot pause and
ask them anything.

## Finishing your turn

**A turn ending "I'll report back when X finishes" is a failed turn.** The
gateway records turn-end as task completion. There is no "still working" state.

**You cannot hold a blocking wait.** A non-interactive turn ends when generation
stops, and a Bash call that sleeps for hours hits the tool timeout. Never wait
on a scheduler job — that is what `ab-notify` is for.

- Do the smallest **complete** thing and report it. A submitted job with known
  gaps beats a perfect plan that never ran.
- **Print evidence as you go**, not in a closing summary you may never reach.
- End with concrete results — ids, paths, numbers, verdicts.

**Your last message is the deliverable.** It is stored whole and returned by
`ab job <ref>`, so it is the one thing guaranteed to reach the caller. Write it
to stand alone: outcome first, then detail. Anything only in your tool calls
costs them a round trip.

## Reporting with `ab-notify`

**If you write the batch script, you own the reporting.** Otherwise the only
signal is output-file mtimes, which cannot distinguish *queued* from *died
before writing* from *filesystem unreachable*.

```bash
#SBATCH --export=ALL,AB_JOB_ID=<ab job uuid>,AB_DATA_DIR=<gateway data dir>
command -v ab-notify >/dev/null || export PATH="<repo>/bin:$PATH"

ab-notify --status running  --msg "server up, generating"  --report-id start
ab-notify --status running  --msg "12/24 sources done"     --report-id p12
ab-notify --status finished --report "$RUNS/RESULTS.md"     --report-id done
ab-notify --status failed   --msg-file "$RUNS/error.log"    --report-id fail
```

| Flag | Use |
|---|---|
| `--status running` | when work **actually starts** — after the model loads, not at submit |
| `--status running` again | at real milestones, so a long run doesn't look hung |
| `--status finished --report PATH` | name the artifact to open |
| `--status failed --msg-file PATH` | error text from a file, not a shell argument |
| `--report-id ID` | **give every call one.** Stable dedup key: a retried step updates its report instead of appending a duplicate |

**Verify `ab-notify` resolves inside the job environment.** It is an installed
console script; where it isn't, call `<repo>/bin/ab-notify` by absolute path. A
script that cannot find it reports nothing and fails silently.

**`AB_JOB_ID` and `AB_DATA_DIR` must both be exported.** The first identifies the
job; the second locates `gateway-endpoint.json` and `.token`. Token resolution is
`--token` → `$AB_TOKEN` → `<data_dir>/.token`.

**Never put the token in the script or in `--export`** — job environments appear
in scheduler metadata other users can often read.

**Three tiers, all exiting 0 — read stderr for which you got.** HTTP, then
shared-filesystem JSONL, then a local temporary JSONL. A **local-only** write is
durable but *not ingestible* until moved to the shared messages dir; treat it as
"recorded, not delivered" and say so in your final message.

**Your reports are post-terminal annotations.** Your turn ends when you submit,
so the caller's job row may read `succeeded` — and their follow may have closed —
hours before your `finished` report lands. That is by design. Write each message
to stand alone; don't assume anyone is watching a live stream. Reports also
survive a gateway restart, since they key on job id even though a restart marks
your own row `failed`.

## Submitting scheduler work

### Discover, don't assume

```bash
sinfo -o "%P %a %l %D %G"                       # partitions
scontrol show partition <p> | grep -E "AllowAccounts|AllowQos|State"
sacctmgr -n show assoc user=$USER format=account,partition%20
scontrol show node <n> | grep -E "ActiveFeatures|CfgTRES|AllocTRES"
```

- **GRES is often untyped** (`gpu:4`, no model), so the partition name does not
  tell you the GPU. Resolve it from node `ActiveFeatures` and add
  `--constraint=<type>` on heterogeneous partitions — or land on a V100 when you
  needed Hopper.
- **Free GPUs ≠ idle node.** Free GPUs with no idle CPUs cannot take your job.

### Skeleton, and the two ordering traps

```bash
#!/bin/bash
#SBATCH --job-name=...  --partition=...  --account=...  --qos=...
#SBATCH --gres=gpu:1  --cpus-per-task=8  --mem=100G  --time=02:00:00
#SBATCH --output=<RUNS>/job-%j.out
#SBATCH --export=ALL,AB_JOB_ID=...,AB_DATA_DIR=...
set -euo pipefail
ab-notify --status running --msg "started on $(hostname)" --report-id start
```

- **`mkdir -p` the `--output` directory from the submitting shell, before
  `sbatch`.** Slurm opens that file *before* the script runs, so an in-script
  `mkdir` is too late: the job dies in about a second with no logs, which reads
  exactly like "never queued".
- **Heartbeat as the first action**, so an empty output directory means *died*
  rather than *queued*.

Then **submit, print the job id and `squeue` line, and end your turn.** Do not
poll it.

### Probes, never full-log dumps

Everything you read lands in the transcript and is **re-read on every later
turn**. A 50K dump that answered one question taxes every remaining turn.

| Question | Probe |
|---|---|
| did it finish | `squeue -j <id> -o "%.8T %.10M %.20R"` or `sacct -j <id> --format=State,ExitCode,Elapsed` |
| is it progressing | `tail -5 <log>`, `grep <marker> <log>`, `wc -l` |
| what is it doing | the job's own `ab-notify` messages — one line, actually informative |

Never `cat` a log or a directory wholesale. One bounded probe is fine; a
`sleep`-loop waiting for completion is not.

**Other schedulers** are the same shape — PBS (`qsub`, `$PBS_JOBID`), LSF
(`bsub`, `$LSB_JOBID`), or bare `nohup`: create the output dir first, heartbeat
first, derive scratch from the scheduler's job-id variable, call `ab-notify`
from inside the script. Only the flag names differ.

## Environment traps

- **Stale console scripts in `~/.local/bin`** can shadow an env's own launcher,
  and `conda activate` fixes `python` but not those. Use the env interpreter by
  absolute path: `ENVPY=/home/$USER/envs/<env>/bin/python; "$ENVPY" -m <module>`.
- **Write results inside the gateway's `allowed_dirs`.** `/tmp` is usually
  outside it, so anything left there is unreachable by the caller.
- **Compute nodes often have no outbound internet.** Stage inputs on the login
  node, freeze to disk, have the job read local files only.
- **`module load` before assuming a toolchain** (`node`, `cuda`, `apptainer`);
  don't stack a system CUDA on a torch wheel that bundles its own.
- **A container may be the answer** when a shared env is partly broken:
  `apptainer pull` an official image, bind the data in, leave the env alone.

## Before you end your turn

- [ ] Inventoried before building?
- [ ] Quoted evidence rather than asserting conclusions?
- [ ] Named every substitution and deviation?
- [ ] Marked unrun work `NOT-RUN`?
- [ ] Does the batch script `ab-notify` at start, milestones, and finish/fail?
- [ ] Does every `ab-notify` call carry a `--report-id`?
- [ ] Confirmed `ab-notify` resolves inside the job environment?
- [ ] Created the output dir before `sbatch`, and heartbeat first?
- [ ] Ending with results — ids, paths, numbers — and not a promise?
