---
name: agent-bridge-client
description: Drive a remote coding agent through an agent-bridge gateway using the `ab` CLI — submit prompts, stream or wait for jobs, steer live work, and transfer artifacts safely.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, TodoWrite, WebFetch, WebSearch
---

# agent-bridge (`ab` CLI)

Use this skill for remote GPUs, schedulers, long jobs, or delegated work on
another host. The local session plans and verifies; the remote agent inventories,
executes, and reports evidence.

## Finding `ab`

Resolve the command once, at the start of the session, and reuse it. Do not
assume it is installed.

1. **On `PATH`** — the normal case once the package is installed:
   ```bash
   ab --version
   ```
2. **A repo checkout** — search the workspaces you can see before asking. The
   entry point is `client/ab.py`:
   ```bash
   ls ~/agent-bridge/client/ab.py ./client/ab.py ../agent-bridge/client/ab.py 2>/dev/null
   # or, if those miss:
   find ~ /workspace /srv -maxdepth 4 -path '*/client/ab.py' 2>/dev/null | head
   ```
   Then invoke it as `python3 <repo>/client/ab.py` — no install needed, stdlib
   only.
3. **Ask.** If neither turns it up, ask the user where the checkout is (or
   whether to `pip install -e` it) rather than guessing a path or giving up.

Set `AB` once and reuse it, so the rest of the session reads the same whichever
form you found:

```bash
AB="ab"                                    # or: AB="python3 /path/to/client/ab.py"
$AB health
```

Examples below write plain `ab` for brevity; substitute whichever form resolved.

## Reachability

```bash
$AB gateways        # every configured gateway: token, reachability, version
$AB gateways --no-probe   # local config only; contacts nothing
$AB health          # one gateway; exit code carries the answer
```

`ab gateways` probes each gateway concurrently and reports two independent
facts per row — whether a **token** loaded, and whether it is actually **up**:

```
 * alpha   http://127.0.0.1:8799   token     up 0.3.0 (47 ms)
   beta    http://127.0.0.1:8801   token     HTTP_ERROR — HTTP 404 from /health; is this an agent-bridge?
   gamma   http://127.0.0.1:8802   no token  REFUSED — nothing is listening; SSH forward or gateway is down
```

It **always exits 0** — a down gateway is data, not a command failure. To
branch in a script, use `ab health` (exit 1 when unreachable) or read
`state`/`reachable` from `ab gateways --output json`.

**`--no-probe` reports configuration only and cannot tell you anything is
working.** It is for the "is my config even loading" question, and it is the
one command that works with no network.

Reachability failures name their own cause, and the two need opposite fixes:

| Symptom | Cause | Fix |
|---|---|---|
| connection **refused** (`WinError 10061`, `ECONNREFUSED`) | no local listener — the SSH forward is down | reopen the tunnel |
| connection **reset** / dropped mid-response | forward is up, nothing serving at the far end | restart the gateway on its host |

Open the tunnel with keepalives, because an idle forward drops silently:

```bash
ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -L <port>:localhost:<port> <host>
```

`autossh -M 0` with the same flags reconnects on its own. If the gateway can
move between hosts, confirm which one is serving before repointing the forward —
`ab info` prints the host.

Client config discovery is `--config`, `$AGENT_BRIDGE_CLIENT_CONFIG`,
`~/.config/agent-bridge/gateways.json`, then `./gateways.json`.

## Windows: `MSYS_NO_PATHCONV=1` on every Git Bash call

Every path `ab` takes is a **remote POSIX path**, and Git Bash rewrites
POSIX-looking arguments into Windows paths before the program sees them:

```
you type:   --cwd /project/data/x
ab sees:    'C:/Program Files/Git/project/data/x'
```

This hits `--cwd`, `--file`, `--dir`, `--to`, and every other path argument.
Usually it surfaces as a `400` — but a rewritten path **can also resolve
somewhere real and wrong**, which is the case that costs you an afternoon.

```bash
MSYS_NO_PATHCONV=1 $AB submit -F task.md --cwd /project/data/x
```

Two things that trip people up:

- **`MSYS2_ARG_CONV_EXCL='*'` does not work here.** It is MSYS2-proper; Git for
  Windows ignores it silently and rewrites the paths anyway. Verified against a
  live gateway.
- **Shell state does not persist between tool calls**, so set it inline on every
  invocation rather than exporting it once.

**PowerShell needs no prefix** — it does no path rewriting, and is the simpler
fallback if the escaping gets tiresome.

## Agent-safe command surface

| Command | Use |
|---|---|
| `gateways` | every gateway: token presence **and** live reachability |
| `health` | one gateway's liveness/version; exit code carries it |
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

### Prompt shape for scheduler work, learned the hard way

Put these in the brief. Each one corresponds to a way this has actually failed:

- **Lead with the action, not the prohibition.** Opening with "do NOT wait for
  the job" primes the agent to relay status instead of working. Say "submit it
  and report the job id" first.
- **Say explicitly that there is no background job to report on** — otherwise a
  turn ends on a promise to check back, which the gateway records as success.
- **Require interleaved evidence**: print each result as it is known, not in a
  closing summary that may never be reached.
- **Give permission to ship incomplete** — a submitted job with known gaps beats
  a perfect plan that never ran.
- **`mkdir -p` the scheduler's output directory from the submitting shell,
  before submitting.** Slurm opens `--output` *before* the script runs, so an
  in-script `mkdir` is too late: the job dies in about a second with no logs at
  all, which reads exactly like "never queued".
- **Heartbeat as the script's first action**, so an empty output directory means
  *died* rather than *queued*.

### Three states, not two

When inferring a job's state from the filesystem, keep these distinct:

| Observation | Means |
|---|---|
| can't reach the gateway | **nothing** about the job |
| reached it, nothing there | not submitted, or died before writing |
| files present | running |

Collapsing the first into the second is how a live job gets reported as dead.
Prefer `ab events` over mtime inference wherever `ab-notify` is wired up.

## Recovering output when the job row can't help

If the row is stale or the run wrote no artifact, the session transcript is the
fallback — and its shape depends on the backend:

- **Claude Code** keeps one file per session, reachable with `ab ls` /
  `ab download`:
  ```
  <remote home>/.claude/projects/<slugified-cwd>/<session-id>.jsonl
  ```
  A growing file means the agent is still working. Downloading it recovers the
  text even when the job row says nothing useful.
- **opencode** keeps sessions in one SQLite database
  (`<remote home>/.local/share/opencode/opencode.db`) with no per-session files,
  so `ab download` cannot help. Read it on the gateway host with
  `opencode export <sessionID>` or `opencode session list`; the id to export is
  the job's `forked_session`.

This is the most expensive read available — a transcript is every tool call and
every tool result. Filter on the remote host and download the extract, not the
whole file.

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
- Distinguish tunnel failure from gateway failure; `gateways --no-probe`
  succeeding proves neither.
- Cancellation interrupts first so the transcript can flush; escalation is a
  fallback.
- A gateway restart explicitly fails formerly running rows and requeues queued
  rows, rather than leaving them silently stale.

## Repository references

- `API.md` — typed HTTP contract
- `client/README.md` — CLI reference
- `config.example.toml` — gateway settings
- `skills/agent-bridge-worker/` — remote worker conventions
