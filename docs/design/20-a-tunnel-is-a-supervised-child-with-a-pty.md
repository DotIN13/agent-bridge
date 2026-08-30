# 20 — A tunnel is a supervised child with a pty

**Shipped:** after 0.3.0 — the first component that runs on the operator's own
machine
**Scope:** new `bridge/` package (`config.py`, `tunnel.py`, `supervisor.py`,
`server.py`, `ui.py`, `__main__.py`), the `ab-bridge` console script,
`client/gateways.example.json`, `README.md`, `bridge/README.md`.

## The gap

`gateway/` runs on the cluster and `client/` talks to it, and between them sat a
thing nothing in this repo owned: the ssh forward. A tunnelled gateway's
`base_url` is a *local* port that answers only while `ssh -L` is up, and the
README's advice was a terminal you must not close:

```bash
ssh -o ServerAliveInterval=60 -L 8787:localhost:8787 midway5   # …and leave it
```

`docs/todo/12` had already recorded what that costs — "sophia unreachable —
tunnel down", "deltaai unreachable — tunnel down": two of five deployments dark
because a laptop slept.

## Why a pty, and not a pipe

The obvious build is `subprocess.Popen(["ssh", …])` plus a supervisor loop. It
does not work on the hosts this exists for, and it fails in the worst way:
silently.

`ssh` reads a password from `/dev/tty`, not from stdin. With pipes there is
nothing to read and nothing to answer, so the child sits there — alive, no
output, no error — until the login times out. A supervisor watching only for exit
sees a healthy process. Then Duo asks for a passcode on top of that.

So each tunnel gets a real pseudo-terminal. Output goes into a bounded ring the
UI reads; an unterminated tail matching a prompt pattern flips the tunnel to
`authenticating` and publishes the question; the answer arrives as an HTTP body,
is written to the master fd, and is dropped.

Three details that were not obvious and are load-bearing:

- **`start_new_session=True` is not enough.** It calls `setsid()`, which the
  process group needs so `kill` can take the whole tree, but leaves the child
  with *no* controlling terminal — and `open("/dev/tty")` then fails. The tty has
  to be claimed explicitly with `TIOCSCTTY` in a `preexec_fn`. Found by a test,
  not by reading: the fake ssh crashed on `/dev/tty` exactly where the real one
  would have hung on it.
- **The pty echoes the answer back**, and that echo is the *line discipline's*
  doing, not the child's. Without `ECHO` off, a password we relayed appears in
  the console the UI renders — put there by us, not by ssh. `ssh` disables echo
  for its own password prompts, but a wrapper may not, so echo is cleared on the
  master at spawn and again before each answer. Also found by a test.
- **A prompt reaches the buffer twice** — once as the unterminated tail, once
  when its newline arrives — so the second copy is dropped, or every question
  reads as two.

## Two lights, never one

`up` needs two independent facts, and they need opposite fixes:

| ssh | endpoint | means |
| --- | --- | --- |
| alive | `up` | working |
| alive | `refused` | the forward has not come up (wrong `-L`, or still authenticating) |
| alive | `reset` | the forward is up; nothing is serving behind it |
| gone | `refused` | the tunnel died |

The endpoint half is `client.abclient.probe_gateway`, reused rather than
rewritten, so this page and `ab gateways` cannot disagree about what "up" means —
including its refused/reset distinction, which is the one an operator most needs.
And `state == "up"` is the predicate, not `reachable`: a forward pointed at the
wrong port connects happily to whatever is there.

`ssh -N` prints nothing on success, so the endpoint check is the *only* thing
that can promote a tunnel to `up`.

## The UI can edit the config, which is the whole risk

The operator asked for add/edit/remove from the page. That makes a loopback web
page able to rewrite the command this process executes — remote code execution
with extra steps, unless three things hold:

1. **Loopback only.** A non-`127.0.0.0/8` `--host` needs
   `--dangerously-bind-all`, whose help states what is being published.
2. **A token, always** — supplied, from `$AGENT_BRIDGE_UI_TOKEN`, or generated
   per run. Never optional: "I'll set one later" is how it stays unset. It
   travels in the url *fragment*, which browsers do not send to the server, so it
   stays out of access logs and caches; the page keeps it in `sessionStorage`.
3. **argv from an allowlist.** The command is `shlex.split` for quoting and then
   executed with no shell; a string containing ``;&|<>$`(){}[]`` is refused with a
   message naming the alternative, and the program must be `ssh`, `autossh` or
   `sshpass` (`--allow-program` extends it). `shlex.split` alone is not enough:
   `ssh a && rm -rf b` splits cleanly and would pass `&&` to ssh as an argument,
   which is not what the author meant.

`sshpass` is on the list deliberately — it is no worse than the password it
wraps, and refusing it only pushes people to a wrapper script we would then have
to allow anyway.

Two more, from the same reasoning: the gateway's own bearer token never leaves
this process (the page asks the daemon, which already resolves
`token`/`token_env`/`token_file` the way the CLI does), and reading the gateway is
four named read-only endpoints rather than a path proxy — an open proxy on
loopback would let any page in the browser submit or cancel jobs.

## The event stream needed its own credential

`EventSource` cannot send an `Authorization` header. The real token in a query
string would sit in browser history and in any log that records a url, so the
page trades it for a single-use ticket good for 30 seconds
(`POST /v1/events/ticket`). The first attempt shipped a comment claiming the
stream was "accepted only from loopback", which was not implemented; the ticket
replaced the claim.

The stream waits in one-second slices rather than one long one: `wait_for_event`
blocks a threadpool thread, so a browser that navigates away would otherwise park
a thread for the whole idle window, and a page that reconnects a few times would
exhaust the pool.

## Config, and staying compatible

Two optional keys in the file `ab` already reads — `ssh` and `autostart` — both
ignored by the CLI. The daemon manages *that* file rather than one of its own,
because two files would disagree about what `midway5` means. Held as the raw
document, so a key this code knows nothing about survives an edit made from the
browser; written temp-then-rename, `0600` (it can hold a token), with one `.bak`;
and `_ensure_default` keeps the result loadable by `ab`, which refuses several
gateways with no default. TOML configs are read but not written: a round trip
without a TOML writer drops the comments and ordering a human maintains, and the
refusal says so.

`autostart` defaults **off**. Five gateways each demanding a Duo push at boot is
not a good morning. A tunnel the operator *did* start is restarted with backoff
when it drops, which is what "keep the bridge running" actually asks for.

## Design

picone's, which is opencode's v2 system: primitive ramps, semantic tokens on top,
light by default and dark under `[data-color-scheme]`. Ported as plain custom
properties rather than reproduced by eye, so the two stay recognisably the same
product. One self-contained document — no bundler, no CDN, no font files —
because a tool whose job is to fix a broken network connection must not need the
network to render, which is also what makes the
`default-src 'none'; connect-src 'self'` policy it sends honest.

Three levels, addressable by url — tabs included — so the back button works and
a reload lands where you were: the list (`#/`), a gateway
(`#/g/<name>`, `/log`, `/config`), a job's events (`#/g/<name>/j/<id>`).

Everything you do *to* a gateway is on the gateway's own page: connect,
disconnect, restart, the ssh log, and its entry in `gateways.json`. The list is
then only a list — one row, one action, no disclosure triangles. The connect
button and any auth prompt are pinned above the tabs rather than inside one,
because ssh waiting for an answer behind a tab is the login that times out; the
prompt also appears on the list, for the same reason.

Two details that are easy to get wrong and were: hover has to reveal the row's
actions from the *same* selector that highlights the row, or the two disagree
about both timing and hit area (they hung off `.item` and `.card`, with a
transition on only one); and a `.section` header is uppercased, so a path or a
command must not be put in one.

**An auth box must not be a password field.** `type="password"` recruits the
browser's password manager: Chrome offers to save an ssh passphrase, and its
autofill overwrites the field while you are typing. Both boxes here — the answer
and the daemon's own token — are text inputs masked with
`-webkit-text-security`, with `type="password"` kept only as a fallback for an
engine without it, since being masked beats being tidy. The manager's popup was
only half the report, though: the other half was ours. The page rebuilds itself
on every poll, so a tick arriving mid-passcode threw the input away. The draft
lives outside the DOM now and the caret is restored after each render, and focus
is taken once per *question* rather than once per render — on the ssh log tab
that was every 1.2 seconds, fighting the operator for the cursor.

That fix then exposed a third bug in the state machine, found only because two
daemons briefly shared a port: an answered prompt's *terminated* copy re-raised
the question, so a tunnel fell back to `authenticating` with nothing waiting on
it, and an endpoint probe could not promote it. Only lines that survive the
duplicate filter may raise a prompt now. Relatedly, `check()` no longer promotes
out of `authenticating` at all: while ssh is asking a question its forward is
not carrying anything, so whatever answers on that port is not us.

## Verified

`tests/bridge/` (56 tests) plus a live run against a fake far side that prompts
for a password and a Duo passcode on its own `/dev/tty` and then serves a gateway
on the forwarded port. Observed end to end: `stopped → starting →
authenticating` with the prompt and `prompt_secret: true`; both answers relayed
and read by the child (`password=…`, `duo=1` in its log) with neither in the
console, only `(answered)`; promotion to `up` once `/health` answered, with the
version and latency shown; `SIGKILL` on the ssh child producing `up → retrying →
authenticating` with a new pid and a fresh prompt; the job list, one job and its
six events read through the tunnel; a `502 gateway_unreachable` when it is down;
an edit adding a gateway that `ab gateways` then lists, with `.bak` kept; a shell
metacharacter and `/bin/sh -c` both refused without touching the file; SSE
delivering transitions, and the same ticket refused on reuse.

**Not verified against real `ssh` or a real Duo push** — neither is available on
this host. The prompt patterns are drawn from OpenSSH's and Duo's actual wording,
and `PROMPT_PATTERNS` is deliberately broad because a missed prompt is a tunnel
that hangs looking healthy; the first real login should confirm the set and add
to it.

## Not done, deliberately

- **No ControlMaster reuse.** Attaching to a master the operator authenticated by
  hand would mean never handling a secret, but it cannot recover once the master
  dies — which is the case this exists for.
- **No job submission from the UI.** The read proxy is read-only. Submitting work
  needs the brief-from-a-file discipline the client skill describes, and a
  textarea in a browser is the wrong shape for it.
- **`ab` does not know about tunnels.** `ab gateways` still reports reachability
  only. Teaching it to start one would put process supervision in a CLI that is
  otherwise stateless.
