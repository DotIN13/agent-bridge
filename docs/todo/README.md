# Todo — open work

Only unfinished items live here. Shipped work moves to [`../design/`](../design/),
which keeps the reasoning behind behaviour that already exists.

Each file states the problem, the evidence it is real, and the proposed change,
so the fix can be argued with before it is written. Numbers are historical
identity and are never reused or renumbered — commit messages and the design
records refer to them.

| # | Item | Severity | Blocked on |
|---|---|---|---|
| [05](05-session-index-hygiene.md) | Index advertises non-resumable ids and empty stub sessions | low | nothing |
| [09](09-steer-vs-resume-is-a-race.md) | Steer-vs-resume is a race the caller has to arbitrate | medium | a design decision |

## Suggested order

**09 when the shape of the CLI is being revisited anyway.** It is friction and a
lost race, not corruption; the guards added in
[design/02](../design/02-mid-turn-steering-or-liveness-gate.md) keep it safe, and
"do nothing, document the pairing" is a defensible end state.

**05 is mostly closed already.** Unresumable `.orphaned-*` ids are filtered out
by [design/04](../design/04-session-index-cwd-is-sort-only.md), and pagination
removed the scarce 40-row window that made empty stub sessions harmful. What
remains is cosmetic: stubs still appear as rows with no messages.
