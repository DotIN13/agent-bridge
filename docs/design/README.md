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
| [04](04-session-index-cwd-is-sort-only.md) | Session index split into directories + paged sessions; neither can lose a row | after 0.3.0 |
| [05](05-session-index-hygiene.md) | Only sessions with a conversation are listed — subagent stubs could hide a directory | after 0.3.0 |
| [06](06-agent-first-cli-api.md) | Making the CLI contract agent-first: output protocol, exit codes, safe transfers | 0.3.0 |
| [07](07-backend-api-contract.md) | Typed HTTP contract, monotonic events, idempotency, restart recovery | 0.3.0 |
| [08](08-resume-handles-readable-time-and-tailing.md) | Canonical resume handle, local ISO timestamps, reading a log from the end | after 0.3.0 |
| [10](10-untitled-sessions-and-a-slow-dirs-view.md) | Slash-command residue is not a session; the dirs view got ~65x faster | after 0.3.0 |
| [11](11-a-turn-is-not-a-job.md) | `expect_report`: a job that hands work to a scheduler waits for it | after 0.3.0 |
| [15](15-reporting-is-a-directory-and-watching-is-a-monitor.md) | Reporting is a directory; watching is a monitor. `ab-notify` retired | after 0.3.0 |
| [16](16-the-turn-is-the-job.md) | The turn is the job, unconditionally: `expect_report` and the `status` vocabulary removed | after 0.3.0 |
| [17](17-a-job-is-a-turn-and-a-report.md) | A job is a turn *and* a report: the `waiting` state, with the agent still alive | after 0.3.0 |

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

**04, 05 and 10 are one subject in three passes**, and the later two are the
reason to be careful with any of it. 05 was filed as cosmetic and was not: a
folder's directory is read out of its transcripts, subagent stubs record none, so
enough fresh stubs made an entire project resolve to nothing and drop out of both
views. 10 then found that 05's own predicate was too loose — it counted `user`
records, and slash-command wrappers are `user` records.

The rule those three converge on: **a session exists if a human spoke or the
agent acted.** Anything touching how transcripts are filtered should run
`tests/backend/test_session_stubs.py`, which pins each failure by name — including
the custom-slash-command case that no session on the development store exercises.

**17 is 16 with its cost paid.** 16 made the turn the whole of a job, which let a
delegate end cleanly having produced nothing. 17 puts `report.md` into the
definition and adds `waiting` for the gap between the two — a post-turn state
again, but one that waits on a file in a directory the gateway already scans,
with the agent still alive and a 30-minute grace window rather than a day. Its
table comparing `waiting` to 11's `awaiting_report` is the thing to read before
assuming this is the same mistake twice.

**16 ends a default that moved three times**, and 11 → 15 → 16 is worth reading
in order: the turn was the job, then it wasn't and every row waited, then waiting
was opt-in, then the long tail became a monitor and the opt-in had nothing left to
do. 16 also records the one thing this costs — a clean turn whose *work* failed
reads `succeeded` — so it is not rediscovered as a bug.

**15 closes the loop 07 and 11 opened.** 07 kept the two lifecycles apart, 11
merged them behind `expect_report` and then defaulted it on, and 15 reversed the
default while keeping the opt-in — the long tail became a monitor with its own
row instead. Read 11 for why the opt-in existed and why a parked job was
deliberately not `running`; 16 then removed it. 15 also deletes the reason
`ab-notify` existed, which was that a compute node could not be seen; it turned
out the *job* could not be identified either, and `$AB_JOB_ID` was never set by
anything.

**11 partly supersedes 07**, and the pair is worth reading together. 07 decided
a coding-agent job and external compute must not be merged into one status
machine, which was right and is still the default. 11 is the opt-in it
anticipated: `expect_report` parks a job in `awaiting_report` until `ab-notify`
closes it. Note that a parked job is deliberately not `running` -- steering, the
session-busy gate and restart recovery all read `running` as "an agent is alive".

**08 introduced the only breaking default in the set**: `ab events REF` returns
the last 50 rather than the first 500. `--after 0` restores the old reading.
