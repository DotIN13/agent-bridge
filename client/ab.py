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
    ab jobs                             # recent jobs with their full ids
    ab job b4c220af                     # a unique id prefix works too
    ab submit -F task.md --title fetch-corpus ; ab events fetch-corpus -f
    ab submit -F nudge.md --session <id> --no-fork   # guidance into that thread
    ab ls /project/jevans/tzhang3/myrepo/out --glob '*.csv'
    ab download --dir /project/.../out --glob '*.csv' --to ./results

Output contract:
    stdout carries everything the command produces — records and the headers
    around them — one line each, ids in full, so grep sees all of it.
    stderr carries only the tool's own failures: bad config, unreachable
    gateway, ambiguous ref. Exception: `submit`, `run` and `--stream` put
    their metadata on stderr, because their stdout is a payload meant to be
    captured or redirected whole (`id=$(ab submit ...)`, `ab run > out.md`).
    Long free text is elided by default and marked with its true size.

    --json  chooses the shape (machine-readable)
    --full  chooses the completeness (no elision)
    They are orthogonal: `--json --full` is the faithful dump, `--full` alone
    is the readable one. `ab job` is never elided — the final report is the
    deliverable.

Global flags: --gateway NAME, --config PATH, --json, --full.
Exit code is non-zero if a run fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from abclient import (Client, ConfigError, GatewayError, load_gateways,  # noqa: E402
                      TERMINAL)


def _err(msg: str, code: int = 1):
    print(f"ab: {msg}", file=sys.stderr)
    raise SystemExit(code)


# Output contract, applied by every command:
#   stdout = everything the command was asked to produce — records AND the
#            headers, counts and hints around them, one line each. If you can
#            see it, you can grep it; you never have to know which stream a
#            line landed on before you can filter it.
#   stderr = only the tool's own failures (see `_err`): bad config, an
#            unreachable gateway, an ambiguous ref. Things that mean the
#            command did not run, not things it produced.
#   default = long free text elided; --full turns that off
#   --json  = shape, not completeness. The two flags are orthogonal, so
#             `--json --full` is the faithful machine-readable dump.
#
# The exceptions are the three commands whose stdout IS a payload meant to be
# captured or redirected whole, where a stray metadata line would corrupt it:
#   ab submit  — stdout is the bare job id, so `id=$(ab submit -F t.md)` works
#   ab run     — stdout is the result text, so `ab run ... > out.md` is clean
#   --stream   — stdout is the live assistant text; tool and status markers
#                interleave on stderr rather than inside the transcript
# Their metadata lines go to stderr for that reason and that reason only.
ELIDE_AT = 200

# Never elided, for two reasons.
#
# Identifiers: you cannot resume a session, cancel a job or open a path from a
# prefix, so a silently shortened id is worse than a long line.
#
# `result` and `error`: the final report is the deliverable and is written to
# stand alone. Clipping it would send the reader to the session transcript for
# the one thing that was already complete, which is the expensive mistake this
# whole contract exists to prevent.
_KEEP_WHOLE = frozenset({
    "id", "job_id", "session_id", "chosen_session", "forked_session",
    "path", "cwd", "base_url", "url", "model", "agent", "type", "status",
    "name", "default", "gateway", "seq", "ts",
    "result", "error",
})


def _note(msg: str) -> None:
    """A header, count or hint. Goes to stdout with everything else — it is
    output the caller asked for, not a diagnostic, and putting it on stderr
    would mean `ab job x | grep status` finds nothing."""
    print(msg)


def _line(s) -> str:
    """One record per line: collapse embedded newlines so a multi-line value
    cannot silently become three records."""
    return " ".join(str(s).split())


def _clip(s: str, n: int = ELIDE_AT) -> str:
    """Elide with the true size attached, so a clipped value is visibly
    clipped and the reader knows what it costs to get the rest."""
    return s if len(s) <= n else f"{s[:n]}… [+{len(s) - n} chars, --full]"


def _elide_obj(obj, full: bool):
    """Apply the same elision to JSON that the line view uses, so --json and
    the default agree on content and differ only in shape."""
    if full:
        return obj
    if isinstance(obj, dict):
        return {k: (v if k in _KEEP_WHOLE else _elide_obj(v, full))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_elide_obj(v, full) for v in obj]
    if isinstance(obj, str):
        return _clip(obj)
    return obj


def _out(args, obj, human):
    """--json picks the shape; --full picks the completeness."""
    full = getattr(args, "full", False)
    if args.json:
        print(json.dumps(_elide_obj(obj, full), indent=2))
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
    """Model ids the gateway's agents advertise; pass one to --model on a job.

    The list is the gateway's (`models = [...]` in each [agents.<name>] section
    of its config.toml), so what you see is what that agent accepts for --model.
    """
    d = _client(args).models(agent=args.agent)

    def human(x):
        if not x.get("models"):
            _note(f"agent '{x['agent']}' advertises no models; --model accepts "
                  f"any id the agent itself supports. Configure them under "
                  f"[agents.{x['agent']}] models = [...].")
            return
        _note(f"models for agent '{x['agent']}' — pass one to --model:")
        for m in x["models"]:
            print(m)
        if x.get("default"):
            _note(f"default when --model is omitted: {x['default']}")

    _out(args, d, human)


def cmd_sessions(args):
    """Sessions you can resume. Ids print in full: under the resume-first
    policy this is where you get the value for --session, and an 8-char prefix
    (what this used to print) is not something you can pass to anything."""
    d = _client(args).sessions(cwd=args.cwd, agent=args.agent)

    def human(x):
        rows = x.get("sessions", [])
        if not rows:
            _note("no sessions on this gateway for that filter")
            return
        _note(f"{'SESSION':<36} {'CWD':<40} TITLE")
        for s in rows:
            title = _line(s.get("title", ""))
            print(f"{s['session_id']:<36} {s['cwd']:<40} "
                  f"{title if args.full else _clip(title, 60)}")

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
                title=args.title, fork=args.fork,
                include_thinking=args.include_thinking,
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
                   files=args.file, upload=args.upload,
                   title=args.title, fork=args.fork,
                   include_thinking=args.include_thinking)

    def human(j):
        print(j["id"])
        if j.get("title"):
            print(f"title: {j['title']}", file=sys.stderr)

    _out(args, job, human)


def cmd_jobs(args):
    """Recent jobs. The only way to recover a full id from a prefix you wrote
    down, so it prints ids in full and never truncates them."""
    import time
    d = _client(args).list_jobs(limit=args.limit)

    def human(x):
        jobs = x.get("jobs", [])
        if not jobs:
            print("no jobs on this gateway")
            return
        now = time.time()

        def ago(ts):
            if not ts:
                return "-"
            m = (now - ts) / 60.0
            return f"{m:.0f}m" if m < 120 else f"{m / 60:.1f}h"

        # SEEN is last_event_at: for a batch job it keeps moving after the row
        # goes terminal, because ab-notify messages arrive long after the
        # agent's turn ended. AGE alone would say "3h old" for something that
        # reported in a minute ago.
        _note(f"{'ID':<36} {'STATUS':<10} {'AGE':>6} {'SEEN':>6}  TITLE")
        for j in jobs:
            label = _line(j.get("title") or j.get("prompt", ""))
            print(f"{j['id']:<36} {j.get('status', ''):<10} "
                  f"{ago(j.get('created_at')):>6} {ago(j.get('last_event_at')):>6}  "
                  f"{label if args.full else _clip(label, 46)}")

    _out(args, d, human)


def cmd_job(args):
    """The final report. Never elided even without --full: this is the whole
    point of the job, it is stored whole, and a worker writes it to stand
    alone. Truncating it would send the reader to the transcript for the one
    thing that was already complete."""
    j = _client(args).get_job(args.id)

    def human(x):
        _note(f"status: {x['status']}")
        if x.get("result"):
            print(x["result"])
        if x.get("error"):
            _note(f"error: {x['error']}")

    _out(args, j, human)


def cmd_events(args):
    c = _client(args)
    after = args.after
    if not args.follow:
        d = c.events(args.id, after)
        if args.until is not None:
            d = dict(d, events=[e for e in d["events"] if e["seq"] <= args.until])
        if args.type:
            keep = set(args.type)
            d = dict(d, events=[e for e in d["events"] if e["type"] in keep])

        def human(x):
            for e in x["events"]:
                print(f"{e['seq']:4} {e['type']}: {_ev_text(e, args.full)}")
        _out(args, d, human)
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
            if f.get("is_dir"):
                print(f" {'<dir>':>12}  {f['path']}/")
            else:
                print(f" {f['size']:>12}  {f['path']}")
    _out(args, d, human)


# -- streaming helpers ----------------------------------------------------
def _ev_text(e: dict, full: bool = False) -> str:
    """One line per event, elided so a long log stays scannable.

    The gateway stores every event whole; the clipping here is presentation
    only. `--full` turns it off — pair it with `--after/--until` to pull one
    bounded slice rather than the entire log, which is the cheap alternative
    to downloading a session transcript.
    """
    d = e.get("data", {})
    cut = (lambda s, n: s or "") if full else (lambda s, n: _elide(s or "", n))
    if e["type"] in ("assistant", "thinking", "result", "tool_result"):
        return cut(d.get("text"), 200)
    if e["type"] == "tool_use":
        return f"{d.get('name')} {cut(json.dumps(d.get('input')), 120)}"
    return cut(json.dumps(d), 160)


def _elide(s: str, n: int) -> str:
    """Clip with a visible marker and the true size, so a truncated line can
    never be mistaken for a short one."""
    return s if len(s) <= n else f"{s[:n]}… [+{len(s) - n} chars, --full]"


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
    common.add_argument("--full", action="store_true",
                        help="do not elide long text. Works with or without "
                             "--json: --json chooses the shape, --full chooses "
                             "the completeness")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=lambda **kw:
                           argparse.ArgumentParser(parents=[common], **kw))

    def job_flags(sp):
        sp.add_argument("--cwd")
        sp.add_argument("--agent")
        sp.add_argument("--model")
        sp.add_argument("--session",
                        help="pin the session to run in (otherwise the "
                             "dispatcher picks one)")
        sp.add_argument("--title", help="human handle; you can pass it to "
                                        "`ab job/events/cancel` instead of the "
                                        "id. Derived from the prompt if unset")
        sp.add_argument("--no-fork", dest="fork", action="store_false",
                        default=True,
                        help="resume the target session IN PLACE instead of "
                             "forking it — for a follow-up or guidance message "
                             "the session must actually see. Requires "
                             "--session; if that session is mid-turn Claude "
                             "queues the message for the end of the turn")
        sp.add_argument("--permission-mode", dest="permission_mode")
        sp.add_argument("--include-thinking", dest="include_thinking",
                        action="store_true", default=False,
                        help="keep the agent's reasoning (thinking) events in "
                             "the event stream; hidden by default")
        sp.add_argument("--upload", action="append", metavar="LOCAL",
                        help="local file to upload with the job (repeatable)")
        sp.add_argument("--file", action="append", metavar="REMOTE",
                        help="remote path to attach (repeatable)")

    sub.add_parser("gateways").set_defaults(func=cmd_gateways)

    sp = sub.add_parser("info"); sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("models", help="model ids this gateway's agents advertise")
    sp.add_argument("--agent", help="which agent's models (default: the gateway's)")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("sessions")
    sp.add_argument("--cwd")
    sp.add_argument("--agent",
                    help="which agent's sessions (default: the gateway's)")
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

    sp = sub.add_parser("jobs", help="recent jobs, with full ids")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_jobs)

    ref_help = "full uuid, a unique id prefix, or the job's title"

    sp = sub.add_parser("job", help="one job, by id, id prefix, or title")
    sp.add_argument("id", metavar="REF", help=ref_help)
    sp.set_defaults(func=cmd_job)

    sp = sub.add_parser("events"); sp.add_argument("id", metavar="REF", help=ref_help)
    sp.add_argument("--after", type=int, default=0,
                    help="only events with seq > N")
    sp.add_argument("--until", type=int, default=None,
                    help="only events with seq <= N; pair with --after "
                         "and --full to pull one bounded slice in full")
    sp.add_argument("--type", action="append", metavar="TYPE",
                    help="only this event type; repeatable "
                         "(assistant, thinking, tool_use, tool_result, "
                         "result, status, error, log, message)")
    sp.add_argument("--follow", "-f", action="store_true")
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("cancel"); sp.add_argument("id", metavar="REF", help=ref_help)
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
