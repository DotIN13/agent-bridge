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
POST /v1/jobs           GET /v1/jobs/{ref}/events (SSE)  or  GET /v1/jobs/{ref} (poll)
      │                                    │
   queued ──▶ running ──▶ succeeded | failed | canceled
                                     │
                     POST /v1/jobs/{ref}/message  ◀── ab-notify, hours later
```

`queued` and `running` are non-terminal; the other three are terminal. A job is
one prompt, run by one agent, in one directory, in one session.

**A terminal job is not necessarily finished work.** If the agent's task was to
submit an sbatch, the job reaches `succeeded` the moment the submission returns
— the compute then runs for hours. `ab-notify` messages keep arriving on that
job's event stream afterwards, which is what makes one reference cover the whole
lifecycle. See [`POST /v1/jobs/{id}/message`](#post-v1jobsidmessage).

`{ref}` throughout is a full uuid, the job's **title**, or a unique id prefix.

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
{ "configured": ["claude", "opencode"], "known": ["claude", "opencode"], "default": "claude" }
```
`configured` comes from config.toml's `[agents.<name>]` sections; `known` is what
the adapter registry ships. Each job picks one backend via the `agent` field.

### `GET /v1/models`  ·  `?agent=<name>`
Model ids this agent is configured to accept, so a caller can see what's
supported before setting `model` on a job. Config-driven — a plain
`models = ["..."]` list under `[agents.<name>]` in the gateway's config.toml.
```json
{
  "agent": "opencode",
  "models": ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"],
  "default": "deepseek/deepseek-v4-flash"
}
```
The list is exactly what's advertised: an agent with no `models` configured
returns an empty list, and `model` on a job is passed through unchecked.
`default` is what the agent uses when a job omits `model`. There are no tiers,
aliases or pricing — just ids, one per line.

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
The session index — **how you pick a `session` to pass to `POST /v1/jobs`**
under the default `direct` dispatch mode. `cwd` (optional) sorts sessions under
that directory first; `agent` (optional) defaults to the default agent.
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
| `session` | string | no | the session to run in. Omit for a fresh one. **Required** when `fork` is `false`. Under `direct` mode this is the whole routing decision — nothing substitutes another session |
| `title` | string | no | human handle for the job. Derived from the prompt's first line when omitted, so every job has one. Pass it wherever an `{id}` is accepted |
| `fork` | bool | no | default `true`. `false` queues the prompt into the target session **in place** — see [Fork vs resume-in-place](#fork-vs-resume-in-place) |
| `model` | string | no | id for this agent from `/v1/models` (e.g. `claude-sonnet-5` or `deepseek/deepseek-v4-flash`). Omit for the agent's default. See [`/v1/models`](#get-v1models--agentname) |
| `permission_mode` | string | no | e.g. `bypassPermissions`, `acceptEdits` |
| `files` | array | no | attachments — see [Files](#files) |

#### Fork vs resume-in-place

By default a job **forks** the session it lands in: the parent is never mutated
and the run gets a fresh session id. That is right for independent tasks.

`"fork": false` instead **resumes the target session in place**, appending to
its own history. Use it when the prompt is a follow-up to work already in that
thread — a course correction, extra context, or a queued instruction to pick up
next — and the session genuinely needs to see it. A fork would put the message
on a branch the original session never reads.

```bash
# nudge a specific in-flight thread rather than branching it
curl -s -X POST http://localhost:8787/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"prompt":"when the fetch finishes, do NOT build sources.json — stop and report",
       "session":"3cf736d5-152a-417a-921c-c17bf1bd233a", "fork": false,
       "title":"halt-before-metadata"}'
```

**`session` is required.** `fork:false` on its own is a `400`. An in-place write
lands permanently in whichever session receives it, so the target is never
inferred — you name it or it doesn't run.

A busy target is fine: resuming queues the message and the agent picks it up at
the end of the current turn, so the gateway does not gate on liveness.

```bash
curl -s -X POST http://localhost:8787/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"prompt":"add a --json flag to cli.py and run the tests",
       "cwd":"/project/jevans/tzhang3/myrepo"}'
# 202
{ "id": "8d2ecd09-…", "status": "queued", "agent": "claude",
  "cwd": "/project/jevans/tzhang3/myrepo",
  "title": "add a --json flag to cli.py and run the tests", "fork": true,
  "files": [] }
```
`agent` is whatever you sent (default: the gateway's default). The worker
builds the matching adapter and runs the prompt in the requested session.

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

Errors: `400 {"error":"prompt is required"}`, `400 {"error":"cwd … not under any allowed_dirs …"}`, `400 {"error":"unknown agent '…'"}`, `400 {"error":"fork=false requires 'session': …"}`.

### `GET /v1/jobs`
Recent jobs, newest first: `{ "jobs": [ <job row>, … ] }`. `?limit=N` (default 50).

### `GET /v1/jobs/{id}`
The job row.

`{id}` takes a **full uuid, the job's title, or any unique leading id prefix** —
`b4c220af`, `halt-before-metadata`, and the full
`b4c220af-ca3b-4568-a9ca-d4f578d25ed3` all resolve. Titles are matched folded
(case-insensitive, punctuation and spaces collapsed to `-`), so
`--title "Halt before metadata"` is addressable as `halt-before-metadata`.

Resolution order is id → title → id-prefix, and applies to `…/cancel` and
`…/events` too. Titles are **not** unique — resubmitting a task reuses its
name — so an ambiguous reference is a `409` listing the candidates, never a
silent pick of the newest. That matters most for `cancel`.

| field | meaning |
|---|---|
| `id`, `status`, `agent`, `prompt`, `cwd`, `requested_session` | as submitted |
| `title` | human handle (given or derived); `title_norm` is its folded lookup key |
| `fork` | `1` forked a session, `0` resumed one in place |
| `chosen_session` | session the job ran against (or `null` if it started fresh) |
| `forked_session` | new session id created by the fork (or `null`) |
| `files` | JSON list of attached file paths (or `null`) |
| `result` | final answer text (present on success; sometimes on failure) |
| `error` | failure reason (or `null`) |
| `cost_usd` | total cost |
| `created_at`, `started_at`, `finished_at` | epoch seconds |

`404 {"error":"no job matching id, title, or id-prefix '…'"}` if unknown;
`409 {"error":"… is ambiguous (N jobs)", "matches":[{id,title,status,created_at}…]}`
if several match — add characters to a prefix, or use the full id from
`GET /v1/jobs`.

### `POST /v1/jobs/{id}/message`
A **running batch job reporting its own lifecycle.** Body is any JSON object;
by convention `{status, msg, report, host, slurm_job_id, ts}`. Appended to the
job's event stream as `type:"message"` and published to the bus, so SSE
subscribers see it immediately.

```bash
curl -s -X POST http://localhost:8787/v1/jobs/$ID/message \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"finished","report":"/project/.../SWAP.md"}'
# { "id": "…", "seq": 1000000 }
```

Use the **`ab-notify`** helper from inside an sbatch rather than curl directly —
it falls back to a shared-filesystem JSONL, then to local `/tmp`, if the gateway
is unreachable from the node.

Like every other authed route this needs `Authorization: Bearer <token>`.
`ab-notify` resolves it as `--token` → `$AB_TOKEN` → `<data_dir>/.token`, so
exporting `AB_DATA_DIR` into the batch job is what makes the HTTP path usable;
without a token it silently takes the filesystem fallback. Don't put the token
in a job script or `--export` — job environments surface in scheduler metadata
that other users on a shared cluster can read.

**Why a job needs this at all.** A job's agent turn ends at `sbatch`; the actual
compute then runs for hours with no connection to the gateway. Without messages
the only signal is guessing from output-file mtimes, which cannot distinguish
"queued" from "died before writing" from "I can't see the filesystem".

**Why batch jobs don't write the DB directly.** It runs in WAL mode, whose index
is mmap'd shared memory and therefore requires every writer on one host. The
JSONL fallback uses `O_APPEND`, which needs no locking, and the gateway ingests
it when the job is next read.

Seq bands keep the writers apart: worker events count from 1, HTTP messages from
`1_000_000`, file-ingested messages from `2_000_000` (seq derived from line
number, so re-ingesting is a no-op).

### `POST /v1/jobs/{id}/cancel`
Cancel a queued or running job. A queued job is marked `canceled` and skipped
when a worker would have picked it up.

A **running** job is *interrupted*, not killed outright — the equivalent of
pressing ESC in the interactive client. `SIGINT` goes to the whole tree (the
dispatcher and the nested agent it forked), so the agent stops its current turn,
flushes its transcript and exits, and **the session stays resumable**. Only if
it hasn't wound down within `[worker] cancel_grace_sec` (default 15s) does the
gateway escalate to `SIGTERM`, then `SIGKILL` after a further 5s. The wall-clock
`timeout_sec` path interrupts the same way.

Escalating matters: `SIGKILL` leaves the transcript mid-write, which is what
makes a killed session awkward to pick up again. Either way the job settles to
`canceled`, and any partial `result` is preserved on the row.

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
List files and directories under an allowed dir (for discovering artifacts;
`is_dir` tells the two apart, `size` is 0 for directories):

```json
{ "dir": "…", "files": [
    { "path": "…/out/results.csv", "is_dir": false, "size": 1234, "mtime": 1785… },
    { "path": "…/out/plots",        "is_dir": true,  "size": 0,    "mtime": 1785… }
] }
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
