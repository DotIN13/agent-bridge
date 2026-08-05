# agent-bridge clients (CLI + MCP)

Laptop-side clients for driving one or more agent-bridge gateways over their SSH
port-forwards. Pure stdlib (Python 3.8+; 3.11+ for a `.toml` config). Two front
ends over one shared client (`abclient.py`):

- **`ab`** — a CLI. The recommended way. Any shell, human, script, or agent
  (Claude Code drives it via Bash) can use it.
- **`agent_bridge_mcp.py`** — an MCP stdio server, for clients that prefer MCP.

```
your shell / Claude Code ──▶ ab (CLI) ─┐
local Claude Code (MCP)  ──▶ mcp server ┴─▶ abclient ──HTTP──▶ gateway @ localhost:8787 (ssh -L)
                                                        └─────▶ gateway @ localhost:8788
```

## Setup

1. Copy the `client/` folder (or clone the repo) to your laptop.
2. Open the tunnel(s): `ssh -L 8787:localhost:8787 midway5` (one Duo push each).
3. Configure gateways in `~/.config/agent-bridge/gateways.json` (see
   `gateways.example.json`):

```json
{
  "default": "midway5",
  "gateways": {
    "midway5": { "base_url": "http://localhost:8787", "token_env": "AGENT_BRIDGE_TOKEN" },
    "other":   { "base_url": "http://localhost:8788", "token_file": "~/.config/agent-bridge/other.token" }
  }
}
```

Token per gateway: `token` (inline), `token_env` (env var name), or `token_file`
(path) — checked in that order. Config discovery: `--config` →
`$AGENT_BRIDGE_MCP_CONFIG` → `~/.config/agent-bridge/gateways.json` →
`./gateways.json`.

## CLI (`ab`)

```bash
python3 client/ab.py <command> [flags]     # or: ln -s .../client/ab.py ~/bin/ab
```

| Command | What it does |
|---|---|
| `ab gateways` | list configured gateways + default |
| `ab models [--agent A]` | model ids a gateway's agent accepts |
| `ab info [--refresh]` | a gateway's cluster capabilities |
| `ab sessions [--cwd DIR] [--agent A]` | sessions a gateway can fork |
| `ab run PROMPT [...]` | submit **and wait**; prints the result |
| `ab submit PROMPT [...]` | submit, print the job id (no wait) |
| `ab job ID` | status/result |
| `ab events ID [--after N] [--follow]` | event log; `--follow` streams live |
| `ab cancel ID` | cancel a queued/running job |
| `ab upload F... [--dir D]` | upload local files → remote paths |
| `ab download [--file R...] [--dir D --glob G] --to LOCAL` | fetch artifacts |
| `ab ls DIR [--glob G] [--recursive]` | list remote files |

Global: `--gateway NAME`, `--config PATH`, `--json` (machine-readable output).
`run`/`submit` take `--cwd --agent --model --session --permission-mode`, plus
`--upload LOCAL` (repeatable) and `--file REMOTE` (repeatable). `ab run` exits
non-zero if the job doesn't succeed.

Examples:

```bash
ab run "run the tests and summarize failures" --cwd /project/jevans/tzhang3/myrepo
ab run "profile this dataset" --upload ./train.csv --stream
ab submit "long job"; ab events <id> --follow; ab job <id>
ab download --dir /project/jevans/tzhang3/myrepo/out --glob '*.csv' --to ./results
```

**Choosing a backend and model.** `ab models` lists the model ids a gateway's
agent accepts (plain strings), and `--model "$(...)"` pins one. The list lives
in the **gateway's** config (`[agents.<name>] models = [...]` in its
config.toml), not in this client, so it reflects what that agent is actually set
up to accept and an operator can change it without redeploying clients.

A gateway can run more than one agent (typically `claude` and `opencode`). Pick
both at submit time: `--agent` names the backend, `--model` the model inside it.
Scope `ab models` / `ab sessions` with `--agent` the same way:

```bash
ab models --agent opencode   # -> deepseek/deepseek-v4-flash, deepseek/deepseek-v4-pro
ab run -F task.md --agent opencode --model deepseek/deepseek-v4-flash --stream
ab run -F task.md --agent claude --model claude-sonnet-5                  # default backend
```

**Long / multiline prompts.** Avoid shell-quoting and `ARG_MAX` by passing the
prompt on stdin or from a file instead of as an argument:

```bash
ab run --prompt-file ./task.md --cwd /project/...     # from a file (best for agents)
ab run - <<'EOF'                                       # heredoc; literal, no expansion
Multi-line prompt with "quotes", $vars, and `backticks` — all safe.
EOF
cat task.md | ab run -                                 # piped
```

`run`/`submit` accept the prompt as a positional arg, `-F/--prompt-file PATH`, or
stdin (`-` or piped). An LLM agent should prefer `--prompt-file` — write the
prompt to a file, then reference it (zero quoting).

Using it from Claude Code: it already has Bash — just run `ab ...` (add a line to
your CLAUDE.md so the agent reaches for it, and `--json` for reliable parsing).

## MCP server

For MCP clients. Same operations as the CLI, as tools:

```bash
claude mcp add agent-bridge -- \
    python3 /path/to/client/agent_bridge_mcp.py --config ~/.config/agent-bridge/gateways.json
```

Tools: `list_gateways`, `cluster_info`, `list_sessions`, `submit_job`, `get_job`,
`job_events`, `cancel_job`, `run_prompt`, `upload_files`, `download_files`,
`list_remote_files`. Each takes an optional `gateway`; `run_prompt`/`submit_job`
take `upload` (local) and `files` (remote).

## Notes

- `gateways.json` and `*.token` are git-ignored (they reference tokens).
- MCP diagnostics go to stderr; stdout is the JSON-RPC channel.
- Both front ends share `abclient.py` — one place to fix transport/auth.
