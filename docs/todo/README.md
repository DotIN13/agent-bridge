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
| [10](10-untitled-sessions-and-a-slow-dirs-view.md) | Real sessions listed as "(no prompt captured)"; `list_dirs` costs 390 ms | low | nothing |

## Suggested order

**09 when the shape of the CLI is being revisited anyway.** It is friction and a
lost race, not corruption; the guards added in
[design/02](../design/02-mid-turn-steering-or-liveness-gate.md) keep it safe, and
"do nothing, document the pairing" is a defensible end state.

**10 is two small things in one function**, split out of
[design/05](../design/05-session-index-hygiene.md) to keep that change to one
subject. The title half matters more than it sounds: a session nobody can
recognise is one nobody resumes, which defeats the resume-first policy as surely
as hiding it would.

**A note on severity.** 05 shipped having been filed "low, no data loss" and
turned out to be able to drop an entire directory from the index. Both items
here were triaged the same way, from the same kind of quick read. Measure before
believing either.
