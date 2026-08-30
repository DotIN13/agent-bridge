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
exec: true     exec "${AB_BIN_PATH:+$AB_BIN_PATH/}ab-serve"
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

## The default expansion, and why it is not `$AB_BIN_PATH/ab-serve`

The obvious default is `$AB_BIN_PATH/ab-serve`, and it is a trap: an unset
variable expands to nothing, the command becomes `/ab-serve`, and it fails as
"not found" — naming a path nobody configured. `${AB_BIN_PATH:+$AB_BIN_PATH/}`
adds the slash only when there is something to put in front of it, so the same
string is `$AB_BIN_PATH/ab-serve` where the variable is set and a bare
`ab-serve` where it is not. Quoted, so a path with a space survives; prefixed
with `exec`, so the login shell is replaced rather than left waiting on a child
and the signal from a dropped connection lands on `ab-serve` itself. A test
expands all three cases through `/bin/sh` rather than reading the string, because
the whole value of the form is what a shell does with it.

Whether the variable is set at all is the other half, and worth stating because
the failure is a command not found and nothing else.

`ssh host cmd` runs a non-interactive shell. bash *does* read `~/.bashrc` in that
case — it detects standard input on a socket and sources it, which is the
behaviour rshd left behind — but nearly every distribution's `.bashrc` opens with
an early return when not interactive, so exports below that line never run. So
the variable exists in a login shell, and does not exist in the shell ssh
actually uses.

Hence the documented forms, in order of how little they assume: a path with `~`
in it (the remote shell expands it, no profile involved), a bare `ab-serve` when
the export sits *above* the interactivity guard, and `bash -lc "exec ab-serve"`
when it has to come from `~/.profile`. `ssh midway5 'echo $AB_BIN_PATH; command
-v ab-serve'` settles which of the three a given machine needs, and is worth
running once before wiring it into a gateway.
