# 07 — Make the backend contract match the CLI

**Severity:** high
**Status:** **done** in 0.3.0
**Scope:** typed HTTP contract, persistence/lifecycle correctness, capabilities,
files, tests, and synchronized help.

## Problem

The route topology already covered the CLI, but it was not a dependable machine
contract. POST bodies and responses were absent from OpenAPI, errors varied,
public rows leaked SQLite representations, collections were unbounded, event
sequence bands could hide later reports, failed attachments could orphan jobs,
restart recovery was absent, and adapter capabilities were undiscoverable.

The implementation keeps the small resources—agents/sessions, jobs/events, and
files—rather than adding server `run`, `wait`, or download-fan-out endpoints.

## Implementation

### Typed API and errors

- Added strict Pydantic request/response models in `gateway/api_models.py`.
- OpenAPI declares bearer auth, JSON/multipart request bodies, success models,
  `202` actions, and typed error responses.
- Unknown submission fields are rejected; cross-field validation enforces
  non-empty prompt and `fork:false => session` before persistence.
- All errors use `{error:{code,message,details}}`.
- Public DTOs normalize `fork`/`include_thinking` to booleans, `files` to an
  array, and omit internal fields such as `title_norm`.

### Minimal bounded resources

- `GET /v1/jobs` returns summary pages only (`limit 1..200`, opaque cursor,
  `has_more`); prompt/result/error remain in job detail.
- JSON events return `{events,status,terminal,next_after,has_more}` with a
  validated `limit 1..1000`; a legacy job object remains optional.
- File listings are bounded/paged. Request-body limits are enforced while
  reading, including chunked requests; external reports have a tighter bound.
- Existing semantic action routes `steer` and `cancel`, event content
  negotiation, file routes, `/v1/models`, and `/v1/help` remain compatible.

### Monotonic events and report deduplication

Every worker, recovery, HTTP report, and filesystem report now appends through
one transactional per-job sequence allocator. Historical high sequence values
are preserved and new allocation starts above the maximum, so `after` and
`Last-Event-ID` cannot permanently skip a later source.

External reports use a separate deduplication identity. `report_id` replay with
the same content returns the original sequence; changed content under one id is
a typed conflict.

### Atomic visibility and retry safety

- Job attachment names are preflighted for traversal/collision, written to a
  staging directory, then promoted before the DB row is visible.
- Failure cleans staging/final paths, so it cannot leave a queued row that was
  never enqueued.
- Optional `Idempotency-Key` stores a semantic request hash and accepted
  response with job creation. Same-key replay returns the original id; changed
  content conflicts.
- Cancellation is idempotent after cancellation has already been achieved.

Strict exactly-once steering is intentionally **not** promised. There is an
unavoidable distinction between the gateway accepting bytes and the model
acting at its next tool boundary; the event stream records actual pickup.

### Restart and shutdown

- Startup removes stale staging/orphan file trees, fails persisted `running`
  rows with an explicit gateway-restarted event, and re-enqueues persisted
  `queued` rows in deterministic order.
- Worker start/stop is idempotent. Graceful stop requests cancellation, wakes
  and joins worker threads, then the lifespan closes SQLite.
- The gateway does not guess that a process survived when it cannot prove it.

### Capability discovery

`GET /v1/agents` now returns configured/known/default plus per-agent models,
default cwd, default model, and capability flags for sessions, fork,
in-place resume, steering, thinking, permission modes, attachments, and model
policy. Global features expose files, cluster info, and SSE.

The CLI consumes this response; `/v1/models` remains a projection for older
clients.

### Self-description and version

Gateway, package, CLI, health, and generated help share version `0.3.0`.
`/llms.txt` is rendered from configured agents, adapter capabilities, allowed
directories, and the actual strict job model. `/openapi.json`, `/docs`, and
`/redoc` are supported discovery surfaces; `/v1/help` remains an alias.

## Batch lifecycle decision

A coding-agent job and external compute are not silently merged into one status
machine. `message` events are **post-terminal annotations**:

- the coding-agent SSE closes when that job becomes terminal;
- `ab-notify` may append later reports;
- clients retrieve them by reconnecting with the last event cursor or polling;
- one already-open SSE does not wait forever for future scheduler work.

This is explicit and testable. A first-class scheduler-work resource/status can
be designed later without overloading current job semantics.

## Compatibility and residual non-goals

- No synchronous server `run`/`wait`; those are client compositions.
- `/v1/help`, `/v1/models`, singular `/message`, and existing file action paths
  are retained.
- Strict exactly-once steer is not claimed.
- Per-job report-only credentials and multi-process gateway ownership leases are
  not introduced; the configured deployment remains one gateway writer under
  the user's account.
- Model catalogs are advertised backend ids, passed through verbatim rather
  than treated as pricing/alias policy.

## Verification

Backend tests cover OpenAPI/auth/error envelopes, strict validation, public DTO
shapes, job idempotency, reference ambiguity, idempotent cancel, report dedup,
bounds, capabilities, historical cursor migration, concurrent monotonic event
allocation, filesystem report ingestion, paging, attachment cleanup/collisions,
restart reconciliation, and shutdown joining. Client compatibility is covered
by the combined full repository suite.
