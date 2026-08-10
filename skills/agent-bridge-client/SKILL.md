---
name: agent-bridge-client
description: Drive a remote coding agent through an agent-bridge gateway using the `ab` CLI — submit prompts, stream or wait for jobs, steer live work, and transfer artifacts safely.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# agent-bridge (`ab` CLI)

Use this skill for remote GPUs, schedulers, long jobs, or delegated work on
another host. The local session plans and verifies; the remote agent inventories,
executes, and reports evidence.

## Setup and reachability

Installed command:

```bash
ab --version
ab gateways                         # local config only
ab health --gateway <name>          # real reachability/version probe
```

Legacy invocation is `python3 <repo>/client/ab.py`. Client config discovery is
`--config`, `$AGENT_BRIDGE_CLIENT_CONFIG`,
`~/.config/agent-bridge/gateways.json`, then `./gateways.json`.

Open the SSH tunnel with keepalives. On Windows Git Bash, prefix calls containing
remote POSIX paths with `MSYS_NO_PATHCONV=1`; PowerShell needs no prefix.

## Agent-safe command surface

| Command | Use |
|---|---|
| `gateways` | local gateways/token presence |
| `health` | liveness and version |
| `agents` | backends, models, capability flags |
| `capabilities` | structured client/server contract |
| `info` | host, GPU, scheduler, allocation info |
| `sessions` | resumable context |
| `jobs` | paged recent summaries |
| `submit -F FILE` | submit and return immediately |
| `run -F FILE` | submit and wait |
| `job REF` | full detail/result |
| `wait REF` | wait for an existing job |
| `events REF -f` | resumable SSE follow |
| `steer REF -F FILE` | redirect a running turn |
| `cancel REF` | interrupt queued/running work |
| `upload`, `download`, `ls` | safe files |

`REF` is a full UUID, title, or unique id prefix. Record full UUIDs because a
prefix can later become ambiguous.

**Always write prompts to a file and pass `-F/--prompt-file`.** This avoids shell
quoting and command-length bugs. `--prompt-stdin` is the explicit stdin form.

## Output protocol

Use `--output json` for one complete machine document (`--json` is an alias).
Use `--output jsonl` for `run`, `wait`, and `events --follow`: every line is one
parseable record with `kind` `event`, `terminal`, `timeout`, or `complete`.
Human output is the default; only human display text is elided, and `--full`
disables that.

Global flags work before or after the command:

```bash
ab --gateway gw --output json jobs
ab jobs --gateway gw --output json
```

Exit codes:

| Code | Meaning |
|---:|---|
| 0 | successful command/query |
| 1 | local config, transport, protocol, or file failure |
| 2 | invalid invocation |
| 3 | waited remote job failed/canceled |
| 4 | wait timeout; remote job may continue |

Timeout never cancels unless `--cancel-on-timeout` is explicit. Snapshot
inspection of a failed `job`/`events` still succeeds unless
`--fail-on-job-failure` is requested.

## Continue existing work first

Before submitting, inspect `ab jobs --output json` and
`ab sessions --output json`, then choose the first match:

1. **Running job:** `ab steer <ref> -F nudge.md`.
2. **Idle session must see follow-up:**
   `ab submit -F followup.md --session <uuid> --no-fork`.
3. **Need history but work branches:**
   `ab submit -F task.md --session <uuid>`.
4. **Genuinely new subject:** omit `--session`.

`--no-fork` requires an idle session. The gateway refuses a live writer with a
`session_busy` error naming the holding job and steer reference. Do not work
around it.

Steer is accepted into the live input channel and observed by the model at its
next tool boundary. A `202` is not a strict exactly-once model-action promise;
watch the `steer` event to see where it landed.

Use `ab agents --output json` to verify per-adapter capabilities before relying
on sessions, steering, thinking, or attachments. Select backend and model per
job with `--agent` and `--model`.

## Reliable long jobs

Prefer submit + follow/wait over a single blocking harness call:

```bash
ab submit -F task.md --title nightly --idempotency-key nightly-v1 --output json
ab events nightly --follow --type assistant --output jsonl > nightly.events.jsonl
ab wait nightly --timeout 1800 --output json
ab job nightly --output json
```

Reuse one `--idempotency-key` only for retries of the same semantic submission.
The gateway returns the original job; a changed request under the same key is a
conflict.

Follow uses SSE with event cursor replay and honors `--after`, `--until`, and
repeatable `--type`. The final job result from `ab job` is complete and should
normally be read before the larger event log.

Thinking is not persisted unless `--include-thinking` was supplied at submit.
Enable it only when reasoning is genuinely required.

## Batch work and `ab-notify`

A coding-agent turn cannot wait hours for scheduler work. Tell the remote agent
to submit and return the scheduler/job identifiers. Make the batch script report
itself:

```bash
#SBATCH --export=ALL,AB_JOB_ID=<ab-job-uuid>,AB_DATA_DIR=<gateway-data-dir>
ab-notify --status running  --msg "server up" --report-id run-start
ab-notify --status finished --report "$RUNS/RESULTS.md" --report-id run-finished
ab-notify --status failed   --msg-file "$RUNS/error.log" --report-id run-failed
```

`ab-notify` tries HTTP, shared JSONL, then local temporary JSONL. URL discovery:
`--url`, `$AB_URL`, `gateway-endpoint.json`; token discovery: `--token`,
`$AB_TOKEN`, `<data_dir>/.token`. Do not expose the token in scheduler submit
arguments.

Batch `message` events are **annotations**, not a second job status. The coding
agent may already be `succeeded`, and its SSE follow closes at that terminal
state. Later reports are retrieved with a new event query/follow from the last
cursor. One open stream does not wait forever for external compute.

`report_id` deduplicates retries. A local-only fallback exits zero because the
message is durably written, but it is not ingestible until that file is moved to
the shared messages directory; read stderr.

## Safe files

Uploads accept readable regular non-symlink files. Preserve a chosen remote name:

```bash
ab submit -F task.md --upload-as inputs/data.csv=./data.csv
ab upload --as inputs/config.json=./config.json
```

Duplicate remote names fail. Directory upload does not follow symlinks.

Downloads preserve relative paths under `--to`, reject traversal/symlink roots,
collisions, and existing targets, and use atomic temporary files:

```bash
ab download --dir /project/x/out --glob '*.csv' --recursive --to ./out
```

Use `--overwrite` explicitly. `--flatten` requests the legacy basename layout,
but collisions remain errors.

Anything outside gateway `allowed_dirs`/file storage is inaccessible. For large
data, use `rsync` into an allowed directory and attach the remote path.

## Verification discipline

- Do not relay the remote agent's conclusion without checking its evidence.
- A terminal gateway job means the coding-agent turn ended; it may only have
  submitted external work.
- Prefer structured `job`, `events`, and downloaded artifacts to filesystem
  mtime inference.
- Distinguish tunnel failure from gateway failure; `gateways` succeeding proves
  neither.
- Cancellation interrupts first so the transcript can flush; escalation is a
  fallback.
- A gateway restart explicitly fails formerly running rows and requeues queued
  rows, rather than leaving them silently stale.

## Repository references

- `API.md` — typed HTTP contract
- `client/README.md` — CLI reference
- `config.example.toml` — gateway settings
- `skills/agent-bridge-worker/` — remote worker conventions
