#!/usr/bin/env python3
"""Agent-first command-line client for agent-bridge gateways.

Both ``ab --json jobs`` and ``ab jobs --json`` are supported. Machine output is
faithful: JSON is one complete document, and JSONL is one typed record per line.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

_CLIENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _CLIENT_DIR)
sys.path.insert(0, os.path.dirname(_CLIENT_DIR))
from abclient import (  # noqa: E402
    AWAIT_SESSION_TIMEOUT, CLIENT_VERSION, EVENT_TYPES, PROBE_TIMEOUT, TERMINAL,
    Client, ConfigError, GatewayError, client_capabilities, load_gateways,
)

EXIT_LOCAL = 1
EXIT_INVOCATION = 2
EXIT_REMOTE = 3
EXIT_TIMEOUT = 4
ELIDE_AT = 200
# `ab events REF` with no paging flags reads the last N rather than the first
# page: the end of a log is where the result, the failure and the last action
# are. `total` in the response keeps the truncation visible.
DEFAULT_TAIL = 50
# How much history `--follow` replays before streaming. Following used to start
# at seq 0 and replay the whole job, which floods a caller on exactly the
# long-running jobs follow exists for.
FOLLOW_PRIME_TAIL = 20


def _err(message: str, code: int = EXIT_LOCAL):
    print(f"ab: {message}", file=sys.stderr)
    raise SystemExit(code)


def _mode(args) -> str:
    output = getattr(args, "output", None)
    if getattr(args, "json", False):
        if output not in (None, "json"):
            _err("--json conflicts with --output " + output, EXIT_INVOCATION)
        return "json"
    return output or "human"


def _emit(args, obj, human, *, kind="result") -> None:
    mode = _mode(args)
    if mode == "json":
        print(json.dumps(obj, indent=2))
    elif mode == "jsonl":
        print(json.dumps({"kind": kind, "data": obj}, separators=(",", ":")),
              flush=True)
    else:
        human(obj)


def _emit_event(args, job_id: str, event: dict) -> None:
    print(json.dumps({"kind": "event", "job_id": job_id, "event": event},
                     separators=(",", ":")), flush=True)


def _emit_terminal(args, job: dict) -> None:
    print(json.dumps({"kind": "terminal", "job": job},
                     separators=(",", ":")), flush=True)


def _emit_timeout(args, job: dict) -> None:
    print(json.dumps({"kind": "timeout", "job": job,
                      "timed_out_waiting": True},
                     separators=(",", ":")), flush=True)


def _emit_complete(args, job: dict, reason: str) -> None:
    print(json.dumps({"kind": "complete", "reason": reason, "job": job},
                     separators=(",", ":")), flush=True)


def _client(args) -> Client:
    return load_gateways(args.config).client(args.gateway)


def _line(value) -> str:
    return " ".join(str(value).split())


def _clip(value: str, size: int = ELIDE_AT) -> str:
    return value if len(value) <= size else \
        f"{value[:size]}… [+{len(value) - size} chars, --full]"


def _epoch(value) -> float | None:
    """Seconds since the epoch from whatever a timestamp field holds.

    The API publishes ISO strings; older gateways published epoch floats, and
    the two are worth tolerating side by side so a new client still reads an
    un-upgraded gateway rather than rendering every time as unknown.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _ts(value) -> str:
    epoch = _epoch(value)
    if epoch is None:
        return "--:--:--"
    import datetime
    return datetime.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def _resolve_prompt(args) -> str:
    if getattr(args, "prompt_file", None):
        try:
            text = open(os.path.expanduser(args.prompt_file),
                        encoding="utf-8").read()
        except OSError as exc:
            _err(f"cannot read --prompt-file: {exc}")
    elif getattr(args, "prompt_stdin", False):
        text = sys.stdin.read()
    elif getattr(args, "prompt", None) == "-":
        text = sys.stdin.read()
    elif getattr(args, "prompt", None):
        text = args.prompt
    elif not sys.stdin.isatty():  # compatibility; --prompt-stdin is explicit
        text = sys.stdin.read()
    else:
        _err("no prompt: use -F/--prompt-file, --prompt-stdin, or PROMPT",
             EXIT_INVOCATION)
    if not text.strip():
        _err("prompt is empty", EXIT_INVOCATION)
    return text


def _upload_aliases(values) -> list[tuple[str, str]]:
    result = []
    for value in values or []:
        remote, separator, local = value.partition("=")
        if not separator or not remote or not local:
            _err(f"invalid --as value {value!r}; expected REMOTE=LOCAL",
                 EXIT_INVOCATION)
        result.append((remote, local))
    return result


def _submission(args) -> dict:
    uploads = list(args.upload or []) + _upload_aliases(args.upload_as)
    return dict(cwd=args.cwd, agent=args.agent, model=args.model,
                session=args.session, permission_mode=args.permission_mode,
                files=args.file, upload=uploads, title=args.title,
                fork=args.fork, include_thinking=args.include_thinking,
                idempotency_key=args.idempotency_key)


def _remote_exit(job: dict) -> None:
    if job.get("timed_out_waiting"):
        raise SystemExit(EXIT_TIMEOUT)
    if job.get("status") in {"failed", "canceled"}:
        raise SystemExit(EXIT_REMOTE)


# discovery -----------------------------------------------------------------
def cmd_gateways(args):
    """Configured gateways and, by default, whether each one is actually up.

    The status column used to report token presence as `[ok]`, which reads as
    health and is not — a fleet that is entirely down still printed `[ok]` and
    exited 0. Token presence and reachability are now separate columns, and
    the reachability one is real.
    """
    gateways = load_gateways(args.config)
    rows = gateways.probe(timeout=args.probe_timeout) if args.probe \
        else gateways.summary()
    data = {"gateways": rows, "default": gateways.default,
            "probed": bool(args.probe)}

    def human(value):
        for gateway in value["gateways"]:
            mark = "*" if gateway["default"] else " "
            token = "token" if gateway["has_token"] else "no token"
            status = _probe_status(gateway) if value["probed"] else "not probed"
            print(f" {mark} {gateway['name']:16} "
                  f"{gateway['base_url'] or '(no base_url)':32} {token:9} {status}")
    _emit(args, data, human)


def _probe_status(gateway: dict) -> str:
    """One column that answers 'can I use this right now'."""
    if gateway.get("state") == "up":
        version = gateway.get("version") or "?"
        latency = gateway.get("latency_ms")
        return f"up {version}" + (f" ({latency:g} ms)" if latency is not None else "")
    label = str(gateway.get("state", "unknown")).upper()
    detail = gateway.get("detail")
    return f"{label}" + (f" — {detail}" if detail else "")


def cmd_health(args):
    client = load_gateways(args.config).client(args.gateway, require_token=False)

    def human(value):
        if value.get("ok"):
            print(f"ok {value.get('gateway', '')} {value.get('version', '')}".rstrip())
        else:
            print(json.dumps(value))
    _emit(args, client.health(), human)


def cmd_agents(args):
    data = _client(args).agents()

    def human(value):
        print(f"default: {value.get('default')}")
        descriptions = {row.get("name"): row for row in value.get("agents", [])}
        for name in value.get("configured", []):
            row = descriptions.get(name, {})
            caps = [key for key, enabled in row.get("capabilities", {}).items()
                    if enabled is True]
            print(f"{name}\tmodel={row.get('default_model') or '-'}\t"
                  f"capabilities={','.join(caps) or 'unknown'}")
    _emit(args, data, human)


def cmd_help(args):
    if args.remote:
        client = load_gateways(args.config).client(args.gateway, require_token=False)
        text = client.remote_help()
        _emit(args, {"help": text}, lambda _value: print(text))
    else:
        if _mode(args) == "human":
            build_parser().print_help()
        else:
            _emit(args, client_capabilities(), lambda _value: None)


def cmd_info(args):
    data = _client(args).info(refresh=args.refresh)

    def human(value):
        print(value.get("summary") or json.dumps(
            {k: v for k, v in value.items() if k != "notes"}, indent=2))
        notes = (value.get("notes") or {}).get("text", "").strip()
        if notes:
            # After the probes, because it answers a different question: what
            # this machine *is*, then what its owner says about working on it.
            print("\n-- operator notes " + "-" * 42)
            print(notes)
    _emit(args, data, human)


def cmd_models(args):
    data = _client(args).models(agent=args.agent)

    def human(value):
        for model in value.get("models", []):
            print(model)
        if value.get("default"):
            print(f"default: {value['default']}")
    _emit(args, data, human)


def cmd_sessions(args):
    """Two views, because there are two questions.

    Without `--cwd`: which directories have work you could continue — the view
    you need before you know which project to ask about, and one the old flat
    list could not give at all. With `--cwd`: the sessions in exactly that
    directory, paged.
    """
    client = _client(args)
    if not args.cwd:
        data = client.session_dirs(agent=args.agent)

        def human(value):
            rows = value.get("dirs", [])
            if not rows:
                print("no directories with sessions on this gateway")
                return
            print(f"{'DIRECTORY':<44} {'SESSIONS':>8}  {'LAST ACTIVE':<26} LATEST")
            for row in rows:
                latest = _line(row.get("latest_title") or "")
                if not args.full:
                    latest = _clip(latest, 40)
                print(f"{row['cwd']:<44} {row['sessions']:>8}  "
                      f"{(row.get('last_active') or '-'):<26} {latest}")
            print(f"{value.get('total', len(rows))} directories · "
                  f"`ab sessions --cwd <dir>` for the sessions in one")
        _emit(args, data, human)
        return

    data = client.sessions(cwd=args.cwd, agent=args.agent,
                           limit=args.limit, cursor=args.cursor)

    def human(value):
        rows = value.get("sessions", [])
        if not rows:
            print(f"no sessions recorded for {args.cwd}")
            return
        print(f"{'SESSION':<36} {'LAST ACTIVE':<26} TITLE")
        for session in rows:
            title = _line(session.get("title", ""))
            if not args.full:
                title = _clip(title, 50)
            print(f"{session['session_id']:<36} "
                  f"{(session.get('last_active') or '-'):<26} {title}")
        print(f"{len(rows)} of {value.get('total', len(rows))}")
        if value.get("next_cursor"):
            print(f"next_cursor: {value['next_cursor']}")
    _emit(args, data, human)


# jobs ----------------------------------------------------------------------
def _human_stream_printer():
    def printer(event):
        event_type = event.get("type")
        data = event.get("data", {})
        if event_type == "assistant":
            sys.stdout.write(data.get("text", ""))
            sys.stdout.flush()
        elif event_type == "tool_use":
            print(f"\n[tool: {data.get('name')}]", file=sys.stderr)
        elif event_type == "status":
            print(f"[{data.get('stage', data)}]", file=sys.stderr)
        elif event_type == "error":
            print(f"\n[ERROR] {data.get('message')}", file=sys.stderr)
    return printer


def cmd_run(args):
    client = _client(args)
    prompt = _resolve_prompt(args)
    mode = _mode(args)
    accepted = client.submit(prompt, **_submission(args))
    callback = None
    if mode == "jsonl":
        callback = lambda event: _emit_event(args, accepted["id"], event)
    elif mode == "human" and args.stream:
        callback = _human_stream_printer()
    job = client.wait(accepted["id"], timeout=args.timeout, on_event=callback,
                      cancel_on_timeout=args.cancel_on_timeout)
    if mode == "json":
        print(json.dumps(job, indent=2))
    elif mode == "jsonl":
        (_emit_timeout if job.get("timed_out_waiting") else _emit_terminal)(args, job)
    else:
        if args.stream:
            print()
        else:
            print(job.get("result") or "")
        if job.get("timed_out_waiting"):
            print(f"[timeout] job {job.get('id')} continues with status "
                  f"{job.get('status')}", file=sys.stderr)
        print(f"[{job.get('status')}] id={job.get('id')} "
              f"session={job.get('session')} "
              f"cost=${job.get('cost_usd')}", file=sys.stderr)
    _remote_exit(job)


def cmd_submit(args):
    client = _client(args)
    job = client.submit(_resolve_prompt(args), **_submission(args))
    # Always wait for the session id. It is what makes the *next* call possible
    # (a follow-up, a steer, a fork), and without it every caller pays a
    # discover-the-session round trip it almost always wants.
    #
    # This waits for the id only, never for the work -- that is `ab run` -- and
    # exceeding `--await-timeout` is not an error: the job keeps running and the
    # row says `session: pending`. So there is no case in which waiting costs
    # more than the timeout, which is why the opt-out went (design/08).
    job = client.await_session(job, timeout=args.await_timeout)

    def human(value):
        # The bare id stays the whole of stdout: `id=$(ab submit -F t.md)` is a
        # documented contract. Everything else is metadata on stderr.
        print(value["id"])
        if value.get("title"):
            print(f"title: {value['title']}", file=sys.stderr)
        state = value.get("session_state", "pending")
        if value.get("session"):
            print(f"session: {value['session']} ({state})", file=sys.stderr)
        elif state == "failed":
            print(f"session: none — job {value.get('status', 'failed')} before it "
                  f"started: {value.get('error') or 'no error recorded'}",
                  file=sys.stderr)
        else:
            print("session: pending — read it from `ab job <ref> --output json`",
                  file=sys.stderr)
    _emit(args, job, human)


def cmd_jobs(args):
    data = _client(args).list_jobs(limit=args.limit, cursor=args.cursor)

    def human(value):
        import time
        rows = value.get("jobs", [])
        if not rows:
            print("no jobs on this gateway")
            return
        now = time.time()
        def ago(timestamp):
            epoch = _epoch(timestamp)
            if epoch is None:
                return "-"
            minutes = (now - epoch) / 60.0
            return f"{minutes:.0f}m" if minutes < 120 else f"{minutes / 60:.1f}h"
        print(f"{'ID':<36} {'STATUS':<10} {'AGE':>6} {'SEEN':>6}  TITLE")
        for job in rows:
            label = _line(job.get("title") or job.get("prompt", ""))
            if not args.full:
                label = _clip(label, 46)
            print(f"{job['id']:<36} {job.get('status',''):<10} "
                  f"{ago(job.get('created_at')):>6} "
                  f"{ago(job.get('last_event_at')):>6}  {label}")
        if value.get("next_cursor"):
            print(f"next_cursor: {value['next_cursor']}")
    _emit(args, data, human)


def cmd_job(args):
    client = _client(args)
    if args.wait:
        job = client.wait(args.id, timeout=args.timeout,
                          cancel_on_timeout=args.cancel_on_timeout)
    else:
        job = client.get_job(args.id)

    def human(value):
        print(f"status: {value['status']}")
        if value.get("result"):
            print(value["result"])
        if value.get("error"):
            print(f"error: {value['error']}")
        if value.get("timed_out_waiting"):
            print(f"timeout: job {value.get('id')} continues")
    _emit(args, job, human,
          kind="timeout" if job.get("timed_out_waiting") else "result")
    if args.wait:
        _remote_exit(job)
    elif args.fail_on_job_failure and job.get("status") in {"failed", "canceled"}:
        raise SystemExit(EXIT_REMOTE)


def cmd_monitors(args):
    data = _client(args).list_monitors(
        job=args.job, status=args.status,
        active=None if args.all else True,
        limit=args.limit, cursor=args.cursor)

    def human(value):
        rows = value.get("monitors", [])
        if not rows:
            print("nothing being watched" if args.all else
                  "nothing being watched right now (--all includes finished)")
            return
        print(f"{'ID':<36} {'STATUS':<9} {'EVERY':>6}  LABEL / LAST READ")
        for row in rows:
            label = row.get("label") or ""
            detail = _line(row.get("detail") or "")
            text = f"{label}  {detail}".strip()
            if not args.full:
                text = _clip(text, 46)
            print(f"{row['id']:<36} {row.get('status',''):<9} "
                  f"{int(row.get('interval_sec') or 0):>5}s  {text}")
        if value.get("next_cursor"):
            print(f"next_cursor: {value['next_cursor']}")
    _emit(args, data, human)


def cmd_monitor(args):
    client = _client(args)
    if args.cancel:
        row = client.cancel_monitor(args.id)
    elif args.wait:
        row = client.wait_monitor(args.id, timeout=args.timeout)
    else:
        row = client.get_monitor(args.id)

    def human(value):
        print(f"status: {value['status']}")
        if value.get("label"):
            print(f"label: {value['label']}")
        print(f"poll: {value['poll_cmd']}")
        print(f"every: {int(value.get('interval_sec') or 0)}s")
        if value.get("job_id"):
            print(f"job: {value['job_id']}")
        if value.get("last_poll_at"):
            print(f"last read: {_ts(value['last_poll_at'])}")
        if value.get("detail"):
            print(f"read: {value['detail']}")
        for path in value.get("result_paths") or ():
            print(f"result: {path}")
        if value.get("note"):
            print(f"note: {value['note']}")
        if value.get("timed_out_waiting"):
            print(f"timeout: watch {value.get('id')} continues")
    _emit(args, row, human,
          kind="timeout" if row.get("timed_out_waiting") else "result")
    if args.wait:
        if row.get("timed_out_waiting"):
            raise SystemExit(EXIT_TIMEOUT)
        # `expired` is the gateway giving up on the watch rather than the work
        # failing, but a caller who waited for an answer did not get one.
        if row.get("status") in {"failed", "expired", "canceled"}:
            raise SystemExit(EXIT_REMOTE)


def cmd_wait(args):
    job = _client(args).wait(args.id, timeout=args.timeout,
                             cancel_on_timeout=args.cancel_on_timeout,
                             on_event=(lambda event: _emit_event(args, args.id, event))
                             if _mode(args) == "jsonl" else None)
    mode = _mode(args)
    if mode == "jsonl":
        (_emit_timeout if job.get("timed_out_waiting") else _emit_terminal)(args, job)
    else:
        _emit(args, job, lambda value: (
            print(value.get("result") or value.get("error") or
                  f"{value.get('id')}: {value.get('status')}")
        ), kind="timeout" if job.get("timed_out_waiting") else "result")
    _remote_exit(job)


def _event_text(event: dict, full=False) -> str:
    data = event.get("data", {})
    def cut(value, size):
        text = value or ""
        return text if full else _clip(text, size)
    if event.get("type") in {"assistant", "thinking", "result", "tool_result"}:
        return cut(data.get("text"), 200)
    if event.get("type") == "tool_use":
        return f"{data.get('name')} {cut(json.dumps(data.get('input')), 120)}"
    return cut(json.dumps(data), 160)


def cmd_events(args):
    client = _client(args)
    mode = _mode(args)
    if not args.follow:
        # A read with no paging flags is almost always "what did it just do",
        # and the interesting end of a log is the bottom. So default to a tail
        # rather than the first page; `--after 0` restores top-down reading, and
        # `total` in the output makes the truncation visible rather than silent.
        tail = args.tail
        if tail is None and args.after is None:
            tail = DEFAULT_TAIL
        data = client.events(args.id, args.after or 0, limit=args.limit, tail=tail,
                             until=args.until, types=args.type or ())
        _emit(args, data, lambda value: [
            print(f"{event['seq']:4} {_ts(event.get('ts'))} {event['type']}: "
                  f"{_event_text(event, args.full)}")
            for event in value["events"]])
        if args.fail_on_job_failure and data.get("status") in {"failed", "canceled"}:
            raise SystemExit(EXIT_REMOTE)
        return

    collected = []
    human_printer = _human_stream_printer()
    wanted = set(args.type or [])
    # Follow used to start at seq 0 and replay every historical event before
    # streaming. Prime with a short tail instead, so following a job that has
    # been running for an hour costs a few lines rather than its whole log.
    # `--after 0` asks for the full replay explicitly; `--tail N` sizes it.
    start = args.after
    if start is None:
        prime = args.tail if args.tail is not None else FOLLOW_PRIME_TAIL
        recent = client.events(args.id, tail=prime, until=args.until,
                               types=args.type or ())
        for event in recent["events"]:
            if mode == "jsonl":
                _emit_event(args, args.id, event)
            elif mode == "json":
                collected.append(event)
            else:
                human_printer(event)
        start = recent["next_after"] or 0
    last_seq = start
    for event in client.iter_events(args.id, start, until=args.until):
        last_seq = max(last_seq, int(event.get("seq", 0)))
        if wanted and event.get("type") not in wanted:
            continue
        if mode == "jsonl":
            _emit_event(args, args.id, event)
        elif mode == "json":
            collected.append(event)
        else:
            human_printer(event)
    job = client.get_job(args.id)
    reached_until = args.until is not None and last_seq >= args.until
    is_terminal = job.get("status") in TERMINAL
    if not reached_until and not is_terminal:
        raise GatewayError(
            f"event follow ended while job {args.id} is still "
            f"{job.get('status')}")
    if mode == "json":
        print(json.dumps({"events": collected, "job": job,
                          "status": job.get("status"),
                          "terminal": job.get("status") in TERMINAL}, indent=2))
    elif mode == "jsonl":
        if reached_until and not is_terminal:
            _emit_complete(args, job, "until")
        elif is_terminal:
            _emit_terminal(args, job)
    else:
        print(file=sys.stderr)
    if job.get("status") in {"failed", "canceled"} and (
            not reached_until or args.fail_on_job_failure):
        raise SystemExit(EXIT_REMOTE)


def cmd_cancel(args):
    data = _client(args).cancel(args.id)
    _emit(args, data, lambda value: print(
        f"{value.get('id')}: " +
        ("already canceled" if value.get("already_terminal") else
         f"canceling (was {value.get('was')})")))


def cmd_steer(args):
    data = _client(args).steer(args.id, _resolve_prompt(args))
    _emit(args, data, lambda value: (
        print(value.get("id", "")),
        print("delivered; pickup occurs at the next tool boundary")
    ))


# files ---------------------------------------------------------------------
def cmd_upload(args):
    aliases = _upload_aliases(args.as_name)
    data = _client(args).upload_files(paths=list(args.paths) + aliases,
                                      dir=args.dir)
    _emit(args, data, lambda value: [print(path) for path in value.get("paths", [])])


def cmd_download(args):
    saved = _client(args).download_files(
        args.to, paths=args.file, dir=args.dir, glob=args.glob,
        recursive=args.recursive, flatten=args.flatten, overwrite=args.overwrite)
    _emit(args, {"downloaded": saved}, lambda value: [
        print(f"{item['local']}  ({item['bytes']} bytes)")
        for item in value["downloaded"]])


def cmd_ls(args):
    data = _client(args).list_files(args.dir, glob=args.glob,
                                    recursive=args.recursive,
                                    limit=args.limit, cursor=args.cursor)
    def human(value):
        for row in value.get("files", []):
            size = "<dir>" if row.get("is_dir") else str(row.get("size", 0))
            suffix = "/" if row.get("is_dir") else ""
            print(f" {size:>12}  {row['path']}{suffix}")
    _emit(args, data, human)


# parser --------------------------------------------------------------------
def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def _add_globals(parser, *, child=False):
    default = argparse.SUPPRESS if child else None
    parser.add_argument("--gateway", "-g", default=default,
                        help="gateway name (default: configured default)")
    parser.add_argument("--config", "-c", default=default,
                        help="path to gateways JSON/TOML")
    parser.add_argument("--output", choices=("human", "json", "jsonl"),
                        default=default, help="output protocol (default: human)")
    parser.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS if child else False,
                        help="alias for --output json")
    parser.add_argument("--full", action="store_true",
                        default=argparse.SUPPRESS if child else False,
                        help="disable elision in human output")
    parser.add_argument("--version", action="version", version=CLIENT_VERSION)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ab", description="agent-bridge CLI for humans, scripts, and agents")
    _add_globals(parser)
    common = argparse.ArgumentParser(add_help=False)
    _add_globals(common, child=True)
    sub = parser.add_subparsers(
        dest="cmd", required=True,
        parser_class=lambda **kwargs: argparse.ArgumentParser(
            parents=[common], **kwargs))

    def command(name, help_text):
        return sub.add_parser(name, help=help_text, description=help_text)

    def prompt_flags(sp, noun="prompt"):
        sp.add_argument("prompt", nargs="?",
                        help=f"{noun} text; '-' reads stdin")
        group = sp.add_mutually_exclusive_group()
        group.add_argument("--prompt-file", "-F",
                           help=f"read {noun} from a UTF-8 file (recommended)")
        group.add_argument("--prompt-stdin", action="store_true",
                           help=f"read {noun} explicitly from stdin")

    def job_flags(sp):
        sp.add_argument("--cwd", help="remote absolute working directory")
        sp.add_argument("--agent", help="agent backend")
        sp.add_argument("--model", help="model id for the selected backend")
        sp.add_argument("--session", help="session id to fork/resume")
        sp.add_argument("--title", help="stable human job reference")
        sp.add_argument("--no-fork", dest="fork", action="store_false", default=True,
                        help="resume an IDLE --session in place")
        sp.add_argument("--permission-mode", dest="permission_mode")
        sp.add_argument("--include-thinking", action="store_true",
                        help="retain reasoning events")
        sp.add_argument("--upload", action="append", metavar="LOCAL",
                        help="attach a local regular file (repeatable)")
        sp.add_argument("--upload-as", action="append", default=[],
                        metavar="REMOTE=LOCAL", help="attach with explicit remote name")
        sp.add_argument("--file", action="append", metavar="REMOTE",
                        help="attach an existing remote path")
        sp.add_argument("--idempotency-key",
                        help="stable retry key for exactly-one job creation")

    sp = command("gateways", "list configured gateways and whether each is up")
    # Probing is the default because the unprobed view is the one that misleads:
    # it can only ever say "configured", and readers hear "working".
    sp.add_argument("--no-probe", dest="probe", action="store_false", default=True,
                    help="list local config only; contact nothing")
    sp.add_argument("--probe-timeout", type=_positive_float, default=PROBE_TIMEOUT,
                    metavar="SECONDS",
                    help=f"per-gateway probe timeout (default {PROBE_TIMEOUT:g}s)")
    sp.set_defaults(func=cmd_gateways)
    command("health", "probe one gateway's liveness and version").set_defaults(func=cmd_health)
    command("agents", "list configured agent backends and capabilities").set_defaults(func=cmd_agents)
    sp = command("help", "show local or live gateway help")
    sp.add_argument("--remote", action="store_true", help="fetch /v1/help")
    sp.set_defaults(func=cmd_help)

    sp = command("info", "show remote host/cluster capabilities")
    sp.add_argument("--refresh", action="store_true", help="start a background re-probe")
    sp.set_defaults(func=cmd_info)
    sp = command("models", "list model ids advertised by an agent")
    sp.add_argument("--agent")
    sp.set_defaults(func=cmd_models)
    sp = command("sessions",
                 "directories with sessions, or the sessions in one (--cwd)")
    sp.add_argument("--cwd", help="exact directory; lists that directory's "
                                  "sessions instead of the directory summary")
    sp.add_argument("--agent", help="scope to one backend")
    sp.add_argument("--limit", type=_positive_int, default=40,
                    help="page size for --cwd (default 40)")
    sp.add_argument("--cursor", help="opaque next_cursor from a prior page")
    sp.set_defaults(func=cmd_sessions)

    sp = command("run", "submit a prompt and wait for completion")
    prompt_flags(sp); job_flags(sp)
    sp.add_argument("--stream", action="store_true",
                    help="stream human assistant text; JSON remains one document")
    sp.add_argument("--timeout", type=_positive_float, default=900.0,
                    help="wait seconds; timeout does not cancel")
    sp.add_argument("--cancel-on-timeout", action="store_true")
    sp.set_defaults(func=cmd_run)

    sp = command("submit", "submit a prompt, waiting only for its session id")
    prompt_flags(sp); job_flags(sp)
    sp.add_argument("--await-timeout", type=_positive_float,
                    default=AWAIT_SESSION_TIMEOUT, metavar="SECONDS",
                    help=f"how long to wait for the session id "
                         f"(default {AWAIT_SESSION_TIMEOUT:g}s). Exceeding it is "
                         f"not an error; the job keeps running")
    sp.set_defaults(func=cmd_submit)

    sp = command("jobs", "list recent job summaries with full ids")
    sp.add_argument("--limit", type=_positive_int, default=50)
    sp.add_argument("--cursor", help="opaque next_cursor from a prior page")
    sp.set_defaults(func=cmd_jobs)

    sp = command("monitors", "list watches on work that outlives a turn")
    sp.add_argument("--job", metavar="REF", help="only watches for this job")
    sp.add_argument("--status", help="queued|running|finished|failed|expired|canceled")
    sp.add_argument("--all", action="store_true",
                    help="include watches that already resolved")
    sp.add_argument("--limit", type=_positive_int, default=50)
    sp.add_argument("--cursor", help="opaque next_cursor from a prior page")
    sp.set_defaults(func=cmd_monitors)

    sp = command("monitor", "show one watch, or stop it")
    sp.add_argument("id", metavar="ID", help="monitor id from `ab monitors`")
    sp.add_argument("--cancel", action="store_true",
                    help="stop watching; the work itself keeps running")
    sp.add_argument("--wait", action="store_true",
                    help="block until the watch resolves (exit 3 if it ends "
                         "failed/expired/canceled, 4 on timeout)")
    sp.add_argument("--timeout", type=_positive_float, default=900.0)
    sp.set_defaults(func=cmd_monitor)

    reference = "full UUID, unique id prefix, or title"
    sp = command("job", "get one job status/result")
    sp.add_argument("id", metavar="REF", help=reference)
    sp.add_argument("--wait", action="store_true", help="wait for terminal status")
    sp.add_argument("--timeout", type=_positive_float, default=900.0)
    sp.add_argument("--cancel-on-timeout", action="store_true")
    sp.add_argument("--fail-on-job-failure", action="store_true")
    sp.set_defaults(func=cmd_job)

    sp = command("wait", "wait for an existing job")
    sp.add_argument("id", metavar="REF", help=reference)
    sp.add_argument("--timeout", type=_positive_float, default=900.0)
    sp.add_argument("--cancel-on-timeout", action="store_true")
    sp.set_defaults(func=cmd_wait)

    sp = command("events", "read or follow a job event stream")
    sp.add_argument("id", metavar="REF", help=reference)
    sp.add_argument("--tail", type=_positive_int, metavar="N",
                    help=f"read the last N events (default {DEFAULT_TAIL} when "
                         f"no --after is given); cannot be combined with --after")
    # Defaults to None, not 0: `--after 0` is the documented way to ask for a
    # top-down read, so "absent" and "explicitly zero" have to stay distinct.
    sp.add_argument("--after", type=int, default=None,
                    help="only seq > N; reads forward from the top")
    sp.add_argument("--until", type=int, help="stop at seq N")
    sp.add_argument("--type", action="append", choices=sorted(EVENT_TYPES),
                    help="event type filter (repeatable); applied within --tail")
    sp.add_argument("--limit", type=_positive_int, default=500)
    sp.add_argument("--follow", "-f", action="store_true", help="use resumable SSE")
    sp.add_argument("--fail-on-job-failure", action="store_true")
    sp.set_defaults(func=cmd_events)

    sp = command("cancel", "interrupt a queued/running job")
    sp.add_argument("id", metavar="REF", help=reference); sp.set_defaults(func=cmd_cancel)
    sp = command("steer", "message a running turn at its next tool boundary")
    sp.add_argument("id", metavar="REF", help=reference)
    prompt_flags(sp, "message"); sp.set_defaults(func=cmd_steer)

    sp = command("upload", "stage local files on the gateway")
    sp.add_argument("paths", nargs="*", help="local regular files")
    sp.add_argument("--dir", help="local directory to upload recursively")
    sp.add_argument("--as", dest="as_name", action="append", default=[],
                    metavar="REMOTE=LOCAL", help="explicit remote name")
    sp.set_defaults(func=cmd_upload)
    sp = command("download", "download remote artifacts safely")
    sp.add_argument("--file", action="append", metavar="REMOTE")
    sp.add_argument("--dir", help="remote directory")
    sp.add_argument("--glob", default="*")
    sp.add_argument("--recursive", action="store_true")
    sp.add_argument("--to", required=True, metavar="LOCAL_DIR")
    sp.add_argument("--flatten", action="store_true",
                    help="legacy basename-only layout; collisions are rejected")
    sp.add_argument("--overwrite", action="store_true")
    sp.set_defaults(func=cmd_download)
    sp = command("ls", "list remote files")
    sp.add_argument("dir")
    sp.add_argument("--glob", default="*")
    sp.add_argument("--recursive", action="store_true")
    sp.add_argument("--limit", type=_positive_int, default=200)
    sp.add_argument("--cursor")
    sp.set_defaults(func=cmd_ls)
    return parser


def _validate(args) -> None:
    _mode(args)  # validates aliases/conflicts
    if hasattr(args, "fork") and not args.fork and not args.session:
        _err("--no-fork requires --session", EXIT_INVOCATION)
    if (getattr(args, "after", None) or 0) < 0:
        _err("--after must be non-negative", EXIT_INVOCATION)
    if (getattr(args, "tail", None) is not None
            and getattr(args, "after", None) is not None):
        # Anchoring from both ends at once has no single sensible reading. The
        # server rejects this too; catching it locally keeps it an invocation
        # error (exit 2) rather than a transport one (exit 1).
        _err("--tail conflicts with --after; choose which end to read from",
             EXIT_INVOCATION)
    if getattr(args, "until", None) is not None:
        # --until pairs with --tail as a bounded window from the right, so only
        # compare against --after when that is the anchor in use.
        floor = (0 if getattr(args, "tail", None) is not None
                 else (getattr(args, "after", None) or 0))
        if args.until < 0 or args.until < floor:
            _err("--until must be non-negative and >= --after", EXIT_INVOCATION)
    if getattr(args, "prompt_file", None) and getattr(args, "prompt", None):
        _err("PROMPT conflicts with --prompt-file", EXIT_INVOCATION)
    if getattr(args, "prompt_stdin", False) and getattr(args, "prompt", None):
        _err("PROMPT conflicts with --prompt-stdin", EXIT_INVOCATION)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate(args)
        args.func(args)
    except (ConfigError, GatewayError, OSError, ValueError) as exc:
        _err(str(exc), EXIT_LOCAL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
