---
name: agent-bridge-client
description: Drive a remote coding agent through an agent-bridge gateway using the `ab` CLI — submit prompts, stream or wait for jobs, steer live work, resume sessions, and transfer artifacts safely.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# agent-bridge (`ab` CLI)

For remote GPUs, schedulers, long jobs, or delegated work on another host. You plan and verify; the remote agent inventories, executes, and reports evidence.

**Flags and subcommands are in `ab help` and `ab <cmd> --help`.** This file is only what the CLI cannot tell you.

## Starting a job

- Find `ab` on PATH; else a checkout (`python3 <repo>/client/ab.py`); else ask the user for the path. Set `AB` once and reuse it.
- **Always `ab gateways`, then `ab health`, before any work.** `gateways` always exits 0 — a down gateway is data; `health` exits 1 when unreachable, so scripts branch on it. A config listing proves nothing: `--no-probe` contacts nothing.
- **Always `ab jobs`, then `ab sessions`, then `ab sessions --cwd <dir>` before submitting anything.** The first shows directories that have work; the second the sessions inside one.
- Always reuse existing sessions for the same project, and submit to the same session for the same subject. A new session is a new subject, not a new turn.

| | Situation | Do |
|---|---|---|
| 1 | a job is **running** on this work | `ab steer` |
| 2 | session **idle**, must see the message | `ab submit --session <id> --no-fork` |
| 3 | want its history, work **branches** | `ab submit --session <id>` |
| 4 | genuinely new subject | `ab submit` |

- **Record full UUIDs.** A prefix can later become ambiguous.
- Always pass prompts with `-F/--prompt-file` — avoids shell quoting and length bugs. Save prompt files in temp dirs, not the project tree, so they do not get uploaded or versioned.

## Briefing the remote agent

You cannot see the remote filesystem, versions, cluster state, or prior runs. The worker cannot see this conversation, the user's goal, or what you ruled out. Neither side can see its own blind spot, so say yours out loud.

- **Known** — the goal, constraints, what is ruled out and why
- **Assumed** — paths, modules, versions, data shapes you have *not* verified, labelled as assumptions
- **Unknown** — what you want discovered and reported back
- **Deliverable** — what the answer must contain to be usable without a follow-up
- **Results** — ask the worker to report, in plain language, what the problem or goal was, what it did, and what it found

## Long and batch jobs

- **A coding-agent worker cannot wait hours for scheduler work.** For batch jobs, tell the remote agent to submit, return the identifiers, and end its turn — and always have the batch script report itself with `ab-notify` (see the worker skill). If the remote agent is running background work rather than a scheduler job, it can monitor that itself and `ab-notify` when done.

## Reading results

- **`ab job REF` first.** The result is stored and printed whole — usually all you need.
- Then `ab events REF`, which reads from the **end** by default; `total`/`first_seq`/`last_seq` show the shape so you never page blind. `--after 0` restores top-down reading.
- Narrow with `--type` before widening `--tail`, or a long job's tool results will flood you.
- **Parse `--output json`**, and use `--output jsonl` when you want one typed record per line.
- **Exit 4 is a timeout, and the job is still running** — it never cancels unless `--cancel-on-timeout`. Exit 3 is the job failing; inspecting a failed job still exits 0 unless `--fail-on-job-failure`.
- **Transcripts are the last resort** — every tool call *and result*, megabytes. Filter on the remote host and download the extract. Claude Code keeps one file per session at `<home>/.claude/projects/<slugified-cwd>/<session-id>.jsonl` (growing = working); opencode keeps one SQLite db, so `download` cannot help — run `opencode export <sessionID>` on the host.

## Files

- Uploads take readable regular non-symlink files; duplicate remote names fail. Downloads preserve relative paths, reject traversal, symlink roots and collisions, and need explicit `--overwrite`.
- Anything outside `allowed_dirs` is unreachable — for bulk data, `rsync` into an allowed dir and attach the remote path.

## Repo

`API.md` (HTTP contract) · `client/README.md` (CLI) · `config.example.toml` (gateway settings) · `docs/design/` (why behaviour is the way it is) · `docs/todo/` (known gaps).
