# 05 — Index advertises non-resumable ids and empty stub sessions

**Severity:** filed low (noise and one unusable id, no data loss) — **wrong, see
"What shipped".** The stubs turned out to be able to hide entire directories.
**Scope:** `gateway/sessions.py`, `gateway/adapters/opencode.py`. Pairs naturally
with [04](04-session-index-cwd-is-sort-only.md).

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

---

## What shipped

Both filters, in both backends, applied before the limit and before `total`.
Three things the write-up above got wrong, all found by measuring first.

### The stubs are subagent transcripts, and they can hide a whole directory

Claude Code writes a transcript for every subagent but records the subagent's
turns inline in its *parent*, so the child file keeps only `ai-title` /
`agent-name`. All ten carried `agent-name`, with `agentName` equal to the title.

Filed as cosmetic. It is not, because of how a folder's directory is resolved:
`_dir_cwd` reads the recorded `cwd` out of the folder's newest few transcripts,
and **a stub records no cwd**. Enough fresh subagent stubs on top of the real
work and the folder resolves to no directory at all — every session in it leaves
both views together. Subagents produce stubs constantly, so this was a live trap
rather than a hypothetical one; a directory with 22 real sessions needed only
three new stubs to vanish.

`_dir_cwd` now probes only transcripts that have a conversation, which is the
same predicate the listings use. Regression test:
`test_stubs_piled_on_top_cannot_hide_a_whole_directory`.

### Filtering exactly is affordable; the reason to fear it was wrong

The obvious objection is that emptiness cannot be known without parsing, and
parsing everything is what [04](04-session-index-cwd-is-sort-only.md)
went out of its way to avoid. Measured instead of assumed: **12 ms for all 113
transcripts across 573 MB**, because the first `user`/`assistant` record lands on
line 3 (worst case 21), so the early exit fires immediately. Metadata-only files
are read whole and are under 1.5 KB.

That settles where the filter goes. It must run **before** the limit — filtering
a page after slicing it frees no slots and returns short pages — and cheapness is
what makes that possible. It also means `total` and the per-directory counts can
be exact rather than approximate, so the dirs view and the sessions view agree
everywhere. Results are cached against mtime, so a stub that later gains messages
starts being listed.

### opencode had it too, and the fix nearly caused the bug it was fixing

3 of 1522 sessions have no messages; one directory held nothing else and was
being advertised as somewhere to continue work.

The database has **two** plausibly-named tables: `message` (50,564 rows) and
`session_message` (**0 rows**, a newer schema not yet in use). Correlating
against the latter would have hidden all 1522 sessions instead of 3 — a far
larger version of the silent loss this item exists to remove. The predicate
carries a comment saying so. Index-backed, 4 ms.

### Measured

| | before | after |
|---|---|---|
| Claude sessions listed | 106 | 96 |
| Claude zero-message rows | 10 | 0 |
| opencode sessions | 1522 | 1519 |
| opencode directories | 39 | 38 (`.../hypogum-next/data`, one unused session) |
| dirs-vs-sessions count mismatches | — | 0 of 15 and 0 of 38 |

`find()` is deliberately unchanged and still resolves a stub by id. A listing is
a recommendation and should offer only sessions worth continuing; a lookup by
explicit id is an instruction, and reporting "no such session" about a file that
plainly exists sends the caller somewhere worse.

### Also settled: 04 dropped nothing worth keeping

This write-up wondered whether an orphaned transcript "may still hold context
someone wants". It does not. All six on disk are 246–272 bytes of metadata, and
each has a bare-uuid sibling holding the actual conversation (7, 5, 8, 3 and 1
messages). Dropping them cost nothing — now confirmed rather than assumed.

### Left open

Title quality, split out as [10](10-untitled-sessions-and-a-slow-dirs-view.md):
11 real sessions still read `(no prompt captured)`, and `list_dirs` spends ~390 ms
parsing full transcripts for titles it could read in a few lines.
