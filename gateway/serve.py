"""``ab-serve`` -- make sure the gateway is up, then hold the ssh open.

The command the dashboard runs on the far side once a forward is up. Its gateway
entry asks for it with ``"exec": true``, which becomes::

    PATH="${AB_PATH:+$AB_PATH:}$PATH"; exec ab-serve

so `$AB_PATH` -- agent-bridge's script directory on this machine -- is searched
first when it is set, and the plain `PATH` answers when it is not. The `ssh` line
may also end in this command directly, which is the same thing said by hand.

There is one shipped with agent-bridge rather than one written per gateway
because every version of this script anybody writes has to answer the same four
questions, and getting any of them wrong is invisible until it costs a day:

1. **Is it already serving?** Then start nothing. A second gateway on the same
   port is not a spare -- it is the first one's port already taken, and a
   traceback in a log nobody is reading.
2. **Is the port held by something that is not us?** Then stop, and do not touch
   it. Killing a process this script did not start is not a decision a launcher
   gets to make; somebody else's notebook on 8787 is somebody else's.
3. **Did it fail to start?** Then exit non-zero, with the reason. That drops the
   ssh, which turns the dashboard's row red -- so the failure arrives where
   somebody is looking, instead of a green tunnel in front of a dead gateway.
4. **What happens when the connection drops?** The gateway keeps running.

The last one is the one worth arguing about. This script *holds* the connection
open but does not own the gateway: it starts it in a session of its own and
parks. Closing a laptop, losing wifi, or `ssh` timing out then costs the tunnel
and nothing else. Jobs here outlive the turn that submitted them by design -- a
`waiting` job is an agent still alive on the far side with an sbatch to report --
so tying the gateway's life to a laptop's would throw away exactly the work this
project exists to keep. Anyone who does want the two tied together does not need
this script: `ssh -L … host 'exec agent-bridge'` is that, in one line.

While parked it re-checks health, and restarts a gateway that has gone. After
`--max-restarts` it exits rather than flapping quietly: a gateway that dies four
times in a row is a gateway somebody has to look at.

It also does what `run.sh` used to, because the script that starts the gateway is
the only one anybody reliably runs: in a checkout it seeds `config.toml` from the
example, creates `.venv` and installs `requirements.txt` when the interpreter
cannot import FastAPI, and puts the directories holding the agent binaries on the
`PATH` the gateway inherits. That last one is not cosmetic -- `bin = "claude"` is
resolved from the gateway's `PATH`, and `ssh host cmd` does not get a login
shell's one, so without it a gateway starts, answers `/health`, and fails every
job with a "not found" nobody would connect to their ssh line.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import load

#: Long enough for a slow import on a cold NFS home, short enough that a broken
#: config is not a two-minute silence.
START_TIMEOUT = 60.0
#: How often the parked holder asks whether the gateway is still there.
POLL_SEC = 15.0
#: Consecutive health failures before the gateway counts as gone. One miss is a
#: busy login node, not a dead process.
MISSES = 2
#: Where the agent binaries live when nothing says otherwise. `claude` installs
#: itself into `~/.local/bin` and opencode into `~/.opencode/bin`, and both are
#: on a login shell's `PATH` and missing from the one `ssh host cmd` gets.
#: Non-existent entries are dropped, so this costs nothing where it is wrong.
AGENT_PATH = ("~/.local/bin", "~/.opencode/bin")
#: A cold NFS home installing FastAPI is minutes, not seconds. Bounded anyway:
#: a hung installer should fail the connect rather than hold it forever.
BOOTSTRAP_TIMEOUT = 900.0

_stopping = False


def _stop(signum, _frame) -> None:  # pragma: no cover - signal path
    global _stopping
    _stopping = True


def health(port: int, timeout: float = 3.0) -> dict | None:
    """The gateway's own answer, or ``None`` if there was not one.

    Loopback rather than ``cfg.host``: the gateway may be bound to ``0.0.0.0``
    for compute-node reports, and what matters here is the address the forward
    lands on. `/health` needs no token, which is why this script never has to
    read one.
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            body = json.loads(response.read() or b"{}")
        return body if isinstance(body, dict) and body.get("ok") else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def port_held(port: int, timeout: float = 1.5) -> bool:
    """Does anything accept a connection here?

    Asked only when `/health` said no, and the two together are what separate
    "nothing is running" from "something that is not a gateway has the port".
    """
    with socket.socket() as probe:
        probe.settimeout(timeout)
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def is_checkout(root: Path) -> bool:
    """Does this directory look like a clone rather than an install?

    Both marker files, because either alone is something a wheel or a stray copy
    can plausibly have.
    """
    return all((root / mark).is_file()
               for mark in ("pyproject.toml", "requirements.txt"))


def checkout_root() -> Path | None:
    """The checkout this script lives in, or ``None`` when it was installed.

    Everything below only makes sense in a checkout: a `pip install` already has
    its dependencies, and creating a venv next to somebody's site-packages is
    not a thing to do because a connection arrived.
    """
    root = Path(__file__).resolve().parent.parent
    return root if is_checkout(root) else None


def seed_config(root: Path) -> Path | None:
    """The checkout's `config.toml`, copied from the example when absent.

    Returned whether it was just written or was already there, because the
    second half is the useful part: `ssh host cmd` starts in `$HOME`, so a
    relative `config.toml` finds nothing, and the checkout's own is the file
    somebody edited. `run.sh` got this by `cd`-ing to the repo first.
    """
    target, example = root / "config.toml", root / "config.example.toml"
    if target.is_file():
        return target
    if not example.is_file():
        return None
    try:
        shutil.copyfile(example, target)
    except OSError as exc:
        say(f"could not seed {target}: {exc}")
        return None
    say(f"no config.toml; copied {example.name} -- edit allowed_dirs and agents")
    return target


def venv_python(root: Path) -> Path:
    """The interpreter inside the checkout's `.venv`, whether or not it exists."""
    relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return root / ".venv" / relative


def deps_ready(python: str | os.PathLike[str] | None = None) -> bool:
    """Can that interpreter import what the gateway needs?

    In-process for our own -- a subprocess to ask about ourselves is a spawn on
    every connect for an answer already in memory -- and by subprocess for any
    other, which is the only way to ask.
    """
    wanted = ("fastapi", "uvicorn")
    if python is None:
        try:
            return all(importlib.util.find_spec(name) for name in wanted)
        except (ImportError, ValueError):
            return False
    try:
        done = subprocess.run(
            [str(python), "-c", f"import {', '.join(wanted)}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def bootstrap_venv(root: Path, *, timeout: float = BOOTSTRAP_TIMEOUT) -> Path | None:
    """`.venv` with the gateway's dependencies in it, created if it is not there.

    This was `run.sh`'s first-run block. It is here because the launcher is the
    script somebody actually runs, and because the failure it prevents reads
    like a bug in agent-bridge rather than a missing step: a fresh clone that
    connects fine and dies with `ModuleNotFoundError: fastapi` in a log.

    Idempotent -- an interpreter that can already import FastAPI is returned
    untouched, so this costs one `import` check on every connect after the
    first. `uv` when there is one, because a cluster home tends to have it and
    it is minutes faster on NFS; `venv` and `pip` otherwise.
    """
    python = venv_python(root)
    if python.is_file() and deps_ready(python):
        return python

    uv = shutil.which("uv")
    venv_dir, requirements = root / ".venv", root / "requirements.txt"
    if uv:
        # `--python sys.executable` rather than a version string: this
        # interpreter is running `ab-serve`, so it already satisfies the floor,
        # and naming a version uv cannot find is a needless way to fail.
        steps = [[uv, "venv", str(venv_dir), "--python", sys.executable],
                 [uv, "pip", "install", "--python", str(python),
                  "-r", str(requirements)]]
    else:
        steps = [[sys.executable, "-m", "venv", str(venv_dir)],
                 [str(python), "-m", "pip", "install", "--quiet",
                  "-r", str(requirements)]]

    say(f"installing the gateway's dependencies into {venv_dir} "
        f"(first run, using {'uv' if uv else 'pip'}; this takes a minute)")
    for step in steps:
        try:
            done = subprocess.run(step, capture_output=True, text=True,
                                  timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            say(f"bootstrap failed: {exc}")
            return None
        if done.returncode != 0:
            say(f"bootstrap failed: {' '.join(step)}")
            for line in (done.stderr or done.stdout or "").strip().splitlines()[-12:]:
                say(f"  {line}")
            return None
    if not deps_ready(python):
        say(f"bootstrap ran but {python} still cannot import fastapi")
        return None
    say(f"dependencies installed; using {python}")
    return python


def child_path(extra: tuple[str, ...] | list[str]) -> str:
    """The `PATH` the gateway should inherit, with the agent binaries on it.

    The gateway resolves `bin = "claude"` through this, and `ssh host cmd` runs
    a non-interactive shell whose `PATH` is missing what a login shell exports
    -- nearly every distribution's `.bashrc` returns early before those lines.
    So without this the gateway starts, answers `/health`, and every job fails
    with a "not found" that has nothing visibly to do with the ssh line.

    Only directories that exist and are not already there are added, so a
    default naming somebody else's layout costs nothing.
    """
    current = os.environ.get("PATH", "")
    already = current.split(os.pathsep)
    add = [str(Path(entry).expanduser()) for entry in extra]
    keep = [entry for entry in add
            if os.path.isdir(entry) and entry not in already]
    return os.pathsep.join([*keep, current]) if keep else current


def missing_agents(cfg, path: str) -> list[str]:
    """Configured agents whose binary is not findable on that `PATH`.

    A line rather than a refusal: one stale entry beside three working ones
    should not stop a gateway, and the operator wants to know which is which
    before a job tells them. Named here because this is the moment the `PATH`
    is decided, and therefore the moment the answer is knowable.
    """
    out: list[str] = []
    for name, agent in sorted(getattr(cfg, "agents", {}).items()):
        binary = getattr(agent, "bin", "") or ""
        if not binary:
            continue
        if os.path.isabs(binary):
            if not os.access(binary, os.X_OK):
                out.append(f"{name} ({binary})")
        elif not shutil.which(binary, path=path):
            out.append(f"{name} ({binary})")
    return out


def gateway_command(config_path: str | None, override: str | None,
                    interpreter: str | os.PathLike[str] | None = None) -> list[str]:
    """How to start the gateway from here.

    In order: an explicit `--agent-bridge`, `agent-bridge` on `PATH`, the
    interpreter the bootstrap prepared, and this one's own ``-m gateway``. The
    dashboard's default command prepends `$AB_PATH` to `PATH` before exec'ing
    this script, so a gateway installed beside `ab-serve` is found here without
    being configured twice.
    """
    if override:
        argv = [override]
    else:
        found = shutil.which("agent-bridge")
        if found:
            argv = [found]
        else:
            argv = [str(interpreter or sys.executable), "-m", "gateway"]
    if config_path:
        argv += ["--config", config_path]
    return argv


def say(line: str) -> None:
    """Anything printed here travels back up the ssh into the dashboard's console.

    Which is the point: `ab-serve`'s stdout is the one log a laptop can read
    without another connection, so the reason a gateway would not start arrives
    beside the row that is red because of it.
    """
    print(f"ab-serve: {line}", flush=True)


def _start(argv: list[str], log_path: Path, *,
           env: dict[str, str] | None = None,
           cwd: str | os.PathLike[str] | None = None) -> tuple[subprocess.Popen, int]:
    """Start the gateway in a session of its own, and remember where its log ends.

    `start_new_session` is what makes it survive us: without it the gateway is
    in this ssh session's process group and takes the `SIGHUP` that arrives when
    the connection drops. The log offset is taken *before* the spawn so a
    failure prints this attempt's output and not an hour of somebody else's.

    `env` carries the `PATH` the agents are found on, and `cwd` is the checkout
    when the command is `-m gateway`, since that form imports from the working
    directory and `ssh host cmd` starts in `$HOME`.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    offset = log_path.stat().st_size if log_path.exists() else 0
    handle = open(log_path, "ab", buffering=0)
    try:
        child = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
            start_new_session=True, env=env, cwd=cwd)
    finally:
        # Ours to close either way: the child has its own descriptor now, and
        # holding this one keeps the file open for as long as the holder parks.
        handle.close()
    return child, offset


def _tail(log_path: Path, offset: int, limit: int = 40) -> list[str]:
    try:
        with open(log_path, "rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def _wait_for_health(port: int, child: subprocess.Popen, timeout: float) -> dict | None:
    """Poll until it answers, it dies, or we run out of patience."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not _stopping:
        answer = health(port, timeout=2.0)
        if answer:
            return answer
        if child.poll() is not None:
            return None
        time.sleep(0.5)
    return health(port, timeout=2.0)


def ensure_serving(port: int, argv: list[str], log_path: Path,
                   start_timeout: float, *, env: dict[str, str] | None = None,
                   cwd: str | os.PathLike[str] | None = None) -> bool:
    """Get to a serving gateway, or say why not. Never kills anything."""
    answer = health(port)
    if answer:
        say(f"already serving on {port} (agent-bridge {answer.get('version', '?')})")
        return True

    if port_held(port):
        # Something answers TCP and does not answer `/health`. It might be an
        # agent-bridge still booting, and it might be a notebook. Neither is
        # ours to kill, and guessing wrong is worse than stopping.
        say(f"port {port} is held by something that is not answering /health; "
            "not touching it")
        return False

    say(f"nothing on {port}; starting: {' '.join(argv)}")
    try:
        child, offset = _start(argv, log_path, env=env, cwd=cwd)
    except OSError as exc:
        say(f"could not run {argv[0]}: {exc}")
        return False

    answer = _wait_for_health(port, child, start_timeout)
    if answer:
        say(f"serving on {port} (agent-bridge {answer.get('version', '?')}), "
            f"pid {child.pid}, log {log_path}")
        return True

    say(f"gateway did not answer on {port} within {start_timeout:.0f}s")
    for line in _tail(log_path, offset):
        say(f"  {line}")
    if child.poll() is None:
        # It is alive and not serving: leave it be and let somebody read the log.
        say(f"the process is still running as pid {child.pid}")
    return False


def park(port: int, argv: list[str], log_path: Path, *, poll: float,
         start_timeout: float, max_restarts: int,
         env: dict[str, str] | None = None,
         cwd: str | os.PathLike[str] | None = None) -> int:
    """Hold the connection open for as long as the gateway is there."""
    misses = 0
    restarts = 0
    while not _stopping:
        # Sliced, so a SIGTERM from a dropped connection is noticed in a moment
        # rather than at the end of the interval. PEP 475 resumes an interrupted
        # sleep, so a handler that only sets a flag is not enough on its own.
        waited = 0.0
        while waited < poll and not _stopping:
            time.sleep(0.5)
            waited += 0.5
        if _stopping:
            break

        if health(port):
            misses = 0
            continue

        misses += 1
        if misses < MISSES:
            continue

        if restarts >= max_restarts:
            say(f"gateway has gone and {max_restarts} restarts are spent; giving up")
            return 1
        restarts += 1
        misses = 0
        say(f"gateway stopped answering on {port}; restart {restarts}/{max_restarts}")
        if not ensure_serving(port, argv, log_path, start_timeout,
                              env=env, cwd=cwd):
            return 1

    say("connection closed; leaving the gateway running")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ab-serve",
        description="Ensure the gateway is serving, then hold this connection "
                    "open. Started over ssh by agent-bridge's dashboard.",
        epilog="In a checkout it first seeds config.toml from the example and "
               "installs the gateway's dependencies into .venv if they are "
               "missing. The gateway is then started in a session of its own "
               "and is left running when this exits: jobs here outlive the "
               "turn that submitted them. For a gateway tied to the "
               "connection, run `agent-bridge` directly instead.")
    parser.add_argument("--config", "-c",
                        help="config.toml path; the same resolution agent-bridge uses")
    parser.add_argument("--agent-bridge",
                        help="the gateway binary to start (default: PATH, then -m gateway)")
    parser.add_argument("--start-timeout", type=float, default=START_TIMEOUT,
                        metavar="SEC", help=f"wait for /health (default {START_TIMEOUT:.0f})")
    parser.add_argument("--interval", type=float, default=POLL_SEC, metavar="SEC",
                        help=f"health check cadence while parked (default {POLL_SEC:.0f})")
    parser.add_argument("--max-restarts", type=int, default=3, metavar="N",
                        help="restarts before giving up and exiting (default 3)")
    parser.add_argument("--path", action="append", metavar="DIR",
                        help="prepend DIR to the PATH the gateway inherits, so "
                             "its agent binaries are found (repeatable; "
                             f"default {', '.join(AGENT_PATH)})")
    parser.add_argument("--no-bootstrap", action="store_true",
                        help="in a checkout, do not seed config.toml or create "
                             ".venv; fail instead if the deps are missing")
    parser.add_argument("--no-park", action="store_true",
                        help="exit as soon as it is serving, instead of holding")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    root = checkout_root()

    configured = args.config or os.environ.get("AGENT_BRIDGE_CONFIG")
    config_path: str | None
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            print(f"ab-serve: config not found: {path}", file=sys.stderr)
            return 2
        config_path = str(path)
    elif Path("config.toml").exists():
        config_path = str(Path("config.toml"))
    elif root is not None and not args.no_bootstrap:
        # `ssh host cmd` starts in `$HOME`, so the relative form above finds
        # nothing and the checkout's own config is what somebody edited.
        seeded = seed_config(root)
        config_path = str(seeded) if seeded else None
    else:
        config_path = None

    try:
        cfg = load(config_path)
    except Exception as exc:  # config errors are the common first failure
        print(f"ab-serve: {exc}", file=sys.stderr)
        return 1

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        # SIGHUP is the one that arrives when the ssh connection goes; it does
        # not exist on Windows, and this script has no business failing there
        # over a signal it will never receive.
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, _stop)

    # Whether anything here can *run* a gateway, before deciding how to start
    # one. An `agent-bridge` on PATH brought its own dependencies with it; this
    # interpreter may not have, which in a checkout is the bootstrap's job.
    interpreter: Path | None = None
    if not args.agent_bridge and not shutil.which("agent-bridge") \
            and not deps_ready():
        if root is None or args.no_bootstrap:
            say("fastapi and uvicorn are missing from "
                f"{sys.executable} and there is no checkout to bootstrap "
                "(pip install -e . , or drop --no-bootstrap)")
            return 1
        interpreter = bootstrap_venv(root)
        if interpreter is None:
            return 1

    port = cfg.port
    log_path = Path(cfg.data_dir) / "gateway.log"
    command = gateway_command(config_path, args.agent_bridge, interpreter)

    child_env = {**os.environ, "PATH": child_path(args.path or list(AGENT_PATH))}
    absent = missing_agents(cfg, child_env["PATH"])
    if absent:
        # Said now rather than discovered per job: this is the one moment the
        # answer is both knowable and in front of somebody.
        say(f"warning: not on PATH: {', '.join(absent)}")

    # `-m gateway` imports from the working directory; a console script does not
    # care, and inheriting `$HOME` for it would be the surprising choice.
    cwd = str(root) if root is not None and "-m" in command else None

    if not ensure_serving(port, command, log_path, args.start_timeout,
                          env=child_env, cwd=cwd):
        return 1
    if args.no_park:
        return 0
    return park(port, command, log_path, poll=args.interval,
                start_timeout=args.start_timeout,
                max_restarts=args.max_restarts, env=child_env, cwd=cwd)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
