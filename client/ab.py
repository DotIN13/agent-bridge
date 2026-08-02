#!/usr/bin/env python3
"""ab — command-line client for agent-bridge gateways.

Runs on your laptop and talks to one or more gateways over their SSH
port-forwards. Pure stdlib. Config: gateways.json (see gateways.example.json).

Examples:
    ab gateways
    ab info
    ab run "run the tests in this repo" --cwd /project/jevans/tzhang3/myrepo
    ab run "profile this data" --upload ./train.csv --stream
    ab submit "long task" ; ab events <id> --follow ; ab job <id>
    ab ls /project/jevans/tzhang3/myrepo/out --glob '*.csv'
    ab download --dir /project/.../out --glob '*.csv' --to ./results

Global flags: --gateway NAME, --config PATH, --json.
Exit code is non-zero if a run fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from abclient import (Client, ConfigError, GatewayError, load_gateways,  # noqa: E402
                      TERMINAL)


def _err(msg: str, code: int = 1):
    print(f"ab: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _out(args, obj, human):
    """Print obj as JSON (if --json) else call human(obj)."""
    if args.json:
        print(json.dumps(obj, indent=2))
    else:
        human(obj)


def _client(args) -> Client:
    gws = load_gateways(args.config)
    return gws.client(args.gateway)


# -- command handlers -----------------------------------------------------
def cmd_gateways(args):
    gws = load_gateways(args.config)
    data = {"gateways": gws.summary(), "default": gws.default}
    def human(d):
        for g in d["gateways"]:
            mark = "*" if g["default"] else " "
            tok = "ok" if g["has_token"] else "NO TOKEN"
            print(f" {mark} {g['name']:16} {g['base_url']:32} [{tok}]")
    _out(args, data, human)


def cmd_info(args):
    d = _client(args).info(refresh=args.refresh)
    _out(args, d, lambda x: print(x.get("summary") or json.dumps(x, indent=2)))


def cmd_models(args):
    """Models the gateway advertises, or one complexity tier resolved to an id.

    The catalog is the gateway's ([[agents.<name>.models]] in its config.toml),
    so what you see is what that agent will actually accept for `--model`.
    """
    d = _client(args).models(agent=args.agent)

    if args.pick:
        model_id = d.get("tiers", {}).get(args.pick)
        if not model_id:
            known = ", ".join(d.get("tiers", {})) or "none"
            _err(f"gateway advertises no '{args.pick}' model (has: {known})")
        chosen = next((m for m in d["models"] if m["id"] == model_id), {"id": model_id})
        _out(args, chosen, lambda x: print(x["id"]))
        return

    def human(x):
        if not x.get("models"):
            print(f"agent '{x['agent']}' advertises no models; --model accepts "
                  f"any id\nthe agent itself supports. Configure them under "
                  f"[[agents.{x['agent']}.models]].")
            return
        print(f"Models offered by agent '{x['agent']}' — pass the id to --model\n")
        print(f"  {'TIER':<9} {'--model':<18} {'CTX':>5} {'$/MTOK IN/OUT':>15}")
        for m in x["models"]:
            price = (f"{m['input_per_mtok']:.2f} / {m['output_per_mtok']:.2f}"
                     if m.get("input_per_mtok") is not None else "")
            print(f"  {m.get('tier',''):<9} {m['id']:<18} "
                  f"{m.get('context',''):>5} {price:>15}")
            blurb = (m.get("use", "") +
                     (f" ({m['note']})" if m.get("note") else "")).strip()
            if blurb:
                print(textwrap.fill(blurb, width=78, initial_indent=" " * 12,
                                    subsequent_indent=" " * 12))
        if x.get("default"):
            print(f"\nThis agent's default when --model is omitted: {x['default']}")
        print("\nPick by task complexity — prints one id, for scripting:")
        print(f"  ab models --pick {next(iter(x.get('tiers', {'hard': 1})))}")
        print("  ab submit -F task.md --model \"$(ab models --pick hard)\"")

    _out(args, d, human)


def cmd_sessions(args):
    d = _client(args).sessions(cwd=args.cwd)
    def human(x):
        for s in x.get("sessions", []):
            print(f" {s['session_id'][:8]}  {s['cwd']:40}  {s.get('title','')[:50]}")
    _out(args, d, human)


def _resolve_prompt(args) -> str:
    """Prompt from --prompt-file, else stdin (positional '-' or piped), else the
    positional arg. Files/stdin avoid shell-quoting and ARG_MAX for long inputs."""
    if getattr(args, "prompt_file", None):
        try:
            text = open(os.path.expanduser(args.prompt_file), encoding="utf-8").read()
        except OSError as e:
            _err(f"cannot read --prompt-file: {e}")
    elif args.prompt == "-" or (args.prompt is None and not sys.stdin.isatty()):
        text = sys.stdin.read()
    elif args.prompt:
        text = args.prompt
    else:
        _err("no prompt: pass it as an argument, via --prompt-file, or on stdin "
             "(e.g. ab run - <<'EOF' ... EOF)")
    if not text.strip():
        _err("prompt is empty")
    return text


def cmd_run(args):
    c = _client(args)
    prompt = _resolve_prompt(args)
    on_event = _stream_printer() if args.stream else None
    job = c.run(prompt, cwd=args.cwd, agent=args.agent, model=args.model,
                session=args.session, permission_mode=args.permission_mode,
                files=args.file, upload=args.upload,
                timeout=args.timeout, on_event=on_event)
    if args.json:
        print(json.dumps(job, indent=2))
    else:
        if args.stream:
            print()  # newline after streamed output
        print(job.get("result") or "")
        meta = (f"[{job.get('status')}] chosen={job.get('chosen_session')} "
                f"forked={job.get('forked_session')} cost=${job.get('cost_usd')}")
        print(meta, file=sys.stderr)
    if job.get("status") != "succeeded":
        raise SystemExit(2)


def cmd_submit(args):
    c = _client(args)
    prompt = _resolve_prompt(args)
    job = c.submit(prompt, cwd=args.cwd, agent=args.agent, model=args.model,
                   session=args.session, permission_mode=args.permission_mode,
                   files=args.file, upload=args.upload)
    _out(args, job, lambda j: print(j["id"]))


def cmd_job(args):
    j = _client(args).get_job(args.id)
    def human(x):
        print(f"status: {x['status']}")
        if x.get("result"):
            print(x["result"])
        if x.get("error"):
            print("error:", x["error"], file=sys.stderr)
    _out(args, j, human)


def cmd_events(args):
    c = _client(args)
    after = args.after
    if not args.follow:
        d = c.events(args.id, after)
        _out(args, d, lambda x: [print(f"{e['seq']:4} {e['type']}: "
                                       f"{_ev_text(e)}") for e in x["events"]])
        return
    import time
    printer = _stream_printer()
    while True:
        d = c.events(args.id, after)
        for e in d["events"]:
            printer(e)
            after = e["seq"]
        if d["terminal"]:
            print(file=sys.stderr)
            break
        time.sleep(1.0)


def cmd_cancel(args):
    d = _client(args).cancel(args.id)
    _out(args, d, lambda x: print(f"{x.get('id')}: canceling (was {x.get('was')})"))


def cmd_upload(args):
    d = _client(args).upload_files(paths=args.paths, dir=args.dir)
    _out(args, d, lambda x: [print(p) for p in x.get("paths", [])])


def cmd_download(args):
    saved = _client(args).download_files(args.to, paths=args.file, dir=args.dir,
                                         glob=args.glob, recursive=args.recursive)
    _out(args, {"downloaded": saved},
         lambda x: [print(f"{s['local']}  ({s['bytes']} bytes)")
                    for s in x["downloaded"]])


def cmd_ls(args):
    d = _client(args).list_files(args.dir, glob=args.glob, recursive=args.recursive)
    def human(x):
        for f in x.get("files", []):
            print(f" {f['size']:>12}  {f['path']}")
    _out(args, d, human)


# -- streaming helpers ----------------------------------------------------
def _ev_text(e: dict) -> str:
    d = e.get("data", {})
    if e["type"] in ("assistant", "thinking"):
        return (d.get("text") or "")[:200]
    if e["type"] == "tool_use":
        return f"{d.get('name')} {json.dumps(d.get('input'))[:120]}"
    if e["type"] == "result":
        return (d.get("text") or "")[:200]
    return json.dumps(d)[:160]


def _stream_printer():
    def printer(e):
        t = e["type"]
        d = e.get("data", {})
        if t == "assistant":
            sys.stdout.write(d.get("text", "")); sys.stdout.flush()
        elif t == "tool_use":
            print(f"\n[tool: {d.get('name')}]", file=sys.stderr)
        elif t == "status":
            print(f"[{d.get('stage', d)}]", file=sys.stderr)
        elif t == "error":
            print(f"\n[ERROR] {d.get('message')}", file=sys.stderr)
    return printer


# -- parser ---------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ab", description="agent-bridge CLI")
    # Global flags live on a shared parent so they can follow the subcommand
    # (e.g. `ab gateways --json`), the natural CLI order.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--gateway", "-g", help="gateway name (default: config default)")
    common.add_argument("--config", "-c", help="path to gateways.json")
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=lambda **kw:
                           argparse.ArgumentParser(parents=[common], **kw))

    def job_flags(sp):
        sp.add_argument("--cwd")
        sp.add_argument("--agent")
        sp.add_argument("--model")
        sp.add_argument("--session")
        sp.add_argument("--permission-mode", dest="permission_mode")
        sp.add_argument("--upload", action="append", metavar="LOCAL",
                        help="local file to upload with the job (repeatable)")
        sp.add_argument("--file", action="append", metavar="REMOTE",
                        help="remote path to attach (repeatable)")

    sub.add_parser("gateways").set_defaults(func=cmd_gateways)

    sp = sub.add_parser("info"); sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("models", help="models this gateway offers, by complexity")
    sp.add_argument("--pick", metavar="COMPLEXITY",
                    help="print just the id for one task complexity tier "
                         "(tiers are whatever the gateway advertises, "
                         "typically simple|standard|hard|frontier)")
    sp.add_argument("--agent", help="which agent's models (default: the gateway's)")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("sessions"); sp.add_argument("--cwd")
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser("run")
    sp.add_argument("prompt", nargs="?",
                    help="prompt text; or '-' / omit to read stdin (heredoc/pipe)")
    sp.add_argument("--prompt-file", "-F", help="read the prompt from a file")
    job_flags(sp)
    sp.add_argument("--stream", action="store_true", help="stream events live")
    sp.add_argument("--timeout", type=float, default=900.0)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("submit")
    sp.add_argument("prompt", nargs="?",
                    help="prompt text; or '-' / omit to read stdin (heredoc/pipe)")
    sp.add_argument("--prompt-file", "-F", help="read the prompt from a file")
    job_flags(sp)
    sp.set_defaults(func=cmd_submit)

    sp = sub.add_parser("job"); sp.add_argument("id"); sp.set_defaults(func=cmd_job)

    sp = sub.add_parser("events"); sp.add_argument("id")
    sp.add_argument("--after", type=int, default=0)
    sp.add_argument("--follow", "-f", action="store_true")
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("cancel"); sp.add_argument("id")
    sp.set_defaults(func=cmd_cancel)

    sp = sub.add_parser("upload")
    sp.add_argument("paths", nargs="*", help="local files")
    sp.add_argument("--dir", help="local dir to upload recursively")
    sp.set_defaults(func=cmd_upload)

    sp = sub.add_parser("download")
    sp.add_argument("--file", action="append", metavar="REMOTE", help="remote path")
    sp.add_argument("--dir", help="remote dir")
    sp.add_argument("--glob", default="*")
    sp.add_argument("--recursive", action="store_true")
    sp.add_argument("--to", required=True, metavar="LOCAL_DIR")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("ls"); sp.add_argument("dir")
    sp.add_argument("--glob", default="*")
    sp.add_argument("--recursive", action="store_true")
    sp.set_defaults(func=cmd_ls)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ConfigError, GatewayError) as e:
        _err(str(e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
