# 10 — Real sessions labelled "(no prompt captured)", and a slow dirs view

**Severity:** low (both are usability, neither loses a session)
**Scope:** `gateway/sessions.py` — `_scan_file`, `list_dirs`.
Found while measuring [05](../design/05-session-index-hygiene.md); split out to
keep that change to one subject.

Two unrelated problems that live in the same function.

## 1. A session you want, that you cannot recognise

Eleven sessions on the live store are real — 2 to 6 messages each, perfectly
resumable — and every one of them lists as `(no prompt captured)`.

```
391b62bc  messages=3   68555b29  messages=3   5b3f00a5  messages=6
cba07d3a  messages=3   6ac8c256  messages=3   6613ce3b  messages=3
c0c3ce64  messages=5   45f54bfe  messages=3   56d673cb  messages=3
9981cdcd  messages=3   81c3615b  messages=2   <- this one has an ai-title
```

The title comes from the first user message, and `_clean_user_text` strips slash
commands, caveats and tool-result wrappers — correctly, since those are not
prompts. A session that opens with one has nothing left to derive a label from.

This is arguably worse than what 05 fixed. A useless row you can skip; a useful
session you cannot identify is one you will not resume, which defeats the
resume-first policy just as surely as hiding it would.

**Fix, in order of preference:**

- Fall back to Claude's own `ai-title` record. Free, but rescues **1 of the 11**
  — the other 10 have no `ai-title` either, so this alone is not the fix.
- Fall back to the `summary` record, already parsed and already on `SessionInfo`.
- Fall back to the first assistant message, truncated. Always available, and for
  a 3-message session it usually describes the work better than the prompt.

Worth checking what those 11 actually open with before choosing — if they are
all `/resume`-style invocations there may be a better signal in the record.

## 2. `list_dirs` costs ~390 ms to produce 15 titles

Measured on the live store, warm:

```
_resumable (the 05 emptiness filter)   2 ms
_dir_cwd                               1 ms
_scan_file x15                       379 ms   <- all of it
```

`list_dirs` calls `_scan_file` on the newest transcript per directory purely to
read `.title`, and `_scan_file` parses up to 4000 lines to also count messages
and collect a summary that this caller discards. The title comes off the first
user record, around line 3.

This is the **entry point** — the call an agent makes before it knows which
project to ask about — so it is the one place latency is most visible.

**Fix:** give `_scan_file` a cheaper mode, or read the title with a bounded
reader the way `_first_cwd` and `_has_message` already do. Note that the
`messages` count would then be unavailable or wrong, so check who reads it:
`list_dirs` does not, but `scan()` publishes it.

## Files

- `gateway/sessions.py` — `_scan_file` (title derivation, `max_lines=4000`),
  `list_dirs` (the per-directory `_scan_file` call)
