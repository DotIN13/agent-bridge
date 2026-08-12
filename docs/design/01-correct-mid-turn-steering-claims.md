# 01 — Correct the "resuming queues the message" claims

**Status: DONE.** Shipped with [02](02-mid-turn-steering-or-liveness-gate.md).
Both false claims are corrected, and the behaviour they described now exists
under a different name (`ab steer`). What follows is the record of why.

**Severity:** high (the contract told callers to do something lossy)
**Scope:** documentation only. No behaviour change.

## Problem

Two places promise that `fork=false` against a *busy* session is safe and that
the message is queued for the end of the current turn:

- `API.md:159` — "A busy target is fine: resuming queues the message and the
  agent picks it up at the end of the current turn, so the gateway does not
  gate on liveness."
- `client/ab.py:451-452` (`--no-fork` help) — "if that session is mid-turn
  Claude queues the message for the end of the turn"

Neither is true. `claude --resume` is not a queue — it starts a second,
independent agent process on a stale snapshot of the transcript.

## Evidence

Session `7d4244b4-731a-4751-9611-1cf9196536d0`, Claude Code 2.1.226.

Turn A was put on ten sequential blocking `sleep 8` Bash calls. Twelve seconds
in — with zero `result` records on its stream, so provably mid-turn — a second
process ran exactly what the adapter runs for `fork=false`:

```
claude --resume 7d4244b4-… -p "STOP immediately. … Reply with exactly: STEERED"
```

- The steer returned `STEERED` **in 4 seconds, as its own complete turn**,
  while A was still running.
- A finished 80s later: 53 tool calls, final answer `TURN_A_DONE`.
  `grep -c STEERED` over A's event stream: **0**. It never saw the message.
- The transcript **forked into two leaves**:

  ```
  branch point a28816d1
    ├── 3f303f47 "STOP immediately…" → d8a94d2c "STEERED"      (the steer)
    └── 3bdef0c1 tool result …       → 70a5e753 "TURN_A_DONE"  (the real turn)
  ```

- A third `--resume` chained onto `70a5e753` — A's leaf. **The steering message
  is orphaned and permanently invisible to that session.**

So a mid-turn `fork=false` does three bad things at once: the message is not
delivered, a second agent runs concurrently in the same cwd with the same
tools, and one of the two branches loses its writes depending on flush order.

`fork=false` against an **idle** session behaves exactly as documented — a
separate run confirmed it chains cleanly onto the previous turn. Only the
busy-target claim is wrong.

## Fix

Replace both passages with what actually happens.

`API.md` — drop the "queues" sentence and say instead:

> The target must be idle. `--resume` is not a queue: against a session that is
> mid-turn it starts a *second* agent on a stale copy of the history, in the
> same cwd, and the two runs fork the transcript — whichever flushes last wins
> and the other branch is discarded. Wait for the turn to finish, or cancel it.

`client/ab.py` `--no-fork` help — same correction, one sentence:

> Requires `--session`, and the session must be idle: resuming a session that
> is mid-turn does not steer it, it starts a competing run and one of the two
> branches is lost.

Cross-check while in there: `skills/agent-bridge-client/SKILL.md:113-135`
describes the resume-first policy and does **not** repeat the false claim, but
it should gain a line warning that resume-in-place needs an idle target, since
that is the file the remote-driving agent actually reads.

## What shipped

- `API.md` — the "busy target is fine" paragraph replaced with the fork-and-lose
  explanation, the `409` body, and a pointer to `/steer`.
- `client/ab.py` — `--no-fork` help now says the target must be idle and names
  `ab steer` for a running turn.
- `skills/agent-bridge-client/SKILL.md` — rewritten around a four-rung ladder
  (steer → resume in place → fork → fresh) with the idle requirement stated.
- `gateway/docs.py` — same ladder in the rendered `llms.txt`, so an agent
  reading the contract cold gets it too.
- `README.md`, `client/README.md` — same correction wherever the old framing
  appeared.
