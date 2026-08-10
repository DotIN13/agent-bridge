# 04 — `cwd` only sorts the session index, and the parse window truncates first

**Severity:** medium (not yet biting on the current store; will bite on a busy host)
**Scope:** `gateway/sessions.py` + one API decision.

## Problem

`GET /v1/sessions?cwd=…` is how a client picks a session to resume, and it is
the input to the whole resume-first policy in
`skills/agent-bridge-client/SKILL.md:113-135`. It does not return all sessions,
and `cwd` does not do what its name suggests.

`gateway/sessions.py:113-141`:

1. Glob every transcript, sort by mtime globally.
2. Parse only `files[:max(limit*3, limit)]` — a **120-file window** at the
   default `max_sessions_in_index = 40` (`gateway/config.py:48`,
   `config.toml:42`).
3. *Then* apply `cwd_filter`, which is a **sort key, not a filter**
   (`sessions.py:134-138`) — matches float to the top, everything else still
   follows.
4. Truncate to `limit`.

Because the window is applied before the cwd ranking, a session in the
requested cwd that is not among the 120 globally-most-recent is invisible. On
midway3, where the gateway's own job sessions churn constantly, recent noise is
exactly what fills that window.

The docstring at `sessions.py:126-127` claims the `limit*3` superset exists
"so cwd_filter can reorder without missing recent matches" — it bounds the
miss, it does not prevent it.

## Evidence

Measured against the live store (87 transcripts, 22 under
`D:\dotty-projects\molly`), shrinking the cap to simulate a busier host:

```
index cap=40  window=120 files -> 40 rows, 22 of 22 molly sessions visible
index cap=20  window= 60 files -> 20 rows, 15 of 22 visible
index cap=10  window= 30 files -> 10 rows,  1 of 22 visible
index cap= 5  window= 15 files ->  5 rows,  1 of 22 visible
```

Also confirmed at the default cap: `scan(cwd_filter="D:\dotty-projects\molly")`
returns 40 rows of which **18 are from other directories** — the filter does
not filter.

Nothing is lost on this machine today (87 < 120). The failure mode is a busy
host, where the client asks for its project's sessions, gets a page of
unrelated ones, and concludes nothing relevant exists — quietly defeating the
resume-first policy and starting a fresh session instead.

## Fix

Filter before windowing, not after:

- When `cwd` is given, treat it as an actual filter and size the window against
  the *matching* set, not the global one. The project-dir slug encodes the cwd
  (`~/.claude/projects/<slugified-cwd>/`), so candidate directories can be
  narrowed by name before any file is parsed — cheap, and it removes the need
  to parse 120 unrelated transcripts on every request.
- Keep a way to see across boundaries, since the current lenient behaviour is
  deliberate (`sessions.py:117-118`: "others still follow so the dispatcher can
  cross a boundary"). Either a separate `strict=1`/`all=1` parameter, or return
  matches first and mark them, so the caller can tell which rows satisfied the
  filter.

**Decision needed:** is `cwd` a filter with an opt-out, or a preference with an
opt-in to strictness? Making it a filter is the least surprising reading of the
parameter name and of `ab sessions --cwd`, but it is a behaviour change for the
`agent_exec` dispatcher, which relies on being shown out-of-tree sessions.

While in here, consider caching. `_scan_file` reads up to 4000 lines of each of
120 transcripts on **every** `/v1/sessions` call (`sessions.py:64-110`) with no
memoisation; an mtime-keyed cache would make the endpoint cheap enough to call
before every submit, which is what the skill instructs.

## Files

- `gateway/sessions.py:113-152`
- `gateway/server.py:164-170` (if a new query parameter appears)
- `gateway/adapters/claude.py:145-162`
- `client/ab.py:192-209`, `API.md` (§`GET /v1/sessions`)
