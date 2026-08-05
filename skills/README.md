# Skills

Two agent skills, one for each end of the connection. They are written to agree
with each other and are backend-agnostic — they hold whether the gateway runs
the job through Claude Code or opencode, so install both, or the conventions
only hold on one side.

| Skill | Install on | Covers |
|---|---|---|
| [`agent-bridge-client`](agent-bridge-client/SKILL.md) | the machine you work from | driving the `ab` CLI, session targeting, monitoring, when a job is actually done |
| [`agent-bridge-worker`](agent-bridge-worker/SKILL.md) | the gateway host | the remote agent's remit, finishing a turn, submitting batch work, `ab-notify` |

```bash
mkdir -p ~/.claude/skills
cp -r skills/agent-bridge-client ~/.claude/skills/     # laptop
cp -r skills/agent-bridge-worker ~/.claude/skills/     # gateway host
```

Neither needs a gateway restart; both take effect on the next session.

## Why two

The division of labour is the point: **the local session plans and reviews with
a human, the remote agent investigates and executes.** Each skill states the
same contract from its own side, so neither has to infer the other's behaviour.

The single most expensive failure this is designed around: a remote agent
ending its turn with *"I'll report back when the job finishes."* The gateway
records turn-end as task completion, and a non-interactive agent turn (`claude
-p` or `opencode run`) cannot hold a blocking wait — so that promise reads as
**success with no deliverable**. The worker skill forbids it; the client skill
explains how to structure a brief so the situation doesn't arise, and
`ab-notify` gives the batch job its own voice.

## Adapting them

`agent-bridge-client` is written generically — replace `<repo>`, `<host>` and
`<workdir>`, drop the Windows/Git Bash section if it doesn't apply, and delete
whatever else is irrelevant.

`agent-bridge-worker` carries some site-specific detail (Slurm flags, module
loads, an example env layout) because it is most useful when concrete. Adjust
the scheduler section for PBS/LSF if that's what you run — the shape is the
same, only the flag names change.
