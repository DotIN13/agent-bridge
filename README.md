# agent-bridge

An HTTP gateway that accepts prompts, runs them through a coding agent in the
**right existing session** (by forking it), and returns results. Built for a
shared HPC login node: stdlib-only Python, localhost-bound, queue-worker model,
SQLite-backed logs that downstream apps can **SSE-stream or poll**.

Claude Code is the first backend; `opencode` / `antigravity-cli` slot in behind
the same adapter interface (`gateway/adapters/`).

```
HTTP client ──POST /v1/jobs──▶ queue ──▶ worker ──▶ ClaudeAdapter
     ▲                                                   │
     └── SSE / poll /v1/jobs/{id}/events ◀── SQLite ◀────┘ (events + result)
```

## Why it's shaped this way (login-node constraints)

- **Duo on every SSH login.** You can't open a fresh SSH connection per task.
  So the gateway runs on the node behind **one** SSH port-forward; all agents
  talk HTTP to the forwarded port. One Duo push, then unlimited requests.
  (Windows can't multiplex SSH — `ControlMaster` is unsupported — so a single
  forwarded port is the portable answer.)
- **Localhost bind + bearer token.** The node is multi-user; nothing listens on
  a public interface. Reach it over the SSH tunnel.
- **Server is FastAPI/uvicorn in a venv; the MCP client is stdlib.** The gateway
  deps (`fastapi`, `uvicorn`, `python-multipart`) install into `.venv` via `uv`
  (`run.sh` does it on first launch). The local clients (`client/`, CLI + MCP)
  stay dependency-free.

## Run it

On the login node, inside a persistent tmux (survives disconnect; the node has
no process reaper and linger is on):

```bash
ssh midway5                 # one Duo push  (see ~/.ssh/config note below)
tmux new -s gw
cd /project/jevans/tzhang3/agent-bridge
cp config.example.toml config.toml   # then edit allowed_dirs etc.
./run.sh
```

It prints the bearer token on startup (auto-generated into `.token` if you left
`auth.token` empty). From your laptop:

```bash
ssh -L 8787:localhost:8787 midway5    # forwards the gateway over the tunnel
```

Then everything below targets `http://localhost:8787`.

### As a systemd user service (restarts across reboot)

```bash
loginctl enable-linger $USER
mkdir -p ~/.config/systemd/user
cp systemd/agent-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-bridge
```

## API

Full reference: **[API.md](API.md)**. Quick tour below.

All routes except `/health`, `/llms.txt`, and `/v1/help` require
`Authorization: Bearer <token>`.

| Method | Path | Purpose |
|---|---|---|
| GET  | `/health` | liveness (no auth) |
| GET  | `/llms.txt` · `/v1/help` | agent-facing usage doc, rendered from live config (no auth) |
| GET  | `/v1/agents` | configured & known agent backends |
| GET  | `/v1/info?refresh=1` | this machine's capabilities (host/CPU/RAM, GPUs, Slurm partitions + GPU inventory, allocation balance), cached |
| GET  | `/v1/sessions?cwd=&agent=` | the session index the dispatcher sees |
| POST | `/v1/jobs` | enqueue a prompt → `202 {id}` |
| GET  | `/v1/jobs` | recent jobs |
| GET  | `/v1/jobs/{id}` | job row (status, result, session ids, cost) |
| POST | `/v1/jobs/{id}/cancel` | cancel a queued/running job (kills the agent) |
| GET  | `/v1/jobs/{id}/events?after=N` | **SSE** stream, or one-shot JSON poll |
| POST | `/v1/files` | upload files (JSON inline or multipart) → remote paths |
| GET  | `/v1/files/list?dir=&glob=&recursive=` | list files within an allowed dir |
| GET  | `/v1/files/content?path=` | stream a file back (artifacts, result CSVs) |

**Driving it from an LLM agent:** point the agent at `GET /llms.txt` first — it
returns the whole contract (endpoints, body schema, event types, this instance's
allowed dirs, dispatch behavior, examples) as markdown, no token needed. The
agent then submits jobs and streams results. See [API.md](API.md#llm-agent-usage).

**Submit:**

```bash
curl -X POST http://localhost:8787/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"prompt":"add a --verbose flag to cli.py and run the tests",
       "cwd":"/project/jevans/tzhang3/myrepo"}'
# -> {"id":"...","status":"queued","agent":"claude","cwd":"..."}
```

Request body: `prompt` (required), `agent` (default `claude`), `cwd`
(validated against `allowed_dirs`), `session` (optional hint), `model`,
`permission_mode`.

**Stream results (SSE):**

```bash
curl -N http://localhost:8787/v1/jobs/$ID/events \
  -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream"
```

Each event has an `id:` (per-job seq), an `event:` type, and JSON `data:`.
Types: `status`, `thinking`, `assistant`, `tool_use`, `tool_result`, `result`,
`error`, `log`. Reconnect with `?after=<last seq>` (or the `Last-Event-ID`
header) to replay only newer events — no gaps, no dupes.

**Poll instead** (same URL, `Accept: application/json`): returns
`{job, events, terminal}` for events after `N`.

`client_example.py` is a ~60-line stdlib client that submits and streams.

## How dispatch works

Per the spec, the dispatcher **is** an agent. For each job the worker launches a
short Claude session whose appended system prompt (`gateway/adapters/claude.py`,
`_DISPATCH_PROMPT`) contains a JSON index of recent sessions. It:

1. picks the session whose cwd/topic best matches the task,
2. forks it: `cat TASK_FILE | claude --resume <id> --fork-session -p --output-format json`,
3. runs the task in the fork and reports the answer.

`--fork-session` guarantees the original session is never mutated — verified: a
run against session `86e…` produced a new `1f7…` and left `86e…` byte-identical.
The task is piped from a file so prompt quoting is never an issue. The worker
records `chosen_session` and `forked_session` on the job row.

Alternative `dispatch_mode = "select_then_exec"` (config): the model only
*chooses* a session (structured output, no tools) and the worker does the
fork+exec directly — more deterministic and cheaper, cleaner token-level
streaming, but the agent isn't "executing itself."

## Configuration

See `config.example.toml`. Key knobs: `worker.concurrency` (mind the sshd
`MaxSessions 10` cap and API cost), `agents.claude.allowed_dirs` (the cwd
allowlist — the gateway refuses jobs outside it), `permission_mode`
(`bypassPermissions` for headless; there's no TTY to answer prompts),
`dispatch_mode`, `model`, `timeout_sec` (`0` = no wall-clock limit; default).
Any scalar is overridable via
`AGENT_BRIDGE_<SECTION>_<KEY>` env vars.

## Files (inputs & artifacts)

Send inputs with a job and pull results back — all sandboxed to `allowed_dirs`:

- **Upload + submit in one call.** `POST /v1/jobs` accepts a `files` array (JSON
  inline: `{name, content_b64|text}` or `{path}` reference) **or** multipart
  (`payload` JSON field + file parts). Uploads land in a **per-user file store**
  (a `$TMPDIR` dir by default, created `0700`), and their absolute paths are
  surfaced to the agent as *ATTACHED FILES* (readable by the forked agent and
  downloadable, even though the store may sit outside `allowed_dirs`).
- **Fetch artifacts.** `GET /v1/files/list?dir=&glob=` to discover, then
  `GET /v1/files/content?path=` to stream a file back (result CSVs, etc.).
- **Large data** → `scp`/`rsync` into an allowed dir over your SSH session and
  pass `{"path": ...}`; caps (`max_file_mb`, `max_request_mb`) bound HTTP uploads.

From a client: `--upload`/`--file` on `ab run` (or `upload`/`files` on the MCP
`run_prompt`), plus `ab upload` / `ab download` / `ab ls`.

## Local clients (CLI + MCP)

Drive one or more gateways from your laptop over the SSH port-forward(s). Both
front ends live in [`client/`](client/README.md), stdlib-only, sharing one
transport module (`abclient.py`):

- **`ab` CLI (recommended)** — any shell/human/script/agent uses it; Claude Code
  drives it via Bash:
  ```bash
  python3 client/ab.py run "run the tests" --cwd /project/jevans/tzhang3/myrepo
  python3 client/ab.py run "profile it" --upload ./train.csv --stream
  python3 client/ab.py download --dir /project/.../out --glob '*.csv' --to ./results
  ```
- **MCP server** — for MCP clients:
  ```bash
  claude mcp add agent-bridge -- \
      python3 /path/to/client/agent_bridge_mcp.py --config ~/.config/agent-bridge/gateways.json
  ```

See [client/README.md](client/README.md) for all commands/tools and config.

## Adding an agent backend

Implement `list_sessions()` and `run(spec, emit)` in a new
`gateway/adapters/<name>.py` (see `base.py` / `claude.py`), then register it in
`gateway/adapters/__init__._REGISTRY`. Emit `Event`s as the run progresses and
return a `RunResult`; queueing, persistence, SSE, and auth are handled for you.

## Caveats

- **Login5-specific.** The gateway lives on one login node; `midway3.rcc`
  round-robins, so your tunnel must target `midway3-login5` explicitly, and it
  dies with that node's reboot (systemd + linger brings it back after).
- **Auth for `claude` must be non-interactive.** The forked runs reuse your
  `~/.claude` credential; if that expires, jobs fail until you re-auth.
- **Policy.** Login nodes are for light work. Keep `concurrency` low and don't
  run heavy compute here — submit those to Slurm from within a job.
- **bypassPermissions** lets the agent edit files and run commands freely inside
  `allowed_dirs`. Scope that list to what you actually want reachable.
```
