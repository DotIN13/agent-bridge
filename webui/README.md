# The dashboard

A local page for the part of agent-bridge that has no CLI worth using: the ssh
forwards on your own machine, whether each one is actually carrying a gateway,
and what is running behind it.

Three views, one at a time. The sidebar lists the gateways in `ab`'s own
`gateways.json`, each with a connect button. Clicking one opens its job list on
the right; clicking a job opens that job's event log, with a back button to the
list. ssh's questions — a password, a passphrase, a two-factor menu — arrive as a
dialog.

```bash
cd webui
npm install
npm run build
npm start          # prints http://127.0.0.1:8765/#t=<token>
```

Open the printed URL, not the bare address: the token is in the fragment.

For development, `npm run dev` runs the API and a Vite dev server together; the
API prints the dev URL as well as its own.

| Variable | Default | What it does |
|---|---|---|
| `AB_WEBUI_PORT` | `8765` | Where the local server listens (loopback only). |
| `AB_WEBUI_TOKEN` | random | Pins the token, so a restart does not invalidate the open tab. |
| `AB_WEBUI_STATE_DIR` | `$XDG_STATE_HOME/agent-bridge/webui` | Where the askpass helper is written. |
| `AGENT_BRIDGE_CLIENT_CONFIG` | `~/.config/agent-bridge/gateways.json` | Which config to read, exactly as `ab` resolves it. |

## What it reads and writes

`ab`'s own `gateways.json`, and nothing of its own. A gateway configured for the
CLI is configured here, and the two cannot drift because there is only one of
them. The dashboard adds two optional keys, which `ab` ignores:

```json
{
  "gateways": {
    "midway5": {
      "base_url": "http://localhost:8787",
      "token_env": "AGENT_BRIDGE_TOKEN",
      "ssh": "ssh -L 8787:localhost:8787 midway5",
      "exec": true,
      "autostart": false
    }
  }
}
```

`ssh` is the command you would type. It is run as written — with a few
keep-alives and `ExitOnForwardFailure=yes` added, and the user's own `-o`s last
so they win. `autostart` brings it up when the server starts and lets a dropped
connection come back on a backoff. `exec` is the next section.

## Starting the gateway on connect

`exec` decides what runs on the far side once the forward is up, and the
connection then lives as long as that command does. Three states, one key:

| `exec` | What runs |
|---|---|
| absent | Nothing. A plain forward, and `-N` goes on the argv. |
| `true` | `PATH="${AB_PATH:+$AB_PATH:}$PATH"; exec ab-serve` — the shipped default. |
| a string | That, verbatim, on the far side. |

In the dialog it is one switch — *Start the gateway when the tunnel comes up* —
and a box that is empty for the default and holds your script when you have one.

`ab-serve` ships with agent-bridge. It checks whether the gateway is already
serving, starts it if not, holds the ssh open while it serves, and **exits if it
cannot** — which drops the tunnel, so the row goes red with the reason in the
console instead of green in front of a dead gateway. It starts the gateway in a
session of its own, so closing a laptop costs the tunnel and not the jobs. The
root [README](../README.md#ab-serve) has the rest.

Three things follow from the mechanism:

- `-N` is added exactly when there is **no** command — the two are exclusive,
  since `-N` means "no command".
- A command written into the `ssh` line wins over `exec`, and the clash is a
  diagnostic rather than a silent choice: the line is the more specific of the
  two and it is right there in the field.
- A command that returns immediately takes the tunnel with it, and the log says
  so rather than reporting a bare "exited with code 0". `systemctl --user start
  agent-bridge` alone is that mistake; `… && exec ab-serve` is not.

### Why the default is written that way

`$AB_PATH` names the directory holding agent-bridge's console scripts on the
cluster. The obvious use of it — `$AB_PATH/ab-serve` — is wrong in two of the
three cases that occur:

| `$AB_PATH` | `$AB_PATH/ab-serve` | Prepending to `PATH` |
|---|---|---|
| set, holds `ab-serve` | works | works |
| unset | runs `/ab-serve`, "not found" | falls back to `PATH` |
| set, does not hold it | fails, with `ab-serve` on `PATH` a metre away | falls back to `PATH` |

So the default prepends and lets the shell's own lookup decide:

```sh
PATH="${AB_PATH:+$AB_PATH:}$PATH"; exec ab-serve
```

`${AB_PATH:+…}` contributes the entry *and* its colon only when the variable is
set, so an unset one leaves `PATH` untouched rather than putting an empty element
in it — an empty element means the current directory, which is not something to
add to a `PATH` a command is about to be looked up in. Measured in sh, dash and
bash, which agree on all three rows. It lands on `ab-serve`'s own environment
too, so the `agent-bridge` it looks for next is found in the directory it was
itself found in. `exec` replaces the login shell, so one process fewer waits
around and the signal from a dropped connection reaches `ab-serve` directly.

**Whether `$AB_PATH` is set at all** is the other half, and the reason the
fallback matters. `ssh host cmd` runs a non-interactive shell: bash does read
`~/.bashrc` there, but nearly every distribution's `.bashrc` opens with an early
return when not interactive, so exports below that line never run — the variable
your login shell has is not the one ssh gets. This settles it in one command:

```bash
ssh midway5 'echo "AB_PATH=$AB_PATH"; command -v ab-serve'
```

If the second line prints a path, the default already works and `$AB_PATH` is
optional. If neither prints anything, the fixes in ascending order of assumption
are: `export AB_PATH=~/.local/bin` *above* the interactivity guard in
`~/.bashrc`; or set `exec` to a path — `"~/.local/bin/ab-serve"`, which the
remote shell expands with no profile involved; or to `"bash -lc 'exec ab-serve'"`
when it has to come from `~/.profile`.

## The security model

The dashboard runs on your machine, holds every gateway token on it, and can
start ssh processes. So:

- **Loopback, always, and not configurable.** Reach it from elsewhere with
  `ssh -L`, which is the thing it is for.
- **A token in the URL fragment.** Loopback is not on its own an authorization
  boundary — every process on the machine can reach 127.0.0.1. A fragment is the
  one part of a URL a browser never sends to a server, so the token stays out of
  access logs and out of the `Referer` on anything the page loads. It moves to
  `sessionStorage` and is stripped from the address bar on first load.
- **The gateway's token never reaches the page.** The browser asks the local
  server; the server adds the bearer header on the way out. What the page is told
  is *where* a token comes from (`token_env`, `token_file`), never its value.
- **`connect-src 'self'`.** Whatever an ssh password is typed into cannot ship it
  anywhere.
- **ssh's stderr is redacted before it is published**, because that log is
  rendered in the page and a verbose ssh will print a bearer token.

## How a credential prompt works

`SSH_ASKPASS`, with `SSH_ASKPASS_REQUIRE=force`. OpenSSH will not read a password
from a process with no tty, but it will run the program `SSH_ASKPASS` names and
read the answer from its stdout — even with no `DISPLAY`. So there is no pty
here: the server writes a tiny helper to its state dir, hands ssh a single-use
token in the *environment* (not on a command line, which every process can
read), and the helper relays one question over a loopback socket.

Two rules the state machine will not break:

- Only a connect somebody clicked may prompt. A retry runs with
  `BatchMode=yes`, because a credential dialog nobody asked for is a credential
  dialog nobody can trust.
- A tunnel with a question waiting is **not** up, however long its process has
  been alive — and a gateway is never promoted to `connected` while one is open.
  Otherwise something else answering that local port reports a working
  connection to a machine you have not authenticated to.

An authentication failure stops the supervisor instead of backing off. Retrying a
two-factor host on a timer is a stream of push notifications to somebody's phone
and a plausible route to a locked account.

## Windows

The prompt relay is picone's mechanism, verbatim, and is reported working on
Windows machines there. It is kept that way on purpose — including the `.cmd`
wrapper, which looks impossible (ssh launches the helper with `CreateProcess`,
which cannot execute a batch file directly) and evidently is not. Whatever
Win32-OpenSSH's exec layer does with that path, it runs it; do not replace the
wrapper on that argument alone.

What is genuinely build-dependent is ssh's askpass support itself. `SSH_ASKPASS`
was honoured by `OpenSSH_for_Windows_8.1p1` and is ignored by `8.6p1`
([Win32-OpenSSH#2115](https://github.com/PowerShell/Win32-OpenSSH/issues/2115)),
and `SSH_ASKPASS_REQUIRE` was ignored outright in 8.1
([#1726](https://github.com/PowerShell/Win32-OpenSSH/issues/1726)). So on one
install the dialog appears and on the next it does not, with the same
configuration.

When it does not, ssh exits exactly as a wrong password does — same code, same
"Permission denied" — so the tunnel counts the questions and says which happened:
an interactive attempt that fails having raised no prompt gets a line in the log
saying nothing could be answered. Two things work when that happens:

- Git for Windows' ssh, named in full in the command:
  `"ssh": "C:\\Program Files\\Git\\usr\\bin\\ssh.exe -N -L 8787:localhost:8787 midway5"`.
  The per-tunnel runnability check handles an absolute path, so this needs no
  code change.
- Or a key in the OpenSSH agent (`ssh-add`, with the *OpenSSH Authentication
  Agent* service running), which asks nothing at all.

Everything else — the tunnel, the probe ladder, the job list, the config writes —
is ordinary Node with no platform-specific path in it. None of it is exercised on
Windows by the test suite, which runs on Linux here.

## Layout

```
src/protocol.ts        the vocabulary both halves share
src/server/
  config.ts            gateways.json: read, watch, atomic write
  ssh-command.ts       parse a command for comprehension; build the argv
  askpass.ts           the bridge ssh knocks on, and the helper it runs
  tunnel.ts            one supervised ssh per command
  probe.ts             closed → listening → reachable → serving
  gateway-client.ts    agent-bridge's HTTP API, including the SSE follow
  jobs.ts              per-gateway polling and per-job streams
  manager.ts           all of the above, reconciled against the config
  http.ts              REST + one websocket, both behind the token
src/web/               Solid, Tailwind v4, Kobalte — picone's stack and tokens
```

The design tokens in `src/web/styles/{colors,theme,tailwind-theme}.css` and the
component CSS in `src/web/components/ui/` are picone's, unchanged, so the two
projects stay diffable. `base.css` is picone's with the mobile interface-zoom
chain removed.

```bash
npm run typecheck      # both targets: node and DOM
npm test               # node --test over src/server/**/*.test.ts
```

The tests spawn a fake `ssh` that authenticates through the real askpass bridge,
and a fake gateway that answers agent-bridge's shapes — so the prompt relay, the
config writes and the probe ladder are exercised rather than described. No ssh
binary is needed to run them.
