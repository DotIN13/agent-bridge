# 19 — One contract, not three: `ab capabilities` removed

**Shipped:** after 0.3.0 — reverses part of
[06](06-agent-first-cli-api.md), which added the verb
**Scope:** `client/ab.py`, `client/abclient.py`, `README.md`,
`client/README.md`, `tests/client/test_cli.py`.

## What it was

06 made the CLI agent-first and added `capabilities` as the one call an agent
could make to learn the contract. It returned three keys:

```
{"client": {version, output_modes, exit_codes, streaming, operations},
 "gateway": "<name>",
 "server":  <verbatim GET /v1/agents>}
```

## Why it went

Every part of it is another command's answer, and by the time monitors shipped
the copy had gone stale:

| Key | Already available as |
| --- | --- |
| `client` | `ab help --output json` |
| `gateway` | `ab gateways` |
| `server` | `ab agents` — the same `GET /v1/agents`, unmodified |

There was never a `/v1/capabilities` route; this was purely a client-side
wrapper. And it cost nothing in round trips to drop: `ab help --output json` is
local, so an agent still makes exactly one network call (`ab agents`) to learn
what a gateway supports.

The concrete harm was drift. The `operations` list was hand-maintained in two
places — `Client.capabilities()` and a near-copy behind `ab help` — and
advertised 18 and 19 verbs against the 21 the parser accepted: both had lost
`monitors` and `monitor`, and one never had `help`. A verb absent from the
contract is a verb an agent will not attempt, which is the failure mode 06 was
trying to prevent. One list in one place, with a test holding it to the parser's
own subcommand choices in both directions, is what that intent actually needs.

## What a caller does now

- **the client contract** — `ab help --output json` (version, output modes,
  exit codes, streaming, every verb).
- **which gateways, and are they up** — `ab gateways`.
- **what a backend supports** — `ab agents --output json`, which is where the
  per-backend `capabilities()` flags live (`steering`, `in_place_resume`,
  `file_attachments`, …). Unchanged, and still the thing to read before writing
  a plan that assumes a feature.

`ab capabilities` is an argparse error now, not a deprecation shim — exit 2 with
the list of valid verbs, the same treatment `--for` and `--expect-report` got.
Loud beats a wrapper nobody maintains.

## Note

The adapter-level `capabilities()` method is untouched and unrelated: it is the
per-backend feature dict published through `/v1/agents`, and
`docs/todo/13`/`14` both plan to extend it.
