# 13 — What Claude Code's own delegation does that this gateway does not

**Severity:** medium overall, with one high item — concurrent jobs in one
checkout have no write isolation, and one job cannot name itself to `ab-notify`.
**Status:** open, partly shipped — phase 1 landed as something better than it
proposed (see [design/15](../design/15-reporting-is-a-directory-and-watching-is-a-monitor.md)):
rather than injecting `AB_JOB_ID`, the job is handed `$AB_JOB_DIR` and needs no
id, url or token at all. Phases 2-6 are open, and phase 2 (worktree isolation)
is now the highest item here
**Scope:** `gateway/adapters/claude.py`, `gateway/adapters/base.py`,
`gateway/config.py`, `gateway/api_models.py`, `gateway/worker.py`,
`client/ab.py`, both skills, `config.example.toml`.

## Why read the other implementation at all

This gateway and Claude Code's Agent tool solve the same problem at different
radii. Claude Code delegates *within* a machine: one session spawns another
against a declared profile, waits or is notified, and gets back one report.
agent-bridge delegates *across* machines: an HTTP job runs a coding agent in a
named session and returns its last message. The transport differs; the hard
parts — what the delegate is allowed to touch, how it is told what it is, how
its work is bounded, how the caller learns it finished — are identical.

We have also already written a delegation router twice. `dispatch_mode`'s two
non-`direct` modes put a Claude session in front of every job to pick a fork
target, and `config.example.toml` now tells operators not to use them:
nondeterministic, a session per job, `--model` silently dropped. That is worth
holding on to while reading what follows, because Claude Code makes almost the
opposite choice at every point where we chose a model in the path.

### Evidence base, and how far to trust it

Measured on this host, 2026-08-29:

```text
$ claude --version
2.1.251 (Claude Code)
```

Three sources, in descending order of durability:

1. `claude --help` and `claude agents --help` — public CLI surface. Safe to
   build against.
2. The Agent tool's own description text, as delivered to the model in this
   session. Stable in intent, reworded often.
3. Strings recovered from the shipped native binary (frontmatter key names,
   refusal messages, telemetry field names). **Descriptive of build 2.1.251
   only.** Cite it for the *shape* of a decision, never as an API.

Nothing below requires reading Claude Code's source; the interesting parts are
in the interface.

## The seven techniques

### 1. A delegate is a named declarative profile, not a call site

Delegates are files: `.claude/agents/<name>.md`, frontmatter plus a system
prompt body. Build 2.1.251's loader reads `name`, `description` (or
`when_to_use` / `when-to-use`), `tools`, `disallowedTools`, `skills`, `model`
(a concrete id, or the literal `inherit`), `effort`, `maxTurns`, `isolation`,
`memory`, `background`, `color`. Definitions under `.claude/agents/` may also
set `permissionMode`, `hooks` and `mcpServers`; the same keys in a *plugin*
agent are ignored with a warning that names the file and points at
`.claude/agents/` for that level of control.

The same object can be handed in at launch instead of on disk:

```text
--agent <agent>    Agent for the current session. Overrides the 'agent' setting.
--agents <json>    JSON object defining custom agents (e.g. '{"reviewer":
                   {"description": "Reviews code", "prompt": "You are a code
                   reviewer"}}')
```

Selection is by prose. The caller sees each profile's one-line "when to use"
and picks; no router model, no scoring. The declaration is what makes the
choice cheap.

**Where we stand.** `[agents.<name>]` in `config.toml` is our *backend* axis —
`claude`, `opencode` — and `JobCreate.agent` picks a backend. There is no role
axis anywhere: everything role-shaped is smuggled into the prompt text on every
submit, so it is neither reusable, discoverable, nor enforceable. Two names for
two different things also collide in the docs: our "agent" is their "adapter",
their "agent" is our missing concept.

### 2. The prompt is the whole interface, and the contract says so out loud

Verbatim from the tool description in this session:

> Any agent other than a fork starts with zero context. […] **Never delegate
> understanding.** Don't write "based on your findings, fix the bug" […] Write
> prompts that prove you understood: include file paths, line numbers, what
> specifically to change.

> When the agent is done, it will return a single message back to you. The
> result returned by the agent is not visible to the user.

> Trust but verify: an agent's summary describes what it intended to do, not
> necessarily what it did.

This is the same contract `skills/agent-bridge-worker` states from the other
end — one self-contained final message, evidence inline, name what you could
not deliver — arrived at independently. Worth noting in both skills that the
convention is not local invention; it survives being rediscovered.

### 3. A restriction that cannot be applied refuses the spawn

The refusal messages in 2.1.251 are unusually explicit about the principle
(reassembled from the binary's split format strings — angle brackets mark where
a runtime tool name goes):

```text
tool names are case-sensitive. Refusing the spawn rather than running it with
this deny silently dropped.
… the spawned agent's resolved tool pool has no <Bash> … refusing the spawn
rather than running a blind agent. Drop the clamp or the Bash deny.
agent() schema mode needs the <structured output> tool … spawn instead of
running an agent that cannot return its structured output.
```

Same instinct as [design/03](../design/03-direct-mode-resumes-in-wrong-cwd.md):
a substitution the caller cannot see is the expensive kind. We are not
consistent about it. `agent_exec` drops `--model` on the floor — the
`config.example.toml` comment documents it as a known cost rather than an
error, and a caller who passes `--model claude-opus-5` into a dispatcher mode
gets a run on something else with nothing on the event stream to say so.

### 4. Depth and concurrency are structural, not advisory

At the nesting limit the Agent tool is **removed from the pool** rather than
being allowed and then refused; the tool-filter reduces to `depth < limit` for
that entry, and the telemetry note says so plainly: *"depth_limit stays near
zero in practice: a subagent at the nesting limit is normally not offered the
tool at all."* Refusals are counted in three named buckets —
`depth_limit`, `concurrency_limit`, `budget` — and `--max-budget-usd` can halt
a run outright. `maxTurns` bounds a single delegate.

**Where we stand.** `[worker] concurrency` bounds how many jobs run at once,
and that is the whole of it. The dispatcher modes hand a session `Bash` plus
`bypassPermissions` and instruct it to run `claude -p`; nothing prevents that
nested agent from doing the same again, and nothing prevents any job from
calling `ab submit` back into the gateway it is running under. `grep -rn` finds
no depth counter, no cost ceiling and no turn ceiling in `gateway/`.
`RunResult` already carries `cost_usd`, so the number needed for a budget gate
is measured and then discarded.

### 5. Filesystem isolation is part of the spawn, and the delegate is told

`isolation: "worktree"` gives the delegate its own git worktree; the worktree
is auto-removed if it made no changes, and otherwise its path and branch come
back in the result. The delegate is *informed*, by injected prompt:

```text
You are running in an isolated git worktree at `…` (a separate working copy of
the repo). Changes you make here do NOT affect the main working directory (`…`)
```

And when isolation is expected but absent, writes are refused rather than
interleaved: *"This subagent's parent bg session hasn't isolated yet, so writes
to the shared checkout are blocked. Re-spawn this agent with
`isolation: \"worktree\"`."*

**Where we stand, and this is the high item.** `[worker] concurrency = 2` in
the shipped example, `default_cwd` one directory, and `WorkerPool._claimed`
keyed by *session*. Two jobs with different sessions and the same cwd both run,
both edit the same checkout, and neither is told the other exists. There is no
git awareness in the gateway at all — it never invokes git. The only mention
anywhere in `gateway/` is `sessions.py` lifting a `gitBranch` field out of a
transcript record for the session index.
Nothing here is corrupted deliberately; it is two agents editing one working
tree, which is the failure that is hardest to reconstruct afterwards because
each transcript looks correct in isolation.

### 6. A delegate is told what it is; waiting is replaced by being woken

Background delegation is a first-class lifecycle, with fleet verbs to match:

```text
claude --bg                 start in the background, print an id
claude agents --json        active sessions as JSON, for scripting, no TTY
claude attach|logs|stop|rm|respawn <id>
```

A long-running tool call that outlives
`CLAUDE_CODE_AUTO_BACKGROUND_TIMEOUT_MS` is converted to a background task
rather than blocking, and its result is delivered when it lands. The
print-mode guidance for such a task states our exact problem in its own words:

> This session takes no further input, though: the command is stopped when your
> turn ends, so its result reaches you only while you are still working.

That is [design/11](../design/11-a-turn-is-not-a-job.md)'s premise stated
upstream: a turn is not the work. `expect_report` is the right shape. Claude
Code's answer inside one machine is a wait ceiling and then a sweep; ours is a
parked row and `ab-notify`, which is stronger, because our work can outlive the
whole process.

Except that we never tell the job who it is. `ab-notify` resolves its job id
from `--job-id` or `$AB_JOB_ID`; the claude adapter spawns the child with the
gateway's inherited environment and appends only an attachments block to its
system prompt. `grep -rn AB_JOB_ID` finds the variable in `ab_notify.py`, in
`README.md` and in the worker skill's `#SBATCH --export` line — **and nowhere
in `gateway/`**. So a remote agent can only close its own job if the caller
happened to paste the uuid into the prompt, while `expect_report` defaults to
true and `report_timeout_sec` defaults to 86400. The default path for a
correctly-behaving worker that was not hand-fed its uuid is: do the work, try
to report, fail to identify itself, park for a day, then fail with
`report_timeout`.

### 7. The handoff can be typed

`--json-schema` on the CLI, `schema` on a spawn; a spawn whose schema cannot be
returned is refused rather than run. Nested runs get proper attribution rather
than being scraped: `--forward-subagent-text` forwards a delegate's text and
thinking as messages with `parent_tool_use_id` set.

**Where we stand.** We already use `--json-schema` in `select_then_exec` for
routing, so the mechanism is understood — but a *job's* result is always free
text, and `_stream(..., capture_nested=True)` recovers a dispatched run's
result by parsing JSON out of a Bash tool_result. The flag that makes that
unnecessary shipped upstream.

## What not to copy

**Context isolation as a selling point.** Claude Code's headline benefit is
keeping a delegate's raw output out of the parent's context window. We get that
free and absolutely: separate process, separate transcript, events in SQLite,
and a client that reads a summary page unless it asks for more.

**The router.** Their caller picks a profile by reading descriptions. Ours tried
a model that picks a session, and both dispatcher modes are now advised
against. Adopt the declaration, not the router.

**In-process depth semantics and `subagent_type: "fork"`.** A fork that
inherits the parent's context is `--fork-session`, at session granularity,
which we have had since 0.3.0. The nesting *limit* is worth taking; the nesting
*model* is not.

## Proposal, staged

Ordered by ratio of harm removed to code written. Each phase stands alone.

| # | Change | Buys | Cost / risk |
|---|---|---|---|
| 1 | ~~Inject job identity~~ **shipped, differently.** The child gets `AB_JOB_DIR` and a preamble naming it; reporting is files in that directory, so there is no id to plumb, no url to discover and no token to protect. `expect_report` also stopped defaulting on, which removed the failure mode this phase existed to fix | design/15 | done |
| 2 | `isolation = "worktree"` per job: `git worktree add` under the data dir, run there, announce the substitution on the event stream as design/03 does for cwd, return path+branch in the result, remove the worktree when nothing changed | concurrent jobs in one repo stop sharing a working tree | needs a non-git fallback (refuse, or run shared and say so); worktree reaping is real operational surface |
| 3 | Delegate profiles: `[profiles.<name>]` in config (`description`, `tools`, `disallowed_tools`, `model`, `effort`, `permission_mode`, `isolation`, `max_turns`, `prompt`), `profile` on `JobCreate`, published in `/v1/agents` and `ab agents --output json` | a role becomes reusable and *enforceable* rather than prose the caller retypes; `--tools` clamping becomes available to any job, not just the router | one new config block and one new job field; profiles must degrade per-backend through the existing `capabilities()` dict |
| 4 | Refuse rather than drop: any request a mode cannot honour (`--model` under `agent_exec`, a profile the backend cannot clamp) becomes a typed error at submit | matches design/03's rule and the error envelope we already have | may reject jobs that "worked" before, in the sense of quietly doing something else |
| 5 | Gates: `AB_DEPTH` incremented into the child env with a configured maximum, refusal counters in the job row, and an optional per-job `max_cost_usd` enforced from the `cost_usd` we already parse | bounds recursion through the gateway and through nested `claude -p`; a runaway costs a refusal instead of an account | needs a decision on whether refusal is a typed submit error or a terminal job |
| 6 | Typed results and real nesting: optional `result_schema` on `JobCreate` (via `--json-schema`), and `--forward-subagent-text` in the dispatcher modes instead of scraping the Bash tool_result | `ab wait --output json` becomes machine-readable; nested events get `parent_tool_use_id` attribution | schema failures need a defined terminal state; the flag is claude-only, so it lands behind `capabilities()` |

Phases 1 and 2 are the ones with a defect behind them. 3 is the interesting
one. 4, 5 and 6 are cheap once 3 exists, and 5 only matters if the dispatcher
modes stay supported.

## Open questions

**Do profiles belong to the operator or the caller?** Server-side
(`[profiles.*]`) makes them auditable, discoverable through `/v1/agents` and
enforceable — the gateway can clamp `--tools` whatever the prompt says.
Caller-side (pass an `--agents` JSON blob straight through) is more flexible
and needs no config. Recommendation: server-side registry plus a `profile`
field, because a caller-supplied system prompt is indistinguishable from prompt
text and buys nothing a longer prompt does not, whereas an operator-owned tool
clamp is a control we currently cannot express at all.

**Does opencode have an equivalent?** Unverified — no `opencode` binary on this
host. `capabilities()` exists exactly so a claude-only feature can be
advertised as absent, but a profile that silently means less on one backend
would be the same class of quiet substitution phase 4 is meant to end.

**Does phase 2 change what `--no-fork` means?** A worktree is a different
directory, and [design/03](../design/03-direct-mode-resumes-in-wrong-cwd.md)
made a session's recorded cwd win over everything. An in-place resume into a
worktree is therefore either a contradiction or a deliberate exception, and
which one it is should be decided before the code is written, not during.
