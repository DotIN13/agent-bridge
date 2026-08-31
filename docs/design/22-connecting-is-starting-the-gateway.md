# 22 — Connecting is starting the gateway: `ab-serve`

Connecting a gateway can be what starts it. A gateway entry says so with one
key, and the command it means ships with agent-bridge:

```json
{ "ssh": "ssh -L 8787:localhost:8787 midway5", "exec": true }
```

`exec: true` puts `ab-serve` on the far side of that connection. It runs on the
login node, ensures the gateway is serving, holds the ssh open for as long as it
is, and exits when it is not. The `ssh` line can also end in a command directly,
which is what `exec` is folded into and what wins when both are there.

## One key, three states

```
exec absent    nothing runs; a plain forward, and `-N` on the argv
exec: true     PATH="${AB_PATH:+$AB_PATH:}$PATH"; exec ab-serve
exec: "…"      that string, verbatim, on the far side
```

`true` rather than a copy of the default string in every config, because the
default is the thing most likely to need changing later — a rename, a flag, a
better expansion — and a file full of copies is a file that cannot be improved.
The dialog draws it as one switch and a box that is empty for the default, so
turning it on needs no knowledge of what the default is.

A command written into the `ssh` line wins over `exec`, with a diagnostic saying
so. The line is the more specific of the two and it is in the field somebody is
looking at; `exec` fills the gap when the line ends at the host. Preferring
either one silently is how a config comes to lie about what it does.

`false` in the file is read as absent rather than kept as a third state. The
dialog sends `false` to mean "remove the key", and one absent state is easier to
reason about than two falsy ones.

## Why a shipped script rather than a per-gateway snippet

The dashboard could have taken an arbitrary `exec` string per gateway and run it.
Every version of that string anybody writes has to answer the same four
questions, though, and each one has a wrong answer that is invisible until it
costs a day:

| | The wrong answer | What it looks like |
|---|---|---|
| Already serving? | Start another | A bind failure in a log nobody reads, and a gateway that looks started |
| Port held by something else? | Free it | Somebody's notebook killed by a launcher that wanted 8787 |
| Failed to start? | Hold the connection anyway | A green row in front of a dead gateway — the worst state this dashboard can be in |
| Connection dropped? | Take the gateway with it | A closed laptop kills every running job |

One script, in the repository, with tests on all four, is a better deal than a
config field and a paragraph of advice. The field still exists — the `ssh` line
can end in anything — but the default advice names something that already gets
these right.

## The two that are trade-offs rather than bugs

**It exits when it cannot serve.** `ab-serve` returning non-zero drops the ssh,
which turns the tunnel red in the dashboard with the tail of `gateway.log` in the
console. The alternative — hold the connection and let the row stay green while
the port answers nothing — hides the failure behind a working-looking tunnel.
Exiting is louder and lands the reason where somebody is looking; that is worth a
tunnel that will not stay up while a config is broken.

**It does not own the gateway.** The gateway is started with
`start_new_session=True` and left running when `ab-serve` goes, so a dropped
connection costs the tunnel and nothing else. This is the decision most likely to
look wrong from outside — a launcher that does not clean up after itself — and it
follows directly from design/17: a `waiting` job is an agent still alive on the
cluster with an sbatch to report, and killing that when a laptop lid closes
discards exactly the work the `waiting` state exists to preserve. Anyone who
wants the two lifetimes tied does not need `ab-serve` at all:
`ssh -L … host 'exec agent-bridge'` is that, in one line, and the `ssh` field
takes it.

While parked it re-checks `/health` and restarts a gateway that has gone, then
gives up after `--max-restarts` (default 3). Restarting forever is how a crash
loop stays invisible; four deaths in a row is a person's problem.

## The parser had to stop refusing commands

`ssh -N` means *no command*, and the argv builder added `-N` unconditionally, so
a trailing command was refused with a diagnostic. Now `-N` is added exactly when
there is no command, and the two are never both present.

Two details in the parse are load-bearing:

- **The command is lifted from the raw line, not rebuilt from tokens.** The
  tokenizer strips quotes, so reassembling
  `'systemctl --user start x && exec ab-serve'` from its pieces makes `&&` an
  argument to `systemctl` and the second half never runs. Tokens now carry their
  spans, and the command is the raw tail with one enclosing quote layer removed —
  then handed to ssh as a single argv entry, which is what ssh would have made of
  it anyway.
- **A flag after the destination is still a flag.** ssh itself stops reading
  options at the destination, so being faithful would read
  `ssh midway5 -L 8787:localhost:8787` as a remote `-L`. That form parsed as a
  forward here before, and silently turning somebody's working config into a
  remote command that fails is a worse outcome than not matching ssh exactly. So
  the command opens at the first token after the destination that does *not*
  start with `-`.

## Where `$AB_PATH` is expanded, and how that is known

By the shell on the far side, which is the only answer that would be correct:
this laptop's `$AB_PATH` says nothing about where a cluster keeps its binaries.
It holds because `spawn` runs ssh with no shell of its own — the command reaches
ssh as one argv element, literally — and sshd hands it to the remote user's login
shell.

Quoting follows from that and is worth stating, because the answer differs by
where the line is typed. In `gateways.json` the quotes are decoration: there is
no local shell to protect the `$` from. In a terminal they are the whole
difference between sending `$AB_PATH` and sending whatever the laptop has. Since
the same string wants to be copy-pasteable between the two, the documented form
is single-quoted, and a test asserts the three forms parse identically so nobody
has to remember which one the dashboard needs.

Checked rather than reasoned about, twice. A fake ssh that runs the command under
a *different* environment shows the remote `AB_PATH` winning while a local one
points somewhere else entirely; and a unit test asserts the argv still carries
the unexpanded `${AB_PATH:+$AB_PATH:}$PATH`. The second is the one that will earn
its keep: a refactor to `shell: true`, or building the command by interpolation
in TypeScript, would expand it locally and silently point every gateway at a
directory on the wrong machine.

## A test that passed for the wrong reason

Worth writing down because the failure mode is general. The fake ssh standing in
for "a client that ignores `SSH_ASKPASS`" was, for a while, a syntax error: an
escaping slip put a real newline inside a JavaScript string literal. Its test
passed anyway. Node prints the offending source line in the traceback, that line
contained the words `Permission denied`, and `classify()` duly reported an
authentication failure — the assertion held on a string that came from the fake
being broken rather than from the behaviour under test.

Every generated fake now goes through one writer that runs `node --check` on
what it wrote. A fake that will not parse fails its test immediately instead of
passing for reasons nobody chose.

## The default command, and why it is not `$AB_PATH/ab-serve`

`$AB_PATH` names the directory holding agent-bridge's console scripts. Building
the command out of it — `$AB_PATH/ab-serve` — is wrong in two of the three cases
that happen:

| `$AB_PATH` | `$AB_PATH/ab-serve` | `PATH="${AB_PATH:+$AB_PATH:}$PATH"; exec ab-serve` |
|---|---|---|
| set, holds `ab-serve` | works | works |
| unset | runs `/ab-serve` — "not found", naming a path nobody configured | falls back to `PATH` |
| set, wrong directory | fails, with `ab-serve` on `PATH` a metre away | falls back to `PATH` |

So the default prepends and lets the shell's own lookup answer. The `:+` form
contributes the entry *and* its colon together, so an unset variable leaves
`PATH` alone rather than adding an empty element — which means the current
directory, and a `PATH` that searches `.` before a command is looked up in it is
its own small hazard. `exec` replaces the login shell, so the signal from a
dropped connection lands on `ab-serve` rather than on a shell waiting for it.

The test runs the string through `/bin/sh` against two stub `ab-serve`s and an
empty directory rather than reading it, because the entire value of the form is
what a shell does with it — and the third row is exactly what the earlier
interpolated version got wrong. sh, dash and bash agree on all three.

Whether the variable is set at all is the other half, and the reason the fallback
carries the weight: the failure without one is a command not found and nothing
else.

`ssh host cmd` runs a non-interactive shell. bash *does* read `~/.bashrc` in that
case — it detects standard input on a socket and sources it, which is the
behaviour rshd left behind — but nearly every distribution's `.bashrc` opens with
an early return when not interactive, so exports below that line never run. So
the variable exists in a login shell, and does not exist in the shell ssh
actually uses.

Hence the documented forms, in order of how little they assume: the default,
which needs nothing when `ab-serve` is on the non-interactive `PATH`;
`export AB_PATH=…` *above* the interactivity guard; `exec` set to a path with `~`
in it, which the remote shell expands with no profile involved; and
`bash -lc "exec ab-serve"` when it has to come from `~/.profile`.
`ssh midway5 'echo "AB_PATH=$AB_PATH"; command -v ab-serve'` settles which a
given machine needs, and is worth running once before wiring it into a gateway.
