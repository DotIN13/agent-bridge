---
name: agent-bridge-install-gateway
description: Install an agent-bridge gateway on a remote host and register it with a local `ab` — clone and put `bin/` on PATH, let `ab-serve` seed the config and its venv, edit the three settings that matter, find the token, write the laptop's `gateways.json` entry, and verify each rung before trusting the next. Use when standing up a gateway on a new login node, when `ab health` cannot reach one, or when a gateway starts but every job fails with a "not found".
allowed-tools: Bash, Read, Write, Edit
---

# Installing a gateway

The gateway runs on the remote host (a cluster login node, usually). You need a
shell there and nothing else: **no root, no pip install, no venv by hand, no
config copied by hand.** `ab-serve` does that work on its first run.

Two halves, and both have to be right before anything works: the gateway on the
host, and one entry in the *laptop's* `gateways.json` pointing an ssh forward at
it.

## 1. On the host: clone, and put `bin/` on `PATH`

```bash
git clone https://github.com/DotIN13/agent-bridge && cd agent-bridge
export PATH="$PWD/bin:$PATH"
```

`ab`, `ab-notify`, `ab-monitor` and `ab-serve` are stdlib-only shims — nothing to
build, which is the point on a node with no network and no permission to install.

**Then make it survive a non-interactive shell**, or nothing else here works.
`ssh host "ab-serve"` runs a *non-interactive* bash: it does read `~/.bashrc`
(stdin is a socket), but almost every distribution's `.bashrc` opens with an
early return when not interactive, so exports below that line never happen. So
put this at the **very top** of `~/.bashrc`, above the guard:

```bash
export PATH=/path/to/agent-bridge/bin:$HOME/.local/bin:$HOME/.opencode/bin:$PATH
```

- **Above any `module load`, too.** A module command that fails aborts the rest
  of the file under `set -e`-ish conditions and leaves the exports unreached —
  and it is the module lines that break when a cluster retires a version.
- `~/.bash_profile` sourcing `~/.bashrc` is the usual arrangement, and is what
  makes the same `PATH` true for interactive logins. Check it does.
- `~/.local/bin` and `~/.opencode/bin` are where `claude` and `opencode` install
  themselves. `ab-serve` prepends them for the gateway anyway, but having them
  here means *your* shell agrees with what the gateway sees.

Settle it with the one command that answers both questions at once:

```bash
ssh host 'command -v ab-serve; command -v claude; echo "PATH=$PATH"'
```

If `ab-serve` does not print a path there, stop and fix `.bashrc` first. Every
later symptom will look like something else.

## 2. One command: `ab-serve`

```bash
bin/ab-serve --no-park          # bootstrap, start it detached, exit
```

What it does, in order, and all of it skippable-if-already-done:

| | | Where |
|---|---|---|
| seeds `config.toml` from `config.example.toml` if absent | keeps an edited one | `serve.py: seed_config` |
| creates `.venv` and installs `requirements.txt` when `agent-bridge` is not on `PATH` and FastAPI cannot be imported — `uv` if present, else `venv` + `pip` | idempotent; one import check per later run | `serve.py: bootstrap_venv` |
| prepends `~/.local/bin` and `~/.opencode/bin` to the `PATH` the **gateway** inherits, and names any configured agent still not findable | `--path DIR` to change | `serve.py: child_path` |
| checks `/health`, starts the gateway if nothing is there, refuses to touch a port held by something else | never kills anything | `serve.py: ensure_serving` |
| starts it in a session of its own, so it outlives this shell and this ssh | | `serve.py: _start` |

Expected output on a fresh clone:

```
ab-serve: no config.toml; copied config.example.toml -- edit allowed_dirs and agents
ab-serve: installing the gateway's dependencies into …/.venv (first run, using uv; this takes a minute)
ab-serve: dependencies installed; using …/.venv/bin/python
ab-serve: warning: not on PATH: opencode (opencode)
ab-serve: nothing on 8787; starting: …
ab-serve: serving on 8787 (agent-bridge 0.3.0), pid 1615, log …/gateway.log
```

Read the `warning:` line. It means a configured agent's binary cannot be found,
and a gateway with that warning starts fine and fails **every** job with a "not
found" — fix `PATH` or set that agent's `bin` to an absolute path.

`--no-park` starts it and exits, which is the form to run by hand. Plain
`ab-serve` holds the connection open while the gateway serves and restarts it up
to three times if it dies; that is the form an ssh line uses.

**On a shared login node, pick a port nobody else has.** `[server] port` in
`config.toml`; `ab-serve` will refuse a port that answers TCP but not `/health`
rather than fight over it.

## 3. Edit exactly three things in `config.toml`

Everything else has a working default. These do not:

```toml
[worker]
concurrency = 16              # a waiting job holds a process, not a slot

[agents.claude]
bin = "claude"                # or an absolute path, if PATH is a fight
default_cwd = "/project/you/some-project"   # NOT this checkout
allowed_dirs = ["/project/you", "/home/you"]
```

- **`allowed_dirs` is the gate on where jobs may run.** A submit whose `--cwd`
  falls outside it is refused (`cwd … is not under any allowed_dirs`), and the
  same list sandboxes `ab upload`/`ab download`. Name the project trees and the
  home you actually work in. It is not an OS-level jail — with
  `permission_mode = "bypassPermissions"` an agent can still write elsewhere —
  so read it as "where a session may be *placed* and what may be *transferred*".
- **`default_cwd` must not be this checkout.** It is what a caller inherits when
  they forget `--cwd`, and a delegate that starts in the tool that dispatched it
  greps the wrong repo and can commit into it.
- **`concurrency`** can be high (16 in the example) because a job `waiting` for
  its report holds a process, not a queue slot.

Restarting after an edit — the gateway does not reload config:

```bash
PID=$(lsof -tiTCP:8787 -sTCP:LISTEN | head -1)   # or read pid from ab-serve's line
kill "$PID"; bin/ab-serve --no-park
```

Never `pkill -f agent-bridge`: a pattern kill matches your own command line and
whatever else mentions it. Stop by pid, found from the port.

## 4. The token

`[auth] token = ""` means the gateway generates one on first start and writes it
to `.token` in the data dir (mode 0600; the data dir is `config.toml`'s own
directory unless `AGENT_BRIDGE_DATA_DIR` says otherwise):

```bash
cat .token                                              # simplest
.venv/bin/python -m gateway --config config.toml --print-token   # equivalently
```

- `/health` needs no token — that is why `ab-serve` can probe without reading
  one. Everything under `/v1/` needs `Authorization: Bearer <token>`.
- **Never put the token in a job environment or `--export`.** Job environments
  are readable from scheduler metadata by other users on a shared node.
- It is gitignored (`.token`, `config.toml`). Keep it that way.

## 5. On the laptop: one entry in `gateways.json`

`~/.config/agent-bridge/gateways.json`, or `$AGENT_BRIDGE_CLIENT_CONFIG`:

```json
{
  "default": "midway5",
  "gateways": {
    "midway5": {
      "base_url": "http://localhost:8787",
      "token_env": "AGENT_BRIDGE_TOKEN",
      "ssh": "ssh -L 8787:localhost:8787 midway5",
      "exec": true
    }
  }
}
```

- `base_url` is **localhost** — the forward's local end, not the host's name.
- Token by `token_env` or `token_file`; a raw `token` works and is the one that
  ends up in a screenshot.
- `ssh` and `exec` are read by the dashboard (`webui/`), not by `ab`. `exec:
  true` runs `PATH="${AB_PATH:+$AB_PATH:}$PATH"; exec ab-serve` on the far side,
  so connecting starts the gateway. Single-quote it if you type the same line in
  a terminal, or your local shell expands `$AB_PATH` before ssh sees it.
- Hold the tunnel yourself with `ssh -N -L 8787:localhost:8787 midway5`, or let
  the dashboard do it.

## 6. Verify in this order

Each rung tells you which half is wrong; skipping one turns a five-second fix
into a hunt.

| | Command | A failure here means |
|---|---|---|
| 1 | on the host: `curl -s localhost:8787/health` | the gateway is not up — read `gateway.log` |
| 2 | `ab health` | the *tunnel* is down, or `base_url`'s port is wrong |
| 3 | `ab agents --output json` | the token is wrong (health needs none, this does) |
| 4 | `ab info` | reachable and authorized; also prints `allowed_dirs` and the operator notes |
| 5 | `ab run --cwd <project> -F /tmp/hello.md --timeout 120` | the agent binary or `allowed_dirs` — the two things a probe cannot check |

Rung 5 is the real acceptance test: a one-line prompt that ends with a report
proves `bin`, `PATH`, `allowed_dirs`, `default_cwd` and the job dir all agree.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `ab-serve: command not found` over ssh but fine when logged in | the export is below `.bashrc`'s interactivity guard, or after a failing `module load` |
| gateway healthy, every job fails `claude: not found` | the gateway's `PATH` — the `warning: not on PATH` line said so at start; set `bin` absolute |
| `cwd … is not under any allowed_dirs` | `--cwd` (or `default_cwd`) outside the list. `/tmp` is usually outside it; the upload store under `$TMPDIR` is the one exception, and only for transfers |
| `port 8787 is held by something that is not answering /health` | somebody else's process, or a gateway still booting. Pick another port; do not kill it |
| `gateway did not answer on 8787 within 60s` | a config or dependency error — `ab-serve` prints the tail of `gateway.log` right there |
| `bootstrap failed` with pip output | no network on the login node, or a proxy. Install the three deps by hand into `.venv`, then re-run |
| the dashboard's tunnel goes red on connect | the remote command exited. A command that returns immediately takes the connection with it; `… && exec ab-serve` does not |

## Persistence

The gateway **already survives** the ssh connection dropping — it is started in
its own session, and `ab-serve` says *"leaving the gateway running"* on the way
out. Jobs outlive the turn that submitted them by design, so a closed laptop
must not take them with it.

It does **not** survive a node reboot. For that, use the shipped user unit:

```bash
loginctl enable-linger $USER            # the step people forget
mkdir -p ~/.config/systemd/user && cp systemd/agent-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now agent-bridge
```

It points at `.venv/bin/python -m gateway` and sets the same `PATH` `ab-serve`
prepends. The two compose: with systemd owning the lifetime, `exec: true` finds
it already serving, says so, and just holds the tunnel.

## Repo

`README.md` (install and `ab-serve`) · `config.example.toml` (every setting,
with the reasoning) · `webui/README.md` (the dashboard and `exec`) ·
`docs/design/22`, `docs/design/24` (why `ab-serve` behaves as it does).
