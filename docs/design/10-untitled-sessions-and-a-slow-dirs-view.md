# 10 — Real sessions labelled "(no prompt captured)", and a slow dirs view

**Severity:** low (both are usability, neither loses a session)
**Scope:** `gateway/sessions.py` — `_scan_file`, `list_dirs`.
Found while measuring [05](05-session-index-hygiene.md); split out to
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

---

## What shipped

Part 2 as described. **Part 1 was the wrong fix to the wrong problem**, and all
three fallbacks proposed above are near-useless — measured before writing any of
them: `ai-title` rescues 1 of 11, `summary` 0, first-assistant 1.

### Those eleven sessions are not real

The premise above — "real, 2 to 6 messages each, perfectly resumable" — is
wrong. Every one contains nothing but slash-command housekeeping:

```
/login   -> "Login successful"
/resume  -> "That session is still running as a background agent."
/clear   -> (nothing)
```

No prompt, no reply, no tool call. `messages` reads 2-6 only because the caveat,
the command and its stdout are each stored as a separate `user` record. Ten of
the eleven are `/resume` or `/login`; the largest assistant contribution across
all of them is 22 characters, "No response requested."

So this is a third flavour of what [05](05-session-index-hygiene.md)
removed, and the predicate that shipped there is too loose: it counts any
`user`/`assistant` record, and a command wrapper is a `user` record. Giving these
a nicer label would have put a title on junk and left them in the index —
actively worse than the apology they displayed, because a labelled row invites a
choice.

`_has_message` therefore became `_has_conversation`: **a human spoke, or the
agent acted** — a `user` record with text surviving `_clean_user_text`, or an
assistant `tool_use`.

The `tool_use` arm is the interesting half. A custom slash command can drive real
work with no human prose at all, and requiring prose would hide it. Measured
first: 0 of the 11 have any tool use, and no session on the store has assistant
work without human text, so the arm changes nothing here. It is in because its
absence would be a false negative, which is the failure this index keeps being
fixed for, and it is pinned by a test since nothing real exercises it.

`(no prompt captured)` fell to **zero** as a side effect. Every surviving session
has a human prompt to name it with, so the title problem dissolved rather than
being solved.

### The dirs view got ~65x faster

`list_dirs` called `_scan_file` per directory purely for `.title`, parsing up to
4000 lines to also count messages and collect a summary it discarded. Replaced
with `_first_title`, a bounded reader in the shape of `_first_cwd`.

| | before | after |
|---|---|---|
| `list_dirs` cold | 404 ms | 22 ms |
| `list_dirs` warm | 388 ms | **6 ms** |
| sessions listed | 96 | 85 |
| `(no prompt captured)` rows | 11 | **0** |
| directories | 15 | 15 |
| dirs with no `latest_title` | — | 0 |
| dirs-vs-sessions mismatches | 0 | 0 |

No directory lost its title to the cheaper reader, and `find()` still resolves
every removed session by explicit id.

### Note on the triage

Filed low, and the severity was right, but the diagnosis was wrong in both
directions: there was no title bug to fix, and there was a filtering bug that
[05](05-session-index-hygiene.md) had already shipped. That is twice in
a row that this queue's write-up has been wrong about the same code — 05 was
called cosmetic and could hide a directory. Read the transcripts before
believing the next one.
