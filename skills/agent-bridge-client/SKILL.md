---
name: agent-bridge-client
description: Drive a remote coding agent through an agent-bridge gateway using the `ab` CLI — submit prompts, stream or wait for jobs, steer live work, resume sessions, and transfer artifacts safely.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# agent-bridge (`ab` CLI)

For remote GPUs, schedulers, long jobs, or delegated work on another host. You
plan and verify; the remote agent inventories, executes, and reports evidence.

## Resolve `ab` once

```bash
ab --version                                   # 1. on PATH
ls ~/agent-bridge/client/ab.py ./client/ab.py  # 2. a checkout -> python3 <repo>/client/ab.py
find ~ /workspace /srv -maxdepth 4 -path '*/client/ab.py' 2>/dev/null | head
```

Ask the user where the checkout is if both miss. Set `AB` once and reuse it;
examples below write plain `ab`.

## Reachability

| Command | Tells you |
|---|---|
| `ab gateways` | every gateway: token presence **and** live reachability |
| `ab gateways --no-probe` | config only, contacts nothing — proves nothing works |
| `ab health` | one gateway; **exit 1 when unreachable**, so scripts branch on it |

`gateways` always exits 0 — a down gateway is data. Failures name their cause:

| Symptom | Cause | Fix |
|---|---|---|
| `REFUSED` (`WinError 10061`) | no local listener; SSH forward down | reopen the tunnel |
| `RESET` | forward up, gateway not serving | restart the gateway on its host |
| `HTTP_ERROR` | something else is on that port | check what the forward points at |

Open tunnels with keepalives — idle forwards drop silently:
`ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -L <port>:localhost:<port> <host>`

## Windows: `MSYS_NO_PATHCONV=1` on every Git Bash call

Every path `ab` takes is a **remote POSIX path**; Git Bash rewrites them first:

```
you type:  --cwd /project/data/x     ab sees:  'C:/Program Files/Git/project/data/x'
```

Hits `--cwd --file --dir --to`. Usually a 400 — but a rewritten path **can
resolve somewhere real and wrong**.

- `MSYS2_ARG_CONV_EXCL='*'` does **not** work (MSYS2-proper; Git for Windows
  ignores it silently).
- Shell state does not persist between calls — set it inline every time.
- PowerShell needs no prefix.

## Commands

| Command | Use |
|---|---|
| `health` · `agents` · `capabilities` · `info` · `models` | discovery |
| `sessions` | directories with sessions; `--cwd DIR` for the sessions in one |
| `jobs [--limit N] [--cursor C]` | paged summaries |
| `submit -F FILE` | submit; waits for the session id |
| `run -F FILE` | submit and wait for the result |
| `job REF` · `wait REF` | detail/result · wait on an existing job |
| `events REF` | last 50 events; `-f` to follow |
| `steer REF -F FILE` | redirect a **running** turn |
| `cancel REF` | interrupt queued/running work |
| `upload` · `download` · `ls` | files |

`REF` = full UUID, title, or unique id prefix. **Record full UUIDs** — a prefix
can later become ambiguous.

**Always pass prompts with `-F/--prompt-file`.** Avoids shell quoting and
length bugs. `--prompt-stdin` is the explicit stdin form.

Check `ab agents --output json` before relying on a feature — capabilities are
per-adapter (`claude` steers, `opencode` does not).

## Output and exits

| Flag | Meaning |
|---|---|
| `--output human` | default; only this elides, `--full` disables |
| `--output json` | one complete faithful document (`--json` is an alias) |
| `--output jsonl` | one typed record per line: `event`, `terminal`, `timeout`, `complete` |

Globals work before or after the subcommand. **Parse `--output json`; never grep
the human view** — it elides, so a later match silently fails.

| Exit | Meaning |
|---:|---|
| 0 | success |
| 1 | local config / transport / protocol / file failure |
| 2 | invalid invocation |
| 3 | the waited-on job failed or was canceled |
| 4 | wait timed out — **the job is still running** |

Timeout never cancels unless `--cancel-on-timeout`. Inspecting a failed job
still exits 0 unless `--fail-on-job-failure`.

## Finding what to continue

`ab sessions` answers two questions, and which one depends on `--cwd`:

```bash
ab sessions                       # which directories have work, and how much
ab sessions --cwd /project/x      # the sessions in exactly that directory
ab sessions --cwd /project/x --limit 100 --cursor <next_cursor>
```

The first is the one to run when you don't yet know which project to ask about.
It is **complete** — every directory, never a page — so "not listed" means
"none", not "crowded out".

The second is an **exact** directory match, not a prefix: `/project/x` and
`/project/x/sub` are separate indexes, so `total` means what it says. Paths
normalise, so `D:\x`, `D:/x` and `d:\X` all match. Page with `--limit` and
`--cursor` (not `--after`; sessions have no sequence to count).

Both views list a session only if **a human spoke or the agent acted**, and
counts match listings. Subagent transcripts and slash-command residue (`/login`,
`/resume`) carry a real id and look resumable while holding nothing, so they are
not offered. A `--session <id>` you name explicitly is still honoured; only the
recommendations are filtered.

## Continue existing work — first match wins

Check `ab jobs` then `ab sessions` before submitting.

| # | Situation | Command |
|---|---|---|
| 1 | a job is **running** on this work | `ab steer <ref> -F note.md` |
| 2 | session is **idle** and must see the message | `ab submit -F f.md --session <id> --no-fork` |
| 3 | want its history, work **branches** | `ab submit -F f.md --session <id>` |
| 4 | genuinely new subject | `ab submit -F f.md` |

Rung 4 last — not because it is cheaper. Context an agent already built (the
repo it read, the quirk it learned) outvalues the tokens a clean start saves.

**Steer is the only thing that reaches a live turn.** The agent takes it at its
next tool boundary, so `202` means accepted, not acted on — watch for the `steer`
event. A turn mid-generation with no tool calls sees it only when that finishes.

**`--no-fork` needs an IDLE target.** Against a mid-turn session it starts a
*second* agent on a stale transcript; both write, the transcript forks, and the
later flush wins — the other branch is silently lost. The gateway refuses with
`409 session_busy` naming `held_by` and `steer_ref`. **Follow the pointer.**

**A named session runs in its own directory.** Resuming or forking carries that
session's history, so the gateway runs it in the project it was created in —
overriding `--cwd` and the configured default, and saying so with a `status`
event (`stage: "cwd"`, naming the directory used and the one replaced). To work
in a *different* tree, start a fresh job rather than pinning a session.

**Getting the session to reuse:** `submit` waits for it and prints it
(`session: <id> (ready)`); `--no-wait` skips the wait. On any job row, `session`
is the single canonical field to pass back to `--session` — prefer it over
`forked_session`/`chosen_session`/`requested_session`.

## Briefing the worker: separate what you know from what you assume

You cannot see the remote filesystem, installed versions, cluster state, or
prior runs. The worker cannot see this conversation, the user's goal, or what you
already ruled out. Neither side can see its own blind spot, so say yours out
loud. A brief has four parts:

| Part | Content |
|---|---|
| **Known** | the goal, the constraints, what has been tried and ruled out, why this approach |
| **Assumed** | paths, module names, versions, data shapes you believe exist but have *not* verified — labelled as assumptions |
| **Unknown** | what you want discovered and reported back |
| **Deliverable** | what the answer must contain to be usable without a follow-up |

**The Assumed section is the one that earns its keep.** An assumption written as
a fact ("edit `src/train.py`") makes the worker comply with something broken or
silently substitute. Written as an assumption ("I believe the entry point is
`src/train.py` — confirm, and say what you found if not") it makes the worker
verify and report. Every path, version, and dataset name you did not personally
read belongs here.

```markdown
## Known
Goal: cut eval wall-clock. Batching is already ruled out — it changed the metric.
## Assumed (verify these)
- entry point `src/eval.py`; a `--workers` flag exists
- torch 2.x with CUDA already in the shared env
## Unknown — report back
Actual GPU model on the partition, and whether the env's torch sees it.
## Deliverable
The command you ran, the before/after timing, and the versions in play.
```

Also state what you **cannot fetch**: anything outside the gateway's
`allowed_dirs` is unreachable to you, so ask for extracts inline rather than
file paths you cannot open.

## Reading a job

**`ab job REF` first.** The result is stored and printed whole — usually all you
need.

**Then events, which now read from the end.** `ab events REF` returns the last
50; `total`/`first_seq`/`last_seq` show the shape so you never page blind.

```bash
ab events REF                        # last 50
ab events REF --tail 200             # more history
ab events REF --tail 20 --type result --type error   # narrow inside the window
ab events REF --after 0              # top-down (the old default)
ab events REF -f                     # follow; primes with a short tail
```

- `--tail` and `--after` conflict (exit 2) — pick an end.
- `--type` filters **inside** `--tail`, so `--type result --tail 5` works.
- `--output json` is faithful/unelided: narrow with `--type` before widening
  `--tail`, or a long job's tool results will flood you.

**Every timestamp is ISO 8601** with the gateway's UTC offset attached — job
`created_at`/`started_at`/`finished_at`/`last_event_at`, event `ts`, session
`last_active`, file `mtime`. No epoch floats to decode. Durations stay numeric:
`elapsed`/`elapsed_hms` on each event give position in the run, usually the
actual question.

**Transcripts are the last resort** — every tool call *and result*, megabytes.
Filter on the remote host, download the extract:

- **Claude Code**: one file per session, via `ab ls`/`ab download` —
  `<home>/.claude/projects/<slugified-cwd>/<session-id>.jsonl`. Growing = working.
- **opencode**: one SQLite db, no per-session files, so `download` cannot help.
  On the host: `opencode export <sessionID>`; the id is the job's `session`.

## Long and batch jobs

Prefer submit + follow/wait over one blocking call:

```bash
ab submit -F task.md --title nightly --idempotency-key nightly-v1
ab events nightly -f --type assistant --output jsonl > nightly.jsonl
ab wait nightly --timeout 1800 --output json
```

Reuse an `--idempotency-key` only for retries of the *same* submission; changed
content under one key is a conflict.

**A coding-agent turn cannot wait hours for scheduler work.** Tell the remote
agent to submit and return the identifiers, and make the batch script report
itself with `ab-notify` (see the worker skill).

**Batch `message` events are post-terminal annotations.** The job row can read
`succeeded` — and a follow can close — hours before the `finished` report
arrives. Retrieve later reports with a fresh `ab events REF --after <cursor>`.
One open stream does not wait out external compute.

**Three states, not two**, when inferring from the filesystem:

| Observation | Means |
|---|---|
| can't reach the gateway | **nothing** about the job |
| reached it, nothing there | not submitted, or died before writing |
| files present | running |

## Files

```bash
ab submit -F task.md --upload-as inputs/data.csv=./data.csv
ab download --dir /project/x/out --glob '*.csv' --recursive --to ./out
```

Uploads take readable regular non-symlink files; duplicate remote names fail.
Downloads preserve relative paths, reject traversal/symlink roots/collisions,
and need explicit `--overwrite`. Anything outside `allowed_dirs` is unreachable —
for bulk data, `rsync` into an allowed dir and attach the remote path.

## Verification discipline

- Check the remote agent's evidence; do not relay its conclusion.
- A terminal job means the **turn** ended — it may only have submitted work.
- A stale `running` row is not proof of life. `ab steer` it: the write fails
  against a dead agent, so that 409 is the most direct liveness probe.
- A gateway restart explicitly fails formerly-running rows (`gateway_restarted`)
  and requeues queued ones, rather than leaving them stale.
- `cancel` interrupts first so the transcript flushes; escalation is a fallback.

## Repo

`API.md` (HTTP contract) · `client/README.md` (CLI) · `config.example.toml`
(gateway settings) · `docs/todo/` (known gaps, in-flight design).
