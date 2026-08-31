# Skills

Three agent skills: one for each end of the connection, plus the one that stands
the remote end up. The two runtime skills are written to agree with each other
and are backend-agnostic — they hold whether the gateway runs the job through
Claude Code or opencode, so install both, or the conventions only hold on one
side.

| Skill | Install on | Covers |
|---|---|---|
| [`agent-bridge-client`](agent-bridge-client/SKILL.md) | the machine you work from | driving the `ab` CLI, session targeting, where a session works, monitoring, when a job is actually done |
| [`agent-bridge-worker`](agent-bridge-worker/SKILL.md) | the gateway host | the remote agent's remit, finishing a turn, submitting batch work, reporting through `$AB_JOB_DIR`, registering a monitor |
| [`agent-bridge-install-worker`](agent-bridge-install-worker/SKILL.md) | whoever is setting the remote host up | installing the worker host: clone and `PATH` under a non-interactive ssh, what `ab-serve` does for you, the three settings worth editing, proving it serves, handing over the token |

```bash
mkdir -p ~/.claude/skills
cp -r skills/agent-bridge-client ~/.claude/skills/     # laptop
cp -r skills/agent-bridge-worker ~/.claude/skills/     # gateway host
cp -r skills/agent-bridge-install-worker ~/.claude/skills/    # whoever installs the host
```

None needs a gateway restart; all take effect on the next session.

`install-worker` is a one-off where the others are continuous, and it is
deliberately separate and deliberately narrow — the remote host only, stopping
at the port and the token. An agent driving jobs should not carry setup
instructions it will never use, and setup is where the `PATH`-under-ssh trap
decides whether anything else works at all.

## Why two at runtime

The division of labour is the point: **the local session plans and reviews with
a human, the remote agent investigates and executes.** Each skill states the
same contract from its own side, so neither has to infer the other's behaviour.

The single most expensive failure this is designed around: a remote agent
ending its turn with *"I'll report back when the job finishes."* The gateway
records turn-end as task completion, and a non-interactive agent turn (`claude
-p` or `opencode run`) cannot hold a blocking wait — so that promise reads as
**success with no deliverable**. The worker skill forbids it; the client skill
explains how to structure a brief so the situation doesn't arise, and
a monitor gives the batch job its own lifecycle, watched by the gateway rather than promised by the agent.

## Adapting them

`agent-bridge-client` is written generically — replace `<repo>`, `<host>` and
`<workdir>`, drop the Windows/Git Bash section if it doesn't apply, and delete
whatever else is irrelevant.

`agent-bridge-worker` carries some site-specific detail (Slurm flags, module
loads, an example env layout) because it is most useful when concrete. Adjust
the scheduler section for PBS/LSF if that's what you run — the shape is the
same, only the flag names change.

`agent-bridge-install-worker` names paths that are conventions rather than facts
— `~/.bashrc`, `~/.local/bin`, port 8787 — so adjust those. Keep the three
checks in *Prove it serves* verbatim: each exists because the failure it catches
is otherwise indistinguishable from the next one's.
