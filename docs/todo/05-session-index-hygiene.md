# 05 — Index advertises non-resumable ids and empty stub sessions

**Severity:** low (noise and one unusable id, no data loss)
**Scope:** `gateway/sessions.py`. Pairs naturally with [04](04-session-index-cwd-is-sort-only.md).

## Problem

Two classes of junk reach `/v1/sessions`, both from
`gateway/sessions.py:119-131` globbing `*/*.jsonl` indiscriminately.

**1. Orphaned transcripts yield ids that cannot be resumed.**
Claude Code renames abandoned transcripts to
`<uuid>.orphaned-<epoch>-<hash>.jsonl`. `sessions.py:101` takes `path.stem`
as the session id, which for those files is the whole
`<uuid>.orphaned-<epoch>-<hash>` string. Passing it to `--session` cannot work.

**2. Metadata-only stubs are listed as sessions.**
Files containing nothing but `{"type":"ai-title"}` / `{"type":"agent-name"}`
records parse to `messages: 0`, `title: "(no prompt captured)"`, `cwd: ""`.
They are not sessions in any useful sense, and each one consumes a slot in the
`limit` cap that a real session would otherwise fill.

## Evidence

`scan(limit=40)` against the live store:

```
transcript files on disk:        87
scan(limit=40) returned:         40
non-resumable ids advertised:     2
    ee846723-fe31-4442-a998-ec4e45dfbfb3.orphaned-1786173485302-5878b540
    c1f230fd-34f8-44e9-b125-aaddb71173fa.orphaned-1786036457429-ec47e51d
zero-message rows advertised:     7
```

So **9 of 40 rows** — nearly a quarter of the index the client reads before
every submit — are unusable.

## Fix

In `scan()`:

- Skip files whose stem is not a bare uuid. That drops `.orphaned-*` outright.
  (Alternative: recover the leading uuid and resume that. Worth a moment's
  thought — an orphaned transcript may still hold context someone wants — but
  the id under it usually has a live file of its own, so listing both would
  just duplicate. Default to skipping.)
- Skip `messages == 0` rows. A stub with no user or assistant record has
  nothing to resume into.

Both filters must be applied **before** the `limit` truncation, or they free no
slots — which is the point.

Worth checking at the same time whether `path.stem` has any other shapes in the
wild (sidechain/subagent transcripts, `.backup`, etc.); a positive uuid match
handles all of them at once and is the more durable rule.

## Files

- `gateway/sessions.py:100-110` (`_scan_file`), `:113-141` (`scan`)
