"""`ab-bridge`: the local daemon that holds the ssh forwards open.

    ab-bridge                      # loopback, a fresh token, prints the url
    ab-bridge --open               # ...and opens a browser at it
    ab-bridge --up midway5         # bring one tunnel up on start
    ab-bridge --print-token        # for scripting against the API

This is the laptop half of agent-bridge. The gateway runs on the cluster; this
runs here, and the only reason it exists is that a tunnelled gateway's
`base_url` is a local port that answers only while an ssh forward is up.
"""
from __future__ import annotations

import argparse
import ipaddress
import sys
import webbrowser

from client._version import __version__
from .config import ConfigError, DEFAULT_PROGRAMS, Store
from .server import create_app, resolve_token
from .supervisor import Supervisor

DEFAULT_PORT = 8765


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ab-bridge",
        description="keep agent-bridge ssh forwards up, and manage them from a "
                    "browser")
    parser.add_argument("--config", "-c",
                        help="gateways.json path (default: the same file `ab` "
                             "reads)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"bind port (default {DEFAULT_PORT})")
    parser.add_argument("--token",
                        help="bearer token; default $AGENT_BRIDGE_UI_TOKEN, "
                             "else one generated per run")
    parser.add_argument("--print-token", action="store_true",
                        help="print the resolved token and exit")
    parser.add_argument("--open", action="store_true",
                        help="open a browser at the tokened url")
    parser.add_argument("--up", action="append", metavar="NAME", default=[],
                        help="bring this tunnel up on start (repeatable); "
                             "`autostart` in the config does the same")
    parser.add_argument("--allow-program", action="append", default=[],
                        metavar="NAME",
                        help="permit another program as an ssh command's first "
                             f"word (default: {', '.join(DEFAULT_PROGRAMS)})")
    parser.add_argument("--dangerously-bind-all", action="store_true",
                        help="allow a non-loopback --host. This UI can rewrite "
                             "and run the ssh command on this machine, so "
                             "exposing it to a network hands that to the "
                             "network. Do not.")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = resolve_token(args.token)
    if args.print_token:
        print(token)
        return 0

    if not _is_loopback(args.host) and not args.dangerously_bind_all:
        print(f"ab-bridge: refusing to bind {args.host}. This daemon runs an "
              f"ssh command that its own web UI can edit, so a non-loopback "
              f"bind publishes command execution on this machine. Use an SSH "
              f"tunnel to reach it, or --dangerously-bind-all if you have read "
              f"that sentence twice.", file=sys.stderr)
        return 2

    programs = tuple(DEFAULT_PROGRAMS) + tuple(args.allow_program)
    try:
        store = Store.load(args.config, programs)
    except ConfigError as exc:
        print(f"ab-bridge: {exc}", file=sys.stderr)
        return 2

    sup = Supervisor(store)
    for name in args.up:
        try:
            sup.up(name)
        except Exception as exc:
            print(f"ab-bridge: --up {name}: {exc}", file=sys.stderr)
    sup.start()

    url = f"http://{args.host or '127.0.0.1'}:{args.port}/"
    print(f"config:  {store.path}"
          f"{'' if store.writable else '  (read-only: TOML)'}", file=sys.stderr)
    tunnels = [entry.name for entry in store.entries() if entry.tunnelled]
    listed = ", ".join(tunnels) or "none configured (add an 'ssh' key)"
    print(f"tunnels: {listed}", file=sys.stderr)
    # The token rides in the fragment: browsers do not send a fragment to the
    # server, so it stays out of access logs and caches on the way in.
    print(f"open:    {url}#token={token}", file=sys.stderr, flush=True)
    if args.open:
        webbrowser.open(f"{url}#token={token}")

    import uvicorn
    app = create_app(sup, token)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        # Ours to clean up: an orphaned `ssh -N` keeps the local port bound and
        # the next run cannot start.
        sup.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
