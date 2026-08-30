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
      "ssh": "ssh -N -L 8787:localhost:8787 midway5",
      "autostart": false
    }
  }
}
```

`ssh` is the command you would type. It is run as written — with `-N`, a few
keep-alives and `ExitOnForwardFailure=yes` added, and the user's own `-o`s last
so they win. `autostart` brings it up when the server starts and lets a dropped
connection come back on a backoff.

Edits from the config dialog preserve everything else in the file, including keys
this build has never heard of, and land as an atomic `0600` write. A `.toml`
config is read-only here: the dialog says so rather than rewriting it as JSON.

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

Untested, and the prompt dialog probably does not work there.

The dashboard itself is ordinary Node and should run. What is in doubt is the one
Windows-specific thing: ssh asking for a credential. The bundled client's askpass
support is not dependable — `SSH_ASKPASS` was honoured by
`OpenSSH_for_Windows_8.1p1` and is ignored by `8.6p1`
([Win32-OpenSSH#2115](https://github.com/PowerShell/Win32-OpenSSH/issues/2115)),
`SSH_ASKPASS_REQUIRE` was ignored outright in 8.1
([#1726](https://github.com/PowerShell/Win32-OpenSSH/issues/1726)), and there is
no native `ssh-askpass` on the platform. On top of that, ssh launches the helper
with `CreateProcess`, which cannot execute the `.cmd` wrapper directly.

So: nothing is lost relative to the pty version, which could not have run there
at all (`pty` is a Unix module), but nothing is gained either. What works on
Windows is a connection that needs no question asked:

- a key in the OpenSSH agent (`ssh-add`, with the *OpenSSH Authentication Agent*
  service running), or a passphraseless key;
- or Git for Windows' ssh, named in full in the command —
  `"ssh": "C:\\Program Files\\Git\\usr\\bin\\ssh.exe -N -L 8787:localhost:8787 midway5"`.
  It is a msys2 OpenSSH build and honours `SSH_ASKPASS`; whether it will run a
  `.cmd` through its own exec is still untested.

When an interactive attempt fails without ssh ever asking, the tunnel's log says
that, rather than leaving a bare "Permission denied" that looks like a typo in a
password nobody was given the chance to type.

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
