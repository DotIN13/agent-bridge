# agent-bridge API reference

agent-bridge **0.3.0** exposes a typed FastAPI API. Its OpenAPI document is at
`/openapi.json`; `/docs` and `/redoc` are FastAPI discovery UIs. The canonical
agent-facing guide is `/llms.txt` (`/v1/help` is a retained alias).

All endpoints except `/health`, `/llms.txt`, and `/v1/help` require:

```text
Authorization: Bearer <token>
```

## Errors

Every gateway error uses one envelope:

```json
{
  "error": {
    "code": "session_busy",
    "message": "session … is being written by a running job",
    "details": {"session": "…", "held_by": "…", "steer_ref": "…"}
  }
}
```

Common statuses are `400` validation/domain error, `401` unauthorized, `404`
not found, `409` conflict, `413` payload too large, and `422` query/schema
validation. Unknown job-submission fields are rejected rather than ignored.

## Resources

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness and version; no auth |
| GET | `/llms.txt`, `/v1/help` | live instance-specific guide; no auth |
| GET | `/v1/agents` | agents, models, defaults, capabilities, server features |
| GET | `/v1/models?agent=` | retained model-catalog projection |
| GET | `/v1/info?refresh=1` | cached cluster capabilities; optional background refresh |
| GET | `/v1/session-dirs?agent=` | directories holding sessions; complete, unpaged |
| GET | `/v1/sessions?cwd=&agent=&limit=&cursor=` | sessions in one directory (exact match), paged |
| POST | `/v1/jobs` | validate, persist, and enqueue a job |
| GET | `/v1/jobs?limit=&cursor=` | paged job summaries |
| GET | `/v1/jobs/{ref}` | full public job detail |
| GET | `/v1/jobs/{ref}/events` | JSON event page or resumable SSE |
| POST | `/v1/jobs/{ref}/steer` | message a running turn |
| POST | `/v1/jobs/{ref}/cancel` | interrupt a queued/running job |
| POST | `/v1/jobs/{ref}/message` | append an external/batch report |
| POST | `/v1/files` | upload reusable files |
| GET | `/v1/files/list` | paged sandboxed listing |
| GET | `/v1/files/content` | streamed file bytes |

`{ref}` is a full UUID, exact normalized title, or unique UUID prefix.
Ambiguous references return `409 ambiguous_reference` with candidates.

## Discovery

### `GET /health`

```json
{"ok": true, "version": "0.3.0"}
```

### `GET /v1/agents`

```json
{
  "configured": ["claude", "opencode"],
  "known": ["claude", "opencode"],
  "default": "claude",
  "agents": [{
    "name": "claude",
    "default_model": "claude-sonnet-5",
    "models": ["claude-haiku-4-5", "claude-sonnet-5"],
    "default_cwd": "/project/x",
    "capabilities": {
      "sessions": true,
      "fork": true,
      "in_place_resume": true,
      "steering": true,
      "thinking_events": true,
      "file_attachments": true,
      "permission_modes": ["default", "acceptEdits", "bypassPermissions", "plan"],
      "model_policy": "advertised-passthrough"
    }
  }],
  "features": {"files": true, "cluster_info": true, "event_stream": "sse"}
}
```

Capabilities are adapter/mode-specific. Consult them before steering or
resuming. `/v1/models` remains for compatibility; configured model ids are
advertised strings and are passed to the backend verbatim.

### `GET /v1/session-dirs?agent=<name>`

Directories that hold sessions — the "where is there work to continue" view,
needed before you know which project to ask about.

```json
{ "dirs": [
    {"cwd": "/project/x", "sessions": 88,
     "last_active": "2026-08-11T18:22:04.113-05:00",
     "latest_session_id": "3cf736d5-…", "latest_title": "fix the parser"}
  ], "total": 12 }
```

**Returned whole, never paged.** Its size is bounded by how many projects exist
(tens), not by a window, so a project cannot silently drop out of it.

### `GET /v1/sessions?cwd=<dir>&agent=<name>&limit=&cursor=`

Sessions, newest first. Returns
`{"sessions":[...], "total", "next_cursor", "has_more"}`.

**`cwd` is an exact directory match, not a prefix.** A project and a
sub-project keep separate indexes, so a count means what it says. Paths are
compared normalised, so `D:\x`, `D:/x` and `d:\X` are the same directory — the
two backends genuinely spell them differently.

Omit `cwd` for every session, still paged. `total` is the real size of the
selection either way, so a short page is visibly a page rather than a silent
sample.

**Paging is by opaque `cursor`, not `after=N`.** Sessions have no monotonic
sequence; ordering on a timestamp alone would skip or repeat rows whenever two
share a millisecond.

> Superseded shape: this route used to return a bare `{"sessions":[...]}` in
> which `cwd` only *sorted*, after the newest `limit * 3` sessions had already
> been selected across all directories. On a real store that hid entire
> projects — two holding 33 and 88 sessions returned nothing at all.

Neither route is authorization: job cwd and file paths remain constrained by the
configured allowed directories.

## Jobs

### `POST /v1/jobs`

The strict JSON request is:

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `prompt` | string | yes | non-empty task text |
| `cwd` | string | no | allowed remote absolute directory |
| `agent` | string | no | configured backend |
| `model` | string | no | backend model id |
| `session` | string | no | session to fork/resume |
| `title` | string | no | human reference; derived when omitted |
| `fork` | boolean | no | default `true`; `false` resumes in place |
| `permission_mode` | string | no | backend-specific mode |
| `include_thinking` | boolean | no | retain reasoning events |
| `files` | array | no | path references or inline file objects |

`fork:false` requires an idle `session`. If a live job holds it, the gateway
returns `409 session_busy` with `held_by` and `steer_ref`. Use `/steer` for the
running turn.

JSON file items are one of:

```json
{"path":"/existing/remote/path"}
{"name":"task.md","text":"..."}
{"name":"data.bin","content_b64":"..."}
```

Multipart uses a JSON `payload` form field and one or more file parts. Names are
validated and collisions are rejected. Job attachments are staged and promoted
before the row becomes visible, so a failed upload does not leave a phantom
queued job.

Successful submission returns `202`, a `Location: /v1/jobs/<id>` header, and:

```json
{
  "id": "…", "status": "queued", "agent": "claude", "cwd": "/project/x",
  "title": "run tests", "fork": true, "include_thinking": false,
  "files": [], "replayed": false,
  "session": null, "session_state": "pending"
}
```

`session` is the id to reuse as `session` on a later job. A pinned target is
echoed straight back with `session_state: "pinned"`, so continuing a thread
needs no extra round trip. A fresh or forked run has no id yet — it first
appears in the agent's init record — so it returns `"pending"`, and the id shows
up as `session` on the job row once the run starts. `ab submit` waits for it by
default (`--no-wait` opts out).

#### Retry idempotency

Send `Idempotency-Key: <stable key>` when a network retry could duplicate
expensive work. The same key and semantic request returns the original job with
`replayed:true`; the same key with different content returns
`409 idempotency_conflict`. Strict exactly-once steering is **not** promised:
accepted delivery and model action are separated by a tool boundary.

### `GET /v1/jobs?limit=<1..200>&cursor=<opaque>`

Returns bounded summaries, excluding prompt/result/error payloads:

```json
{"jobs":[{"id":"…","status":"running","title":"…","fork":true}],
 "next_cursor":"…","has_more":true}
```

Pass `next_cursor` unchanged to fetch the next page. Public booleans are JSON
booleans, `files` is always an array in detail, and internal DB fields are not
exposed.

### `GET /v1/jobs/{ref}`

Returns full detail: summary fields plus `prompt`, `permission_mode`, `files`,
`result`, and `error`. `status` is `queued`, `running`, `succeeded`, `failed`,
or `canceled`.

At gateway startup, persisted queued jobs are re-enqueued. Rows that were
running across a restart are marked failed with a recovery event; the service
does not claim that a vanished process is still live.

### `POST /v1/jobs/{ref}/steer`

Body: `{"prompt":"new guidance"}` (legacy `text` is accepted). Returns `202`
when the live input channel accepts it. Delivery is observed at the next tool
boundary and later appears as a `steer` event. A `202` is not exactly-once model
execution. Unsupported adapters/modes and terminal jobs return a typed `409`.

### `POST /v1/jobs/{ref}/cancel`

Requests an interrupt and returns `202 {id,status:"canceling",canceling:true,was}`.
Cancellation sends SIGINT first so the agent can flush its transcript, then
escalates after the configured grace period. Repeating cancellation after the
job is already canceled is idempotent and returns `200` with
`already_terminal:true`; succeeded/failed jobs return `409 job_terminal`.

## Events

### JSON page

`GET /v1/jobs/{ref}/events?after=N&limit=L&legacy=false`, where `after >= 0`
and `1 <= L <= 1000`:

```json
{
  "events": [{"seq":12,"ts":"2026-08-11T14:52:40.572-05:00",
              "elapsed":6.689,"elapsed_hms":"+00:00:06",
              "type":"assistant","data":{"text":"…"}}],
  "status": "running", "terminal": false,
  "next_after": 12, "has_more": false,
  "total": 27, "first_seq": 1, "last_seq": 27, "job": null
}
```

`total`, `first_seq` and `last_seq` describe the whole log, so a caller can place
its window without paging forward to discover the end.

**Reading from the end.** `?tail=N` returns the last N events instead of paging
forward from `after`, still in chronological order. `tail` and `after` cannot be
combined (`400 invalid_request`) — anchoring from both ends has no single
sensible reading. `tail` pairs with `until=S` for a bounded window from the
right, and with repeatable `type=T`, which filters **inside** the window; a
`type` applied afterwards would make `tail=3&type=result` empty on any long job.
`ab events` defaults to a tail; `--after 0` restores top-down reading.

**Timestamps are ISO 8601, everywhere.** Every timestamp the API publishes is a
string in the **gateway's local** time with the UTC offset attached
(`2026-08-11T14:52:40.572-05:00`) — no bare epoch floats reach a caller. That
covers job `created_at`/`started_at`/`finished_at`/`last_event_at`, event `ts`,
session `last_active`, file `mtime`, and the `ts` inside an `ab-notify` report
payload.

Local rather than UTC because the reader correlating a job against an sbatch log
holds a local clock; the offset keeps it unambiguous, and "local" means the
gateway's zone, which for a tunnelled client is not their own.

Cursors are unaffected — `next_after` and `Last-Event-ID` are `seq`-based, and
job pagination hides `created_at` inside an opaque cursor, so nothing a caller
reads needed the raw number. **Durations stay numeric:** `elapsed` (seconds
since the job's first event) with `elapsed_hms` alongside, because position
within the run is usually the real question.

All event sources share one transactional, per-job monotonic sequence allocator.
`next_after` and SSE `Last-Event-ID` are safe cursors with no sequence bands.
`legacy=true` includes the full job object for older clients.

### SSE

Send `Accept: text/event-stream`. Each frame contains `id`, `event`, and JSON
`data`; idle streams emit `: ping`. Reconnect with `after` or
`Last-Event-ID`. The stream replays persisted events and closes when the
**coding-agent job** becomes terminal.

Event types include `status`, `assistant`, `thinking`, `tool_use`,
`tool_result`, `steer`, `result`, `error`, `log`, and `message`.

### External and batch reports

`POST /v1/jobs/{ref}/message` accepts conventional fields `status`, `msg`,
`report`, `host`, `slurm_job_id`, `ts`, and optional `report_id`; extra
scheduler-specific fields are preserved. Reusing one `report_id` with the same
body returns the original sequence with `duplicate:true`; conflicting content
returns `409 report_id_conflict`.

Reports are **post-terminal annotations**, not a second job status machine. They
may arrive after the agent SSE stream has closed. Fetch them by reconnecting
with the last cursor or polling an event page. A single already-open SSE request
does not wait forever for future batch work.

`ab-notify` posts here, then falls back to shared JSONL, then local temporary
JSONL. Filesystem reports are ingested on job/event reads and receive the same
monotonic sequence allocation. Use `--report-id`/`$AB_REPORT_ID` for retry
deduplication.

## Files

### `POST /v1/files`

Accepts the same JSON/multipart file forms as jobs and returns
`{upload_id,dir,paths}`. Empty uploads, traversal, duplicate names, and configured
size violations are rejected.

### `GET /v1/files/list`

Query: `dir`, `glob=*`, `recursive=false`, `limit=1..1000`, and optional opaque
`cursor`. Returns `{dir,files,next_cursor,has_more}`. Paths are realpath-checked
against allowed roots/file storage.

### `GET /v1/files/content?path=<path>`

Streams one file with `Content-Length` and `Content-Disposition`. Large transfer
fan-out and safe local layout are client concerns; the `ab` client preserves
relative paths, rejects collisions, and writes atomically.

## Compatibility decisions

- `/v1/help`, `/v1/models`, singular `/message`, and current file paths remain.
- There is no synchronous `run` or server-side `wait`; clients compose submit,
  resumable events, and detail reads.
- Client wait timeout does not cancel a remote job unless explicitly requested.
- Post-terminal messages remain annotations; reconnect/poll to retrieve them.
- Strict exactly-once steering is not claimed.
