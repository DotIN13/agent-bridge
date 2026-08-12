# 03 — `direct` mode resumes a pinned session in the caller's cwd

**Status: DONE.** A named session's recorded cwd now always wins. See
"What shipped" at the bottom; the rest is the record of why.

**Severity:** high (silent wrong-directory execution)
**Scope:** both adapters. The API question below was dissolved rather than
answered — see the decision note.

## Problem

Resuming by session id is **not** cwd-scoped — that part is fine. But the
resumed turn runs in whatever cwd the worker passes, not the session's own, so
an agent whose entire history is about project X ends up doing its work in
`default_cwd`.

`gateway/adapters/claude.py:176-214` (`_run_direct`) passes `spec.cwd`
straight to `Popen`. `spec.cwd` comes from `gateway/worker.py:99`
(`job["cwd"] or agent_cfg.default_cwd`), and `job["cwd"]` is never null because
`gateway/server.py:185` has already collapsed a missing `cwd`:

```python
cwd = acfg.resolve_cwd(spec.get("cwd"))   # None -> acfg.default_cwd
```

So `ab submit -F task.md --session <uuid>` with no `--cwd` resumes the thread
correctly and then runs it in `D:\dotty-projects\agent-bridge` (or whatever
`default_cwd` is), where its relative paths, `Glob`, and `Bash` all point at
the wrong repo.

The dispatcher modes knew about this — every routing template says
`cd <that session's cwd> && claude --resume …`
(`gateway/adapters/claude.py:70, 87, 102`). `direct` mode, which is now the
default (`gateway/config.py:42`), dropped it.

## Evidence

Resumed `ef78b696-984d-4872-8edc-43babd6e75b8` — which lives under
`~/.claude/projects/D--dotty-projects-molly/`, i.e. cwd `D:\dotty-projects\molly`
— from a shell whose cwd was `D:\dotty-projects\agent-bridge`:

- It resolved and ran fine (so no "session not found" problem, and the
  transcript was appended back into molly's project dir, not a new one).
- The records it appended carry `"cwd":"D:\\dotty-projects\\agent-bridge"` —
  the caller's directory.

## Fix

When the caller pinned a session and did **not** name a cwd, run in that
session's own cwd.

The adapter already has it: `list_sessions()` returns `SessionInfo.cwd`
(`gateway/sessions.py:24-33`). So in `_run_direct`, when
`spec.requested_session` is set and no explicit cwd was given, look the session
up in the index and use its cwd — validated through `AgentConfig.resolve_cwd`
so it still cannot escape `allowed_dirs`.

Also worth adding `--add-dir <session cwd>` in `direct` mode. `_run_agent_exec`
adds every allowed dir (`claude.py:269-274`); `_run_direct` adds nothing but
attachment parents.

**The API question:** the worker currently cannot distinguish "caller passed a
cwd" from "server defaulted it", because `server.py:185` resolves `None` before
storing. Options:

- store the caller's raw `cwd` (nullable) on the job row and resolve later, or
- keep the resolved value but add a `cwd_explicit` boolean, or
- decide a pinned session's cwd **always** wins and an explicit `cwd` alongside
  `--session` is a `400`.

The third is the simplest contract but rejects a legitimate case (fork a
session's context, apply it to a sibling repo). Leaning toward storing the raw
value.

Fallbacks to define: session not in the index (it may have aged out — see
[04](04-session-index-cwd-is-sort-only.md)), or its recorded cwd resolves
outside `allowed_dirs`. Falling back to `default_cwd` reproduces today's bug
quietly; erroring is louder but blocks a resume that would otherwise work.

## Files

- `gateway/adapters/claude.py:176-214`
- `gateway/server.py:184-187, 205-210`
- `gateway/worker.py:96-110`
- `gateway/db.py` (if the row gains a column)
- Same question applies to `gateway/adapters/opencode.py` — check whether
  opencode sessions carry a cwd and whether it has the same gap.

---

## Decision: the recorded cwd always wins

The three options above all existed to answer "did the caller pass a cwd, or did
the server default it?" — a question that needed a new column or a flag to
survive `resolve_cwd` collapsing `None`.

The chosen rule dissolves it: **an existing session with a recorded cwd always
runs there**, over both an explicit `cwd` and the configured default. Nothing
downstream needs to know how `spec.cwd` was arrived at, so no column, no
`cwd_explicit`, no `400` on a legitimate combination.

The cost is that an explicit `--cwd` alongside `--session` is overridden. That is
acceptable only because it is **announced**: a `status` event with
`stage: "cwd"`, naming the directory used and the one replaced. Silently taking a
different directory than the caller named would be the same class of bug as the
one being fixed.

## What shipped

- `sessions.find(session_id)` — a targeted by-id lookup, deliberately **not**
  built on `scan()`. Using the bounded index would mean a session outside the
  window reads as absent and falls back to the default, reproducing this exact
  bug for old sessions (and coupling the fix to [04](04-session-index-cwd-is-sort-only.md)).
- `adapters/base.resume_cwd()` — one resolver both adapters share: validates
  through `AgentConfig.resolve_cwd` so a recorded cwd can never escape
  `allowed_dirs`, emits the `stage: "cwd"` status event on substitution, and
  falls back rather than failing.
- `claude._cwd_for()` and `opencode._cwd_for()`. opencode had the same gap and
  needed its own by-id query (`SELECT directory FROM session WHERE id=?`), plus
  its `--dir` flag and the process cwd kept in agreement.

Three fallbacks, each keeping today's behaviour so the worst case is the bug this
replaces and never a resume that refuses to run: session not found, session with
no recorded cwd, recorded cwd outside `allowed_dirs`.

**Verified live.** Session `ef78b696` (home `D:\dotty-projects\molly`) resumed
with no `--cwd` against a gateway whose `default_cwd` is
`D:\dotty-projects\agent-bridge`:

```
before:  /d/dotty-projects/agent-bridge     <- wrong repo, silently
after:   /d/dotty-projects/molly
seq 3:   status {"stage":"cwd","cwd_source":"session",
                 "cwd":"D:\dotty-projects\molly",
                 "replaced":"D:\dotty-projects\agent-bridge"}
```

A fresh job with no session still uses the requested cwd and emits no
substitution event. Fallbacks and the window-independence of `find` are covered
in `tests/backend/test_resume_cwd.py`.
