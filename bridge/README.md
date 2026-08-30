# `ab-bridge` — the laptop half

The gateway runs on the cluster. `ab` talks to it. This keeps the ssh forward in
between alive, and lets you drive it from a browser.

```bash
ab-bridge --open          # loopback, a fresh token, opens the page
```

It prints a url with the token in the fragment:

```
config:  /home/you/.config/agent-bridge/gateways.json
tunnels: midway5, deltaai
open:    http://127.0.0.1:8765/#token=Xy…
```

## Why it exists

A tunnelled gateway's `base_url` is a *local* port. It answers only while an ssh
forward is up, and keeping that up used to mean a terminal you could not close:

```bash
ssh -o ServerAliveInterval=60 -L 8787:localhost:8787 midway5   # …and leave it
```

Worse, the hosts this is for want a password **and** a Duo push, and a daemon has
no terminal to be asked on. `ssh` reads a password from `/dev/tty`, so a
subprocess with pipes gets nothing and hangs looking healthy. So each tunnel gets
a real pty: what ssh asks appears in the UI, you type the answer there, and it
goes straight to the child (`docs/design/20`).

## Config

Two optional keys per gateway in the same `gateways.json` the CLI reads. Both are
ignored by `ab`, so nothing else changes:

```json
{
  "default": "midway5",
  "gateways": {
    "midway5": {
      "base_url": "http://localhost:8787",
      "token_env": "AGENT_BRIDGE_TOKEN",
      "ssh": "ssh -N -o ServerAliveInterval=60 -L 8787:localhost:8787 midway5",
      "autostart": false
    }
  }
}
```

- **`ssh`** — a command line or a list of arguments. Run **without a shell**:
  quotes and spaces work, `&&`, `|`, `$()` and redirection are refused with a
  message rather than passed to ssh as literal arguments. The program must be
  `ssh`, `autossh` or `sshpass` (extend with `--allow-program`).
- **`autostart`** — bring it up when the daemon starts. Off by default: five
  gateways each demanding a Duo push at boot is not a good morning. A tunnel you
  *did* start is restarted automatically when it drops, which is the part
  "keep it running" actually means.

The UI can write this file. The previous copy is kept as `.bak`, and the file is
written `0600` because it can hold a token.

## The page

Three levels, one click apart, addressable by url:

| Level | Url | What is there |
| --- | --- | --- |
| gateways | `#/` | every gateway, its tunnel state and endpoint state, start/stop/restart, the ssh console, the auth prompt, add/edit/remove |
| jobs | `#/g/<gateway>` | that gateway's jobs, read through the tunnel |
| events | `#/g/<gateway>/j/<job>` | one job's detail and its event stream |

**Two lights, never merged.** "ssh pid 4098" and "endpoint up" are separate facts
with different fixes: ssh alive with the endpoint `refused` is a forward that has
not come up; `reset` is a forward that is up with nothing serving behind it. The
words come from `probe_gateway`, so the page and `ab gateways` agree.

Design is [picone](https://github.com/DotIN13/picone)'s — opencode's v2 token
system, ported as plain custom properties. One self-contained document: no
bundler, no CDN, no font files, because a tool for fixing a broken network must
not need the network to render.

## What it refuses, and why

This process runs a command from a file its own web page can edit. Three things
follow, and they are enforced rather than documented:

- **Loopback only.** `--host` outside 127.0.0.0/8 needs
  `--dangerously-bind-all`, whose help says what you are publishing. Reach it
  over an ssh forward instead.
- **A token, always.** Given, or `$AGENT_BRIDGE_UI_TOKEN`, or generated per run.
  It travels in the url *fragment*, which browsers never send to a server, so it
  stays out of access logs and caches; the page keeps it in `sessionStorage`.
- **argv, from an allowlist.** See `ssh` above. Without it, a text field in a
  loopback web page is a shell.

Secrets are relayed, never held: a prompt's answer goes to the pty and is
dropped. The pty's echo is turned off so it cannot come back into the console
either — `ssh` does that itself for passwords, but a wrapper may not, and this is
not a thing to leave to the child.

Reads of the gateway go through four named read-only endpoints
(`jobs`, one job, its events, monitors), not a path proxy: an open proxy on
loopback would let any page in the browser submit or cancel jobs. The gateway's
own bearer token stays in this process.

## CLI

```
ab-bridge [--config PATH] [--host 127.0.0.1] [--port 8765]
          [--token T | --print-token] [--open] [--up NAME]...
          [--allow-program NAME]... [--dangerously-bind-all]
```

## API

Bearer token on everything. `GET /` is the page.

| Method | Path | |
| --- | --- | --- |
| GET | `/v1/state` | every gateway, its tunnel and its endpoint |
| POST | `/v1/tunnels/{name}/up` \| `/down` \| `/restart` | |
| POST | `/v1/tunnels/{name}/answer` | `{"text": "…"}` — may be a secret; not stored |
| GET | `/v1/tunnels/{name}/output?after=N` | the ssh console |
| POST | `/v1/events/ticket` | a single-use 30s ticket |
| GET | `/v1/events?ticket=…` | SSE of state changes |
| PUT/DELETE | `/v1/gateways/{name}` | edit `gateways.json` |
| POST | `/v1/gateways/{name}/default` | |
| GET | `/v1/gateways/{name}/jobs`, `/jobs/{id}`, `/jobs/{id}/events`, `/monitors` | read the gateway through its tunnel |
