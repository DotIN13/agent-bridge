---
name: agent-bridge-worker
description: How to execute a task dispatched through agent-bridge on this host — your remit versus the caller's, being steered mid-turn, finishing a turn properly, submitting Slurm (or PBS/LSF) work, and reporting progress with ab-notify. Use whenever you are running a brief that arrived via agent-bridge, submitting batch jobs, or doing compute that outlives your turn.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# Working as an agent-bridge worker

You are the **remote agent**. A local session, working with a human, wrote the brief you are executing. `ab job <id>` shows which backend and model you are.

**Flags are in `ab help` and `ab-notify --help`.** This file is the judgement that is not in either.

## Workflow

1. **Inventory, then do the work.** You can see this machine; the caller cannot.
2. **If it hands off to a scheduler or a long background run**, submit it and monitor with bounded probes or a background wait — never a blocking sleep, which just hits the tool timeout.
3. **Always do `ab-notify --status running` at real milestones**, so a long run does not look hung. Every call carries a `--report-id`.
4. **Always do `ab-notify --status finished` — or `failed` — mandatory.** It is what closes the job. Send it from this turn if the work finished here; from inside the batch script if the work outlives your turn. No report, no finish: the job sits until its deadline and is failed as `report_timeout`.

## Your report is the caller's only window

Your last message is stored whole and returned by `ab job <ref>`.

- Answer in plain language, start with the problem/goal, then the steps you took, then the result. Include any numbers, paths, or identifiers that are relevant to the caller's next turn.
- **Self-contained.** Assume they read only this message, with no transcript. Spell out identifiers in full; never "the file above".
- **Evidence inline, not by reference.** A path they cannot open is not evidence.
- **Answer each assumption the brief flagged** — which held, which did not: *"entry point is `src/eval.py` as assumed; no `--workers` flag, it is `--num-workers`"*. Highest-value part of the report: an unrefuted wrong assumption goes straight into their next brief.
- **Volunteer the environment facts that shaped the result** — actual GPU, actual versions, what already existed, what the data really looked like. A number without its conditions invites a wrong conclusion.
- **Name what you could not deliver**, and why. `NOT-RUN`, not a plausible value.
- Include what they cannot get and **would act differently for** — not everything you saw. A full log dump is the same round trip in the other direction, and it costs them context on every later turn.

## Reporting with `ab-notify`

**If you write the batch script, you own the reporting.** Otherwise the only signal is output-file mtimes, which cannot distinguish *queued* from *died before writing* from *filesystem unreachable*.

**Your turn ending does not end the job — you have to say so.** By default a job parks in `awaiting_report` when your turn finishes, and the caller's `ab wait` is blocked on it. Only `ab-notify --status finished` or `--status failed` closes it; progress reports (`running`, `queued`) deliberately do not. A terminal report is honoured the moment it arrives — mid-turn or after the turn parks — so sending `finished` as your last action before ending the turn is fine.

**This applies to every job, not just batch ones.** Answer a question and stop, and the job stays open until its deadline expires and is failed with `report_timeout` — the caller learns nothing except that you went quiet. So:

- **Finish every job with `ab-notify --status finished`**, or `failed` with the reason, as the last thing you do.
- If the work outlives your turn, the *script* owns that call — arrange it on every exit path, including the failure ones (`trap`, `set -e`).
- Report `failed` when the work failed. A `finished` report on broken work is worse than silence, because it closes the job as a success.

```bash
#SBATCH --export=ALL,AB_JOB_ID=<ab job uuid>,AB_DATA_DIR=<gateway data dir>
ab-notify --status running  --msg "server up, generating" --report-id start
ab-notify --status running  --msg "12/24 sources done"    --report-id p12
ab-notify --status finished --msg-file "$RUNS/RESULTS.md" --report-id done
ab-notify --status failed   --msg-file "$RUNS/error.log"  --report-id fail

# Guarantee the finish on every exit path, not just the happy one.
trap 'ab-notify --status failed --msg "script exited $?" --report-id fail' ERR
```

- **`--msg-file` uploads the whole file** — full content, no truncation, sent as a multipart file to the gateway. Point it at your real report or log; `--msg` is for short inline notes. The old `--report` flag is gone (merged into `--msg-file`).
- Call it when work **actually starts** (after the model loads, not at submit), again at real milestones so a long run doesn't look hung, and at finish/fail.
- **Give every call a `--report-id`.** Stable dedup key: a retried step updates its report instead of appending a duplicate.
- **Export both `AB_JOB_ID` and `AB_DATA_DIR`.** The first identifies the job, the second locates `gateway-endpoint.json` and `.token`. Token resolution is `--token` → `$AB_TOKEN` → `<data_dir>/.token`.
- **Never put the token in the script or in `--export`** — job environments appear in scheduler metadata other users can often read.
- **Verify `ab-notify` resolves inside the job environment.** Where it isn't installed, call `<repo>/bin/ab-notify` by absolute path. A script that cannot find it reports nothing and fails silently.
- **Three tiers, all exiting 0 — read stderr for which you got.** HTTP, then shared-filesystem JSONL, then a local temporary JSONL. A **local-only** write is durable but *not ingestible* until moved to the shared messages dir; treat it as "recorded, not delivered" and say so in your final message.
- **The row waits for you by default**, so your `finished` is what closes the job rather than an annotation on an already-closed one. On a job submitted `--no-expect-report` it is the old way round: the row read `succeeded` when your turn ended, and your report lands after. Either way write each message to stand alone, and either way reports survive a gateway restart, since they key on job id.

## Submitting scheduler work

- **Discover, don't assume**: `sinfo -o "%P %a %l %D %G"`, `scontrol show partition <p>`, `scontrol show node <n>`, `sacctmgr -n show assoc user=$USER`.
- **GRES is often untyped** (`gpu:4`, no model), so the partition name does not tell you the GPU. Resolve it from node `ActiveFeatures` and add `--constraint=<type>` on heterogeneous partitions — or land on a V100 when you needed Hopper.
- **Free GPUs ≠ idle node.** Free GPUs with no idle CPUs cannot take your job.
- **`mkdir -p` the `--output` directory from the submitting shell, before `sbatch`.** Slurm opens that file *before* the script runs, so an in-script `mkdir` is too late: the job dies in about a second with no logs, which reads exactly like "never queued".
- **Heartbeat as the first action**, so an empty output directory means *died* rather than *queued*.
- Then **submit, print the job id and `squeue` line, and end your turn.** Do not poll it.
- **Other schedulers are the same shape** — PBS (`qsub`, `$PBS_JOBID`), LSF (`bsub`, `$LSB_JOBID`), or bare `nohup`. Only the flag names differ.

## Environment traps

- **Stale console scripts in `~/.local/bin`** can shadow an env's own launcher, and `conda activate` fixes `python` but not those. Use the env interpreter by absolute path: `ENVPY=/home/$USER/envs/<env>/bin/python; "$ENVPY" -m <module>`.
- **Write results inside the gateway's `allowed_dirs`.** `/tmp` is usually outside it, so anything left there is unreachable by the caller.
- **Compute nodes often have no outbound internet.** Stage inputs on the login node, freeze to disk, have the job read local files only.
- **`module load` before assuming a toolchain**; don't stack a system CUDA on a torch wheel that bundles its own.
- **A container may be the answer** when a shared env is partly broken: `apptainer pull` an official image, bind the data in, leave the env alone.
