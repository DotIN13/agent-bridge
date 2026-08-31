---
name: agent-bridge-install-worker
description: Install agent-bridge on the remote worker host — clone and put `bin/` on PATH so a non-interactive ssh finds it, let `ab-serve` seed the config and its venv, edit the three settings that matter, hand over the token, and prove it serves before leaving. Use when standing up a worker host (a cluster login node), when `ab-serve` will not start, or when a gateway is healthy but every job fails with a "not found". Covers the remote host only; configuring a caller is `agent-bridge-client`.
allowed-tools: Bash, Read, Write, Edit
---

# Installing the worker host

The machine where the delegate runs — a cluster login node, usually. It holds
the gateway process, the SQLite database, the job directories, and the agent
binaries (`claude`, `opencode`) that actually do the work.

You need a shell there and nothing else: **no root, no pip install, no venv by
hand, no config copied by hand.** `ab-serve` does that on its first run.

**Scope.** This is the remote half only. What a caller puts in its
`gateways.json`, how it drives `ab`, and how the dashboard holds the tunnel are
`agent-bridge-client` and `webui/README.md`. Finish here, hand over the port and
the token, and stop.

## 1. Clone, and put `bin/` on `PATH`

```bash
git clone https://github.com/DotIN13/agent-bridge && cd agent-bridge
export PATH="$PWD/bin:$PATH"
```

`ab`, `ab-notify`, `ab-monitor` and `ab-serve` are stdlib-only shims — nothing to
build, which is the point on a node with no network and no permission to install.

**Then make it true for a non-interactive shell**, or nothing else here works.
A caller starts the gateway with `ssh host "ab-serve"`, which runs a
*non-interactive* bash: it does read `~/.bashrc` (stdin is a socket), but almost
every distribution's `.bashrc` opens with an early return when not interactive,
so exports below that line never happen. Put this at the **very top** of
`~/.bashrc`, above the guard:

```bash
export PATH=/path/to/agent-bridge/bin:$HOME/.local/bin:$HOME/.opencode/bin:$PATH
```

- **Above any `module load`, too.** A module line that fails when a cluster
  retires a version can abort the rest of the file and leave the exports
  unreached.
- `~/.bash_profile` sourcing `~/.bashrc` is the usual arrangement and is what
  makes the same `PATH` true for interactive logins. Check that it does.
- `~/.local/bin` and `~/.opencode/bin` are where `claude` and `opencode` install
  themselves. `ab-serve` prepends them for the gateway anyway; having them here
  means your own shell agrees with what the gateway will see.

Settle both questions in one command, from a laptop or with `ssh localhost`:

```bash
ssh host 'command -v ab-serve; command -v claude; echo "PATH=$PATH"'
```

If `ab-serve` prints no path there, stop and fix `.bashrc` first. Every later
symptom will look like something else.

## 2. One command: `ab-serve`

```bash
bin/ab-serve --no-park          # bootstrap, start it detached, exit
```

Each step is skipped when it has already been done:

| | | Where |
|---|---|---|
| seeds `config.toml` from `config.example.toml` if absent | an edited one is left alone | `serve.py: seed_config` |
| creates `.venv` and installs `requirements.txt` when `agent-bridge` is not on `PATH` and FastAPI cannot be imported — `uv` if present, else `venv` + `pip` | idempotent; one import check per later run | `serve.py: bootstrap_venv` |
| prepends `~/.local/bin` and `~/.opencode/bin` to the `PATH` the **gateway** inherits, and names any configured agent still not findable | `--path DIR` to change | `serve.py: child_path` |
| probes `/health`, starts the gateway if nothing is there, and refuses to touch a port held by something else | it never kills anything | `serve.py: ensure_serving` |
| starts it in a session of its own, so it outlives this shell and any ssh | | `serve.py: _start` |

Expected output on a fresh clone:

```
ab-serve: no config.toml; copied config.example.toml -- edit allowed_dirs and agents
ab-serve: installing the gateway's dependencies into …/.venv (first run, using uv; this takes a minute)
ab-serve: dependencies installed; using …/.venv/bin/python
ab-serve: warning: not on PATH: opencode (opencode)
ab-serve: nothing on 8787; starting: …
ab-serve: serving on 8787 (agent-bridge 0.3.0), pid 1615, log …/gateway.log
```

**Read the `warning:` line.** It means a configured agent's binary cannot be
found, and a gateway with that warning starts fine and fails *every* job with a
"not found". Fix `PATH`, or set that agent's `bin` to an absolute path.

`--no-park` starts it and exits — the form to run by hand. Plain `ab-serve`
holds the connection open while the gateway serves and restarts it up to three
times if it dies; that is the form a caller's ssh line uses.

**On a shared login node, choose a port nobody else has** (`[server] port`).
`ab-serve` refuses a port that answers TCP but not `/health` rather than fight
over it, so a collision is a message, not a mystery.

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

- **`allowed_dirs` gates where jobs may run.** A submit whose `--cwd` falls
  outside it is refused (`cwd … is not under any allowed_dirs`), and the same
  list sandboxes `ab upload`/`ab download`. Name the project trees and the home
  actually in use. It is not an OS-level jail — with
  `permission_mode = "bypassPermissions"` an agent can still write elsewhere —
  so read it as "where a session may be *placed*, and what may be *transferred*".
- **`default_cwd` must not be this checkout.** It is what a caller inherits when
  they forget `--cwd`, and a delegate that starts in the tool that dispatched it
  greps the wrong repo and can commit into it.
- **`concurrency`** can be high (16 in the example) because a job `waiting` for
  its report holds a process, not a queue slot.

Two more worth a look on a cluster: `[notes] path` — the operator notes served
by `ab info`, which is where you write the account to charge and the partition
that has the GPUs — and `report_wait_sec`, the grace window for a report.

The gateway does not reload config, so restart it after editing:

```bash
PID=$(lsof -tiTCP:8787 -sTCP:LISTEN | head -1)   # or read the pid from ab-serve's line
kill "$PID"; bin/ab-serve --no-park
```

Never `pkill -f agent-bridge`: a pattern kill matches your own command line and
anything else that mentions it. Stop by pid, found from the port.

## 4. Prove it serves, on this host

Three checks, in this order, all local — no tunnel involved, so a failure is
this host's:

```bash
curl -s localhost:8787/health                       # {"ok":true,"version":"0.3.0"}
curl -s -H "Authorization: Bearer $(cat .token)" localhost:8787/v1/agents
tail -20 gateway.log                                # if either was quiet
```

`/health` needs no token — that is why `ab-serve` can probe without reading one.
Everything under `/v1/` needs the bearer, so the second call is what proves the
token file and the auth path agree.

What `/v1/agents` does **not** tell you is whether those agents can actually
run: it reflects `config.toml`, so an agent whose binary is missing is listed
exactly like one that works, and jobs for it are accepted and then fail. The
`warning: not on PATH` line from step 2 and `command -v claude` are the only
checks for that. What it does confirm cheaply is each agent's `default_cwd` —
worth a glance here, since this is where you find out it is still the checkout.

**The real acceptance test is one job.** `ab` is already on `PATH` from step 1,
so the host can be its own client for a single run:

```bash
mkdir -p ~/.config/agent-bridge
cat > ~/.config/agent-bridge/gateways.json <<JSON
{"gateways": {"local": {"base_url": "http://localhost:8787",
                        "token_file": "$PWD/.token"}}}
JSON
echo 'Print the hostname and the current directory. Then stop.' > /tmp/smoke.md
ab run --cwd <a dir inside allowed_dirs> -F /tmp/smoke.md --timeout 120
```

That exercises `bin`, the gateway's `PATH`, `allowed_dirs`, `default_cwd` and the
job dir together — the five things that are each individually fine in a
configuration that still cannot run anything. It is a self-test: the caller's own
config is not this skill's business.

## 5. Hand over two facts

A caller needs the port and the token, and nothing else from here:

```bash
cat .token                                              # simplest
.venv/bin/python -m gateway --config config.toml --print-token   # equivalently
```

`[auth] token = ""` means the gateway generated one on first start and wrote it
to `.token` in the data dir — mode 0600, in `config.toml`'s own directory unless
`AGENT_BRIDGE_DATA_DIR` says otherwise.

- **Never put the token in a job environment or an `--export`.** Job
  environments are readable from scheduler metadata by other users on a shared
  node.
- Send it over something that is not chat if you can. It is gitignored
  (`.token`, `config.toml`); keep it that way.
- The caller will point an ssh forward at your port and can start `ab-serve`
  itself on connect — which is the reason step 1's `.bashrc` work matters.

## 6. Install the worker skill for the delegate

The agent running on this host should carry `agent-bridge-worker`, or it will
not know that its report is what finishes a job:

```bash
bin/install-skills agent-bridge-worker    # into ~/.claude/skills and ~/.agents/skills
```

No gateway restart needed; it applies to the next job.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `ab-serve: command not found` over ssh but fine when logged in | the export sits below `.bashrc`'s interactivity guard, or after a failing `module load` |
| gateway healthy, every job fails `claude: not found` | the gateway's `PATH` — the `warning: not on PATH` line said so at startup; set `bin` to an absolute path |
| `cwd … is not under any allowed_dirs` | a `--cwd` (or `default_cwd`) outside the list. `/tmp` is usually outside it; the upload store under `$TMPDIR` is the one exception, and only for transfers |
| `port 8787 is held by something that is not answering /health` | somebody else's process, or a gateway still booting. Choose another port; do not kill it |
| `gateway did not answer on 8787 within 60s` | a config or dependency error — `ab-serve` prints the tail of `gateway.log` right there |
| `bootstrap failed` with pip output | no network on the login node, or a proxy in the way. Install the three dependencies into `.venv` by hand, then re-run |
| jobs run but the caller sees no progress | `$AB_JOB_DIR` writes are not reaching the gateway's data dir — put the data dir on a filesystem the compute nodes share |

## Persistence

The gateway **already survives** an ssh connection dropping: it is started in a
session of its own, and `ab-serve` prints *"leaving the gateway running"* on the
way out. That is deliberate — jobs here outlive the turn that submitted them, so
a closed laptop must not take them with it.

It does **not** survive a node reboot. For that, use the shipped user unit:

```bash
loginctl enable-linger $USER            # the step people forget
mkdir -p ~/.config/systemd/user && cp systemd/agent-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now agent-bridge
```

It points at `.venv/bin/python -m gateway` and sets the same `PATH` `ab-serve`
prepends. The two compose: with systemd owning the lifetime, a caller's
`ab-serve` finds it already serving, says so, and starts nothing.

## Repo

`config.example.toml` (every setting, with the reasoning) · `README.md` (install
and `ab-serve`) · `docs/design/22` and `docs/design/24` (why `ab-serve` behaves
as it does) · `skills/agent-bridge-worker` (what the delegate needs to know).
