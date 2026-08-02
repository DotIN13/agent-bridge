"""Entry point: python -m gateway [--config config.toml]"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import load
from .server import Gateway


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent-bridge")
    default_cfg = os.environ.get("AGENT_BRIDGE_CONFIG", "config.toml")
    ap.add_argument("--config", "-c", default=default_cfg,
                    help="path to config.toml (default: ./config.toml)")
    ap.add_argument("--print-token", action="store_true",
                    help="print the resolved auth token and exit")
    args = ap.parse_args(argv)

    cfg_path = args.config if Path(args.config).exists() else None
    if args.config and cfg_path is None:
        print(f"warning: config {args.config} not found; using defaults",
              file=sys.stderr)
    cfg = load(cfg_path)

    # Make data dir available to adapters (task files, per-job dirs).
    os.environ.setdefault("AGENT_BRIDGE_DATA_DIR", cfg.data_dir)

    if args.print_token:
        print(cfg.token)
        return 0

    token_file = Path(cfg.data_dir) / ".token"
    where = f"persisted at {token_file}" if token_file.exists() else "from config.toml"
    print(f"token: {cfg.token}  ({where})", flush=True)
    Gateway(cfg).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
