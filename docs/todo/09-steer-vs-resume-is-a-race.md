# 09 — Steer-vs-resume is a race the caller has to arbitrate

**Severity:** medium (friction and a lost race, not corruption)
**Status:** open — needs a design decision, not just work
**Scope:** `gateway/server.py`, `gateway/worker.py`, `client/ab.py`,
`client/abclient.py`, both skills.

## Problem

The two guards added in
[design/02](../design/02-mid-turn-steering-or-liveness-gate.md) point at each
other:

```
POST /v1/jobs/{id}/steer  on a terminal job
  -> 409 job_terminal  "submit a follow-up with 'session' + fork=false instead"

POST /v1/jobs  with fork=false on a claimed session
  -> 409 session_busy  {held_by, steer_ref}   "steer that job instead"
```

Each error's remedy is the other call. That is the signature of a decision the
caller should not be making: **whether to steer or to resume depends on whether
a turn is still running, which can change between the check and the call.** A
caller reads `ab jobs`, sees `running`, calls `ab steer`, and loses if the turn
ended in the gap — then has to translate the 409 into the other call itself.

The state that settles it lives in `WorkerPool._claimed`, on the gateway. So does
the mapping from a session id to the job currently holding it, which the caller
otherwise has to reconstruct.

Nothing here is unsafe — the guards make sure of that. It is a round trip and a
branch that the gateway is better placed to take.

## What is *not* the problem

Rung 2 versus rung 3 — resume in place or fork — is **intent**, not state. No
amount of server-side knowledge infers whether the caller wants a branch. That
stays an explicit flag. Only the steer-vs-resume seam is racy, and only it should
collapse.

## Sketch

One verb that takes a destination and text, and routes on state the gateway
already holds. `ab send <ref>` where `<ref>` is a job **or** a session id:

| `<ref>` resolves to | Action |
|---|---|
| *(omitted)* | create a job, fresh session |
| a running, steerable job | steer it |
| a session claimed by a running job | **steer the holder** — today's `session_busy` |
| an idle session | create a job; `--fork` chooses branch vs in place |
| a terminal job | resolve to the session it wrote, then as above |

Two properties make it worth doing server-side rather than as a client loop:

- **Race-free in both directions.** A `SteerError` mid-dispatch falls through to
  the create path; the create path reserves the session claim under the pool lock
  instead of discovering the collision one step later.
- **`action` in the response** (`created` | `steered`), because auto-routing is
  only acceptable if the caller can see which way it went.

Collapsing job refs and session ids into one address space also removes a
papercut: continuing a finished job means reading `session` off the row and
passing it back. Collapsing the three session columns into that one field
already did most of this — the remaining step is not having to read the row at
all.

## The decision this needs

[design/07](../design/07-backend-api-contract.md) deliberately kept the resource
set small — *"no synchronous server `run`/`wait`; those are client
compositions"* — and an earlier version of this proposal was dropped for exactly
that reason. A new `POST /v1/messages` cuts against it.

Three ways to reconcile, in increasing cost:

1. **Do nothing; document the pairing.** Both skills already say "follow the
   pointer" on a 409. Cheapest, and the race stays the caller's.
2. **Client-side `ab send`.** Ergonomics without a new endpoint, but it
   reintroduces the TOCTOU it exists to remove — the window just moves into the
   client.
3. **Server-side routing.** Actually race-free, at the cost of the resource that
   07 declined.

Worth noting that (1) is a defensible end state. The friction is small and the
guards are correct; this item exists so the trade-off is on the record rather
than rediscovered.

## Verification, if built

`--tail`-style flag conflicts aside: a steer racing a turn's end lands as a
follow-up rather than a 409; an in-place submit racing a claim is either routed
to the holder or reserved, never failed at dispatch; `action` reports the branch
taken; and the three legacy session fields keep working.
