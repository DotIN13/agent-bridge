# Todo — open work

Only unfinished items live here. Shipped work moves to [`../design/`](../design/),
which keeps the reasoning behind behaviour that already exists.

Each file states the problem, the evidence it is real, and the proposed change,
so the fix can be argued with before it is written. Numbers are historical
identity and are never reused or renumbered — commit messages and the design
records refer to them.

| # | Item | Severity | Blocked on |
|---|---|---|---|
| [03](03-direct-mode-resumes-in-wrong-cwd.md) | `direct` mode resumes a pinned session in the caller's cwd | high | a small API question |
| [04](04-session-index-cwd-is-sort-only.md) | `cwd` only sorts the session index; the parse window truncates before it | medium | nothing |
| [05](05-session-index-hygiene.md) | Index advertises non-resumable ids and empty stub sessions | low | nothing |
| [09](09-steer-vs-resume-is-a-race.md) | Steer-vs-resume is a race the caller has to arbitrate | medium | a design decision |

## Suggested order

**03 first.** It is the only one that silently does the wrong thing rather than
merely showing less than it could, and its blast radius grew when the production
gateway took `C:\Users\tiger` into `allowed_dirs`: a job pinning a session whose
real cwd was elsewhere runs the agent against the wrong tree.

**04 next, and sooner than its severity suggests.** It is latent only because the
store is still under the window. Measured 2026-08-11: **110 transcripts against a
120-file pre-parse window.** Past that, sessions under the requested `cwd` start
disappearing from the index, which quietly defeats the resume-first policy both
skills push hardest — and the failure looks like "no relevant session exists".

**09 when the shape of the CLI is being revisited anyway.** It is friction and a
lost race, not corruption; the guards added in
[design/02](../design/02-mid-turn-steering-or-liveness-gate.md) keep it safe.

**05 last.** Real but currently latent — the orphaned transcripts on disk have
aged out of the window, so exposure today is 0 unusable ids and 3 empty rows.
It returns whenever a recent transcript gets orphaned.
