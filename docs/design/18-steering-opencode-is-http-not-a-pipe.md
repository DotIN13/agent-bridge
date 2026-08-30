# 18 — Steering opencode is HTTP, not a pipe

**Shipped:** after 0.3.0 — extends [02](02-mid-turn-steering-or-liveness-gate.md),
which built the channel for claude and left `"steering": False` on the other
backend
**Scope:** `gateway/adapters/base.py`, `gateway/adapters/opencode.py`,
`gateway/config.py`, `gateway/server.py`, `config.example.toml`, `API.md`,
`README.md`, `gateway/docs.py`.

## What opencode actually does

02 found that claude reaches a running turn through streaming stdin, verified it
on the CLI, and built `Steering` around a pipe. The opencode adapter has
advertised `steering: False` ever since. That was accurate, and it was also never
investigated — so the first question was whether opencode has a mechanism at all.

It does, and it is better named than ours. Read from the published API (the
`@opencode-ai/sdk` v2 client, and `packages/protocol/src/groups/session.ts`):

    POST /api/session/<id>/prompt
      {"prompt": {"text": "..."}, "delivery": "steer" | "queue"}
    -> 200 {"data": {"admittedSeq": N, "promotedSeq": M, "id": "...",
                     "delivery": "steer", ...}}

    POST /api/session/<id>/interrupt   -> 204
      "Interrupt active execution owned by this OpenCode process.
       Idle interruption is a no-op."

`delivery` is opencode's own distinction between the two things a caller might
mean: `steer` goes into the turn that is already running, `queue` waits for it to
go idle. The response is a *receipt* — `admittedSeq` is "the session has it",
`promotedSeq` is "the running turn has it". That is strictly more than claude
gives us, where the only evidence a steer landed is the agent echoing it back on
stdout (`--replay-user-messages`), and where "at the next tool boundary" is a
measured behaviour rather than a documented one.

Errors are typed: `409 ConflictError`, `404 SessionNotFoundError`.

## Why this needed more than a new `send()`

The mechanism exists; reaching it does not, and this is the whole cost of the
change. Reading `packages/opencode/src/cli/cmd/run.ts`:

- **stdin is not a channel.** `const piped = process.stdin.isTTY ? undefined :
  await Bun.stdin.text()` — `opencode run` reads stdin to EOF as its prompt,
  before the turn starts. There is nothing to write into later.
- **the server has no port.** Non-interactive `opencode run` builds its client as
  `createOpencodeClient({baseUrl: "http://opencode.internal", fetch: fetchFn})`,
  where `fetchFn` calls `Server.Default().app.fetch(...)` in process. No listener,
  so nothing to connect to. `run` even *declares* a `--port` flag; nothing in the
  file reads it.

So a steerable opencode job has to be attached to a server that does listen:
`opencode serve --hostname 127.0.0.1 --port 0` (which prints
`opencode server listening on http://127.0.0.1:PORT`), then
`opencode run --attach <url> --password <pw>`.

## The shape chosen

**`Steering` grew a second way to be bound, rather than growing a subclass.**
`bind(stdin)` is the pipe; `bind_remote(send, interrupt, note)` takes two
callables. The callables belong to the adapter because only it knows the url, the
credential, and the session id — and the session id it usually learns *during*
the run, from the records opencode streams back. A steer in the first second of a
fresh session therefore fails with "opencode has not reported its session id yet
— try again", which is a retry, not a refusal.

**The credential rides in the environment, not on argv.** `opencode run
--attach` takes a `--password`, but argv is world-readable through `/proc` on a
shared host — the same reason the gateway's own token is kept out of a job
environment that a scheduler would publish. `ServerAuth.header` falls back to
`OPENCODE_SERVER_PASSWORD`, so passing it that way costs nothing.

**One server per job, not one per gateway.** Its lifetime is the job's, so a
stuck turn or a crash cannot take the next job's steering with it, and there is no
long-lived listener to secure between runs. The password is generated per job and
passed in the environment — `opencode serve` warns and then serves
*unauthenticated* if it finds none, which on a shared host is a shell for anyone
who can reach the port. An operator who would rather run one server says so with
`[agents.<name>] server_url`, and its password comes from the gateway's
environment (`OPENCODE_SERVER_PASSWORD`), never from the config file — the same
rule the auth token follows.

**Attaching is not free, so a job that cannot afford it does not.** `--attach`
cannot assume the server shares a filesystem with the client, so `opencode run`
inlines each attached file as a `data:` url, caps it at 10 MiB, and refuses a
directory outright — and it exits before the turn starts. Here the two *are* the
same host, but the check is opencode's. A job whose attachments would trip it
runs unattached and unsteerable, with the reason on its own event stream
(`"corpus is a directory"`), because a job losing its work to gain steering is
the wrong trade. The same fallback covers a server that never announces a port
and one too old to answer `/api/health`.

## What a caller sees

`POST /v1/jobs/<id>/steer` now returns the *handle's* note rather than a fixed
sentence, because the old one described claude:

| Backend | Channel | Note |
| --- | --- | --- |
| claude, `direct` | the child's stdin | "the agent picks this up at its next tool boundary" |
| opencode, `direct` + `steering` | the attached server's API | "opencode admits this into the session and promotes it into the running turn (delivery=steer)" |

The `steer` event differs the same way: claude's is written when the agent echoes
the message, opencode's when the server admits it, and carries
`admitted_seq`/`promoted_seq`/`message_id`. A 409 names what is missing — off in
config, no server, an attachment that forced the unattached path, or a session id
that has not arrived yet.

## Not done, deliberately

- **`delivery: "queue"` is not exposed.** `ab steer` means "into this turn"; the
  thing to do after a turn is another job in the same session, which
  `ab submit --session <id> --no-fork` already is. Adding a flag would mean
  explaining a queue that only one backend has.
- **Cancel still goes through the signal ladder**, as 02 left it. The in-band
  interrupt is bound and reachable (`Steering.interrupt`), and for opencode it is
  an HTTP call that does not depend on the child reading anything — which removes
  02's stated objection for this backend. Worth revisiting as a fast path with
  signals as the fallback, in a change of its own.
- **No `--attach` for claude.** There is no equivalent; the pipe is the channel.

## Verified

Against a stub standing in for both halves of the binary
(`tests/backend/test_opencode_steering.py`): a steerable job attaches to a server
of its own and the password never reaches an event; a mid-turn steer arrives at
`POST /api/session/ses_stub/prompt` as `{"prompt": {"text": ...}, "delivery":
"steer"}` with basic auth and the `x-opencode-directory` header, and its receipt
becomes one `steer` event; a refusal carries opencode's own `ConflictError`
message; an interrupt reaches `/interrupt`; a directory attachment, a silent
server, and a server without `/api/health` each run the job unattached with a
reason; the job's own server is dead once the run ends, and the operator's is not
touched; and a dead ambient `HTTPS_PROXY`/`HTTP_PROXY` does not break the
loopback call, because the opener disables proxies explicitly.

**Not verified against a real `opencode` binary** — none is installed on this
host. The wire contract is taken from the published SDK and the protocol source
rather than from a live run, so the first real use should confirm the two
things a stub cannot: that `delivery: "steer"` is picked up mid-turn rather than
after it, and how long `opencode serve` takes to announce a port on a cold node
(`SERVE_WAIT_SEC` is 30s).
