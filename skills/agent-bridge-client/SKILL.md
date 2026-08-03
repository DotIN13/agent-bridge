---
name: agent-bridge
description: Drive a remote coding agent through an agent-bridge gateway using the `ab` CLI — submit prompts, watch job events, upload inputs and download artifacts. Use for work that needs a remote machine's GPUs, a batch scheduler (Slurm/PBS/LSF), or delegating a long-running task to an agent on another host.
allowed-tools: Bash, Read, Write, Glob, Grep
---

# agent-bridge (`ab` CLI)

Drives a coding agent on a remote host through the agent-bridge gateway. Work is
submitted as a prompt; the gateway runs it in a Claude Code session on that
machine. Use it for remote GPUs, batch jobs, and long delegated tasks.

> **Adapt before use.** Replace `<repo>`, `<host>`, and `<workdir>` below with
> your own paths, and delete whatever doesn't apply. The conventions and
> failure modes are general; the paths are not.

## Working conventions

Division of labour between this session and the remote agent. The remote side
has its own skill — `skills/agent-bridge-worker/` in the repo — install it on
the gateway host so both halves agree.

**Local Claude — plan and review, with the user.**

- Work out *what* to run and *why*, then write the spec. The remote agent
  executes a brief; it does not choose the research direction.
- **Verify what comes back rather than relaying it.** The remote agent reports;
  you check. A job that reports `FAIL` may have a broken checker; a "model
  error" may be an inconsistency in the input data. Relaying either as fact
  sends the work the wrong way.
- **Do the waiting.** Poll and surface progress. Never push a blocking wait
  onto the remote agent — it cannot hold one.
- Hold the context: what's been ruled out, what's next, and what a result would
  mean *before* you have it.

**Remote agent — investigate, suggest, execute.** Put this in the brief:

- **Inventory before building.** It can see the machine; you can't.
- **Propose.** If the spec is wrong or unsatisfiable, say so and say what you
  did instead — don't silently comply with something broken.
- **Report evidence, quoted, not conclusions.**
- **Never silently substitute** a different partition, model, dataset or target.
- **Say what you couldn't do**; mark unrun work `NOT-RUN` rather than inferring.

**Long compute is the remote agent's to report** — see
[Batch work](#batch-work-the-pattern-that-works).

## Invoking

```bash
python3 <repo>/client/ab.py gateways
```

Stdlib only, no install. Config lives at
`~/.config/agent-bridge/gateways.json`; the token is read from a separate
`token_file`.

**On Windows / Git Bash, prefix every call with `MSYS_NO_PATHCONV=1`.** Git Bash
rewrites POSIX-looking arguments into Windows paths, and every path `ab` takes
is a *remote* POSIX path:

```
you type:   --cwd /srv/project/x
ab sees:    'C:/Program Files/Git/srv/project/x'
```

It usually fails as a 400, but a rewritten path can also resolve somewhere real
and wrong. `MSYS2_ARG_CONV_EXCL='*'` does **not** work — that's MSYS2-proper and
Git for Windows ignores it. Shell state doesn't persist between calls, so set it
inline each time. PowerShell needs no such flag.

## Commands

| Command | What it does |
|---|---|
| `gateways` | configured gateways; confirms a token is loaded |
| `info [--refresh]` | remote host capabilities (CPU/RAM, GPUs, scheduler) |
| `models [--pick TIER]` | models the gateway offers; `--pick` prints one id |
| `sessions [--cwd DIR]` | sessions you can target |
| `jobs [--limit N]` | recent jobs, with full ids and titles |
| `submit -F FILE [...]` | submit, print job id, return immediately |
| `run -F FILE [...]` | submit **and block** until done |
| `job REF` | status / result / error |
| `events REF [--after N] [--follow]` | event log; `--follow` polls to terminal |
| `cancel REF` | interrupt a queued or running job |
| `upload` · `download` · `ls` | files in and out |

`REF` = full uuid, the job's **title**, or a unique id prefix. `--json` on
anything for parseable output.

## Rules that matter

**Always pass prompts with `-F/--prompt-file`.** Inline prompts with embedded
quotes can produce malformed requests that create no job and report no error.
Compose the file with the Write tool — not a heredoc — so no shell quoting is
involved anywhere.

**Name the session, or accept a fresh one.** Under the default `direct` dispatch
the worker runs `claude --resume <session>` itself, with no routing model:

```bash
ab submit -F task.md --session <uuid>              # fork that session
ab submit -F task.md --session <uuid> --no-fork    # append in place
ab submit -F task.md                               # fresh session
```

`ab sessions --json` lists candidates. `--no-fork` requires `--session` and is
for a follow-up the session itself must see — a fork puts the message on a
branch the original never reads.

**Give every job a `--title`**, then address it by name: `ab events my-run -f`.
Titles auto-derive from the prompt's first line if unset. Ambiguous refs return
409 with candidates rather than guessing — which matters most for `cancel`.

**Record full UUIDs.** Prefixes resolve only while unique.

**Use `submit` + `events`, not `run`, for anything slow.** `run` blocks and a
harness will usually background it out from under you.

**Background the follower and read its log.** `events --follow` polls once a
second and exits at the terminal event, so it behaves well as a background
command whose output file you read incrementally.

**Pin the model with a full id and verify it took.** Aliases track the newest
model in a tier, so an aliased run isn't reproducible. The `init` line in
`ab events` reports the model *actually* running — check that, not the job JSON.

## Batch work: the pattern that works

**A remote agent cannot hold a blocking wait.** Its turn ends when generation
stops, and a shell call that sleeps for hours hits the tool timeout. Ask it to
"wait for the job" and it will end its turn promising to report back — which the
gateway records as **success**, with no deliverable.

So don't ask it to wait. Ask it to **submit and report the job id**, and have
the *batch script* report its own lifecycle:

```bash
#SBATCH --export=ALL,AB_JOB_ID=<the ab job uuid>,AB_DATA_DIR=<gateway data dir>
ab-notify --status running  --msg "server up, generating"
ab-notify --status finished --report "$RUNS/RESULTS.md"
ab-notify --status failed   --msg-file "$RUNS/error.log"
```

Those land in the same `ab events <ref>` stream as the agent's own output, so
one reference covers submission → queue → run → finish → report path.

`ab-notify` tries HTTP, then a shared-filesystem JSONL, then local `/tmp`, and
prints where it wrote — a message is never silently lost. It finds the gateway
via `--url` → `$AB_URL` → `<data_dir>/gateway-endpoint.json`, never assuming
loopback (on a compute node `127.0.0.1` is that node).

**Prompt shape for batch work**, learned the hard way:

- Lead with the **action**, not a prohibition. Opening with "do NOT wait for the
  job" primes the session to relay status instead of working.
- Say explicitly there is no background job to report on.
- Require **interleaved evidence** — print each result as it's known, not in a
  closing summary that may never arrive.
- Permit shipping incomplete: *a submitted job with known gaps beats a perfect
  plan that never ran.*
- `mkdir -p` the scheduler's output directory **before** submitting. Slurm opens
  `--output` before the script runs, so an in-script `mkdir` is too late and the
  job dies instantly with no logs.
- Heartbeat as the script's first action, so an empty output dir means "died"
  rather than "queued".

## Monitoring: three states, not two

When inferring a job's state from the filesystem, keep these distinct:

| State | Meaning |
|---|---|
| can't reach the gateway | says **nothing** about the job |
| reached it, nothing there | not submitted, or died before writing |
| files present | running |

Folding the first into the second is how a live job gets reported as dead.
Prefer `ab events` over mtimes once `ab-notify` is wired up.

## Recovering output when the API can't help

Claude Code session transcripts are readable through `ab ls` / `ab download`:

```
<remote home>/.claude/projects/<slugified-cwd>/<session-id>.jsonl
```

Growing size = still working. Downloading it gives you a job's text even when
the job row is stale or the run wrote no files.

## When it can't connect

Two causes, distinguishable by the error:

1. **Tunnel down** — *connection refused*: no local listener. Reopen the
   forward. Use keepalives, because idle forwards drop silently:
   ```
   ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -L 8787:localhost:8787 <host>
   ```
   `autossh -M 0` with the same flags reconnects automatically.
2. **Gateway dead** — *connection reset*: the forward is up, nothing is
   listening at the far end. Restart the gateway on its host.

`ab gateways` works offline — it only reads local config, so success there tells
you nothing. `ab info` is the real reachability probe.

## Gotchas

- **A stale `running` row is not proof of life.** A session killed by a usage
  limit leaves its job row `running` indefinitely. Cross-check `ab sessions` or
  a recent `ab-notify` message.
- **`cancel` interrupts, it doesn't kill.** SIGINT first (like ESC), escalating
  to SIGTERM then SIGKILL only after the grace period, so the session usually
  stays resumable.
- **Anything outside the gateway's `allowed_dirs` is unreachable** — including
  `/tmp` on most deployments. Have jobs write results somewhere you can fetch.
- **A terminal job is not necessarily finished work.** If the agent's task was
  to submit a batch job, `succeeded` means the submission returned.

## Cost

Remote compute is usually cheaper than hosted APIs and is often the point of the
setup. Keep guards in the tool being run, not in the prompt — an instruction to
an agent you can't observe is not a control.

## Repo

`API.md` for the HTTP surface · `client/README.md` for the CLI ·
`config.example.toml` for gateway settings (`dispatch_mode`, `[messages] dir`) ·
`bin/ab-notify` for the batch-job reporter ·
`skills/agent-bridge-worker/` for the remote agent's half of the contract.
