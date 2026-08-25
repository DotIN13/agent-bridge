# Todo — open work

Only unfinished items live here. Shipped work moves to [`../design/`](../design/),
which keeps the reasoning behind behaviour that already exists.

Each file states the problem, the evidence it is real, and the proposed change,
so the fix can be argued with before it is written. Numbers are historical
identity and are never reused or renumbered — commit messages and the design
records refer to them.

| # | Item | Severity | Blocked on |
|---|---|---|---|
| [09](09-steer-vs-resume-is-a-race.md) | Steer-vs-resume is a race the caller has to arbitrate | medium | a design decision |
| [12](12-fleet-drift-and-self-update.md) | Five deployments drift silently; three have no update path | high | nothing — staged, phase 1 is standalone |

## Suggested order

**12 first, and only its first phase.** The version string has not moved since
2026-08-10 and `main` is 28 commits past it, so every gateway in the fleet
reports `0.3.0` and two of them demonstrably run different code. Phase 1 —
stamp the commit, serve it, and add `ab fleet` — is a day's work, carries no
operational risk, and is what turns "probably stale" into a number. The
self-updating parts behind it can wait for that number to say how badly they
are needed.

**09 is blocked on a decision rather than on work.** It is friction and a lost
race, not corruption; the guards added in
[design/02](../design/02-mid-turn-steering-or-liveness-gate.md) keep it safe, and
"do nothing, document the pairing" is a defensible end state. Worth taking when
the shape of the CLI is being revisited anyway.

## A note on triage

The last two items to ship were both written up wrongly, in opposite directions.
[05](../design/05-session-index-hygiene.md) was filed "low, cosmetic, no data
loss" and could drop an entire directory out of the index.
[10](../design/10-untitled-sessions-and-a-slow-dirs-view.md) described a title
bug that did not exist, and all three fixes it proposed turned out to be
near-useless; the real defect was in the filter 05 had just shipped.

Both were caught by measuring before implementing, and in both cases the measuring
took longer than the change. Read the code and the data before believing a
write-up here — including this one.
