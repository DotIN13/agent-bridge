# 26 — Wake the delegate for its report

**Shipped:** on `feat/report-reminder`, after 0.3.0 — uses [17](17-a-job-is-a-turn-and-a-report.md)'s
liveness, which nothing was using
**Scope:** `gateway/worker.py`, `gateway/config.py`, `config.example.toml`,
`tests/backend/test_waiting.py`, `README.md`, `API.md`.

## What changed, and why

17 made a job finish only when its turn ends **and** `report.md` exists, and it
holds the agent process alive through the `waiting` window on purpose: the
delegate is still there, still steerable, and can still write the file. But
nothing reached for that liveness. A delegate that ended its turn without the
file simply sat for the full grace window and then failed with `report_missing` —
even though it was, at that moment, alive and one message away.

The common failure is not a delegate that refused to report. It is one that did
the work and stopped before writing the file. So the gateway now uses the channel
17 left open and asks:

    Your turn ended, but $AB_JOB_DIR/report.md has not been written.
    Are you finished? If so, write your report there now ...

That is the whole feature: a nudge, not a second deadline and not a new state.

## The mechanics

**At `turn_end`, before `waiting`.** In `_on_turn_end`, a turn that ends with no
report first tries the steer channel. If it lands, the reminder starts a new turn
and the row is left `running` — which is accurate, the delegate is working again —
and the `report_wait_sec` clock starts when *that* turn ends. Only a delegate that
ignores the nudge reaches `waiting`.

**Once per job, ever.** The reminder itself produces a `turn_end`, which would ask
again, and a silent delegate would be nudged in a loop. `_reminded` holds the jobs
already asked and is cleared when the per-job state is released. A second
`turn_end` with no report falls through to `waiting` exactly as before.

**Only where there is a channel.** claude in `direct` mode keeps its stdin pipe
open after the turn, so the message reaches it. opencode's child exits with the
turn (design/18) and the dispatcher modes never emit `turn_end`; for those the
worker applies the same report rule at run end, as it already did. A backend that
reports no channel is never asked, and `steer.send` failing on a broken pipe is
caught and logged rather than treated as a report.

**Configurable.** `[worker] report_reminder` (default `true`). `false` restores
the plain wait, for an operator who would rather the grace window be silent.

## What to be careful with

- **One reminder, not a rate limit.** The guard is a set, not a timer: the second
  turn end writes `waiting` and the deadline does the rest. Adding "retry every N
  minutes" would need the loop broken differently, and nothing has asked for it.
- **The row is `running` while the nudge's turn runs.** That is the point — the
  agent is mid-response — but it means the report sweeper (which only touches
  `waiting` rows) will not close the job during that turn. It closes when the run
  ends, or the row goes `waiting` on the next turn end.
- **The reminder text asks about the file, not the work.** The turn already
  ended; the usual case is a finished delegate. A message that says "keep going"
  would turn every forgotten file into more work.
- **`tests/backend/test_waiting.py`** carries the record: a nudge on a live
  channel, exactly one however many turns end, the report-after-nudge path that
  finishes the job, `report_reminder = false`, and a channel-less backend that
  waits as before.
