---
name: agent-bridge-worker
description: How to execute a task dispatched through agent-bridge on this host — your remit versus the caller's, being steered mid-turn, finishing a turn properly, submitting Slurm (or PBS/LSF) work, reporting milestones with ab-notify or a file in $AB_JOB_DIR, and registering a monitor for work that outlives your turn. Use whenever you are running a brief that arrived via agent-bridge, submitting batch jobs, or doing compute that outlives your turn.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# Working as an agent-bridge worker

You are the **remote agent**. A local session, working with a human, wrote the brief you are executing. `ab job <id>` shows which backend and model you are.

**Flags are in `ab help`, `ab-notify --help` and `ab-monitor --help`.** This file is the judgement that is not in any of them.

## Workflow

1. **Inventory, then do the work.** You can see this machine; the caller cannot.
2. **Report a milestone at each real step**, so a long run does not look hung: `ab-notify --msg "..."`.
3. **Then take one of two paths, by how long the work will take.**

**Under an hour — stay with it.** Keep working, wait out the run with bounded probes (never a blocking sleep, which just hits the tool timeout), and write the real report when it is done:

```bash
cp "$RUNS/RESULTS.md" "$AB_JOB_DIR/report.md"     # or write it yourself
```

Then end your turn. **Writing the report is what finishes the job** — your turn ending is only half of it.

**An hour or more — hand it over and hand it back.** Submit, register a monitor, write a *preliminary* report, end your turn:

```bash
JOBID=$(sbatch --parsable run.sbatch)
ab-monitor add --slurm "$JOBID" --label train --interval 15m --deadline 12h \
  --result "$RUNS/RESULTS.md"
cat > "$AB_JOB_DIR/report.md" <<'EOF'
# Submitted, not finished
Slurm 12345 on gpu-h200, ~8h. Watch: monitor `train`.
What I did: <the steps>, the sbatch file at <path>, the config at <path>.
What to expect: RESULTS.md at <path> when it completes.
Assumptions: <which held, which did not>.
EOF
```

The preliminary report is what lets the job finish instead of holding a slot for eight hours, and the monitor is what records the outcome later. **Both, or neither is any use**: a monitor with no report leaves the caller waiting on a job that will time out, and a report with no monitor loses the ending.

## Your report is the caller's only window

Your last message is stored whole and returned by `ab job <ref>`.

- Answer in plain language, start with the problem/goal, then the steps you took, then the result. Include any numbers, paths, or identifiers that are relevant to the caller's next turn.
- **Self-contained.** Assume they read only this message, with no transcript. Spell out identifiers in full; never "the file above".
- **Evidence inline, not by reference.** A path they cannot open is not evidence.
- **Answer each assumption the brief flagged** — which held, which did not: *"entry point is `src/eval.py` as assumed; no `--workers` flag, it is `--num-workers`"*. Highest-value part of the report: an unrefuted wrong assumption goes straight into their next brief.
- **Volunteer the environment facts that shaped the result** — actual GPU, actual versions, what already existed, what the data really looked like. A number without its conditions invites a wrong conclusion.
- **Name what you could not deliver**, and why. `NOT-RUN`, not a plausible value.
- Include what they cannot get and **would act differently for** — not everything you saw. A full log dump is the same round trip in the other direction, and it costs them context on every later turn.

## Reporting through `$AB_JOB_DIR`

Your job has a directory of its own, already created, in `$AB_JOB_DIR`. Writing a file there is how anything other than your final message reaches the caller. **No job id, url or token is involved** — that plumbing is gone, along with the failure where a job could not identify itself and sat until its deadline.

```bash
ab-notify --msg "server up, generating"                       # a milestone
ab-notify --msg "12/24 sources done" --report-id sources      # a named one
ab-notify --msg-file "$RUNS/step-3.log" --report-id step-3    # from a file
cp "$RUNS/RESULTS.md" "$AB_JOB_DIR/report.md"                 # the deliverable
```

`ab-notify` is a convenience over one write, and is worth using because it names the file for you in a way that sorts. Where it is not on PATH, do the write:

```bash
echo "server up, generating" > "$AB_JOB_DIR/progress/010-up.md"
```

- **Each file becomes one event** on your job's stream, so the caller sees it without reading your transcript. `ab events <ref> --type message` is the progress log.
- **Rewriting a file with new content reports again**; rewriting it unchanged does not. So a retried step can overwrite its own milestone without piling up duplicates — that is what `--report-id` is for, and why a retry with the same one reports once.
- **Milestones are ingested in name order**, not by mtime. `ab-notify` handles that: `--report-id` gives a stable name, and without one you get a timestamp that sorts the way it happened. Writing them by hand, number them — `010-`, `020-`.
- **A milestone is a note, not a log.** `ab-notify --msg-file` refuses anything over 64 KB rather than posting the first part of it; a whole log belongs in `report.md`, or point it at an excerpt.
- **Put the whole content in the file.** A path only you can open is not evidence; `report.md` is uploaded whole and `ab job <ref>` prints it.
- **`report.md` is what finishes the job**, together with your turn ending. Until both have happened the row reads `waiting`, your process stays alive, and you can still be steered — which is also your chance to write the report if you forgot. Nothing else you write ends a job; a `status` file is read as one more milestone and means nothing to the gateway.
- **If you never write one, the job fails** with `report_missing` after the gateway's grace window (30 minutes by default). Your turn's last message is kept either way, but the row says the deliverable is absent — which is the honest reading.
- **`ab-notify` needs `$AB_JOB_DIR` and nothing else** — no url, no token, and no discovery. A batch script on another node can still write into the directory if you export `AB_JOB_DIR` to it and the data dir is shared, but a **monitor** is the better answer for anything that outlives your turn.

**Your final message and `report.md` are both read**, and they are not the same thing. The message is what the caller sees first; the report is the artifact that finishes the job and survives being read months later. When the work is short they can say the same thing — write the report, then say it. When it is long, the report is preliminary and says what was submitted and where the results will be.

## Work that outlives your turn: register a monitor

**You cannot wait for a scheduler.** A turn that blocks on an eight-hour job hits the tool timeout, and a turn that ends with *"I'll report back when it finishes"* is a job the gateway records as succeeded with no deliverable.

So hand the waiting to the gateway. Submit, register a monitor, report what you submitted, end the turn:

```bash
JOBID=$(sbatch --parsable run.sbatch)
ab-monitor add --slurm "$JOBID" --label train \
  --interval 15m --deadline 12h --result "$RUNS/RESULTS.md"
```

- **`--slurm` reads `sacct`, not `squeue`**, on purpose: `squeue` forgets a job the moment it leaves the queue, so a completed run and a lost one look identical.
- **Anything else is `--poll`**, and you author the command: its first word of output is the status. Plain words (`running`, `finished`, `failed`) and Slurm state names are both understood; `--map 'GREEN=finished;RED=failed'` covers the rest.

  ```bash
  ab-monitor add --label server --interval 60s \
    --poll 'curl -sf localhost:8080/health && echo running || echo failed'
  ```
- **`--result` names the files the caller will want** when it finishes. They are reported with the terminal event, so the caller knows what to download without asking.
- **`ab-monitor` is a convenience, not a dependency.** It writes one key-value file; if it is not on PATH, write the file:

  ```bash
  cat > "$AB_JOB_DIR/monitors/train" <<'EOF'
  poll = sacct -n -X -j 12345 --format=State
  interval = 15m
  result = /project/x/runs/RESULTS.md
  EOF
  ```

  The file name is the watch's identity, so writing it twice registers one monitor.
- **Say the monitor's label in your final message and in `report.md`**, so the caller can find it: `ab monitors --job <ref>`, then `ab monitor <id> --wait`.
- **A monitor is not your job's status.** Your job finishes when your turn ends and its report is written; the watch resolves on its own, hours later. Its ending is recorded on your job's event stream — what was watched, what it last read, how long it ran, and the result paths — so the outcome is on the record even though the job closed long before.

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
