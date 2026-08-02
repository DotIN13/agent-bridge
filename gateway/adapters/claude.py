"""Claude Code adapter.

dispatch_mode = "agent_exec" (default, matches the spec): we launch one Claude
session as a *dispatcher* with Bash. Its appended system prompt tells it to read
the session index, pick the best-matching session, and delegate by forking it:

    cat TASK_FILE | claude --resume <id> --fork-session -p \
        --output-format json --permission-mode bypassPermissions

i.e. Claude finds the right session to fork and executes it itself. We stream the
dispatcher's events and parse the nested run's JSON out of the Bash tool result.

dispatch_mode = "select_then_exec": the model only returns a session id (no
tools); the worker forks + execs it directly and streams that. More
deterministic, cleaner logs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from ..config import AgentConfig
from ..sessions import SessionInfo, scan
from .base import Event, JobSpec, RunResult, _kill_group

_RESUME_RE = re.compile(r"--resume[= ]+([0-9a-fA-F-]{36})")
_ARROW_RE = re.compile(r"session:\s*(\S+)\s*->\s*(\S+)")

_DISPATCH_PROMPT = """\
You are the DISPATCHER for an agent gateway on the midway3-login5 login node.
A user has submitted a task via HTTP (it is your user prompt, and also stored
verbatim at:
    TASK_FILE = {task_file}
).

Your ONLY job is to route this task to the most appropriate existing Claude Code
session and run it there by FORKING that session. Do NOT perform the task
yourself in this dispatcher session.

Steps:
1. Read AVAILABLE SESSIONS below. Each entry: session_id, cwd, title, summary,
   git_branch, last_active (epoch), messages.
2. Choose the single session whose cwd and topic best match the task. Prefer a
   session whose cwd is the task's target project.
3. Execute the task by forking that session, piping the exact task from the file
   so quoting is never an issue:

       cd <that session's cwd> && cat "{task_file}" | claude \\
           --resume <session_id> --fork-session -p \\
           --output-format json --permission-mode {permission_mode}{model_flag}

   --fork-session guarantees a NEW session id; the original is never mutated.
4. If NO existing session fits, start fresh in the best allowed directory:

       cd <dir> && cat "{task_file}" | claude -p \\
           --output-format json --permission-mode {permission_mode}{model_flag}

5. The nested command prints a JSON object with "result" and "session_id".
   After it finishes, output on its own line:
       session: <chosen_session_id_or_NEW> -> <new_session_id_from_json>
   then output the nested run's "result" text verbatim as your final answer.

Rules:
- Operate ONLY within these directories: {allowed_dirs}. Never cd elsewhere.
- Always use --fork-session when resuming; never resume in place.
- Run exactly one nested claude command. Keep your own commentary minimal.

AVAILABLE SESSIONS (JSON, newest first):
{sessions_json}
"""

_SELECTOR_PROMPT = """\
You are a router. Given a user task and a list of existing Claude Code sessions,
pick the ONE session whose cwd and topic best match, so it can be forked and
continued. If none fit, set start_fresh=true and give the best cwd to start in.
Respond only via the structured output tool.

AVAILABLE SESSIONS (JSON, newest first):
{sessions_json}
"""

_SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": {"type": ["string", "null"]},
        "start_fresh": {"type": "boolean"},
        "cwd": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["start_fresh", "cwd", "reason"],
}


class ClaudeAdapter:
    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg

    # -- sessions ---------------------------------------------------------
    def list_sessions(self, cwd_filter: str | None = None) -> list[SessionInfo]:
        return scan(limit=self.cfg.max_sessions_in_index, cwd_filter=cwd_filter)

    def _index_json(self, cwd_filter: str | None) -> str:
        infos = self.list_sessions(cwd_filter)
        compact = [
            {
                "session_id": s.session_id,
                "cwd": s.cwd,
                "title": s.title,
                "summary": s.summary,
                "git_branch": s.git_branch,
                "last_active": int(s.last_active),
                "messages": s.messages,
            }
            for s in infos
        ]
        return json.dumps(compact, indent=2)

    # -- run --------------------------------------------------------------
    def run(self, spec: JobSpec, emit: Callable[[Event], None]) -> RunResult:
        if self.cfg.dispatch_mode == "select_then_exec":
            return self._run_select_then_exec(spec, emit)
        return self._run_agent_exec(spec, emit)

    def _model_flag(self, spec: JobSpec) -> str:
        model = spec.model or self.cfg.model
        return f" --model {model}" if model else ""

    def _write_task_file(self, spec: JobSpec) -> str:
        d = Path(os.environ.get("AGENT_BRIDGE_DATA_DIR", spec.cwd)) / "jobs" / spec.job_id
        d.mkdir(parents=True, exist_ok=True)
        f = d / "task.txt"
        f.write_text(spec.prompt)
        return str(f)

    # -- mode 1: dispatcher forks + executes itself -----------------------
    def _run_agent_exec(self, spec: JobSpec, emit) -> RunResult:
        task_file = self._write_task_file(spec)
        perm = spec.permission_mode or self.cfg.permission_mode
        system = _DISPATCH_PROMPT.format(
            task_file=task_file,
            permission_mode=perm,
            model_flag=self._model_flag(spec),
            allowed_dirs=", ".join(self.cfg.allowed_dirs),
            sessions_json=self._index_json(spec.cwd),
        )
        args = [
            self.cfg.bin, "-p", spec.prompt,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", perm,
            "--tools", "Bash,Read,Glob,Grep",
            "--append-system-prompt", system,
        ]
        for d in self.cfg.allowed_dirs:
            args += ["--add-dir", d]

        res = RunResult(ok=False)
        self._stream(args, spec.cwd, emit, res, capture_nested=True,
                     cancel=spec.cancel)
        return res

    # -- mode 2: model selects, worker executes ---------------------------
    def _run_select_then_exec(self, spec: JobSpec, emit) -> RunResult:
        system = _SELECTOR_PROMPT.format(sessions_json=self._index_json(spec.cwd))
        sel_args = [
            self.cfg.bin, "-p", spec.prompt,
            "--output-format", "json",
            "--tools", "",
            "--append-system-prompt", system,
            "--json-schema", json.dumps(_SELECT_SCHEMA),
        ]
        if self.cfg.model:
            sel_args += ["--model", self.cfg.model]
        emit(Event("status", {"stage": "selecting"}))
        sel = _run_json(sel_args, spec.cwd, self.cfg.timeout_sec)
        choice = _parse_structured(sel)
        emit(Event("status", {"stage": "selected", "choice": choice}))

        target_cwd = spec.cwd
        chosen = None
        if choice and not choice.get("start_fresh") and choice.get("session_id"):
            chosen = choice["session_id"]
        if choice and choice.get("cwd"):
            try:
                target_cwd = self.cfg.resolve_cwd(choice["cwd"])
            except ValueError:
                pass

        perm = spec.permission_mode or self.cfg.permission_mode
        exec_args = [self.cfg.bin, "-p", spec.prompt,
                     "--output-format", "stream-json", "--verbose",
                     "--permission-mode", perm]
        if chosen:
            exec_args += ["--resume", chosen, "--fork-session"]
        if spec.model or self.cfg.model:
            exec_args += ["--model", spec.model or self.cfg.model]

        res = RunResult(ok=False, chosen_session=chosen)
        self._stream(exec_args, target_cwd, emit, res, capture_nested=False,
                     cancel=spec.cancel)
        return res

    # -- shared streaming -------------------------------------------------
    def _stream(self, args, cwd, emit, res: RunResult, *, capture_nested: bool,
                cancel=None):
        proc = subprocess.Popen(
            args, cwd=cwd, text=True, bufsize=1,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if cancel is not None:
            cancel.bind(proc)  # kills the process group on cancel (even if already set)
        # timeout_sec <= 0 disables the wall-clock kill (jobs run unbounded).
        timer = None
        if self.cfg.timeout_sec and self.cfg.timeout_sec > 0:
            timer = threading.Timer(self.cfg.timeout_sec, _kill, [proc])
            timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    emit(Event("log", {"raw": line[:2000]}))
                    continue
                self._handle_record(rec, emit, res, capture_nested)
        finally:
            if timer is not None:
                timer.cancel()
            rc = proc.wait()
            stderr = proc.stderr.read() if proc.stderr else ""

        if res.ok:
            return
        # No successful result event: treat as failure.
        res.ok = False
        if not res.error:
            res.error = (stderr or f"claude exited with code {rc}").strip()[:4000]
        emit(Event("error", {"message": res.error, "returncode": rc}))

    def _handle_record(self, rec, emit, res: RunResult, capture_nested: bool):
        rtype = rec.get("type")
        if rtype == "system":
            emit(Event("status", {"subtype": rec.get("subtype"),
                                  "session_id": rec.get("session_id"),
                                  "model": rec.get("model"),
                                  "tools": rec.get("tools")}))
        elif rtype == "assistant":
            for block in rec.get("message", {}).get("content", []):
                bt = block.get("type")
                if bt == "text":
                    emit(Event("assistant", {"text": block.get("text", "")}))
                elif bt == "thinking":
                    emit(Event("thinking", {"text": block.get("thinking", "")}))
                elif bt == "tool_use":
                    inp = block.get("input", {})
                    emit(Event("tool_use", {"name": block.get("name"), "input": inp}))
                    if capture_nested:
                        cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                        m = _RESUME_RE.search(cmd or "")
                        if m:
                            res.chosen_session = m.group(1)
        elif rtype == "user":
            for block in _as_list(rec.get("message", {}).get("content")):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = _flatten(block.get("content"))
                    emit(Event("tool_result", {"text": text[:8000]}))
                    if capture_nested:
                        self._absorb_nested(text, res)
        elif rtype == "rate_limit_event":
            emit(Event("log", {"rate_limit": rec.get("rate_limit_info")}))
        elif rtype == "result":
            text = rec.get("result", "") or ""
            res.result = text
            res.cost_usd = rec.get("total_cost_usd")
            res.ok = not rec.get("is_error", False)
            m = _ARROW_RE.search(text)
            if m and capture_nested:
                if m.group(1) != "NEW":
                    res.chosen_session = res.chosen_session or m.group(1)
                res.forked_session = res.forked_session or m.group(2)
            emit(Event("result", {"text": text, "cost_usd": res.cost_usd,
                                  "chosen_session": res.chosen_session,
                                  "forked_session": res.forked_session,
                                  "is_error": rec.get("is_error", False)}))

    def _absorb_nested(self, text: str, res: RunResult):
        """Pull the forked session id (and real answer) out of the nested
        `claude ... --output-format json` output embedded in a Bash tool result."""
        obj = _find_json_with(text, "session_id")
        if not obj:
            return
        res.forked_session = res.forked_session or obj.get("session_id")
        if obj.get("result"):
            res.result = obj["result"]
        if obj.get("total_cost_usd") is not None:
            res.cost_usd = (res.cost_usd or 0) + obj["total_cost_usd"]


def _kill(proc: subprocess.Popen):
    # reap the whole tree (dispatcher + any nested/forked agent), not just the group
    _kill_group(proc)


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text", "") if b.get("type") == "text" else json.dumps(b))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _find_json_with(text: str, key: str) -> dict | None:
    """Find the last top-level JSON object in text that contains `key`."""
    found = None
    for m in re.finditer(r"\{", text):
        start = m.start()
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start:i + 1]
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        pass
                    else:
                        if isinstance(obj, dict) and key in obj:
                            found = obj
                    break
    return found


def _run_json(args, cwd, timeout) -> dict | None:
    # timeout <= 0 -> no limit
    timeout = timeout if timeout and timeout > 0 else None
    try:
        out = subprocess.run(
            args, cwd=cwd, text=True, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def _parse_structured(envelope: dict | None) -> dict | None:
    if not envelope:
        return None
    result = envelope.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return _find_json_with(result, "start_fresh")
    return None
