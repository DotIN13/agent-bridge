# agent-bridge MCP server (local)

A single-file, stdlib-only MCP server you run on **your local machine**. It lets
a local Claude Code (or any MCP client) drive one or more agent-bridge gateways
through tools — submit prompts, wait for results, inspect sessions, and read each
cluster's capabilities — routed to whichever gateway you name.

```
local Claude Code ──stdio(MCP)──▶ agent_bridge_mcp.py ──HTTP──▶ gateway @ localhost:8787 (ssh -L)
                                        │                └─────▶ gateway @ localhost:8788
                                        └── gateways.json (you configure)
```

## 1. Copy the two files to your laptop

`agent_bridge_mcp.py` and a config based on `gateways.example.json`. Requires
Python 3.8+ (3.11+ if you use a `.toml` config).

## 2. Open the SSH tunnel(s)

Each gateway is reached over its own port-forward — one Duo push per tunnel:

```bash
ssh -L 8787:localhost:8787 midway5     # gateway on login5
# ssh -L 8788:localhost:8787 other     # a second cluster, forwarded to :8788
```

## 3. Configure gateways

`~/.config/agent-bridge/gateways.json` (or pass `--config`):

```json
{
  "default": "midway5",
  "gateways": {
    "midway5":  { "base_url": "http://localhost:8787", "token_env": "AGENT_BRIDGE_TOKEN" },
    "other":    { "base_url": "http://localhost:8788", "token_file": "~/.config/agent-bridge/other.token" }
  }
}
```

Per gateway, supply the token one of three ways (checked in this order):
`token` (inline), `token_env` (name of an env var), `token_file` (path). Prefer
`token_env`/`token_file` so tokens stay out of the config file. The token is the
one the gateway printed on startup (`python3 -m gateway --print-token`).

Config discovery order: `--config` → `$AGENT_BRIDGE_MCP_CONFIG` →
`~/.config/agent-bridge/gateways.json` → `./gateways.json`.

## 4. Register with Claude Code

```bash
claude mcp add agent-bridge -- \
    python3 /path/to/agent_bridge_mcp.py --config ~/.config/agent-bridge/gateways.json
```

Or add to `.mcp.json` (project) / `~/.claude.json` (user):

```json
{
  "mcpServers": {
    "agent-bridge": {
      "command": "python3",
      "args": ["/path/to/agent_bridge_mcp.py", "--config",
               "/path/to/gateways.json"],
      "env": { "AGENT_BRIDGE_TOKEN": "the-midway5-token" }
    }
  }
}
```

Then in Claude Code the tools appear as `agent-bridge:*`.

## Tools

| Tool | What it does |
|---|---|
| `list_gateways` | list configured gateways + which is default |
| `cluster_info` | a gateway's machine/cluster capabilities (host, CPU/RAM, GPUs, Slurm partitions + GPU inventory, balance); `refresh` re-probes |
| `list_sessions` | sessions a gateway can fork (optional `cwd`) |
| `submit_job` | submit a prompt, return a job id immediately (no wait) |
| `get_job` | fetch a job's status/result |
| `job_events` | fetch a job's event log incrementally (`after` = last seq seen) — poll for progress/streaming |
| `cancel_job` | cancel a queued/running job (kills the agent on the gateway) |
| `run_prompt` | submit **and wait** for the result (polls to completion) — the usual one |

Every tool takes an optional `gateway` argument; omit it to use the default.
`submit_job`/`run_prompt` accept `prompt` (required), `cwd`, `agent`, `model`,
`session`, `permission_mode`. `run_prompt` also takes `timeout_sec` (client wait
cap; it does not cancel the job) and `poll_interval_sec`.

Example (what the model calls): `run_prompt` with
`{"gateway":"midway5","prompt":"run the tests in this repo","cwd":"/project/jevans/tzhang3/myrepo"}`.

## Notes

- If a tunnel is down, tools return a clear error ("cannot reach … is the SSH
  port-forward up?").
- Diagnostics go to stderr; stdout is reserved for the MCP JSON-RPC channel.
- Keep `gateways.json` out of version control (it may reference tokens); the
  repo `.gitignore` already excludes `gateways.json` and `*.token`.
