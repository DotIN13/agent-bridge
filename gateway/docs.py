"""Self-describing, agent-facing usage doc served at /llms.txt and /v1/help.

Rendered from live config so an LLM agent fetches accurate, instance-specific
instructions (real allowed dirs, agents, dispatch mode) before driving the API.
"""
from __future__ import annotations

from . import __version__
from .config import Config


def render_llms_txt(cfg: Config) -> str:
    agents = ", ".join(sorted(cfg.agents)) or "(none)"
    allowed = "\n".join(f"  - {d}" for d in _allowed_dirs(cfg))
    dispatch = ", ".join(
        f"{n}={c.dispatch_mode}" for n, c in cfg.agents.items()
    )
    base = f"http://{cfg.host}:{cfg.port}"
    _def = cfg.agents.get(cfg.default_agent) if cfg.agents else None
    models = ", ".join(_def.models) if _def and _def.models else "none configured"
    return f"""\
# agent-bridge — how to use this API (for LLM agents)

You are talking to **agent-bridge {__version__}**, an HTTP gateway that runs a
coding-agent prompt inside the most relevant *existing* agent session (Claude
Code or opencode; it forks that session, so nothing is mutated) and streams back
the result.

Submit a task, then either stream events (SSE) or poll until the job reaches a
terminal status. One job = one prompt executed by one agent in one directory.

## Base URL & auth
- Base URL: {base} (this is where you fetched this document).
- All endpoints EXCEPT `/health`, `/llms.txt`, `/v1/help` require a header:
      Authorization: Bearer <TOKEN>
  You must already have the token (it is NOT served here). It is typically in
  the env var AGENT_BRIDGE_TOKEN. If you get 401, you are missing/using a bad token.
- Content-Type: application/json for POST bodies.

## The one workflow you need
1. POST /v1/jobs with {{"prompt": "...", "cwd": "..."}}  -> returns {{"id": ...}}
2. Stream:  GET /v1/jobs/{{id}}/events  with header  Accept: text/event-stream
   or Poll: GET /v1/jobs/{{id}}          until "status" is terminal.
3. Read the final answer from the job's "result" field, or the "result" SSE event.

Terminal statuses: succeeded, failed, canceled. Non-terminal: queued, running.

## Endpoints
GET  /health                 -> {{"ok": true}}                        (no auth)
GET  /llms.txt | /v1/help     -> this document                        (no auth)
GET  /v1/agents               -> {{"configured":[...],"default":"..."}}
GET  /v1/models[?agent=]      -> model ids this agent is configured to accept
                                 (a simple list). Read before setting `model`
                                 on a job; pass `agent` to scope to a specific
                                 one (configured: {agents}). Models for
                                 {cfg.default_agent}: {models}.
GET  /v1/info[?refresh=1]  -> this machine's capabilities (host/CPU/RAM,
                                 local GPUs, Slurm partitions + GPU inventory,
                                 allocation balance). Cached; read before choosing
                                 where/how to run heavy work. `summary` is a
                                 one-line human digest.
GET  /v1/sessions?cwd=&agent= -> {{"sessions":[{{session_id,cwd,title,summary,
                                  git_branch,last_active,messages}}]}}
POST /v1/jobs                 -> 202 {{"id","status","agent","cwd"}}
GET  /v1/jobs                 -> {{"jobs":[...recent...]}}
GET  /v1/jobs/{{id}}            -> the job row (see fields below)
POST /v1/jobs/{{id}}/cancel    -> 202; cancel a queued/running job (kills the
                                 agent process). 409 if already finished.
GET  /v1/jobs/{{id}}/events    -> SSE if Accept: text/event-stream,
                                 else JSON {{"job","events","terminal"}}. Poll
                                 with ?after=<last seq> to page incrementally.
POST /v1/files                -> upload files; returns {{"paths":[...]}} to pass
                                 later as files:[{{"path":...}}]. JSON inline or
                                 multipart (see below).
GET  /v1/files/list?dir=&glob=&recursive= -> [{{path,is_dir,size,mtime}}] files
                                 and dirs within allowed dirs
GET  /v1/files/content?path=  -> streams a file back (artifacts, result CSVs)

## Sending files with a job (uploads)
Two ways, both one call:
  A. JSON body with a `files` array; each item is one of:
       {{"name":"data.csv","content_b64":"<base64>"}}   (binary)
       {{"name":"run.py","text":"..."}}                  (text)
       {{"path":"/abs/existing/on/node"}}                (reference; no upload)
  B. multipart/form-data: a form field `payload` = the JSON body above, plus one
     or more file parts (field name `files`). Best for larger/binary files.
Uploaded files land in a per-user file store (a temp dir by default, locked to
you) and their absolute paths are surfaced to the agent as ATTACHED FILES; fetch
them back later via /v1/files/content. For very large data, scp/rsync into an
allowed dir and pass {{"path":...}} instead.

## POST /v1/jobs request body
  prompt           (string, REQUIRED) the task to run. Write it as you would to
                   a coding agent working in a repo. It executes in a forked
                   session at `cwd`, so refer to files by repo-relative path.
  cwd              (string, optional) absolute working directory. MUST be within
                   an allowed directory (see below) or the request is rejected 400.
                   Defaults to the agent's default_cwd.
  agent            (string, optional) one of: {agents}. Default: {cfg.default_agent}.
  session          (string, optional) a session_id hint to prefer forking.
  model            (string, optional) model id for this agent, e.g. from
                   /v1/models (for claude: "claude-sonnet-5"; for opencode:
                   "deepseek/deepseek-v4-flash"). Omit for the agent default.
  permission_mode  (string, optional) e.g. "bypassPermissions","acceptEdits".
  include_thinking (boolean, optional) keep the agent's reasoning ("thinking")
                   events in the stream. Hidden by default; pass true only when
                   you need to see the reasoning.
  files            (array, optional) attachments (see "Sending files with a job").

Allowed directories (jobs outside these are refused):
{allowed}

## Job row fields (GET /v1/jobs/{{id}})
  id, status, agent, prompt, cwd, requested_session,
  chosen_session   (session the dispatcher forked, or null),
  forked_session   (new session id created by the fork, or null),
  result           (final answer text; present when succeeded, sometimes on fail),
  error            (failure reason or null),
  cost_usd, created_at, started_at, finished_at

## SSE event stream (GET /v1/jobs/{{id}}/events, Accept: text/event-stream)
Each event: `id: <seq>`, `event: <type>`, `data: <json>`. Reconnect with
`?after=<last seq>` (or Last-Event-ID header) to resume with no gaps/dupes.
Event types and their data:
  status       progress markers ({{stage:"running"|"done", ...}})
  thinking     {{text}}   model reasoning; only present if the job was submitted
                          with include_thinking=true (hidden by default)
  assistant    {{text}}   assistant output, streamed in chunks
  tool_use     {{name,input}}   a tool the agent invoked
  tool_result  {{text}}
  result       {{text, cost_usd, chosen_session, forked_session, is_error}} FINAL
  error        {{message}}   the run failed
  log          misc/raw lines
Stop when you see event `status` with data.stage=="done", or a `result`/`error`.

## How dispatch works (why you don't pick a session)
Default `direct` dispatch runs the prompt in the session you name (`session`),
or a fresh one if you omit it — no routing model in the path. `GET /v1/sessions`
lists what's forkable. Use `session` to pin one; the fork never mutates it.
`fork: false` + `session` resumes that thread in place instead of forking.
Other dispatch modes exist for claude (`agent_exec`, `select_then_exec`) and
ask the agent to pick a fork target; see config.toml.

## Minimal examples
curl -s -X POST {base}/v1/jobs \\
  -H "Authorization: Bearer $AGENT_BRIDGE_TOKEN" -H "Content-Type: application/json" \\
  -d '{{"prompt":"run the test suite and summarize failures","cwd":"{_first_allowed(cfg)}"}}'

# then stream:
curl -sN {base}/v1/jobs/<ID>/events \\
  -H "Authorization: Bearer $AGENT_BRIDGE_TOKEN" -H "Accept: text/event-stream"

# or poll to completion (JSON):
curl -s {base}/v1/jobs/<ID> -H "Authorization: Bearer $AGENT_BRIDGE_TOKEN"

## Rules for good behavior
- Keep cwd inside the allowed directories.
- Prefer streaming for long jobs; there is no server-side wall-clock limit, so a
  job runs until the agent finishes. Use your own client timeout if needed.
- One prompt per job. For a follow-up, submit a new job (optionally pass the
  prior `forked_session` as `session` to continue that thread).
- Dispatch modes in effect: {dispatch}.
"""


def _allowed_dirs(cfg: Config) -> list[str]:
    seen: list[str] = []
    for a in cfg.agents.values():
        for d in a.allowed_dirs:
            if d not in seen:
                seen.append(d)
    return seen


def _first_allowed(cfg: Config) -> str:
    dirs = _allowed_dirs(cfg)
    return dirs[0] if dirs else "/path/to/project"
