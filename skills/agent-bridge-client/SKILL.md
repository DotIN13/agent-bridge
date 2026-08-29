---
name: agent-bridge-client
description: Drive a remote coding agent through an agent-bridge gateway using the `ab` CLI — submit prompts, stream or wait for jobs, steer live work, resume sessions, and transfer artifacts safely.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# agent-bridge (`ab` CLI)

For remote GPUs, schedulers, long jobs, or delegated work on another host. You plan and verify; the remote agent inventories, executes, and reports evidence.

**Flags and subcommands are in `ab help` and `ab <cmd> --help`.** This file is only what the CLI cannot tell you.

## Workflow

1. **`ab gateways`** — which are configured and which are actually reachable. Never assume; a down gateway is data, not an error.
2. **`ab health`** — the one you are about to use. Exits 1 when unreachable, so branch on it before doing anything else.
3. **`ab jobs`** — is this work already running? If so, steer it instead of starting a second one.
4. **`ab sessions`** — which directories have work at all.
5. **`ab sessions --cwd <dir>`** — the sessions in that project. Pick one to continue; a new session is a new subject.
6. **`ab submit -F brief.md [--session <id>]`** — always from a file, never an inline prompt. The brief **must** carry a Verification section naming the check that settles the work, and a Finishing section saying whether to commit, what the report must contain, and that `ab-notify` closes the job. A brief without the close is a job that hangs; a brief without the check produces a report you cannot tell from a guess. Full template below.
7. **`ab wait` asynchronously** — bounded `--timeout`, or in the background. Never block your turn on it: use `--for turn` to collect what the agent said now, and come back for the report later.

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

## Briefing the remote agent

You cannot see the remote filesystem, versions, cluster state, or prior runs. The worker cannot see this conversation, the user's goal, or what you ruled out. Neither side can see its own blind spot, so say yours out loud.

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

**Verification and Finishing are required.** Without them you get work you cannot check and a job that never closes.

- **Goal** — what the work is for, in a sentence or two, and why it matters. A delegate that knows the point can make a judgment call when it hits something you did not anticipate; one holding only an instruction follows it off a cliff.
- **Task** — the steps, numbered, concrete enough to act on: which script, which directory, which data, in what order. Name what is *out* of scope too. For a lookup, hand over the exact command. For an investigation, hand over the **question** instead of a step list — prescribed steps become dead weight when the premise is wrong.
- **Known** — the facts the delegate must treat as settled and work within: paths, module loads, versions, the account to charge, the partition to use, what has already been ruled out and why. Say that these are given, not up for improvement; a delegate that re-litigates them burns the turn.
- **Assumed** — everything you believe but have *not* verified, labelled as such: paths you think exist, flags you think the script takes, the shape of the data. **Require the delegate to confirm each one and say in the report which held and which did not.** An unrefuted wrong assumption goes straight into your next brief, which is how a whole chain of jobs inherits one mistake.
- **Verification** — the tests, benchmarks or checks that confirm the work, named concretely: the command to run and what a pass looks like, the number that must move, the file that must exist, the baseline to compare against. Require the **evidence** in the report rather than a claim about it — *"ran the tests"* is not evidence, the tail of the output is. State the three rules the delegate will not otherwise assume: **a claim of done rests on output it actually saw**, not on what the step should have produced; **a step that failed, was skipped or was substituted goes in the first sentence** of the report, ahead of the successes; **anything not run is named `NOT-RUN`**, never a plausible-looking value. You cannot rerun the check from here, which is exactly why it has to be in the brief. Then verify what you can: read the report against `ab events REF --type tool_use`, or download the artifacts and look at them.
- **Finishing** — three things, all explicit:
  - **Git.** Whether to commit, whether to push, and to which branch — or not to. Say it either way: *"commit to `<branch>` and push"*, or *"leave the tree dirty, do not commit"*. A delegate guessing at this is how work lands on `main` or is lost with the session.
  - **The report, and it should be comprehensive rather than short.** It is stored whole and it is the only window you have; a report that saves you a page of reading and costs you a follow-up job is a bad trade. Comprehensive in *coverage*, not a log dump — the bulk belongs in the files it points you at. Require: **what it did for each step** in Task; **the decisions it made and the methodology behind them** — what it chose, what it rejected, and why; **the verification output and the results**, numbers with the conditions that produced them; **which assumptions held**; and **absolute paths to the result files and to the important process files** — scripts, configs, logs, the sbatch file — so you can fetch exactly those with `ab download` instead of trawling a transcript.
  - **The close.** `ab-notify --status finished` — or `failed`, with the reason — is what closes the job. By default the row parks in `awaiting_report` when the turn ends, so a brief that omits this is a job that hangs until `report_timeout`, and you learn only that the worker went quiet. The call needs the job id, which you do not have until `ab submit` returns it, so deliver it one of these ways:
    1. **First steer, right after submit** — `ab steer <ref> -F id.md` carrying *"you are job `<uuid>`; close it with `ab-notify --status finished --job-id <uuid>`"*. Reaches the turn at its next tool boundary. Needs the job to be running, so retry if it is still queued.
    2. **A distinctive `--title`**, and a brief that tells the delegate to find its own row with `ab jobs`.
    3. **`--no-expect-report`**, when the turn genuinely is the whole job — then nothing has to close it.

    If the work outlives the turn, the *batch script* owns the call, on every exit path; background work the delegate started itself, it monitors itself and reports when done. Both are the worker skill's subject.

## Long and batch jobs

- **Every job waits for a report by default.** When the turn ends the job parks in `awaiting_report`, and only `ab-notify --status finished|failed` closes it — so `ab wait` waits for the work, not for the agent to stop talking. Say so in the brief: the worker must report, or the job never closes.
- **`--no-expect-report` for a turn that is the whole job** — a question answered, a file read, anything with no work outliving the turn. Use it deliberately; a job whose worker never reports sits until the gateway's deadline and then fails with `report_timeout`, which means it went quiet, not that the work failed.
- A parked job is **not** `running`: no agent is alive, its session is free to resume, and it survives a gateway restart.
- **`ab wait` has two ends to choose between.** Default waits for both — the job is done. `--for turn` returns as soon as the agent stops talking, with the work still out there, which is what you want before steering or reading the turn's own result. `--for report` waits specifically for the `ab-notify` finish.
- **A coding-agent worker cannot wait hours for scheduler work.** For batch jobs, tell the remote agent to submit, return the identifiers, and end its turn — and always have the batch script report itself with `ab-notify` (see the worker skill). If the remote agent is running background work rather than a scheduler job, it can monitor that itself and `ab-notify` when done.

## Reading results

- **`ab job REF` first.** The result is stored and printed whole — usually all you need.
- Then `ab events REF`, which reads from the **end** by default; `total`/`first_seq`/`last_seq` show the shape so you never page blind. `--after 0` restores top-down reading.
- Narrow with `--type` before widening `--tail`, or a long job's tool results will flood you.
- **Parse `--output json`**, and use `--output jsonl` when you want one typed record per line.
- **`ab-notify` reports arrive as `message` events** — `ab events REF --type message` is the batch job's own progress log, separate from the agent's turn. What it ran is in `tool_use`/`tool_result`; there is no `ab-notify` or `tool` type, and `--type` rejects anything not in its list.
- A `message` event carries the reporter's own `status` (`queued`/`running`/`finished`/`failed`), which is **not** the job's status. By default the row stays `awaiting_report` until a `finished`/`failed` report closes it; under `--no-expect-report` the row reads `succeeded` while its batch work is still `running`.
- **Exit 4 is a timeout, and the work is still outstanding** — it never cancels unless `--cancel-on-timeout`. Check the row before assuming an agent is alive: `awaiting_report` means the turn ended and the report has not come. Exit 3 is the job failing; inspecting a failed job still exits 0 unless `--fail-on-job-failure`.
- **Transcripts are the last resort** — every tool call *and result*, megabytes. Filter on the remote host and download the extract. Claude Code keeps one file per session at `<home>/.claude/projects/<slugified-cwd>/<session-id>.jsonl` (growing = working); opencode keeps one SQLite db, so `download` cannot help — run `opencode export <sessionID>` on the host.

## Files

- Uploads take readable regular non-symlink files; duplicate remote names fail. Downloads preserve relative paths, reject traversal, symlink roots and collisions, and need explicit `--overwrite`.
- Anything outside `allowed_dirs` is unreachable — for bulk data, `rsync` into an allowed dir and attach the remote path.

## Repo

`API.md` (HTTP contract) · `client/README.md` (CLI) · `config.example.toml` (gateway settings) · `docs/design/` (why behaviour is the way it is) · `docs/todo/` (known gaps).
