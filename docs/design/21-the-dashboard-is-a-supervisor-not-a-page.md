# 21 — The dashboard is a supervisor with an askpass, not a page with a terminal

`webui/` is a local dashboard for the one part of this project that never had a
usable interface: the ssh forwards on the user's own machine. It lists the
gateways in `gateways.json`, connects them, and reads the jobs behind each one.

This is the second attempt. The first was reverted (`4dae28e`, reverting
`3b06ae7 dcf66c0 1b7ed02 968b4ea`) on the grounds that it did not look good
enough to keep, and the reasons it did not are the reasons this one is shaped
differently.

## What changed between the two

| | First attempt (reverted) | This one |
|---|---|---|
| Stack | Python stdlib server, HTML built by string concatenation | TypeScript, express + ws, Solid + Vite + Tailwind v4 — picone's stack |
| Styling | An ad-hoc palette, then opencode tokens pasted in | picone's token files verbatim, so the two stay diffable |
| ssh credentials | A pty with `TIOCSCTTY`, prompt detection on an unterminated tail | `SSH_ASKPASS` + `SSH_ASKPASS_REQUIRE=force` |
| Live updates | The page polled and rebuilt its DOM | One websocket; Solid updates the nodes that changed |
| Where it lives | `bridge/`, a Python package with a console script | `webui/`, an npm package with no Python in it |

The stack change is the user's instruction. The other three follow from it or
from what the first attempt cost.

## The pty was the wrong mechanism

The first attempt spawned ssh on a pty so it could carry a password prompt, and
paid for it three times:

- `start_new_session=True` calls `setsid()`, which gives the child its own
  process group and **no controlling terminal**. `open("/dev/tty")` then fails
  and ssh's prompt never arrives. It needed `ioctl(0, TIOCSCTTY, 0)` in a
  `preexec_fn` — a fix found only by a test that hung.
- The pty's line discipline **echoes the answer back**, so a relayed password
  landed in the console the page was rendering. Not the child's doing, and not
  something the child can be asked to stop: `ECHO` has to be cleared on the
  master.
- Prompt detection meant reading an unterminated tail and guessing when a
  question had been asked. An answered prompt's terminated copy re-raised the
  same question, dropping the tunnel back to `authenticating` with nothing
  waiting.

`SSH_ASKPASS` has none of these. OpenSSH will not read a password from a process
with no tty, but with `SSH_ASKPASS_REQUIRE=force` it will run the program
`SSH_ASKPASS` names and read one line from its stdout — no `DISPLAY` needed. The
prompt arrives as an argument, the answer leaves on stdout, and there is nothing
to detect and nothing to echo. It carries a passphrase, a password and a
two-factor menu through one channel. The mechanism is picone's
(`apps/server/src/remote/askpass/`), and it is the single biggest reason this
rebuild is smaller than what it replaces.

The helper is written to disk at runtime rather than shipped, so it is found the
same way under `tsx` and under `dist`, and so the `node` that runs it is provably
the one running the server. It authenticates with a single-use token handed to it
in its **environment**, not on its command line, which every process on the
machine can read.

### It does not fix Windows, and could not have

Worth stating plainly, because dropping the pty looks like a portability win and
is not one. The pty version could not run on Windows at all — Python's `pty` is a
Unix module — so the platform was never served. Askpass does not serve it either:
`SSH_ASKPASS` was honoured by `OpenSSH_for_Windows_8.1p1` and is ignored by
`8.6p1` (Win32-OpenSSH#2115), `SSH_ASKPASS_REQUIRE` was ignored outright in 8.1
(#1726), there is no native `ssh-askpass` there, and ssh launches the helper with
`CreateProcess`, which cannot execute the `.cmd` wrapper directly. The move is
neutral for Windows and removes three failure modes on Unix, which is why it is
still the right one.

What follows from that is a message rather than a mechanism: when an interactive
attempt fails with `auth` and no prompt was ever raised, the log says so, and on
win32 it names the two configurations that do work (an agent or a key; or Git for
Windows' ssh in full). A wrong password and an ssh that cannot ask exit
identically — same code, same "Permission denied" — so the only way to tell them
apart is to have counted the questions. `tunnel.test.ts` has both halves: a fake
ssh that ignores `SSH_ASKPASS` must produce the note, and a genuinely refused
password must not.

## The three invariants that are not cosmetic

These are the rules the first attempt learned by getting them wrong, and they
survive the rewrite with tests on them:

1. **A tunnel with a question waiting is not up**, however long its process has
   been alive. `ssh -N` prints nothing when it works, so "up" is inferred from
   not having exited — and that inference is wrong while a password box is on
   screen. `tunnel.test.ts` holds a prompt open past the settle window and
   asserts the status.
2. **A gateway is never promoted to `connected` while a prompt is open.**
   Something else answering the forwarded port — an old tunnel, a service on the
   same number — would otherwise report a working connection to a machine nobody
   has authenticated to. `server.test.ts` starts an impostor gateway on the
   forwarded port mid-authentication and asserts the row does not go green.
3. **An authentication failure stops the supervisor**, rather than backing off.
   Retrying a two-factor host on a timer is a stream of push notifications to
   somebody's phone and a plausible route to a locked account.

A fourth belongs with them, and is about the page rather than the process: **a
state push must not touch an input somebody is typing into.** In the reverted
version the page rebuilt its DOM on every poll and a half-typed password went
with it. Solid's fine-grained updates fix that structurally — except that
`<Show keyed>` on the prompt *object* remounted the dialog on every state frame
and reintroduced it exactly. Keying on the prompt's **id** fixes it; the live run
now types a password, forces two state pushes, and asserts the box still holds
it.

## The probe ladder, and why `listening` is a warning

`closed → listening → reachable → serving`. The rungs exist because "listening"
lies: when the far end of an `ssh -L` dies, the local socket keeps accepting
connections and then resets them, so a check that stops at `connect()` reports a
healthy tunnel to a machine that is gone. Only an answer from the far side is
evidence, and only `/health` returning agent-bridge's `{"ok": true}` earns the
top rung.

The gateway's own status is asked in two steps for the same reason: `/health`
needs no token and `/v1/agents` does, which separates *the port is dead* from
*the token is wrong* without guessing. A row that says `unauthorized` names a
different fix from one that says `unreachable`.

## One config file, and the dashboard is a guest in it

The dashboard reads `ab`'s `gateways.json` rather than a copy, resolving it in
`ab`'s own order. A gateway configured at the CLI is configured here. It adds two
keys `ab` ignores — `ssh`, the command that makes `base_url` reachable, and
`autostart` — and when it writes, unknown keys survive verbatim, the write is
atomic and `0600` (one of the three token forms is a raw token), and a rename
keeps the entry's position in the file, because the sidebar is drawn in file
order and a row that moves to the bottom reads as one entry vanishing and another
appearing.

The command is parsed for **comprehension**, never re-serialized over what was
written: enough to draw the ports and probe them, with a diagnostic for anything
refused. Three things are refused rather than passed through — a non-loopback
bind, `-R`, and `-g`/`GatewayPorts=yes` — because each publishes a route into the
cluster to the whole network, which is never what somebody pasting a command into
a local dashboard meant.

## Security, given what this process can do

It holds every gateway token on the machine and can start ssh processes, so:
loopback only and not configurable; a token in the URL **fragment**, which is the
one part of a URL a browser never sends to a server; the gateway's token added by
the server on the way out and never handed to the page; `connect-src 'self'`, so
whatever a password is typed into cannot ship it anywhere; and ssh's stderr
scanned for anything token-shaped before it is published, because that log is
rendered in the page.

The credential box is a masked *text* input rather than `type="password"`.
Chrome's password manager offers to save anything typed into a password field and
its bubble steals the focus — which on a two-factor prompt means the next
question arrives underneath a dialog about the last one.

## What was left out

- **Multiplexing.** picone carries `ControlMaster` so a port can be added to a
  live connection. Nothing here asks for that, and a stale control socket makes
  ssh disable multiplexing and carry on, so every later `-O` fails with no
  explanation. Not worth the failure mode for a feature with no caller.
- **A port scanner.** picone's "what did I forget" button enumerates loopback
  listeners. An app quietly enumerating your sockets is a surprising thing for it
  to be doing, and the forward rows already say which ports this config expects.
- **Submitting a job.** That is `ab submit`, and a form for it would be a second
  place for the contract in design/19 to drift.
