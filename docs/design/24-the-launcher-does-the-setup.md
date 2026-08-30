# 24 — The launcher does the setup: `run.sh` folded into `ab-serve`

`run.sh` was the script that made a checkout runnable: create `.venv`, install
`requirements.txt`, copy `config.example.toml`, put `~/.local/bin` on `PATH`, then
`exec python -m gateway` in a tmux window. `ab-serve` (design/22) was the script
that made a *connection* start a gateway: probe the port, start what is missing,
hold the ssh, exit loudly. Two launchers, one gateway, and the seam between them
was where the failures lived.

They are one script now. `ab-serve` does the setup first and then does what it
already did.

## Why merge rather than keep two

The setup has to happen wherever the gateway is started, and there are two places
that start one: a person on the login node, and a laptop's ssh line. A step that
only one of them performs is a step that is *sometimes* performed, which is worse
than either — the machine works until the day it is started the other way.

Three concrete versions of that, all reachable before this change:

| | What happened |
|---|---|
| Fresh clone, dashboard connects | `ab-serve` found no `agent-bridge`, fell back to `-m gateway`, and the gateway died with `ModuleNotFoundError: fastapi` in a log nobody had a path to. The fix was to ssh in and run `run.sh` once — knowable, and nothing said so |
| `run.sh` in tmux, then a dashboard connect | Two gateways attempted on one port. `ab-serve` no-ops correctly, but only because it probes; `run.sh` in a second window is a bind failure |
| Dashboard connect, no `run.sh` ever | The one below, which is the real reason this was worth doing |

## The `PATH` bug this was hiding

`run.sh` had a line whose comment explained it: *"claude lives in
`~/.local/bin`, which non-login shells may miss."* `config.toml` says
`bin = "claude"`, a bare name the adapter hands to `Popen`, so it is resolved
from the environment the **gateway** inherited. `ssh host cmd` runs a
non-interactive shell, and nearly every distribution's `.bashrc` returns early
when not interactive, so the exports below that line never run.

Which means a gateway started through `ab-serve` over ssh had a `PATH` without
`~/.local/bin` in it, and the failure was: connect succeeds, tunnel green,
`/health` fine, every job fails with `claude: not found`. Nothing about that
points at the ssh line, and the same gateway started by `run.sh` on the same host
worked.

This is the `$AB_PATH` trap from design/22 one layer down — and it was written
*into* design/22 without noticing that the layer below had the same problem. So
`ab-serve` now prepends `~/.local/bin` and `~/.opencode/bin` (existence-filtered,
`--path DIR` to change) to the `PATH` it hands the gateway, and names any
configured agent that still cannot be found. A line at connect time, in front of
the person connecting, instead of a discovery per job.

Named rather than refused, deliberately: one stale agent entry beside two working
ones should not stop a gateway, and the operator wants the list either way.

## Installing dependencies from a connect handler

This is the part that deserves an argument, because a script that runs because a
socket opened should not casually spend two minutes on the network.

It is bounded by every condition that makes it safe to be surprising:

- **Only in a checkout.** `is_checkout()` wants `pyproject.toml` *and*
  `requirements.txt` next to the package. A `pip install` already has its
  dependencies, and writing a venv beside somebody's site-packages because a
  connection arrived is not a thing to do.
- **Only when needed.** The gate is `agent-bridge` not on `PATH` *and* this
  interpreter cannot `import fastapi`. After the first run it is one in-process
  `find_spec` per connect.
- **Only bootstrapping.** `uv venv` + `uv pip install -r requirements.txt`, or
  `venv` + `pip` when there is no `uv`. No `git pull`, no `--upgrade`: this makes
  a clone runnable, it does not manage a machine.
- **Loudly.** It says which tool it is using before it starts, and prints the last
  twelve lines of a failure. `--no-bootstrap` turns it off, and then a missing
  dependency is a message rather than an install.

`uv` first because a cluster home tends to have it and it is minutes faster on
NFS. `--python sys.executable` rather than `--python 3.11`, since the interpreter
running `ab-serve` already satisfies the floor and naming a version uv cannot find
is a needless way to fail.

## Two smaller things that came with it

**The config is found from `$HOME`.** `ssh host cmd` starts there, so the old
`./config.toml` lookup found nothing and the gateway ran on defaults — a
different port from the one somebody configured, which reads as "the tunnel is
broken". Resolution is now explicit `--config`, then `./config.toml`, then the
checkout's own, seeded if absent. `run.sh` got this for free by `cd`-ing to the
repo first; it was never a decision, just a side effect nobody had to name.

**`-m gateway` gets the checkout as its cwd.** That form imports from the working
directory, so a fallback start from `$HOME` could not find the package. The
console-script form doesn't care and is left with the inherited cwd, because
changing it for that case would be a surprise with no cause.

## What is no longer true

The tmux dance is gone. `run.sh`'s header told you to `ssh`, `tmux new -s gw`,
`cd`, `./run.sh` — four steps and a session to keep alive, because the gateway
died with its shell. `ab-serve` starts it with `start_new_session=True`, so
`bin/ab-serve --no-park` bootstraps, starts a detached gateway and exits. Nothing
has to stay logged in.

`docs/todo/12` still says tmux is not supervision, which is true and now differently
so: what starts a cluster gateway is a script with a restart budget, not a window.

## Tests

Eight, each naming a way this specifically could be got wrong rather than
re-testing that a gateway starts (design/22 covers that):

- one marker file is not a checkout, and this repo is one;
- the example is copied once and an edited config survives the next connect;
- a venv that can already import FastAPI costs exactly one subprocess — the
  import check — and no installer runs;
- a failed bootstrap names the command *and* shows pip's own last lines, since
  "bootstrap failed" alone sends somebody to the wrong repository;
- the bootstrapped interpreter starts the gateway, and an `agent-bridge` on
  `PATH` still outranks it because it brought its own dependencies;
- a prepended directory wins, a missing one is dropped, one already present is
  not duplicated;
- an unfindable agent is named and an unset `bin` is not;
- and one through `main()` for the wiring, because every piece above can be right
  while the environment reaching the child is not.
