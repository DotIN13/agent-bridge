---
name: agent-bridge-client
description: Drive a remote coding agent through an agent-bridge gateway using the `ab` CLI — submit prompts, watch job events, upload inputs and download artifacts. Use for work that needs a remote machine's GPUs, a batch scheduler (Slurm/PBS/LSF), or delegating a long-running task to an agent on another host.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# agent-bridge (`ab` CLI)

Drives a coding agent on a remote host through the agent-bridge gateway. Work is
submitted as a prompt; the gateway runs it in an agent session on that machine —
Claude Code or opencode, whichever `--agent` names, on whichever `--model` the
job pins. Use it for remote GPUs, batch jobs, and long delegated tasks.

> **Adapt before use.** Replace `<repo>`, `<host>`, and `<workdir>` below with
> your own paths, and delete whatever doesn't apply. The conventions and
> failure modes are general; the paths are not.

## Working conventions

Division of labour between this session and the remote agent. The remote side
has its own skill — `skills/agent-bridge-worker/` in the repo — install it on
the gateway host so both halves agree.

**Local session — plan and review, with the user.**

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
| `models [--agent A]` | model ids a gateway's agent accepts |
| `sessions [--cwd DIR] [--agent A]` | sessions you can target |
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
the worker resumes the named session itself (`claude --resume <session>` /
`opencode run -s <session>`), with no routing model:

```bash
ab submit -F task.md --session <uuid>              # fork that session
ab submit -F task.md --session <uuid> --no-fork    # append in place
ab submit -F task.md                               # fresh session
```

`ab sessions --json` lists candidates. `--no-fork` requires `--session` and is
for a follow-up the session itself must see — a fork puts the message on a
branch the original never reads.

**Prefer continuity. Check `ab sessions` before every submit** and pick in this
order:

1. **Resume in place** — `--session <uuid> --no-fork`. The default when a
   relevant session exists. The agent keeps the context it already built and
   does not re-derive what it worked out last time.
2. **Fork** — `--session <uuid>`. When you want that session's history but the
   work branches: a new turn on the same thread, or a variant you don't want
   written back into the original.
3. **Fresh** — omit `--session`. Only when nothing relevant exists.

A remote agent that has already read the repo, found the config and learned the
cluster's quirks is worth far more than the tokens saved by starting clean.
Reaching for a fresh session because it is cheaper throws that away and buys a
re-derivation of the same facts.

The cost is real, though, and worth managing rather than ignoring: a resumed
session re-reads its whole transcript on every turn, so a thread that has run
all day is expensive to continue. Keep sessions scoped to a line of work and
start a new one when the subject genuinely changes — not to save money on a
task that needs the history.

**Pick the backend and model per job.** `--agent` (claude or opencode) and
`--model` are independent knobs; both are set at submit time. Scope the catalogs
the same way:

```bash
ab models --agent opencode                 # deepseek/deepseek-v4-flash, deepseek/deepseek-v4-pro
ab submit -F task.md --agent opencode --model deepseek/deepseek-v4-flash
ab run -F task.md --agent claude --model claude-sonnet-5   # claude is the gateway default
ab sessions --agent opencode               # sessions the opencode adapter can fork
```

The gateway advertises exactly what each backend accepts (`/v1/models`, a plain
list); a model not in that list is passed through unchecked, so pin ids verbatim.

**Give every job a `--title`**, then address it by name: `ab events my-run -f`.
Titles auto-derive from the prompt's first line if unset. Ambiguous refs return
409 with candidates rather than guessing — which matters most for `cancel`.

**Record full UUIDs.** Prefixes resolve only while unique.

**Use `submit` + `events`, not `run`, for anything slow.** `run` blocks and a
harness will usually background it out from under you.

**Reasoning is hidden by default.** `thinking` events (the agent's reasoning)
are dropped from the stream unless you pass `--include-thinking` at submit time.
Only turn it on when you genuinely need the reasoning — it is verbose and
usually noise.

**Background the follower and read its log.** `events --follow` polls once a
second and exits at the terminal event, so it behaves well as a background
command whose output file you read incrementally.

**Verify the model that actually ran.** `--model` is passed through unchecked, so
check the run, not your intent: the `init` line in `ab events` reports the model
in use (claude); for opencode, `ab job <id>` shows the requested `model`.

**Spend the cheapest model that fits the task.** If your prompt already spells
out the plan step by step — the agent is executing, not deciding — a cheaper
model (haiku for claude, deepseek-v4-flash for opencode) does the same job for a
fraction of the price. Reach for opus only when the task needs the agent to
reason, choose, and improvise. Claude's input price spans 10× from haiku to
fable; don't pay opus to run a checklist.

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

The HTTP path needs the **bearer token**, resolved as `--token` → `$AB_TOKEN` →
`<data_dir>/.token`. So `AB_DATA_DIR` in the job's `--export` is what enables
tier 1 at all — it locates both the endpoint file and the token. Without a
token it quietly falls to the shared filesystem. Never pass the token itself
through `--export`; job environments leak into scheduler metadata on shared
clusters.

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

## Output contract

Everything a command produces goes to **stdout**, one line per record, ids in
full — so `grep` sees all of it, headers included. stderr carries only the
tool's own failures (bad config, unreachable gateway, ambiguous ref), meaning
the command did not run rather than something it produced.

The exceptions are the three commands whose stdout is a payload you capture or
redirect whole, where a stray metadata line would corrupt it: `submit` (bare
job id, for `id=$(ab submit -F t.md)`), `run` (result text, for `> out.md`) and
`--stream` (live assistant text). Their metadata goes to stderr.

**`--json` and `--full` are orthogonal.** `--json` picks the shape; `--full`
picks the completeness. Long free text is elided by default *in both*, so the
two views agree on content and differ only in form — `--json --full` is the
faithful dump. Every clip carries its true size, `… [+N chars, --full]`, so a
truncated value can never be mistaken for a short one.

Never elided, with or without `--full`: **identifiers** (you cannot resume a
session or cancel a job from a prefix) and **`result`/`error`** (the final
report is the deliverable).

## Reading a job's output

**`ab job <ref>` first, and usually last.** The final message is stored whole
and printed whole — no truncation anywhere in that path. A worker following its
own skill writes that message to stand alone, so in most cases it is the only
thing you need to read.

**Then `ab events`, which elides by default.** Every event is stored complete.
To read the full text of a slice without pulling the whole log:

```bash
ab events my-run --after 40 --until 60 --full     # one bounded window
ab events my-run --type tool_result --full        # just the tool output
ab events my-run --json --full                    # everything, machine-readable
```

Bounding matters. `--full` with no range on a long job returns every tool
result in full, which is the thing this is meant to avoid.

**Parse `--json`, don't grep the line view.** Grep works on it now, but the
line view elides at 200 chars, so a pattern that appears later in a long tool
result will not match and the miss is silent. For a field you are branching on
— a status, a cost, a session id — read `--json`.

### Only then, the session transcript

Reach for it when the job row genuinely cannot help — the gateway lost state,
or the run died before emitting a result. **It is the most expensive read
available**: a transcript is every tool call *and every tool result*, megabytes
for a long session, nearly all of it noise you have already seen summarised.
Filter it on the remote host and download the extract, not the file.

- **Claude Code transcripts** are files, reachable through `ab ls` / `ab download`:
  ```
  <remote home>/.claude/projects/<slugified-cwd>/<session-id>.jsonl
  ```
  Growing size = still working. Downloading it gives you a job's text even when
  the job row is stale or the run wrote no files.
- **opencode sessions** live in a SQLite DB at
  `<remote home>/.local/share/opencode/opencode.db` (no per-session files). Read
  them with `opencode export <sessionID>` or `opencode session list` on the
  gateway host; the job's `forked_session` id is the one to export.

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
