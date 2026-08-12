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

---

## Decision: two views instead of one better-ordered list

Reordering the existing operation was the wrong shape. A single flat list was
answering two different questions at once — *"where is there work?"* (you do not
know the directory yet) and *"what is in this project?"* (you do) — and a
globally-windowed sample answers neither, without admitting it.

Split, and each level becomes complete:

- **`GET /v1/session-dirs`** — every directory with sessions. Bounded by how
  many projects exist, not by a window, so nothing can drop out of it.
- **`GET /v1/sessions?cwd=&limit=&cursor=`** — sessions in exactly one
  directory, paged, with a real `total`.

The window is not fixed; it stops existing.

`cwd` is an **exact** match, not a prefix (decided: no recursion), so a count
means what it says and a sub-project keeps its own index. Paging is by opaque
cursor rather than `after=N`, because sessions have no monotonic sequence and
ordering on a timestamp alone would skip or repeat rows on a tie.

## opencode was worse, and was already broken

The original write-up measured only the Claude backend. opencode has the same
two defects over **1,522 sessions in 39 directories** against the same 120-row
window, and one directory holds 867 of them. Measured against the real store
before the fix:

```
  D:/dotty-projects/zimo           on disk 33 | returned 0
  D:/dotty-projects/molly-sachs    on disk 88 | returned 0
  D:/dotty-projects/agent-bridge   on disk  4 | returned 4
```

121 resumable sessions were invisible — permanently, not "once the store grows".
Only the recently-active directory showed up at all. So this was not a latent
risk on that backend; it was silently defeating the resume-first policy on every
call.

## What shipped

Both backends, same two-phase shape — resolve the matching directories, then
take the newest N within them — differing only in how directories are found:

| | Claude Code | opencode |
|---|---|---|
| find directories | folder names (17), cwd read once per folder and cached | `SELECT DISTINCT directory` (39) |
| newest N in them | glob + sort + parse survivors | one `WHERE directory IN (…)` query |

Deliberately **not** un-slugifying folder names to get a cwd, though the mapping
is derivable (`D:\dotty-projects\molly` ⇔ `D--dotty-projects-molly`, every
non-alphanumeric character becoming one `-`). It is undocumented and lossy, and
a wrong rule produces *false negatives* — the same silent loss being fixed.
Reading the recorded `cwd` once per folder cannot.

Paths are compared normalised: Claude records `D:\x`, opencode records `D:/x`,
and Windows differs in case too.

**05 is partly closed as a side effect.** Unresumable `.orphaned-*` stems are
filtered before both the listings and the per-directory counts — advertising a
count that included ids nothing can resume would have been a new lie. The
zero-message half of 05 matters much less now: pagination removed the scarce
40-row window those stubs were competing for.

**Verified.** `zimo` 0 → 33, `molly-sachs` 0 → 88, and cursor walks are complete
and duplicate-free on both backends (88 sessions in 9 pages, 25 in 4). Covered
in `tests/backend/test_session_index.py`.
