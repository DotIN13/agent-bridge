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
| [13](13-delegation-techniques-from-claude-code.md) | Delegation techniques from Claude Code: no role axis, no write isolation | medium | nothing — phase 1 shipped as design/15; worktree isolation is next |
| [14](14-the-prompt-contract-both-ways.md) | The prompt contract, both ways: what a brief must carry, what a report must state, and where that text lives | medium | nothing — client skill done, worker half open |

## Suggested order

**12 first, and only its first phase.** The version string has not moved since
2026-08-10 and `main` is 28 commits past it, so every gateway in the fleet
reports `0.3.0` and two of them demonstrably run different code. Phase 1 —
stamp the commit, serve it, and add `ab fleet` — is a day's work, carries no
operational risk, and is what turns "probably stale" into a number. The
self-updating parts behind it can wait for that number to say how badly they
are needed.

**Then 13's phase 2, now that phase 1 has shipped.** The job-id defect is gone —
a job reports through a directory it is handed
([design/15](../design/15-reporting-is-a-directory-and-watching-is-a-monitor.md)) —
but the other defect in that study is untouched: two jobs with the same cwd edit
one checkout with no isolation and no mutual awareness, because claiming is
per-session. The profiles work behind it is a design question and can wait.

**14's worker half is what remains there.** The preamble exists now and states
the facts a delegate cannot otherwise know; what is still only in a skill, on a
host that may not have it installed, is the report contract — failures in the
first sentence, a claim of done resting on observed output. Decide whether the
preamble becomes the authoritative copy before adding a third voice to it.

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
