# 06 — Make the CLI API legible and reliable for agents

**Severity:** medium
**Status:** **done** in 0.3.0
**Scope:** `client/ab.py`, `client/abclient.py`, packaging, tests, and
agent-facing documentation.

## Problem

The original CLI operations existed, but agents could not reliably compose
them: global flags were position-sensitive, streaming could corrupt JSON,
follow ignored filters, wait/timeout exits differed, downloads flattened paths,
and package installation created no commands.

## Implementation

### Stable invocation and discovery

- Global `--gateway`, `--config`, `--output`, `--json`, and `--full` work before
  or after a subcommand.
- Added `--version`, `health`, `agents`, `capabilities`, `help --remote`, and
  `wait`; root help describes every command.
- Installed console scripts: `ab`, `agent-bridge`, and `ab-notify`. Legacy
  direct script/module invocation remains supported.
- `agents` consumes backend model/default/capability data, so clients can detect
  sessions, in-place resume, steering, thinking, and attachments.

### Machine output and exits

`--output` now defines a protocol:

- `human` is the readable default; only it uses optional `--full` display
  elision.
- `json` is one complete faithful document; `--json` is an alias.
- `jsonl` is one typed record per line for streaming operations, including
  `event`, `terminal`, `timeout`, and bounded `complete` records.

`run --stream --output json` no longer mixes raw text into JSON.
`events --follow --output jsonl` honors `--after`, `--until`, and repeatable
`--type`.

Exit codes are centralized:

| Code | Meaning |
|---:|---|
| 0 | success/query complete |
| 1 | local config/transport/protocol/file error |
| 2 | invalid invocation |
| 3 | waited remote job failed/canceled |
| 4 | wait timeout while the remote job may continue |

Timeout does not cancel unless `--cancel-on-timeout` is explicit. Snapshot
inspection remains successful by default, with `--fail-on-job-failure` when a
remote terminal failure should become the process outcome.

### Shared streaming and waiting

- `abclient.Client` implements one resumable SSE parser with heartbeat handling,
  `Last-Event-ID`, duplicate suppression, bounded reconnects, and JSON fallback.
- `run`, `wait`, and `events --follow` use the shared operation instead of
  independent polling/rendering loops.
- `wait REF` and `job REF --wait` wait for an existing job and return its id and
  state on timeout.

### Prompt and validation behavior

- `-F/--prompt-file` remains the recommended agent-safe path.
- Added explicit `--prompt-stdin`; legacy piped-stdin behavior remains.
- Local validation rejects `--no-fork` without `--session`, invalid event
  types/ranges, conflicting output flags, and nonpositive bounds/timeouts.
- Submission accepts an optional `--idempotency-key` for retry-safe creation.

### Safe file transfers

- Uploads require readable regular non-symlink files and reject duplicate
  remote destinations.
- `--upload-as REMOTE=LOCAL` / `upload --as REMOTE=LOCAL` preserves explicit
  remote identity; directory upload does not follow symlinks.
- Downloads preserve relative paths by default, reject traversal, symlink
  roots, duplicate targets, and existing files, and publish via atomic temporary
  files.
- `--overwrite` is explicit. `--flatten` retains the old basename layout but
  still rejects collisions.

## Compatibility decisions

- Existing command names, `--json`, `events -f`, `job`, and direct Python
  invocation remain.
- Machine JSON changed from presentation-elided data to faithful data; human
  output retains elision.
- Implicit piped stdin remains for compatibility, but explicit prompt files or
  `--prompt-stdin` are the documented agent contract.

## Verification

Client tests cover global parsing, output conflicts, help/version, faithful
JSON, parseable JSONL, follow filters, exit codes, SSE parsing/fallback, upload
and download safety, shared versioning, and isolated editable-install smoke
execution of all three console scripts. The full
repository suite is the release gate.
