# agent-bridge

A typed HTTP gateway and agent-first CLI for running coding-agent prompts in
named sessions on remote machines. It is designed for a shared HPC login node:
one SSH port-forward, bearer auth, a bounded worker queue, SQLite persistence,
resumable SSE, and stdlib-only clients.

Claude Code and opencode implement the same adapter interface. Every job chooses
an `agent`, optional `model`, cwd, and session policy.

```text
laptop: ab
       │  POST /v1/jobs; resumable GET /events
       ▼
FastAPI gateway ── bounded workers ── Claude Code / opencode
       │
       ├── SQLite jobs + one monotonic event stream per job
       └── files + shared ab-notify fallback
```

## Install and run

```bash
python -m pip install -e .
cp config.example.toml config.toml   # edit allowed_dirs and agents
agent-bridge --config config.toml
```

Stable console commands are installed together:

- `agent-bridge` — gateway process
- `ab` — CLI
- `ab-notify` — compute/batch reporter

Legacy invocation remains supported: `python -m gateway`,
`python client/ab.py`, and `bin/ab-notify`.

On a laptop, keep one tunnel alive:

```bash
ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 \
  -L 8787:localhost:8787 midway5
```

Configure gateways in `~/.config/agent-bridge/gateways.json`; see
`client/gateways.example.json` and [client/README.md](client/README.md).

## Agent-first CLI

```bash
ab --version                         # 0.3.0
ab health
ab agents --output json
ab sessions --cwd /project/x --output json
ab submit -F task.md --title nightly --idempotency-key nightly-v1
ab events nightly --follow --type assistant --output jsonl
ab wait nightly --timeout 900 --output json
```

Global flags work before or after the command. `--output` modes are:

- `human` — readable UI; `--full` disables display elision.
- `json` — exactly one faithful document; `--json` is an alias.
- `jsonl` — typed streaming records (`event`, `terminal`, `timeout`,
  `complete`).

New discovery/lifecycle commands include `health`, `agents`, `capabilities`,
`help --remote`, and `wait`. Follow uses resumable SSE and honors `--after`,
`--until`, and repeatable `--type` filters.

| Exit | Meaning |
|---:|---|
| 0 | success/query completed |
| 1 | local config, transport, protocol, or file failure |
| 2 | invalid invocation |
| 3 | waited remote job failed/canceled |
| 4 | wait timeout; remote job may still be running |

Timeout does not cancel unless `--cancel-on-timeout` is explicit. Prefer
`-F/--prompt-file` for agent-written prompts.

## Continue rather than restart

Pick the first operation that matches:

```bash
ab steer <ref> -F nudge.md                       # job running now
ab submit -F followup.md --session <uuid> --no-fork  # idle session, in place
ab submit -F branch.md --session <uuid>          # fork existing history
ab submit -F new.md                              # genuinely fresh subject
```

`--no-fork` requires an idle session. The gateway refuses a busy target with a
typed `session_busy` error containing the holding job and steer reference.
`steer` reaches a running turn at its next tool boundary; accepted delivery is
not a strict exactly-once model-action guarantee.

Use `ab agents --output json` to discover which adapter/mode supports sessions,
forking, in-place resume, steering, thinking, and attachments. `/v1/models` and
`ab models` remain compatibility projections of configured model ids.

## Backend contract

Full reference: [API.md](API.md). Live instances serve `/llms.txt`, `/v1/help`,
`/openapi.json`, `/docs`, and `/redoc`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | public liveness/version |
| GET | `/llms.txt`, `/v1/help` | live agent guide |
| GET | `/v1/agents` | agents, models, capabilities, features |
| GET | `/v1/info`, `/v1/sessions` | cluster and session discovery |
| POST/GET | `/v1/jobs` | idempotent submit; paged summaries |
| GET | `/v1/jobs/{ref}` | full typed detail |
| GET | `/v1/jobs/{ref}/events` | JSON page or resumable SSE |
| POST | `/v1/jobs/{ref}/steer`, `/cancel`, `/message` | semantic actions/reports |
| POST/GET | `/v1/files`, `/v1/files/list`, `/v1/files/content` | file transfer |

Errors use one envelope:

```json
{"error":{"code":"session_busy","message":"…","details":{"held_by":"…"}}}
```

Job submissions and public responses are typed; unknown fields are rejected.
`GET /v1/jobs` returns bounded summary pages rather than prompts/results.
Jobs, events, and files use validated limits and opaque cursors. Every event
source shares one transactional monotonic sequence, so `after` and
`Last-Event-ID` are safe.

`Idempotency-Key` makes job-creation retries return the original id. Attachments
are staged before the job is visible, preventing phantom queued rows. On gateway
startup, queued jobs are re-enqueued and stale running rows become explicit
restart failures. Shutdown interrupts and joins workers before closing SQLite.

Route aliases (`/v1/help`, `/v1/models`, singular `/message`) are retained for
compatibility. Waiting remains a client operation; there is no synchronous
server `run` endpoint.

## Files

Uploads accept inline/path references or multipart files. Duplicate and unsafe
names are rejected. The client requires regular non-symlink local files and
supports explicit remote names:

```bash
ab submit -F task.md --upload-as inputs/data.csv=./data.csv
ab upload --as inputs/task.md=./task.md
```

Downloads preserve remote relative paths under `--to`, reject traversal,
symlink roots, collisions, and existing files, and publish through atomic
temporary files. Use `--overwrite` explicitly. `--flatten` is a legacy layout;
collisions are still errors.

## Batch and external reports

An agent turn may finish after submitting Slurm work. Put `ab-notify` in the
batch script:

```bash
#SBATCH --export=ALL,AB_JOB_ID=<job-uuid>,AB_DATA_DIR=<gateway-data-dir>
ab-notify --status running  --msg "server up" --report-id run-start
ab-notify --status finished --report "$RUNS/RESULTS.md" --report-id run-finished
ab-notify --status failed   --msg-file "$RUNS/error.log" --report-id run-failed
```

Delivery tries HTTP, shared `<data_dir>/messages/<job>.jsonl`, then local
`$TMPDIR`. HTTP URL discovery is `--url`, `$AB_URL`, then
`gateway-endpoint.json`; token discovery is `--token`, `$AB_TOKEN`, then
`<data_dir>/.token`.

These `message` events are **post-terminal annotations**, not a second job
status machine. The coding-agent SSE closes when the job becomes terminal.
Reports arriving later are retrieved by reconnecting with the last cursor or
polling events. `report_id` deduplicates a retried report.

## Configuration and operations

See `config.example.toml`. Important controls:

- `[server] host/port` — bind loopback behind SSH, or an internal address when
  compute nodes need direct report HTTP.
- `[worker] concurrency`, `cancel_grace_sec`.
- `[files] enabled`, store, per-file and request bounds.
- `[messages] dir` — shared filesystem fallback.
- `[agents.<name>] allowed_dirs`, `default_cwd`, `dispatch_mode`, model catalog,
  permission mode, and timeout.

Only the login-node gateway writes SQLite WAL. Compute nodes append JSONL when
HTTP is unavailable. Scope `allowed_dirs` deliberately; noninteractive
permission modes let an agent edit and execute within those roots.

A systemd user service example is in `systemd/agent-bridge.service`. Skills for
both ends of the workflow live under `skills/`.

## Development

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m compileall -q gateway client tests
```

Backend tests cover typed OpenAPI/errors, public DTOs, idempotency, monotonic
cursors, attachment atomicity, restart recovery, bounds, and capabilities.
Client tests cover parser/output/exit contracts, SSE, safe files, and package
entry points.
