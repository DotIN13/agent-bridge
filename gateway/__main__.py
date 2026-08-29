"""Gateway process entry point: ``agent-bridge`` / ``python -m gateway``."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import load
from .server import Gateway


def find_ab_notify() -> str | None:
    """Resolve the reporter, preferring PATH and falling back to the checkout."""
    found = shutil.which("ab-notify")
    if found:
        return found
    local = Path(__file__).resolve().parent.parent / "bin" / "ab-notify"
    return str(local) if local.is_file() else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-bridge")
    parser.add_argument("--config", "-c",
                        help="config.toml path; explicit missing paths fail")
    parser.add_argument("--print-token", action="store_true",
                        help="print the resolved bearer token and exit")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    configured = args.config or os.environ.get("AGENT_BRIDGE_CONFIG")
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            print(f"agent-bridge: config not found: {path}", file=sys.stderr)
            return 2
        config_path = str(path)
    else:
        default = Path("config.toml")
        config_path = str(default) if default.exists() else None
    cfg = load(config_path)
    os.environ.setdefault("AGENT_BRIDGE_DATA_DIR", cfg.data_dir)

    if args.print_token:
        print(cfg.token)
        return 0

    # Once this refused to start without `ab-notify`, because a job submitted
    # with `expect_report` parked until the reporter closed it and a missing
    # binary meant jobs that could never finish. Reporting is a directory now:
    # every job is handed `$AB_JOB_DIR` and closes itself with `echo`, so a
    # missing reporter costs an old sbatch script its progress calls, not a
    # gateway that hands out unfinishable work. Worth saying, not worth
    # refusing to boot over.
    notifier = find_ab_notify()
    print(f"reporter: {notifier or 'not found (jobs report through $AB_JOB_DIR)'}",
          file=sys.stderr, flush=True)

    token_file = Path(cfg.data_dir) / ".token"
    where = str(token_file) if token_file.exists() else "configured auth token"
    print(f"authentication ready ({where}); use --print-token to reveal it",
          file=sys.stderr, flush=True)
    Gateway(cfg).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
