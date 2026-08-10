# Todo

One file per fix, worked through in order. Each states the problem, the
evidence it is real, and the proposed change — so the fix can be argued with
before it is written.

Findings are from an empirical session against Claude Code **2.1.226** on
2026-08-09; the experiments are described inline in each item.

| # | Item | Severity | Status |
|---|---|---|---|
| [01](01-correct-mid-turn-steering-claims.md) | Docs claim `fork=false` queues into a live turn; it does not | high | **done** |
| [02](02-mid-turn-steering-or-liveness-gate.md) | No mid-turn steering, and no liveness gate either | high | **done** |
| [03](03-direct-mode-resumes-in-wrong-cwd.md) | `direct` mode resumes a pinned session in the caller's cwd | high | open — small API question |
| [04](04-session-index-cwd-is-sort-only.md) | `cwd` only sorts the index; the parse window truncates before it | medium | open |
| [05](05-session-index-hygiene.md) | Index advertises non-resumable ids and empty stub sessions | low | open |
| [06](06-agent-first-cli-api.md) | Make the CLI contract agent-first | medium | **done** (0.3.0) |
| [07](07-backend-api-contract.md) | Harden backend machine contract and lifecycle | high | **done** (0.3.0) |

01 and 02 were the same subject split by cost and shipped together: the docs
correction plus the behaviour behind it — a liveness gate on `fork=false`, and
`POST /v1/jobs/{id}/steer` for reaching a turn already running.
