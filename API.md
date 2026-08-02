# agent-bridge API reference

Base URL is wherever the gateway is reachable — over the SSH port-forward that is
`http://localhost:8787`. All responses are JSON unless noted.

- **Auth:** every endpoint except `/health`, `/llms.txt`, and `/v1/help`
  requires `Authorization: Bearer <token>`. Missing/wrong token → `401`.
- **Agent discovery:** `GET /llms.txt` returns a compact, instance-specific
  usage guide written for an LLM agent to read and then drive the API. Same
  content at `GET /v1/help`. No auth, so an agent can bootstrap before it has a
  token — the token itself is never served there.

---

## Job lifecycle

```
POST /v1/jobs           GET /v1/jobs/{id}/events (SSE)  or  GET /v1/jobs/{id} (poll)
      │                                    │
   queued ──▶ running ──▶ succeeded | failed | canceled
```

`queued` and `running` are non-terminal; the other three are terminal. A job is
one prompt, run by one agent, in one directory, in a forked session.

---

## Endpoints

### `GET /health`  (no auth)
Liveness. `200 {"ok": true, "version": "0.1.0"}`.

### `GET /llms.txt`  ·  `GET /v1/help`  (no auth)
Agent-facing usage doc as `text/markdown`, rendered from live config (real
allowed dirs, agents, dispatch mode). See [LLM usage](#llm-agent-usage).

### `GET /v1/agents`
Which backends exist.
```json
{ "configured": ["claude"], "known": ["claude"], "default": "claude" }
```

### `GET /v1/models`  ·  `?agent=<name>`
Models this agent offers, so a caller can choose by task complexity rather than
by guessing an id. Config-driven — `[[agents.<name>.models]]` in the gateway's
config.toml — so what's listed is what that agent is set up to accept.
```json
{
  "agent": "claude",
  "models": [
    { "id": "claude-sonnet-5", "alias": "sonnet", "tier": "standard",
      "context": "1M", "input_per_mtok": 2.0, "output_per_mtok": 10.0,
      "use": "Most coding, analysis, and multi-step work.",
      "note": "introductory pricing through 2026-08-31" }
  ],
  "tiers":   { "standard": "claude-sonnet-5" },
  "default": ""
}
```
`id` is the only required field per entry; the rest is advertising copy and may
be absent. There is no built-in catalog — an agent with no `models` configured
returns an empty list, and `model` on a job is passed through unchecked. `tiers` maps a complexity to an id (first model declared for a tier
wins) so clients don't reimplement the choice — `default` is what the agent uses
when a job omits `model`. Prefer a full id over an alias: `opus`/`sonnet`/`haiku`
track the newest model in each tier, so an alias makes a run non-reproducible.

### `GET /v1/info`  ·  `?refresh=1`
This machine's capabilities, probed once on startup (concurrently, in the
background) and **cached** — reads are instant (~1 ms). Add `?refresh=1` to
trigger a background re-probe (hardware rarely changes, so you rarely need it).
The probe set is generic (`hostname`, `nvidia-smi`, `sinfo`, an
allocation-balance command); values are whatever this cluster reports. Only
read-only commands run; env vars are reported **presence-only** (never values).

```json
{
  "ready": true,
  "collected_at": 1785633000.1,
  "took_ms": 472,
  "summary": "midway3-login5.rcc.local · RHEL 8.10 · 64 CPU/251GB · no local GPU · slurm 20.11.8 · GPU nodes: a100×124, h200×28, h100×22 · balance pi-jevans 964932 SU",
  "host": { "hostname": "…", "os": "…", "kernel": "…", "cpu_model": "…",
            "cpus": 64, "sockets": 2, "cores_per_socket": 16, "mem_gb": 251 },
  "gpu_local": "NONE",
  "scheduler": { "type": "slurm", "version": "slurm 20.11.8" },
  "partitions": [ { "partition": "gpu", "avail": "up", "nodes_aiot": "11/0/0/11" } ],
  "gpus": [ { "type": "a100", "nodes": 31, "gpus": 124, "idle_nodes": 4 } ],
  "accounts": [ { "account": "pi-jevans", "allocation": 1510000,
                  "usage": 544871, "balance": 964932 } ],
  "env_present": { "ANTHROPIC_API_KEY": false, "OPENAI_API_KEY": true },
  "_probes": { "gpus": { "took_ms": 270, "error": null } }
}
```

Before the first probe finishes (sub-second), returns `{"ready": false,
"status": "probing"}`. `gpus` aggregates all GPU nodes by accelerator type
(parsed from Slurm `Gres`/`Features`) — total GPUs and idle node counts, not a
per-node dump. Configure via `[cluster]` in `config.toml` (`enabled`,
`probe_timeout_sec`, `env_presence`).

### `GET /v1/sessions?cwd=<dir>&agent=<name>`
The session index the dispatcher chooses from. `cwd` (optional) sorts sessions
under that directory first; `agent` (optional) defaults to the default agent.
```json
{ "sessions": [
  { "session_id": "86e8bafe-…", "cwd": "/project/…/agent-bridge",
    "project": "-project-…-agent-bridge", "title": "reply with exactly: ok",
    "summary": "", "git_branch": "HEAD", "last_active": 1785631440,
    "messages": 3 }
] }
```

### `POST /v1/jobs`
Enqueue a task. Returns `202`.

Request body:

| field | type | required | notes |
|---|---|---|---|
| `prompt` | string | **yes** | the task; runs in a forked session at `cwd` |
| `cwd` | string | no | absolute dir; must be within an allowed dir or `400`. Defaults to the agent's `default_cwd` |
| `agent` | string | no | one of `/v1/agents`; defaults to `default` |
| `session` | string | no | session_id hint to prefer forking |
| `model` | string | no | alias/id: `opus`, `sonnet`, `haiku`, or full id. See [`/v1/models`](#get-v1models--agentname) for what this agent offers |
| `permission_mode` | string | no | e.g. `bypassPermissions`, `acceptEdits` |
| `files` | array | no | attachments — see [Files](#files) |

```bash
curl -s -X POST http://localhost:8787/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"prompt":"add a --json flag to cli.py and run the tests",
       "cwd":"/project/jevans/tzhang3/myrepo"}'
# 202
{ "id": "8d2ecd09-…", "status": "queued", "agent": "claude",
  "cwd": "/project/jevans/tzhang3/myrepo", "files": [] }
```

**With files, one call** — either JSON inline or multipart:

```bash
# JSON inline (small/text): each files[] item is {name,content_b64|text} or {path}
curl -s -X POST http://localhost:8787/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"prompt":"summarize the attached csv","cwd":"/project/jevans/tzhang3/myrepo",
       "files":[{"name":"in.csv","text":"a,b\n1,2\n"}]}'

# multipart (larger/binary): form field `payload`=JSON + file parts named `files`
curl -s -X POST http://localhost:8787/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -F 'payload={"prompt":"profile the attached data","cwd":"/project/jevans/tzhang3/myrepo"};type=application/json' \
  -F 'files=@./train.csv'
```

The gateway saves uploads in a per-user file store (a `$TMPDIR` dir by default,
`0700`; configurable via `[files].dir`) and surfaces their absolute paths to the
agent as *ATTACHED FILES*; they're also recorded on the job row's `files`. The
store is readable by the forked agent and downloadable via `/v1/files/content`
even though it may sit outside `allowed_dirs`.

Errors: `400 {"error":"prompt is required"}`, `400 {"error":"cwd … not under any allowed_dirs …"}`, `400 {"error":"unknown agent '…'"}`.

### `GET /v1/jobs`
Recent jobs, newest first: `{ "jobs": [ <job row>, … ] }` (default 50).

### `GET /v1/jobs/{id}`
The job row.

| field | meaning |
|---|---|
| `id`, `status`, `agent`, `prompt`, `cwd`, `requested_session` | as submitted |
| `chosen_session` | session the dispatcher forked (or `null`) |
| `forked_session` | new session id created by the fork (or `null`) |
| `files` | JSON list of attached file paths (or `null`) |
| `result` | final answer text (present on success; sometimes on failure) |
| `error` | failure reason (or `null`) |
| `cost_usd` | total cost |
| `created_at`, `started_at`, `finished_at` | epoch seconds |

`404 {"error":"job not found"}` if unknown.

### `POST /v1/jobs/{id}/cancel`
Cancel a queued or running job. A running job's agent process (and its process
group, including any forked child agent) is killed; the job settles to
`canceled`. A queued job is marked `canceled` and skipped when a worker would
have picked it up.

```bash
curl -s -X POST http://localhost:8787/v1/jobs/$ID/cancel \
  -H "Authorization: Bearer $TOKEN"
# 202 { "id": "…", "canceling": true, "was": "running" }   # or "was":"queued"
```

`202` with `was` = `running` | `queued`. `409 {status, error:"job already
finished"}` if terminal. `404` if unknown. The job's final `status` becomes
`canceled` (a terminal status); poll `GET /v1/jobs/{id}` to confirm.

### `GET /v1/jobs/{id}/events`
Two modes, chosen by the `Accept` header.

**Stream (SSE)** — `Accept: text/event-stream`:

```bash
curl -sN http://localhost:8787/v1/jobs/$ID/events \
  -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream"
```

Each event has an `id:` (per-job sequence integer), an `event:` type, and a JSON
`data:` line:

```
id: 5
event: assistant
data: {"seq":5,"ts":1785631440.1,"type":"assistant","data":{"text":"Running tests…"}}
```

Reconnect with `?after=<last seq>` or the `Last-Event-ID` header to replay only
newer events — no gaps, no duplicates. Heartbeat comments (`: ping`) arrive every
~15 s. The stream ends after the terminal `status` event (`stage:"done"`).

Event types and `data`:

| `event` | `data` |
|---|---|
| `status` | progress: `{"stage":"running"\|"done", …}` |
| `thinking` | `{"text"}` model reasoning (may be absent) |
| `assistant` | `{"text"}` assistant output (chunked) |
| `tool_use` | `{"name","input"}` a tool the agent ran |
| `tool_result` | `{"text"}` |
| `result` | `{"text","cost_usd","chosen_session","forked_session","is_error"}` — final |
| `error` | `{"message"}` run failed |
| `log` | misc/raw lines |

**Poll (JSON)** — any other `Accept`, with optional `?after=<seq>`:

```json
{ "job": { …job row… },
  "events": [ {"seq":6,"ts":…,"type":"result","data":{…}} ],
  "terminal": true }
```

Poll with `after` set to the last `seq` you saw to page forward; stop when
`terminal` is `true`.

## Files

All paths are sandboxed to `allowed_dirs` (realpath-resolved; `..`/absolute
escapes and out-of-tree reads → `400`).

### `POST /v1/files`
Upload files for reuse (or just to stage them). Same body shapes as job
attachments — JSON `{ "files": [ ... ] }` inline, or multipart (`payload` field +
file parts). Returns:

```json
{ "upload_id": "…", "dir": "<abs remote dir>",
  "paths": ["<abs remote path>", …] }
```

Pass those paths to a later job as `"files": [{"path": "<abs remote path>"}]`.

### `GET /v1/files/list?dir=<dir>&glob=<glob>&recursive=<bool>`
List files under an allowed dir (for discovering artifacts):

```json
{ "dir": "…", "files": [ {"path": "…", "size": 1234, "mtime": 1785…} ] }
```

### `GET /v1/files/content?path=<abs path>`
Streams the file bytes (`application/octet-stream`, `Content-Disposition`
attachment). Use for result CSVs and other artifacts — streamed, so large files
are fine. `400` if the path is outside `allowed_dirs` or not a file.

**Large data:** the `files_max_file_mb` / `files_max_request_mb` caps bound HTTP
uploads; beyond them, `scp`/`rsync` into an allowed dir over your SSH session and
reference the file by `{"path": ...}`.

---

## LLM-agent usage

Point an agent at `GET /llms.txt` first — it returns the whole contract
(endpoints, body schema, event types, allowed dirs, dispatch behavior, examples)
tailored to this instance. A typical agent loop:

1. `GET /llms.txt` → learn the API and the allowed directories.
2. `POST /v1/jobs` with `prompt` (and `cwd` to steer the project).
3. Stream `…/events` (SSE) or poll `GET /v1/jobs/{id}`; read `result`.
4. To continue a thread, submit a new job passing the prior `forked_session` as
   `session`.

The agent does **not** pick a session — the gateway's dispatcher reads the
session index and forks the best match itself. Set `session` only to force one.

---

## Notes

- **No server-side timeout by default** (`timeout_sec = 0`): a job runs until the
  agent finishes. Use a client-side timeout if you need one, or set a positive
  `timeout_sec` in config.
- **cwd allowlist** is enforced on every job; keep `cwd` within the configured
  `allowed_dirs`.
- **Sequences** (`seq`) are per-job and monotonic — safe to use as SSE
  `Last-Event-ID` and as a poll cursor.
