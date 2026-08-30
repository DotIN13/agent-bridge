"""Live, agent-facing help served at /llms.txt and /v1/help.

The guide is rendered from the same configured agents and Pydantic request model
used by the HTTP API, so field names, version, allowed roots, and capabilities
do not drift into deployment-specific fiction.
"""
from __future__ import annotations

import json

from . import __version__
from .adapters import build as build_adapter
from .api_models import JobCreate
from .config import Config


def render_llms_txt(cfg: Config) -> str:
    base = f"http://{cfg.host}:{cfg.port}"
    agents = ", ".join(sorted(cfg.agents)) or "(none)"
    allowed = "\n".join(f"  - {path}" for path in _allowed_dirs(cfg))
    fields = ", ".join(JobCreate.model_fields)
    agent_rows = []
    for name in sorted(cfg.agents):
        agent = cfg.agents[name]
        caps = build_adapter(agent).capabilities()
        models = ", ".join(agent.models) or "(none advertised)"
        agent_rows.append(
            f"  - {name}: default_model={agent.model or '(backend default)'}; "
            f"models={models}; capabilities="
            f"{json.dumps(caps, sort_keys=True, separators=(',', ':'))}")
    described_agents = "\n".join(agent_rows) or "  - (none configured)"
    dispatch = ", ".join(
        f"{name}={agent.dispatch_mode}" for name, agent in cfg.agents.items())

    return f"""\
# agent-bridge {__version__} — live API guide for agents

One job is one coding-agent prompt. Submit it, consume its resumable event
stream, then read the full detail. Continue existing context before starting a
fresh session.

## Connection
- Base URL: {base}
- Public: GET /health, /llms.txt, /v1/help
- Every other endpoint: Authorization: Bearer <TOKEN>
- Typed OpenAPI: /openapi.json (interactive views: /docs, /redoc)
- Errors: {{"error":{{"code":"...","message":"...","details":{{...}}}}}}

## Minimal workflow
1. GET /v1/agents and /v1/sessions; inspect capabilities and existing context.
2. POST /v1/jobs with {{"prompt":"...","cwd":"..."}}.
   For retry-safe submission, reuse one Idempotency-Key with the same request.
3. GET /v1/jobs/{{ref}}/events with Accept: text/event-stream.
   Resume with Last-Event-ID or ?after=<last seq>.
4. GET /v1/jobs/{{ref}} for the full result/error.

Statuses: queued, running, succeeded, failed, canceled. References are full
UUIDs, exact titles, or unique UUID prefixes; ambiguity is a 409.

## Discovery
GET /v1/agents -> configured agents, models/defaults, capability flags, and
server features. Configured here:
{described_agents}
GET /v1/models?agent= -> retained model-catalog projection.
GET /v1/info?refresh=1 -> cached host/GPU/scheduler capabilities; refresh starts
an asynchronous re-probe.
GET /v1/sessions?cwd=&agent= -> resumable session candidates.

Configured agent names: {agents}. Dispatch modes: {dispatch}.

## Submit: POST /v1/jobs
Strict JSON fields (unknown fields are rejected): {fields}.
- prompt: required non-empty string.
- cwd: optional allowed absolute directory; defaults per agent.
- agent/model: select backend and its model.
- session: existing session id; omit for fresh.
- title: human handle; derived from the prompt when omitted.
- fork: default true. false resumes an IDLE session in place and requires
  session. A busy target returns session_busy with held_by and steer_ref.
- permission_mode: backend-specific override.
- include_thinking: retain thinking events; default false.
- files: path refs or inline {{name,text}} / {{name,content_b64}}.
Multipart is also accepted: JSON `payload` plus file parts.

Allowed directories:
{allowed}

A successful submission is 202 with Location and
{{id,status,agent,cwd,title,fork,include_thinking,files,replayed}}.
Idempotency-Key replay returns the same id with replayed=true; a changed request
under the same key is idempotency_conflict.

## Continue work: use the first matching operation
1. A job is RUNNING -> POST /v1/jobs/{{ref}}/steer {{"prompt":"..."}}.
2. Session is IDLE and must see the message -> POST /v1/jobs with
   session + fork:false.
3. Need its history but work branches -> POST /v1/jobs with session (forks).
4. Genuinely new subject -> omit session.

Steer 202 means the live input channel accepted the message; the model sees it
at its next tool boundary. Strict exactly-once steering is not promised. Watch
for the `steer` event to see where it landed.

POST /v1/jobs/{{ref}}/cancel interrupts first (SIGINT/ESC semantics), then
escalates after the grace period. Repeating an already-achieved cancellation is
idempotent.

## Jobs and events
GET /v1/jobs?limit=1..200&cursor= -> paged summary rows only, with
next_cursor/has_more. GET /v1/jobs/{{ref}} -> full public detail.

GET /v1/jobs/{{ref}}/events?after=N&limit=1..1000&legacy=false ->
{{events,status,terminal,next_after,has_more,job:null}}.
Every source uses one transactional, monotonic per-job sequence allocator.

With Accept: text/event-stream the same URL returns SSE frames and heartbeats.
Event types: status, assistant, thinking, tool_use, tool_result, steer, result,
error, log, message. The SSE request closes when the CODING-AGENT job is
terminal.

## Batch/external reports are post-terminal annotations
POST /v1/jobs/{{ref}}/message accepts status, msg, report, host, slurm_job_id,
ts, optional report_id, and extra scheduler fields. report_id deduplicates
retries. A worker reports milestones by writing files into $AB_JOB_DIR
(`ab-notify --msg` does that write); this endpoint is the HTTP equivalent.

A report may arrive after the agent job and its SSE request have finished. It
does not reopen or replace job status. Reconnect with the last cursor or poll an
event page to retrieve later `message` annotations; one open SSE does not wait
forever for batch work.

## Files
POST /v1/files -> upload JSON/multipart, returning {{upload_id,dir,paths}}.
GET /v1/files/list?dir=&glob=*&recursive=false&limit=1..1000&cursor= -> paged
sandboxed rows. GET /v1/files/content?path= -> streamed bytes.

## Client-safe examples
ab agents --output json
ab submit -F task.md --title nightly --idempotency-key nightly-20260810 --output json
ab events nightly --follow --type assistant --output jsonl
ab wait nightly --timeout 900 --output json

CLI wait timeouts do not cancel unless --cancel-on-timeout is explicit. CLI
exit codes: 0 success, 1 local/transport error, 2 invocation error, 3 waited
remote failure/cancel, 4 wait timeout while the remote job may continue.
"""


def _allowed_dirs(cfg: Config) -> list[str]:
    seen: list[str] = []
    for agent in cfg.agents.values():
        for path in agent.allowed_dirs:
            if path not in seen:
                seen.append(path)
    return seen
