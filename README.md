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
       └── files, per-job report dirs, gateway-polled monitors
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
- `ab-monitor` — register a watch on work that outlives a turn
- `ab-notify` — report a milestone from inside a job

Legacy invocation remains supported: `python -m gateway`,
`python client/ab.py`, `bin/ab-monitor`, and `bin/ab-notify`.

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
| GET | `/v1/info`, `/v1/sessions` | cluster and session discovery, plus operator notes |
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

## Reporting, and work that outlives a turn

Every job is handed a directory of its own in `$AB_JOB_DIR`
(`<data_dir>/reports/<job-id>`, created before the agent starts). It reports by
writing files there — no job id, url or token:

```bash
echo "12/24 sources done" > "$AB_JOB_DIR/progress/020-sources.md"
cp "$RUNS/RESULTS.md"       "$AB_JOB_DIR/report.md"
echo finished             > "$AB_JOB_DIR/status"       # or: failed
```

Each readable file becomes one `message` event, deduplicated by path *and*
content, so rewriting a file with new content reports again and rewriting it
unchanged does not. Compute nodes write here too, which is why the data dir
belongs on the shared filesystem.

A job goes terminal when its turn does. Work that outlives the turn is a
**monitor**: its own row, with a poll command the delegate authors, run by the
gateway on a timer.

```bash
JOBID=$(sbatch --parsable run.sbatch)
ab-monitor add --slurm "$JOBID" --label train --interval 15m --deadline 12h \
  --result "$RUNS/RESULTS.md"
```

`--slurm` reads `sacct` rather than `squeue`, which forgets a job once it leaves
the queue. Anything else is `--poll <cmd>`, whose first word of output is the
status; plain words (`running`/`finished`/`failed`) and Slurm state names are
both understood, and `--map` covers the rest. `ab-monitor` only writes a
key-value file into `$AB_JOB_DIR/monitors/`, so a heredoc does the same job when
it is not on PATH.

Callers read watches with `ab monitors --job <ref>` and `ab monitor <id>
[--wait]`; transitions also land on the job's `message` stream as post-terminal
annotations. `[monitors]` bounds how many watches a gateway keeps, the interval
floor, the poll timeout, and the deadline ceiling.

`ab-notify --msg "12/24 done" [--report-id sources]` is a convenience for the
milestone write: it names the file so milestones sort the way they happened, and
a retry under the same `--report-id` overwrites rather than adding a second one.
It reports progress and nothing else — it has no `--status`, because a job ends
when its turn ends and long work is a monitor.
`POST /v1/jobs/{ref}/message` remains for anything that wants immediate delivery
over HTTP.

Opt into the old behaviour with `ab submit --expect-report` when you want one
`ab wait` to cover both the turn and the work it started: the row parks in
`awaiting_report` until `status` says `finished`/`failed`, and fails with
`report_timeout` if nothing ever does.

## Operator notes

`GET /v1/info` returns the probed facts about the host and, beside them, the
contents of one markdown file — `gateway.md` in the data dir by default:

```bash
ab info          # probes first, then the notes under a rule
```

It is for what a person knows and a probe cannot find: the account to charge,
the partition with the GPUs, the filesystem that is nearly full. Edit it on the
host — an agent job can, `ab upload` can, an editor over ssh can — and the next
`/v1/info` reflects it. There is no write endpoint on purpose; `[notes] path`
and `[notes] max_bytes` are the only configuration.

## Configuration and operations

See `config.example.toml`. Important controls:

- `[server] host/port` — bind loopback behind SSH, or an internal address when
  compute nodes need direct report HTTP.
- `[worker] concurrency`, `cancel_grace_sec`.
- `[files] enabled`, store, per-file and request bounds.
- `[agents.<name>] allowed_dirs`, `default_cwd`, `dispatch_mode`, model catalog,
  permission mode, and timeout.

Only the login-node gateway writes SQLite WAL; nothing else writes the database.
A job reports by writing files into its own directory under the data dir, so put
the data dir on a filesystem the compute nodes share if a batch script is going to
write there too. Scope `allowed_dirs` deliberately; noninteractive
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
