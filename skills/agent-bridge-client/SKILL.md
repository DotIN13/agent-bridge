---
name: agent-bridge-client
description: Drive a remote coding agent through an agent-bridge gateway using the `ab` CLI — submit prompts, stream or wait for jobs, steer live work, resume sessions, and transfer artifacts safely.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# agent-bridge (`ab` CLI)

For remote GPUs, schedulers, long jobs, or delegated work on another host. You
plan and verify; the remote agent inventories, executes, and reports evidence.

**Flags and subcommands are in `ab help` and `ab <cmd> --help`.** This file is
only what the CLI cannot tell you.

## Start every session this way

- Find `ab` on PATH; else a checkout (`python3 <repo>/client/ab.py`); else ask
  the user for the path. Set `AB` once and reuse it.
- **Always `ab gateways`, then `ab health`, before any work.** `gateways` always
  exits 0 — a down gateway is data; `health` exits 1 when unreachable, so scripts
  branch on it. A config listing proves nothing: `--no-probe` contacts nothing.
- Read the failure: `REFUSED` = no listener, reopen the tunnel · `RESET` =
  forward up, gateway not serving · `HTTP_ERROR` = something else on that port.
- Open tunnels with keepalives — idle forwards drop silently:
  `ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -L <port>:localhost:<port> <host>`

## Continue existing work before starting new

- **Always `ab jobs`, then `ab sessions`, then `ab sessions --cwd <dir>` before
  submitting anything.** The first shows directories that have work; the second
  the sessions inside one.
- Both views are complete for what they cover, and counts match listings, so
  "not listed" means none — not crowded out of a window.
- First match wins:

| | Situation | Do |
|---|---|---|
| 1 | a job is **running** on this work | `ab steer` |
| 2 | session **idle**, must see the message | `ab submit --session <id> --no-fork` |
| 3 | want its history, work **branches** | `ab submit --session <id>` |
| 4 | genuinely new subject | `ab submit` |

- Rung 4 last — not because it is cheaper. Context an agent already built (the
  repo it read, the quirk it learned) outvalues the tokens a clean start saves.
- **Record full UUIDs.** A prefix can later become ambiguous.
- Always pass prompts with `-F/--prompt-file` — avoids shell quoting and length
  bugs.

## Session and steering semantics

- **Steer is the only thing that reaches a live turn.** Taken at the next tool
  boundary, so `202` means accepted, not acted on — watch for the `steer` event.
  A turn mid-generation with no tool calls sees it only when that finishes.
- **`--no-fork` needs an IDLE target.** Against a mid-turn session it starts a
  *second* agent on a stale transcript; both write, the transcript forks, and the
  later flush wins — the other branch is silently lost. The gateway refuses with
  `409 session_busy` naming `held_by` and `steer_ref`. **Follow the pointer.**
- **A named session runs in its own directory.** Resuming or forking carries that
  session's history, so the gateway runs it in the project it was created in,
  overriding `--cwd` and the default, and says so with a `status` event
  (`stage: "cwd"`). To work in a *different* tree, start a fresh job.
- `submit` waits for the session id and prints it. On any job row, `session` is
  the canonical field to pass back — prefer it over `forked_session` /
  `chosen_session` / `requested_session`.
- Listings offer only sessions where **a human spoke or the agent acted**;
  subagent and slash-command transcripts carry real ids but hold nothing. An
  explicit `--session <id>` is still honoured.
- Check `ab agents --output json` before relying on a feature — capabilities are
  per-adapter (`claude` steers, `opencode` does not).

## Windows: `MSYS_NO_PATHCONV=1` on every Git Bash call

- Every path `ab` takes is a **remote POSIX path**, and Git Bash rewrites them
  first: `--cwd /project/data/x` arrives as `C:/Program Files/Git/project/data/x`.
- Hits `--cwd --file --dir --to`. Usually a 400 — but a rewritten path **can
  resolve somewhere real and wrong**.
- `MSYS2_ARG_CONV_EXCL='*'` does **not** work. Shell state does not persist
  between calls, so set it inline every time. PowerShell needs no prefix.

## Output and exits

- **Parse `--output json`; never grep the human view** — it elides, so a later
  match silently fails. `--output jsonl` gives one typed record per line.
- **Exit 4 is a timeout, and the job is still running** — it never cancels
  unless `--cancel-on-timeout`. Exit 3 is the job failing; inspecting a failed
  job still exits 0 unless `--fail-on-job-failure`.

## Brief the worker: split what you know from what you assume

You cannot see the remote filesystem, versions, cluster state, or prior runs. The
worker cannot see this conversation, the user's goal, or what you ruled out.
Neither side can see its own blind spot, so say yours out loud.

- **Known** — the goal, constraints, what is ruled out and why
- **Assumed** — paths, modules, versions, data shapes you have *not* verified,
  labelled as assumptions
- **Unknown** — what you want discovered and reported back
- **Deliverable** — what the answer must contain to be usable without a follow-up

**Assumed is the part that earns its keep.** "Edit `src/train.py`" makes the
worker comply with something broken or silently substitute; "I believe the entry
point is `src/train.py` — confirm, and say what you found if not" makes it verify
and report. Every path and version you did not personally read belongs there.

Also state what you **cannot fetch**: anything outside the gateway's
`allowed_dirs` is unreachable to you, so ask for extracts inline rather than file
paths you cannot open.

## Reading a job

- **`ab job REF` first.** The result is stored and printed whole — usually all
  you need.
- Then `ab events REF`, which reads from the **end** by default;
  `total`/`first_seq`/`last_seq` show the shape so you never page blind.
  `--after 0` restores top-down reading.
- Narrow with `--type` before widening `--tail`, or a long job's tool results
  will flood you.
- Every timestamp is ISO 8601 with the gateway's offset. Durations stay numeric:
  `elapsed`/`elapsed_hms` give position in the run, usually the actual question.
- **Transcripts are the last resort** — every tool call *and result*, megabytes.
  Filter on the remote host and download the extract. Claude Code keeps one file
  per session at `<home>/.claude/projects/<slugified-cwd>/<session-id>.jsonl`
  (growing = working); opencode keeps one SQLite db, so `download` cannot help —
  run `opencode export <sessionID>` on the host.

## Long and batch jobs

- Prefer submit + follow/wait over one blocking call. Reuse an
  `--idempotency-key` only for retries of the *same* submission.
- **A coding-agent turn cannot wait hours for scheduler work.** Tell the remote
  agent to submit and return the identifiers, and have the batch script report
  itself with `ab-notify` (see the worker skill).
- **Batch `message` events are post-terminal annotations.** The job row can read
  `succeeded` — and a follow can close — hours before the `finished` report
  arrives. Retrieve later reports with a fresh `ab events REF --after <cursor>`.
- **Three states, not two**, when inferring from the filesystem: can't reach the
  gateway tells you **nothing**; reached it and nothing is there means not
  submitted or died before writing; files present means running.

## Files

- Uploads take readable regular non-symlink files; duplicate remote names fail.
  Downloads preserve relative paths, reject traversal, symlink roots and
  collisions, and need explicit `--overwrite`.
- Anything outside `allowed_dirs` is unreachable — for bulk data, `rsync` into an
  allowed dir and attach the remote path.

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
(gateway settings) · `docs/design/` (why behaviour is the way it is) ·
`docs/todo/` (known gaps).
