# 08 — Resume handles, readable timestamps, and reading a log from the end

**Severity:** medium-high (all three are daily friction for an agent caller)
**Status:** **done** — all three parts implemented, 79-test suite green
**Scope:** `gateway/api_models.py`, `gateway/db.py`, `gateway/server.py`,
`client/ab.py`, `client/abclient.py`, tests, and the agent-facing docs.

Three independent problems that share one cause: the API is shaped for the
process that *writes* the data rather than the model that *reads* it. Each part
below stands alone and can ship alone.

---

## Part 1 — The resume handle is not where the caller needs it

### Problem

Continuing a session is the behaviour both skills push hardest ("continue the
work that exists; starting fresh is the last resort"), and it is the one thing
submit does not help with.

`JobAccepted` (`api_models.py:128`) carries `id, status, agent, cwd, title,
fork, include_thinking, files, replayed` — **no session field of any kind.**
Not even an echo of `requested_session`, which the caller just supplied and the
server already validated. So a model that pins a session gets nothing back it
could chain on, and a model that starts a fresh one has no idea what to do next.

Then the job row offers **three** session fields with no statement of which to
pass back:

```
requested_session   what the caller asked for
chosen_session      what routing selected
forked_session      the id the run actually wrote
```

Nothing in the contract says "this is the one you hand to `--session`". Measured
in this session under `direct` dispatch: a fresh job returns
`chosen=None forked=<new id>`; an in-place resume of X returns
`chosen=X forked=X`. So `forked_session` is right in every case — but the model
has to infer that from three similarly-named fields, and inferring wrong means
either a lost thread or a write to the wrong one.

### Why submit genuinely cannot always answer

For a fresh or forked job the session id does not exist yet. It first appears in
the agent's `init` record, a second or two into the run, which the adapter
already captures (`claude.py` `_handle_record`, the `system`/`init` branch). So
`JobAccepted` cannot promise an id — but it can stop being silent about it.

### Proposed

**A single canonical field, named for its use.** Add `session` to
`JobSummary`/`JobDetail`, resolved server-side as
`forked_session or chosen_session or requested_session`. Keep all three
existing fields for compatibility; `session` is the documented answer to "what
do I pass to `--session` next time".

**Make submit say what it knows.** Add to `JobAccepted`:

```jsonc
{ "session": "3cf736d5-…" | null,
  "session_state": "pinned" | "pending" }
```

`pinned` when the caller named one (echo it — zero cost, removes a round trip
for the whole follow-up/steer path). `pending` when the run will create one,
which tells the model to read `.session` off the job row rather than wonder.

**CLI.** `ab submit` keeps the bare job id on stdout — that contract is load
bearing. Add `session=<id>` or `session=pending` to the stderr metadata line,
alongside the existing `title=`.

**Decided: submit awaits the session by default.** `ab submit` blocks only until
the session id is known — the agent's `init` record, not job completion — and
prints it. `--no-wait` restores the old instant return. Rationale: the id is
what makes the next call possible, and a default that omits it means every
agent pays a discover-the-session round trip it will almost always want.

Both adapters can satisfy this. `claude` emits `session_id` on its `init`
status record; `opencode` emits it on `step_start` (`opencode.py:220`), both
early in the stream rather than at the end.

The waiting lives **client-side**, polling the job row — no new endpoint, so 07's
"waiting is a client composition" holds.

Four cases the default has to survive, none of which may hang:

| Case | Behaviour |
|---|---|
| id arrives | print it; `session_state: "ready"` |
| job dies before `init` (bad model, missing binary) | stop as soon as the row goes terminal; report `session_state: "failed"` and the job's error |
| job still queued behind others (`concurrency` full) | keep waiting until the timeout — a queued job has no session yet and that is not an error |
| timeout (`--await-timeout`, default 30s) | `session_state: "pending"`, print the job id, **exit 0** |

**Timeout is not a failure.** The submission succeeded and the job is running;
the session is an enhancement on top. Exit 4 would be wrong here and would break
`id=$(ab submit …)` for callers that check status. The bare job id stays on
stdout in every case — that contract is load bearing — with the session on
stderr in human mode and in the document under `--output json`.

---

## Part 2 — Timestamps are epoch floats

### Problem

Every public timestamp is a bare float:

- `EventRecord.ts` (`api_models.py:148`)
- `JobSummary.created_at`, `started_at`, `finished_at`, `last_event_at`

`1786394450.1806405` is not readable by a model without arithmetic, and
arithmetic it cannot check. Worse, the number a model most often wants is not
the wall clock at all but **position within the run** — "was this before or
after the steer landed", "how long was that tool call". Today that is two
subtractions against a value it has to find first.

The human view already renders `HH:MM:SS` locally (`ab.py` `_ts`), so this is
specifically a machine-output gap — the `--output json` path an agent uses.

### Proposed

**Superseded: ISO replaces the floats rather than accompanying them.** This part
shipped first as `*_iso` siblings, on the belief that the floats were cursors.
They are not -- `next_after` and `Last-Event-ID` are `seq`-based, and job
pagination hides `created_at` in an opaque cursor -- so nothing a caller reads
needed the number, and publishing both left a float as the first thing a reader
saw. Every published timestamp is now an ISO string in place, including two this
section missed: session `last_active` and file `mtime`, plus the `ts` inside an
`ab-notify` report payload.

**Decided: local time, with the UTC offset attached** — e.g.
`2026-08-10T16:07:23.451-05:00`, not a `Z`-suffixed UTC rendering. Local is the
clock a reader is already holding when they correlate a job against an sbatch
log or a terminal scrollback, and the explicit offset keeps it unambiguous and
still lexically comparable within one zone.

Worth documenting rather than discovering: these are rendered server-side, so
"local" means **the gateway's** timezone. For a gateway reached over an SSH
forward from another zone, that is deliberately the host doing the work — and
the offset says which zone it was, so nothing is guessable-but-wrong.

**Also add `elapsed` to `EventRecord`** — seconds since that job's first event,
as a number, plus `elapsed_hms` (`+00:01:23`). This is the field that actually
answers "where in the run", and it is derivable only if you have already
fetched event #1.

Human output keeps local time: someone correlating against an sbatch log wants
their own clock, and that view is not what a model parses.

---

## Part 3 — You can only read the log from the top

### Problem

`EventsPage` reports `next_after` and `has_more` but **no total**, and
`events_after` (`db.py:463`) is forward-only:

```sql
SELECT seq,ts,type,data FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?
```

The route defaults to `after=0` (`server.py:490`). So the default read starts at
the **beginning** of the log, and the most interesting part of a run — what it
just did, why it stopped, the result — is at the end. A model wanting the end
has to either pull the whole log or page forward blind, because nothing tells it
how far the end is. There is no `COUNT(*)` on events anywhere in `db.py`.

`--follow` inherits the same default: with no `--after` it starts at seq 0 and
replays the entire job before streaming, which floods an agent's context on
exactly the long-running jobs follow exists for.

### Proposed

**Tell the caller the shape of the log.** Add to `EventsPage`:

```jsonc
{ "total": 412, "first_seq": 1, "last_seq": 412, … }
```

One `COUNT(*)` and a `MIN/MAX` per request, on an already-indexed
`(job_id, seq)`. With this alone a model can compute its own window instead of
probing for the end.

**Add a real tail.** `GET …/events?tail=N` → the last N events. In SQL,
`ORDER BY seq DESC LIMIT N` then re-sort ascending so the returned page stays in
chronological order (the response shape must not change with the access path).
CLI: `--tail N`.

**Make `--tail` the default for non-follow reads.** `ab events REF` with no
paging flags returns the last 50 rather than the first 500. This is what the
model wants almost every time it looks at a finished or wedged job.

> **This is a breaking change to a default**, and the only one in this
> document. `--after 0` restores the old behaviour explicitly, and `total`
> makes the truncation visible rather than silent. It should be called out in
> the changelog and in both skills, not slipped in.

**Fix follow's starting point.** `--follow` should prime from the tail and then
stream — default `--tail 20`, with `--after 0 --follow` for a full replay. A
follow that begins by replaying 400 historical events is not a follow.

**Flag interactions:**

| Combination | Behaviour |
|---|---|
| `--tail N` + `--until U` | last N events at or before U — a bounded window from the right |
| `--tail N` + `--after A` | **invocation error** (exit 2). Anchoring from both ends at once has no single sensible reading; make the caller choose |
| `--tail N` + `--type T` | last N events *of that type*, filtered before the limit — otherwise `--type result --tail 5` returns nothing on a long job |

That last row is the subtle one: filtering must happen inside the tail query,
not after it, or the flags silently fight.

**One caveat to document.** `--output json` is faithful (07 made it so
deliberately), so 50 tailed events including large `tool_result` bodies is a lot
of context. The mitigation is already there — `--type` to narrow, `--until`/
`--after` to bound — and the docs should point at it rather than the tail
default quietly getting smaller.

---

## Verification

- **Part 1:** submit with a pinned session returns it as `pinned` without
  waiting; a fresh submit waits and returns `ready` with the new id; `session`
  equals `forked_session` across fresh, fork, and in-place resume; `--no-wait`
  returns immediately with `pending`; a submit whose job fails before `init`
  stops at the terminal row rather than waiting out the timeout; a timeout
  exits 0 with the job id still on stdout.
- **Part 2:** every float timestamp has an ISO sibling that round-trips to the
  same instant; `elapsed` of the first event is 0; ISO values sort in seq order.
- **Part 3:** `total` matches a direct count; `tail=N` returns the same rows as
  reading to the end with `after`, in the same order; `tail` + `type` filters
  before limiting; `tail` + `after` is rejected; follow with no flags emits no
  historical event older than the tail window.

## Non-goals

- No change to the seq allocator or cursor semantics — `after`/`Last-Event-ID`
  keep working exactly as they do, and `tail` is a read path, not a new cursor.
- No new waiting endpoint. Submit's session wait is a client composition over
  the existing job row, exactly as `run` and `wait` already are.
- Not touching the three legacy session fields. `session` is added beside them.
