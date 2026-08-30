# agent-bridge CLI client

A stdlib-only client for driving one or more agent-bridge gateways through SSH
port-forwards. `abclient.py` provides discovery, SSE streaming, waiting, errors,
uploads, and safe downloads to the `ab` CLI.

## Install or copy

An editable/package install provides stable commands:

```bash
python -m pip install -e .
ab --version                       # 0.3.0
```

No install is needed, though — the shims in `bin/` are stdlib only, so a clone
on `PATH` is a working client:

```bash
export PATH="/path/to/agent-bridge/bin:$PATH"
ab --version
```

Direct invocation also works: `python client/ab.py --version`.

The copied `client/` directory remains dependency-free. TOML client config
requires Python 3.11; JSON works on older supported Python versions.

Configure `~/.config/agent-bridge/gateways.json`:

```json
{
  "default": "midway5",
  "gateways": {
    "midway5": {
      "base_url": "http://localhost:8787",
      "token_env": "AGENT_BRIDGE_TOKEN"
    }
  }
}
```

Token order: `token`, `token_env`, `token_file`. Config discovery:
`--config`, `$AGENT_BRIDGE_CLIENT_CONFIG`,
`~/.config/agent-bridge/gateways.json`, `./gateways.json`.

Three further keys are read by the dashboard in [`webui/`](../webui/README.md)
and ignored here: `ssh`, the command that makes `base_url` reachable; `exec`,
what to run on the far side once it is up (`true` for the shipped `ab-serve`, or
a script of your own); and `autostart`. `ab` never starts a tunnel — it expects
one to exist — so an entry carrying them works unchanged at the CLI.

## CLI

Global flags work before or after the command:

```bash
ab --gateway midway5 --output json jobs
ab jobs --gateway midway5 --output json
```

| Command | Purpose |
|---|---|
| `gateways [--no-probe]` | every configured gateway: token presence **and** live reachability |
| `health` | unauthenticated liveness/version probe of one gateway |
| `agents` | configured backends, models, and capability flags |
| `help [--remote]` | local CLI help; `--output json` gives the client contract (version, output modes, exit codes, verbs); `--remote` fetches `/v1/help` |
| `info [--refresh]` | cached host/cluster capabilities |
| `models [--agent A]` | advertised model ids |
| `sessions [--cwd D] [--agent A]` | resumable/forkable sessions |
| `submit -F FILE [...]` | submit and return the job id immediately |
| `run -F FILE [...]` | submit and wait |
| `jobs [--limit N] [--cursor C]` | paged job summaries |
| `job REF [--wait]` | job detail, optionally wait |
| `wait REF` | wait for an existing job |
| `events REF [-f]` | event page or resumable SSE follow |
| `steer REF -F FILE` | message a running turn |
| `cancel REF` | interrupt a queued/running job |
| `upload`, `download`, `ls` | safe file transfer and listing |

`REF` is a full UUID, exact title, or unique UUID prefix. Ambiguity is an error.
Use `-F/--prompt-file` for agent-generated prompts to avoid shell quoting.
`--prompt-stdin` is the explicit stdin form.

### Machine output

`--output` is the canonical output selector:

- `human` (default): readable tables/transcripts; long display text may be
  elided unless `--full`.
- `json`: exactly one complete, faithful JSON document. It is never mixed with
  streamed assistant text. `--json` is an alias.
- `jsonl`: one compact typed record per line, intended for `run`, `wait`, and
  `events --follow`. Record kinds include `event`, `terminal`, `timeout`, and
  `complete`.

```bash
ab submit -F task.md --title nightly --output json
ab events nightly --follow --type assistant --output jsonl
ab wait nightly --timeout 900 --output json
```

Follow uses resumable SSE and honors `--after`, `--until`, and repeatable
`--type`. A bounded `--until` can finish before the remote job is terminal.

### Exit codes and timeouts

| Code | Meaning |
|---:|---|
| 0 | command/query succeeded |
| 1 | local config, transport, protocol, or file error |
| 2 | invalid CLI invocation |
| 3 | waited remote job failed or was canceled |
| 4 | wait timed out while the remote job may still be running |

`run`, `wait`, and `job --wait` do **not** cancel on timeout. Use
`--cancel-on-timeout` only when cancellation is intended. Snapshot `job` and
`events` queries return zero for inspectable failed jobs unless
`--fail-on-job-failure` is supplied.

### Submission and retry safety

`run` and `submit` accept `--cwd`, `--agent`, `--model`, `--session`,
`--no-fork`, `--title`, `--permission-mode`, `--include-thinking`, repeatable
`--upload`/`--file`, and `--idempotency-key`.

- `--no-fork` requires an **idle** `--session`; use `steer` for a running turn.
- Reuse one `--idempotency-key` when retrying the same submission. The same key
  with different content is rejected.
- `agents --output json` reports which adapter/mode supports sessions,
  in-place resume, steering, thinking, and attachments.

### Safe files

Uploads require readable regular non-symlink files. Use
`--upload-as REMOTE=LOCAL` on a job or `upload --as REMOTE=LOCAL` to preserve an
explicit remote name; duplicate destinations are rejected.

Downloads preserve relative paths under `--to` by default, reject traversal,
symlink roots, target collisions, and existing files, and publish each file via
an atomic temporary file. Use `--overwrite` explicitly to replace existing
files. `--flatten` is the legacy basename-only layout; collisions still fail.

```bash
ab download --dir /project/x/out --glob '*.csv' --recursive --to ./out
ab download --file /project/x/report.md --to ./out --overwrite
```

## Notes

- Open the tunnel first, for example
  `ssh -o ServerAliveInterval=60 -L 8787:localhost:8787 midway5`.
- `gateways` probes every gateway concurrently and always exits 0 — a down
  gateway is data, not a command failure. Branch on `ab health` (exit 1 when
  unreachable) or on `state`/`reachable` in `--output json`.
- `gateways --no-probe` is pure offline config inspection and cannot tell you
  anything is working.
- On Git Bash, prefix remote POSIX paths with `MSYS_NO_PATHCONV=1` to prevent
  path rewriting. PowerShell needs no special flag.
