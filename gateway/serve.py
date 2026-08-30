"""``ab-serve`` -- make sure the gateway is up, then hold the ssh open.

The command the dashboard's `ssh` line runs on the far side::

    ssh -L 8787:localhost:8787 midway5 '~/.local/bin/ab-serve'

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
"""
from __future__ import annotations

import argparse
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


def gateway_command(config_path: str | None, override: str | None) -> list[str]:
    """How to start the gateway from here.

    `agent-bridge` on PATH when there is one, and this interpreter's own
    ``-m gateway`` when there is not -- which is the case in a checkout, and the
    same fallback `find_ab_notify` makes for the reporter.
    """
    if override:
        argv = [override]
    else:
        found = shutil.which("agent-bridge")
        argv = [found] if found else [sys.executable, "-m", "gateway"]
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


def _start(argv: list[str], log_path: Path) -> tuple[subprocess.Popen, int]:
    """Start the gateway in a session of its own, and remember where its log ends.

    `start_new_session` is what makes it survive us: without it the gateway is
    in this ssh session's process group and takes the `SIGHUP` that arrives when
    the connection drops. The log offset is taken *before* the spawn so a
    failure prints this attempt's output and not an hour of somebody else's.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    offset = log_path.stat().st_size if log_path.exists() else 0
    handle = open(log_path, "ab", buffering=0)
    try:
        child = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
            start_new_session=True)
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
                   start_timeout: float) -> bool:
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
        child, offset = _start(argv, log_path)
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
         start_timeout: float, max_restarts: int) -> int:
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
        if not ensure_serving(port, argv, log_path, start_timeout):
            return 1

    say("connection closed; leaving the gateway running")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ab-serve",
        description="Ensure the gateway is serving, then hold this connection "
                    "open. Started over ssh by agent-bridge's dashboard.",
        epilog="The gateway is started in a session of its own and is left "
               "running when this exits: jobs here outlive the turn that "
               "submitted them. For a gateway tied to the connection, run "
               "`agent-bridge` directly instead.")
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
    parser.add_argument("--no-park", action="store_true",
                        help="exit as soon as it is serving, instead of holding")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    configured = args.config or os.environ.get("AGENT_BRIDGE_CONFIG")
    config_path: str | None
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            print(f"ab-serve: config not found: {path}", file=sys.stderr)
            return 2
        config_path = str(path)
    else:
        default = Path("config.toml")
        config_path = str(default) if default.exists() else None

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

    port = cfg.port
    log_path = Path(cfg.data_dir) / "gateway.log"
    command = gateway_command(config_path, args.agent_bridge)

    if not ensure_serving(port, command, log_path, args.start_timeout):
        return 1
    if args.no_park:
        return 0
    return park(port, command, log_path, poll=args.interval,
                start_timeout=args.start_timeout, max_restarts=args.max_restarts)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
