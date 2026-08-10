# 02 — Mid-turn steering, or a liveness gate

**Status: DONE — option C, both parts.** What follows is the design record; the
"what shipped" section at the bottom is the summary.

**Severity:** high

## Problem

There is no way to send a message to a running worker, and nothing stops a
caller from trying. See [01](01-correct-mid-turn-steering-claims.md) for the
experiment: a mid-turn `fork=false` silently forks the transcript and loses one
branch.

The server already believes it guards this. `gateway/server.py:195-199`:

```python
if not fork and not spec.get("session"):
    # Without a named target there is nothing to check for liveness
    # before dispatch, so the no-race guarantee cannot be made.
    raise HTTPException(400, "fork=false requires 'session': …")
```

and `gateway/adapters/claude.py:233-236` repeats it — "the worker has to know
the target up front to check that nothing else is mid-turn in it". **That check
does not exist anywhere in the codebase.** The comments describe an intended
invariant that was never implemented, which is why the gap survived review.

Separately, the adapter cannot address a live child even if it wanted to:
`gateway/adapters/claude.py:344-357` builds `Popen` with `stdout=PIPE,
stderr=PIPE` and no `stdin`, so the running `claude` process has no input
channel.

## Options

### A. Liveness gate only (cheap, honest, no new capability)

Before dispatching a `fork=false` job, check whether the target session is
currently being run and reject with `409` if so.

Two sources of truth, and we should probably use both:

1. **Our own jobs** — the worker pool already tracks running jobs
   (`WorkerPool._cancels`, `gateway/worker.py:31`). Add a map from session id →
   running job id, and reject if the target is in it. Catches every
   gateway-initiated run.
2. **Foreign runs** — a session an interactive user is driving by hand is
   invisible to us. Best available signal is transcript mtime plus the absence
   of a terminating record; both are heuristics. Worth deciding whether we care
   or whether "the gateway only guarantees this for its own jobs" is enough.

Cost: small. Does not enable steering; just stops the silent corruption.

### B. Real steering over `--input-format stream-json` (the actual feature)

Claude Code supports streaming input: `--print --input-format stream-json`
reads user messages as JSON lines on stdin, which is the only mechanism that
delivers a message into a turn that is already running.

**Verified working on the CLI (2.1.226).** This is not a theory — the
experiment is below. The Agent SDK's `ClaudeSDKClient` is a wrapper over
exactly this CLI flag pair, so the CLI loses nothing by not using the SDK.

Launched a turn told to make ten sequential blocking `sleep 6` Bash calls, then
wrote a second stream-json user message into the **live** process's stdin
twenty seconds in, mid-turn:

```
[  18.9s] TOOL     Bash sleep 6 && echo step      <- 3rd call, in flight
[  20.0s] MAIN     >>> WRITING STEER INTO LIVE STDIN <<<
[  25.0s] REPLAY   <-- acknowledged: 'STOP. Do not run any more sleep commands…'
[  26.8s] ASSIST   'STEERED'
[  26.8s] RESULT   is_error=False 'STEERED'
=== tool calls made: 3 (of 10 requested) ===
```

The running turn **picked the message up at the next tool boundary** (~5s,
when the in-flight `sleep 6` returned), abandoned the remaining seven calls,
and answered the steer. That is real steering — the same session, the same
turn, no fork, no second process, no lost branch. Contrast with
[01](01-correct-mid-turn-steering-claims.md), where the identical intent via
`--resume` produced two competing agents and discarded one.

`--replay-user-messages` echoes each stdin message back on stdout once
accepted, which gives the gateway a delivery receipt to put in the event log
rather than having to assume the write landed.

**Interrupt works over the same channel**, as a control request rather than a
signal:

```json
{"type":"control_request","request_id":"req-1","request":{"subtype":"interrupt"}}
```

Acknowledged in under 100ms with
`{"type":"control_response","response":{"subtype":"success","request_id":"req-1","response":{"still_queued":[]}}}`,
and the turn ends immediately with `subtype: error_during_execution`. This is
strictly better than the SIGINT escalation in `interrupt_group`
(`gateway/adapters/base.py:59`): in-band, acknowledged, no process-group
signalling, no 15-second grace, and it tells you what was still queued. Worth
considering as the cancel path too, with the signal ladder kept as the fallback
for a child that has stopped reading stdin.

Work required:

- `_run_direct` launches with `--input-format stream-json --output-format
  stream-json`, `stdin=PIPE`, and writes the initial prompt as a stream-json
  user message instead of passing `-p <prompt>`.
- The worker keeps the live stdin handle addressable by job id (alongside the
  existing `Cancellation` in `_cancels`).
- New route `POST /v1/jobs/{id}/steer` writes a user message into that handle,
  or `409` if the job is not running.
- New client verb, e.g. `ab steer <ref> -F note.md`.
- Events: the steer should land in the job's own event log so `ab events`
  shows where it was injected.

Settled by the experiment above: delivery is at the next tool boundary, not at
end of turn, and the running turn genuinely changes course.

Still open:
- What happens to a steer sent to a job whose child has already exited but
  whose row is still `running` (the stale-`running` case the skill warns about
  at `SKILL.md:321`)? Writing to a closed stdin raises `BrokenPipeError` — that
  is probably the cleanest liveness probe we have, and it argues for treating a
  failed steer write as evidence the row is stale.
- A turn that makes no tool calls has no boundary to interrupt at, so a steer
  sent during a long single-shot generation should be expected to land after
  it, not during. Worth confirming before documenting a latency guarantee.
- Whether the initial prompt should still go through `--append-system-prompt`
  for attachments, or move into the first stream-json message.

Cost: moderate, and it changes how every `direct` job is launched — the riskiest
part is that the initial prompt path changes for all jobs, not just steered
ones.

### C. Both

A first as the safety floor, then B as the capability. B does not remove the
need for A: a `fork=false` job targeting a session someone else is running is
still a race, and the gate is what catches it.

## Recommendation

C, in that order: A now, B next.

A is small, removes a silent data-loss path, and makes the two comments quoted
above true instead of aspirational. It should not wait on B.

B is no longer speculative — the mechanism is verified end to end and is the
feature people actually want ("nudge a worker that is going the wrong way").
It also subsumes the awkward parts of the current design: cancel becomes an
acknowledged in-band control request instead of a SIGINT ladder, and a failed
stdin write becomes a real liveness probe. The reason it still goes second is
scope, not doubt — it changes how every `direct` job is launched, so it wants
its own change with its own testing rather than riding along with a `400`.

A stays necessary after B ships. Steering only reaches a job **this gateway is
running**; a `fork=false` job aimed at a session someone else is driving is
still the race from [01](01-correct-mid-turn-steering-claims.md), and the gate
is what catches it.

## What shipped

**A — the gate.** `WorkerPool` now tracks which session each running job
*writes* (`_claimed`): an in-place resume claims its target before dispatch, and
a fork or fresh run claims the id from its init record, so a session created
moments ago is protected too. `POST /v1/jobs` rejects a `fork=false` job aimed
at a claimed session with a `409` that names the holder and its steer URL. The
worker re-checks at dispatch, covering a job that sat in the queue while
something else took the session.

**B — the steering channel.** `Steering` (`gateway/adapters/base.py`) wraps the
child's stdin. `_run_direct` launches with `--input-format stream-json
--output-format stream-json --replay-user-messages` and writes the prompt as the
first message, so every direct job is steerable for the price of one pipe.
`POST /v1/jobs/{id}/steer` finds the handle by job id and writes a user message.
The steer is logged from the agent's *echo* of it, not at the HTTP edge, so the
event stream shows where in the run it actually landed.

One consequence worth remembering: in streaming-input mode the agent stays alive
waiting for more work after it answers, so **closing stdin is what ends the
run**. The adapter closes on the result record and again in its `finally` —
without the second one, an error path would leave `proc.wait()` blocked forever.

Verified end to end against a live gateway: a ten-step job steered at ~20s took
the message at the next tool boundary and stopped after 3 tool calls; the gate
returned its `409` with the right steer URL; the claim released on completion;
and plain unsteered jobs still finish in ~5s with no hang.

**Not done, deliberately:** cancel still goes through the signal ladder. The
control-request interrupt is better in every way that matters *except* that it
needs the child to still be reading stdin, and cancel is exactly the path that
has to work when it isn't. Worth revisiting as a fast path with signals as the
fallback.

## Files

- `gateway/adapters/base.py` — `Steering`, `SteerError`, `JobSpec.steer`
- `gateway/adapters/claude.py` — streaming stdin, close-on-result, `steer` events
- `gateway/worker.py` — `_claimed` / `_steers` registries, `claimant`, `steering`
- `gateway/server.py` — submit gate, `POST /v1/jobs/{id}/steer`
- `client/abclient.py`, `client/ab.py` — `Client.steer`, `ab steer`
- `API.md`, `README.md`, `client/README.md`, `gateway/docs.py`,
  `skills/agent-bridge-client/SKILL.md`
