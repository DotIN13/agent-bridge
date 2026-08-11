# Design records — shipped

Why the current behaviour is the way it is. These began as items in
[`../todo/`](../todo/) and moved here once implemented, so the reasoning and the
evidence survive the queue being cleared. Numbers keep their original identity;
commit messages refer to them.

Read one of these before changing the behaviour it describes — several record a
decision that looks arbitrary until you know what it was chosen against.

| # | Record | Shipped in |
|---|---|---|
| [01](01-correct-mid-turn-steering-claims.md) | `fork=false` does not queue into a live turn — the docs said it did | with 02 |
| [02](02-mid-turn-steering-or-liveness-gate.md) | Mid-turn steering over streaming stdin, plus a liveness gate on in-place resume | with 01 |
| [03](03-direct-mode-resumes-in-wrong-cwd.md) | A named session's recorded cwd always wins, and the substitution is announced | after 0.3.0 |
| [06](06-agent-first-cli-api.md) | Making the CLI contract agent-first: output protocol, exit codes, safe transfers | 0.3.0 |
| [07](07-backend-api-contract.md) | Typed HTTP contract, monotonic events, idempotency, restart recovery | 0.3.0 |
| [08](08-resume-handles-readable-time-and-tailing.md) | Canonical resume handle, local ISO timestamps, reading a log from the end | after 0.3.0 |

## The load-bearing bits

**01 + 02 were one subject split by cost** — a docs correction that could ship
immediately, and the behaviour behind it. 02 carries the empirical evidence that
`--resume` forks a transcript rather than queuing into it, including the
transcript tree showing the orphaned branch. That experiment is the reason the
gate exists; don't remove it on the assumption that resuming is safe.

**07 decided against new resources** — no synchronous server `run`/`wait`, no
download fan-out. Those are client compositions. It also decided that batch
`message` events are post-terminal annotations rather than a second job status,
which is why an SSE follow closes before external compute finishes.

**03 dissolved its own API question** rather than answering it. All three options
existed to distinguish "the caller passed a cwd" from "the server defaulted it";
letting the session's recorded cwd always win means nothing downstream needs to
know. The override is announced on the event stream, which is what makes
overriding an explicit `--cwd` acceptable rather than another silent substitution.

**08 introduced the only breaking default in the set**: `ab events REF` returns
the last 50 rather than the first 500. `--after 0` restores the old reading.
