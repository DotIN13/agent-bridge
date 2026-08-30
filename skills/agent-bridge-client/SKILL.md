---
name: agent-bridge-client
description: Drive a remote coding agent through an agent-bridge gateway using the `ab` CLI — ground a plan in the remote host with `ab info` and one short `ab run` alignment turn, brief the delegate from a file, wait for or stream jobs, steer live work, resume sessions, read progress from `$AB_JOB_DIR`, watch scheduler work with monitors, and transfer artifacts safely.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# agent-bridge (`ab` CLI)

For remote GPUs, schedulers, long jobs, or delegated work on another host. You plan and verify; the remote agent inventories, executes, and reports evidence.

**Flags and subcommands are in `ab help` and `ab <cmd> --help`.** This file is only what the CLI cannot tell you.

## Workflow

1. **`ab gateways`, `ab health` and `ab info`** — which are configured and which are actually reachable. `ab info` shows you the host as it actually is: CPU/RAM, local GPUs, Slurm partitions and their GPU inventory, allocation balance — and then, under a rule, the **operator notes**: what a person knows and a probe cannot find, like the account to charge, the partition that has the GPUs, the filesystem that is nearly full. Read this *before* writing a plan; half of what you would otherwise guess is stated here, and it costs no agent turn.
4. **`ab agents --output json`** — which backend you are driving and what it actually supports: sessions, fork, in-place resume, steering, thinking, attachments. A plan that assumes steering on a backend without it is infeasible before it starts.
5. **`ab jobs`** — is this work already running? If so, steer it instead of starting a second one.
6. **`ab sessions`**, then **`ab sessions --cwd <dir>`** — which directories have work, then the sessions in that project. Pick one to continue; a new session is a new subject.
7. **`ab run -F recon.md`** — recommended alignment turns before the real brief, in the session that will do the work. Required for anything needing a full brief; skipped for a lookup. See *Align the plan before you delegate*.
8. **`ab submit -F brief.md --session <uuid> --no-fork`** — always from a file, never an inline prompt. The brief **must** carry a Verification section naming the check that settles the work, and a Finishing section saying whether to commit, what the report must contain, and — when the work outlives the turn — that the worker registers a monitor and names it. A brief without the check produces a report you cannot tell from a guess. Full template below.
9. **`ab wait` asynchronously** — Use background bash jobs or Monitor tools to keep the wait in the background. A job is `succeeded` once its turn has ended **and** the worker has written `$AB_JOB_DIR/report.md`; between the two it reads `waiting`. Long running work like batch jobs that outlives the turn is a monitor, and `ab monitor <id> --wait` blocks on that instead.

## Starting a job

- Find `ab` on PATH; else a checkout (`python3 <repo>/client/ab.py`); else ask the user for the path. Set `AB` once and reuse it.
- `gateways` always exits 0 — a down gateway is data, not a failure. `health` exits 1 when unreachable, so scripts branch on that one. A config listing proves nothing: `--no-probe` contacts nothing.
- Reuse existing sessions for the same project, and submit to the same session for the same subject. A new session is a new subject, not a new turn.

| | Situation | Do |
|---|---|---|
| 1 | a job is **running** on this work | `ab steer` |
| 2 | session **idle**, must see the message | `ab submit --session <id> --no-fork` |
| 3 | want its history, work **branches** | `ab submit --session <id>` |
| 4 | genuinely new subject | `ab submit` |

- **Record full UUIDs.** A prefix can later become ambiguous.
- Always pass prompts with `-F/--prompt-file`, from a temp dir — see the brief template below.

## Align before you delegate

Run a short recon before submitting any substantial brief. Skip recon for simple lookups.

```bash
ab run -F recon.md --title <task>-recon --timeout 300 --output json

ab submit -F brief.md --session <uuid> --no-fork --title <task>
```

Use recon to:

* Verify the assumptions your full brief depends on.
* Discover environment facts that may change the plan: paths, versions, hardware, installed tools, existing outputs, and data shape.
* Estimate whether the work will outlive a single agent turn and require a monitor.
* Get an explicit feasibility verdict and an alternative when the proposed approach is wrong.
* Prevent the recon from starting the actual work.

Keep `recon.md` short. Use these sections:

```markdown
# Goal
# Check
# Constraints
# Output
```

After recon, fold confirmed facts into `Known`, correct or remove false assumptions, and leave only unresolved items in `Assumed`. It is recommended to resume the real work with the same session so the delegate retains the recon context.

## Briefing the remote agent

You cannot see the remote filesystem, versions, cluster state, or prior runs — which is what the alignment turn above is for. The worker cannot see this conversation, the user's goal, or what you ruled out. Neither side can see its own blind spot, so say yours out loud.

**Always write the brief to a file and submit it with `-F`.** Never inline a brief as a shell argument: quoting mangles it, length limits truncate it silently, and a file is the one artifact you can re-read, diff against the report, and resubmit after an edit. Keep it in a temp dir rather than the project tree, so it is never uploaded or committed by accident.

Six sections, in this order:

```markdown
# Goal          — what we are doing, and why it matters
# Task          — the steps, specifically
# Known         — settled facts; the delegate follows these
# Assumed       — unverified; the delegate confirms these and reports which held
# Verification  — the tests/benchmarks that confirm the work
# Finishing     — commit/push or not, what the report must contain, how to close the job
```

* Make verification concrete: commands, expected outputs, benchmarks, files, or comparison criteria.
* Require the report to cover completed work, decisions, verification results, assumption outcomes, and absolute paths to important artifacts.
* If the work will be long-running (1h+), ask remote to start the background/batch job work, register `ab-monitor`s, and write a preliminary report to signal job finish before the turn ends.

## Long and batch jobs

- **A job finishes when its turn ends and its report is written.** In between it is `waiting`: the worker's process is still alive, still steerable, and the gateway is watching for `report.md`. If none arrives within the grace window the job fails with `report_missing` — the deliverable is the point, so its absence is a failure rather than a footnote.
- **Work that outlives the turn is a monitor** — its own row, polled by the gateway, resolving hours later without holding the job open. Require one in the brief for anything scheduler-shaped, and require a **preliminary report** with it: that is what lets the job close instead of sitting in `waiting`. Its ending is recorded on the job's own event stream, so `ab events <ref> --type message` still tells you how the batch work finished.
- **One end to wait for.** `ab wait <ref>` returns when the row is terminal, which is when the turn ended. To block on the batch work instead, wait on its monitor: `ab monitor <id> --wait`.
- **A coding-agent worker cannot wait hours for scheduler work.** Tell it to submit, register a monitor, return the identifiers, and end its turn. The gateway polls; the worker does not sit there, and neither do you.

## Reading results

- **`ab job REF` first.** The turn's last message is stored and printed whole — usually all you need. `report.md` arrives as a `message` event, so `ab events REF --type message` is where the artifact itself is.
- **`waiting` means the turn ended and the report has not landed yet.** Give it a moment; it becomes `succeeded` within a sweep of the file appearing, or `failed` with `report_missing` at the deadline.
- Then `ab events REF`, which reads from the **end** by default; `total`/`first_seq`/`last_seq` show the shape so you never page blind. `--after 0` restores top-down reading.
- Narrow with `--type` before widening `--tail`, or a long job's tool results will flood you.
- **Parse `--output json`**, and use `--output jsonl` when you want one typed record per line.
- **Everything reported outside the turn arrives as `message` events** — files the worker wrote into `$AB_JOB_DIR`, and monitor transitions. `ab events REF --type message` is that progress log, separate from the agent's turn; what it *ran* is in `tool_use`/`tool_result`, and `--type` rejects anything not in its list.
- A `message` event carries the reporter's own `status` (`queued`/`running`/`finished`/`failed`), which is **not** the job's status: the row usually reads `succeeded` while a monitor on its batch work is still `running`. A monitor event also carries `monitor_status`, where `expired` means the gateway stopped watching rather than the work failing.
- **Exit 4 is a timeout, and the work is still outstanding** — it never cancels unless `--cancel-on-timeout`, and `ab monitor --wait` is the same: the watch continues. Exit 3 is the job failing; inspecting a failed job still exits 0 unless `--fail-on-job-failure`.
- **Transcripts are the last resort** — every tool call *and result*, megabytes. Filter on the remote host and download the extract. Claude Code keeps one file per session at `<home>/.claude/projects/<slugified-cwd>/<session-id>.jsonl` (growing = working); opencode keeps one SQLite db, so `download` cannot help — run `opencode export <sessionID>` on the host.

## Files

- Uploads take readable regular non-symlink files; duplicate remote names fail. Downloads preserve relative paths, reject traversal, symlink roots and collisions, and need explicit `--overwrite`.
- Anything outside `allowed_dirs` is unreachable — for bulk data, `rsync` into an allowed dir and attach the remote path.

## Repo

`API.md` (HTTP contract) · `client/README.md` (CLI) · `config.example.toml` (gateway settings) · `docs/design/` (why behaviour is the way it is) · `docs/todo/` (known gaps).
