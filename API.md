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
| GET | `/v1/info?refresh=1` | cached cluster capabilities plus operator notes; optional background refresh |
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

### `GET /v1/info` and operator notes

Two kinds of knowledge in one answer. Above: what the probes measured. Below,
under `notes`: a markdown file on the gateway host, holding what no probe can
discover — which account to charge, which filesystem is full, which env has
which package.

```json
{ "ready": true, "summary": "login5 · 64 CPU/251GB · slurm 20.11.8",
  "notes": {"text": "## Slurm
- --account=pi-jevans …",
            "updated_at": "2026-08-22T11:06:34.748-05:00",
            "path": "/home/you/.agent-bridge/gateway.md"} }
```

They are together on purpose: nobody thinks to ask for local conventions they
do not know exist, so the request that answers "what is this machine" answers
"how do I work on it" as well. `text` is `""` when the file is absent, which is
not an error — a gateway whose owner has written nothing is not broken. An
unreadable file reads as empty too, because these notes must never be able to
take `/v1/info` down.

**There is no write endpoint.** The file lives on the host that serves it, so
the ways to change it already exist and are better: an agent with file tools
edits it in place, `POST /v1/files` uploads it, or its owner opens an editor
over ssh. A fourth way could only clobber the other three. `path` is published
so an agent asked to update the notes knows what to open, and `[notes] path`
in the config moves it (default `gateway.md` in the data dir).

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

### Job status

`queued` → `running` → `waiting` → `succeeded` | `failed` | `canceled`.

A job is `succeeded` when **both** halves have happened: its turn has ended and
`$AB_JOB_DIR/report.md` has been written. Between them the row is `waiting`, which
is non-terminal — the agent process is still alive, can still be steered, and can
still write the file. The gateway notices the report within a sweep (5s) and the
row becomes `succeeded` with `reason: report_written`.

A `waiting` job with no report by `worker.report_wait_sec` (default 1800, 0 waits
indefinitely) fails with `report_missing`. That window is a grace period, not a
wait: the delegate is meant to write the report *before* ending its turn. The
turn's last message is kept on the row either way.

A turn that fails or is canceled goes terminal directly — there is no deliverable
to wait for. A caller that wants to end a job early uses
`POST /v1/jobs/{ref}/cancel`, which also releases a `waiting` job.

Work that outlives the turn is a **monitor** with its own row and its own
terminal states — see *Monitors* below, and `ab monitor <id> --wait` to block on
one.

`expect_report: true` is refused with a typed `400 expect_report_removed` naming
monitors. It once parked a row in `awaiting_report` until something called in to
close it; that made a caller's mistake — a brief that never arranged a report —
indistinguishable from work still running, and cost a day per occurrence at the
old deadline (design/11, /15, /16). The field stays on the DTO only so the
refusal can explain itself; `expect_report: false` is accepted and is what every
job does.

**Both routes list a session only if a human spoke or the agent acted**, and the
counts match the listings. Three kinds of transcript exist without anything
having happened in them: subagent files (Claude Code writes one per subagent but
records its turns in the parent), slash-command residue (`/login`, `/resume` —
the caveat, command and stdout are each stored as a `user` record, so a naive
count reads 3 messages), and opencode sessions created and never used. Each has a
real id and looks resumable. `GET /v1/jobs` and a resume by explicit id are
unaffected — only the recommendations are filtered.

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
up as `session` on the job row once the run starts. `ab submit` always waits for
it, bounded by `--await-timeout` (default 30s); exceeding that is not an error,
and the job keeps running with `session_state: "pending"`.

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

**`session` is one field, and it is populated from the first moment the run
knows it.** It starts as whatever the caller pinned, or null for a fresh run,
and is replaced by the id the agent actually reports -- so a `running` job names
its session, and the value is always the one to pass back as `session` on the
next job. It used to be three columns (`requested_session`, `chosen_session`,
`forked_session`) written only when the job reached a terminal state, which left
every running job blank and every caller guessing which of the three to read.
Older databases are migrated in place; the three no longer appear in any
response.

### `GET /v1/jobs/{ref}`

Returns full detail: summary fields plus `prompt`, `permission_mode`, `files`,
`result`, and `error`. `status` is `queued`, `running`, `succeeded`, `failed`,
or `canceled`.

At gateway startup, persisted queued jobs are re-enqueued. Rows that were
running across a restart are marked failed with a recovery event; the service
does not claim that a vanished process is still live.

### `POST /v1/jobs/{ref}/steer`

Body: `{"prompt":"new guidance"}` (legacy `text` is accepted). Returns `202`
when the live input channel accepts it, with a `note` describing what that
backend does with it — the two are not the same mechanism:

| Backend | Channel | Delivery |
| --- | --- | --- |
| claude, `direct` | the child's stdin, `--input-format stream-json` | taken at the next tool boundary; the `steer` event is the agent's own echo |
| opencode, `direct` + `steering` | `POST /api/session/<id>/prompt` on the server the run is attached to, `delivery: "steer"` | admitted synchronously, then promoted into the running turn; the `steer` event carries the receipt (`admitted_seq`, `promoted_seq`) |

A `202` is not exactly-once model execution. Unsupported adapters/modes and
terminal jobs return a typed `409` whose message says what is missing — an
opencode job that ran unattached (see `[agents.*] steering` in
`config.example.toml`) names the reason it did.

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
session `last_active`, file `mtime`, and the `ts` inside an external report
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

Reports also arrive without HTTP: files a delegate writes into its job dir
(`$AB_JOB_DIR` = `<data_dir>/reports/<job-id>`) are ingested on job/event reads
and by the gateway's sweeper, and receive the same monotonic sequence allocation.
Job-dir reports are deduplicated by relative path and content digest; HTTP
reports use `report_id`. Neither can move a job: a report is an annotation, and
the turn's end is what ends a job.

There is no shared-filesystem JSONL channel. It existed for a compute node that
could not reach the gateway; nothing has written it since `ab-notify` became a
job-dir reporter, and a reader with no writer was removed.

### Monitors

`POST /v1/monitors` registers a watch on work that outlives a turn: `poll` (a
command whose first word of output is the status) or `slurm` (sugar for an
`sacct` read), plus `job`, `label`, `map`, `interval_sec`, `deadline_sec`,
`note`, and `result_paths`. The gateway polls on a timer, bounded by
`[monitors]`. `GET /v1/monitors` pages with the same opaque cursor as jobs and
filters on `job`, `status` and `active`; `GET /v1/monitors/{id}` is the detail;
`POST /v1/monitors/{id}/cancel` stops watching and is idempotent — it says
nothing about the work, which keeps running.

A monitor's terminal transition is the **record of how the long task ended**,
carried on the creating job's stream long after that job closed: `terminal: true`
plus the label, the poll command, the last output, when it resolved, how long it
was watched, and the `result_paths`. Monitor statuses are `queued`, `running`,
`finished`, `failed`, `expired` and `canceled`. `expired` means a deadline passed and the gateway stopped watching,
which is a weaker claim than the work having failed. Only *transitions* emit
events, on the creating job's stream, carrying both a report-shaped `status` and
the precise `monitor_status`.

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
