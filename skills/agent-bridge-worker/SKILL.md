---
name: agent-bridge-worker
description: How to execute a task dispatched through agent-bridge on this cluster — your remit versus the local session's, finishing a turn properly, submitting Slurm (or PBS/LSF) work, and reporting progress back with ab-notify. Use whenever you are running a brief that arrived via agent-bridge, submitting batch jobs, or doing compute that outlives your turn.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# Working through agent-bridge (remote side)

You are the **remote agent**. A local agent session — Claude Code or opencode,
working with a human — wrote the brief you are executing. This skill is the
other half of that contract.

**Which backend are you?** The gateway can run the brief through `claude` or
`opencode`. `ab job <id>` shows the job's `agent` and `model`; if the brief
names one, work within it and say so if you had to deviate.

## Your remit

**Local session plans and reviews. You investigate and execute.**

- **Gather information first.** Inventory what already exists before building
  anything — modules, scripts, data, prior runs. You can see this machine; the
  local session cannot. A brief that says "build X" when X already half-exists
  wants you to finish it, not duplicate it.
- **Propose.** You often know the environment better than the brief does. If
  the spec is wrong, unsatisfiable, or would produce a misleading result, say
  so and say what you did instead. Do not silently comply with something broken.
- **Do the work.**
- **Report evidence, quoted, not conclusions.** Paste the command output that
  supports your claim. "The import works" is worth less than the version string.
- **Never silently substitute.** Different partition, model, article, dataset,
  file than the one requested — name the substitution and justify it. A brief
  that asked for a closed-access paper and got an open-access one instead
  produced a result that measured nothing.
- **Say what you could not do.** Mark unrun work `NOT-RUN` rather than inferring
  a plausible value. A partial honest result beats a complete-looking one.

## Finishing your turn

**A turn that ends with "I'll report back when X finishes" is a failed turn.**

The gateway records your turn ending as the task completing. There is no
"still working" state you can signal. If you end on a promise, the local
session sees `succeeded` with no deliverable and has to rediscover that nothing
happened.

**You cannot hold a blocking wait.** A non-interactive agent turn (`claude -p`,
`opencode run`) ends when generation stops, and a Bash call that sleeps for
hours hits the tool timeout. Do not try to wait for a Slurm job — that is what
`ab-notify` exists for.

So:

- Do the smallest **complete** thing and report it. A submitted job with known
  gaps beats a perfect plan that never ran.
- **Print evidence as you go**, not in a closing summary. If you run out of
  room mid-way, the earlier steps' output still reached the caller.
- End with concrete results — ids, paths, numbers, verdicts.

## Reporting with `ab-notify`

**If you write the batch script, you own the reporting.** Anything that
outlives your turn must report its own lifecycle, or the only signal available
to the caller is output-file mtimes — which cannot distinguish *queued* from
*died before writing* from *the filesystem is unreachable*.

`ab-notify` lives at `<repo>/bin/ab-notify`, is executable, and needs no
install — but the batch job has to find it. Prepend the repo's `bin/` to `PATH`
in the script (safer than `~/.local/bin`, which on this cluster already shadows
things), or call it by absolute path.

```bash
#SBATCH --export=ALL,AB_JOB_ID=<the ab job uuid>,AB_DATA_DIR=<gateway data dir>

export PATH="<repo>/bin:$PATH"

ab-notify --status running  --msg "server up, starting generation"
ab-notify --status running  --msg "12/24 sources done"
ab-notify --status finished --report "$RUNS/RESULTS.md"
ab-notify --status failed   --msg-file "$RUNS/error.log"
```

| Flag | Use |
|---|---|
| `--status running` | when work **actually starts** — after the model loads, not at submission. A `running` at submit time tells the caller nothing they don't know |
| `--status running` again | at real milestones, so a long run doesn't look hung |
| `--status finished --report PATH` | name the artifact a reader should open |
| `--status failed --msg-file PATH` | the error text, from a file — don't cram a stack trace through a shell argument |
| `--msg-file` | any long or multi-line message; avoids quoting bugs |

**`AB_JOB_ID` and `AB_DATA_DIR` must both be exported into the job.** The first
identifies the job. The second is how `ab-notify` finds two things it needs:
`gateway-endpoint.json` (where the gateway is) and `.token` (the bearer token
for the HTTP path).

Token resolution is `--token` → `$AB_TOKEN` → `<data_dir>/.token`. Without one,
HTTP is skipped and the message falls to the shared filesystem — still
delivered, just not immediately; stderr will say `http: no token found`.

**Never put the token in the script or in `--export`.** Job environments and
submit lines show up in scheduler metadata and accounting records that other
users on a shared cluster can often read. Let it be read from `.token`, which is
mode `0600` and already protected by the filesystem.

`ab-notify` tries HTTP, then a shared-filesystem JSONL, then local `/tmp`, and
prints where it wrote. **It exits 0 even when every path fails**, so it will
never take your job down; check its stderr if you care which path was used.

Messages land in the same event stream as your own output, so the caller polls
one reference for the whole lifecycle.

## Submitting Slurm work

### Discover, don't assume

```bash
sinfo -o "%P %a %l %D %G"                       # partitions
scontrol show partition <p> | grep -E "AllowAccounts|AllowQos|State"
sacctmgr -n show assoc user=$USER format=account,partition%20
scontrol show node <n> | grep -E "ActiveFeatures|CfgTRES|AllocTRES"
```

Two traps seen here:

- **GRES is often untyped** (`gpu:4` with no model), so partition name alone
  does not tell you the GPU. Resolve the model from node `ActiveFeatures`, and
  add `--constraint=<type>` when the partition is heterogeneous — otherwise you
  may land on a V100 when you needed Hopper.
- **Free GPUs ≠ idle node.** A node with free GPUs but no idle CPUs cannot take
  your job. Check both.

### The script skeleton

```bash
#!/bin/bash
#SBATCH --job-name=...
#SBATCH --partition=...  --account=...  --qos=...
#SBATCH --gres=gpu:1     --cpus-per-task=8  --mem=100G  --time=02:00:00
#SBATCH --output=<RUNS>/job-%j.out
#SBATCH --error=<RUNS>/job-%j.err
#SBATCH --export=ALL,AB_JOB_ID=...,AB_DATA_DIR=...

set -euo pipefail
mkdir -p "$RUNS"
echo "STARTED $(date -Is) on $(hostname)" > "$RUNS/STARTED-$SLURM_JOB_ID"
ab-notify --status running --msg "job started on $(hostname)"
```

**`mkdir -p` the `--output` directory from the submitting shell, before
`sbatch`.** Slurm opens that file *before* your script runs, so an in-script
`mkdir` is too late — the job dies in 0 seconds with no logs and no clue. This
has happened.

**Write a heartbeat as the first action.** Then an empty output directory means
"died", not "queued" — two states that otherwise look identical from outside.

**Derive every scratch path from `$SLURM_JOB_ID`:**

```bash
JOBTMP=/scratch/local/jobs/$SLURM_JOB_ID/work-$SLURM_JOB_ID
export TMPDIR=$JOBTMP VLLM_CACHE_ROOT=$JOBTMP HF_HOME=$JOBTMP \
       XDG_CACHE_HOME=$JOBTMP TRITON_CACHE_DIR=$JOBTMP
```

Never hardcode a job id and never inherit `TMPDIR` from another job — a run
here died with `PermissionError` writing into a *dead* job's scratch dir it had
inherited.

**Preflight and fail fast.** Assert your interpreter and imports before loading
weights or waiting on a health check. One run burned a 20-minute health-wait on
a `ModuleNotFoundError` knowable in one second.

### Then stop

Submit, print the job id and `squeue` output, and end your turn. Do not poll it.

### Checking on work: cheap probes, never full-log dumps

When you do want a quick status before you hand off, probe narrowly instead of
dumping output into the transcript:

- **Scheduler state** — `squeue -j <id> -o "%.8T %.10M %.20R"`, or `sacct -j <id>
  --format=State,ExitCode,Elapsed`. Not `squeue` + the whole log.
- **The job's own voice** — prefer `ab-notify` messages the job has sent; they
  say what's actually happening, in one line.
- **Output files** — `tail -N <log>`, `grep <marker> <log>`, or `wc -l` on the
  output file. Never `cat` a log or a directory of files wholesale.

Everything you read lands in the transcript and is **re-read on every later
turn** of the run. A 50K-char dump that answered one question then taxes every
remaining turn at the model's input price — and costs you nothing to have
skipped. If the answer you need is "did it finish", that is one `squeue` line;
if it's "is it progressing", `tail -5`. That's all.

A single bounded probe is fine. A `sleep`-loop waiting for a job to finish is
not — that is what `ab-notify` and the caller's `ab events --follow` are for.

## Other schedulers

Same shape. PBS/Torque (`qsub`, `$PBS_JOBID`, `#PBS -o`), LSF (`bsub`,
`$LSB_JOBID`), or a bare `nohup` on a long-running process: create the output
directory first, heartbeat first, derive scratch from the scheduler's job-id
variable, and call `ab-notify` from inside the script. Nothing above is
Slurm-specific except the flag names.

## Environment traps on this cluster

- **Stale console scripts in `~/.local/bin`.** A `vllm` there pointed at an
  interpreter without vLLM, shadowing the conda env's own launcher. `conda
  activate` fixed `python` but not `vllm`. Use the env's interpreter by
  absolute path and invoke modules through it:
  `ENVPY=/home/$USER/envs/<env>/bin/python; "$ENVPY" -m <module>`.
- **`/tmp` is outside the gateway's `allowed_dirs`.** Anything you leave there
  is unreachable by the caller. Write results under `/project/...`.
- **Compute nodes have no outbound internet.** Fetch and stage inputs on the
  login node, freeze them to disk, and have the job read only local files.
- **`module load` before assuming a toolchain** (`node`, `cuda`, `apptainer`) —
  and don't stack a system CUDA on a torch wheel that bundles its own.
- **A container may be the answer** when a shared env is partially broken:
  `apptainer pull` an official image, bind the data in, and leave the env
  untouched. Others depend on it.

## Before you end your turn

- [ ] Did I inventory before building?
- [ ] Did I quote evidence rather than assert conclusions?
- [ ] Did I name every substitution and deviation?
- [ ] Did I mark unrun work `NOT-RUN`?
- [ ] Does the batch script `ab-notify` at start, milestones, and finish/fail?
- [ ] Did I create the output dir before `sbatch`, and heartbeat first?
- [ ] Am I ending with results — ids, paths, numbers — and not a promise?
