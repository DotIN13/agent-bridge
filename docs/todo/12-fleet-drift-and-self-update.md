# 12 — The fleet drifts silently, and there is no way to update most of it

**Severity:** high (silent divergence; no remediation path to three of five hosts)
**Status:** open — design
**Scope:** `client/_version.py`, `/health`, new `GET /v1/version`, new
`gateway/update.py`, new `bin/ab-update`, new `ab fleet`, an `[update]` block in
`config.example.toml`, `systemd/agent-bridge.service`, and the on-host directory
layout.

## Problem

`~/.config/agent-bridge/gateways.json` lists five deployments — `local`, `wsl`,
`midway3`, `sophia`, `deltaai`. Each is a separate checkout of this repo on a
separate machine. Nothing tells anyone when they diverge, and for three of them
there is no convenient way to fix it once they have.

### Evidence

Measured 2026-08-24, local CDT.

```text
$ for g in local wsl midway3 sophia deltaai; do ab --gateway $g health; done
local     unreachable — nothing listening on 8788
wsl       {"ok": true, "version": "0.3.0"}
midway3   {"ok": true, "version": "0.3.0"}
sophia    unreachable — tunnel down
deltaai   unreachable — tunnel down
```

Both live hosts report the same version string as `main`. Neither is running it:

```text
$ ab --gateway midway3 info --output json   # keys only
['_probes','accounts','collected_at','env_present','gpu_local','gpus',
 'host','partitions','ready']
```

No `notes` key, so midway3 predates `38f8c82 serve operator notes from
/v1/info`. `/health` did not notice, and could not have.

`client/_version.py` was last touched by `3aeb4e7` (2026-08-10). `main` is **28
commits** past it and `git tag` is empty. Eight records in `docs/design/` are
marked "shipped: after 0.3.0". **The version number identifies a release that
stopped existing two weeks ago**, which is why both gateways can honestly claim
`0.3.0` while running different code from each other and from here.

### Why it matters more than staleness usually does

The three cluster hosts are reachable **only** through an SSH tunnel that costs
a Duo push per connection and cannot be scripted unattended. So a fix landed
here does not reach midway3 by any path a human will take often, and nothing on
either end reports the gap. A bug fixed in `main` stays live on the machine
doing the research.

## Why this is harder than `git pull && systemctl restart`

Five constraints, each read out of the code or measured on the hosts.

**1. Restarting fails every running job.** `Database.recover_on_start`
(`gateway/db.py:865`) turns each `status='running'` row into `failed` with
`gateway_restarted`, and `WorkerPool.stop` cancels live turns before the join.
Agent turns here run for hours. So a restart is destructive unless the gateway
is drained first. `awaiting_report` deliberately survives — the comment above
that query says so — which means parked batch jobs need not block an update.

**2. tmux is not supervision.** `ab-serve` (which absorbed `run.sh`, design/24)
starts the gateway detached and restarts it a bounded number of times; a process
that exits so a new tree can take over simply stops. Self-update has to be *conditional on a supervisor being present* and
must refuse otherwise, rather than exiting hopefully.

**3. `/project` on midway3 is ~97% full.** An in-place `git checkout` that runs
out of disk halfway leaves a tree that is neither version, with the service
pointed at it. Whatever swaps the code must be able to fail without having
touched what is currently running.

**4. Unauthenticated `api.github.com` allows 60 requests/hour per IP, and a
login node is one IP shared by everyone on it.** A poll loop against
`/releases/latest` will 403 at unpredictable times for reasons unrelated to this
service, and that failure looks exactly like "no update available". `git
ls-remote --tags --refs <remote>` has no such limit, needs no token, and uses
the same HTTPS the checkout already uses. Measured from the laptop: 1.4 s.

**5. Windows has no unprivileged symlink.** The atomic-swap step below needs a
directory junction (`mklink /J`) or a rename dance on `local`.

## Design

Five parts, separable. Part 1 is worth shipping on its own.

### 1. Make the reported version identify a commit

A self-updater built on a hand-edited string will report "up to date" forever.
Three changes:

- `client/_version.py` gains `__commit__` and `__installed_at__`, written at
  deploy time. When they are absent and the deployment is a git checkout, the
  gateway fills them at startup from `git rev-parse --short HEAD` plus a
  `-dirty` suffix; otherwise it reads `RELEASE.json` from the release directory.
  A deployment that can identify itself no other way reports `commit: null`,
  which is itself the finding.
- Tags start existing: annotated `vX.Y.Z`, pushed, with a GitHub Release. CI —
  or a pre-tag check — asserts `__version__` matches the tag, so the two cannot
  drift apart again.
- `/health` gains `commit`. It stays public, small, and unauthenticated. A new
  authenticated `GET /v1/version` carries the rest:

```json
{
  "version": "0.4.0",
  "commit": "38f8c82",
  "channel": "stable",
  "installed_at": "2026-08-24T10:12:03-05:00",
  "supervisor": "systemd",
  "update": {
    "mode": "notify",
    "state": "available",
    "latest": {"version": "0.4.1", "commit": "a1b2c3d"},
    "checked_at": "2026-08-24T09:00:00-05:00",
    "last_error": null
  }
}
```

`state` is one of `current`, `available`, `staged`, `applying`, `failed`.

### 2. Discovery — `git ls-remote`, not the REST API

Two channels:

- `stable` — the highest semver annotated tag on the configured remote.
- `edge` — the sha of `refs/heads/main`.

Both come from one `git ls-remote --tags --refs` / `--heads`, run as a
subprocess with a timeout on the same style of background task as
`_expire_reports`. It writes state and sends no signal. Cost is one ~1.4 s
subprocess every six hours.

```toml
[update]
mode = "notify"        # off | notify | auto
channel = "stable"     # stable | edge
remote = "https://github.com/DotIN13/agent-bridge.git"
check_interval_sec = 21600
max_defer_sec = 86400  # auto only: how long to wait for a quiet moment
keep_releases = 2
post_install = ["bin/install-skills"]
```

`remote` is host configuration and never a request parameter. See the security
note below.

### 3. Trigger — a timer and an endpoint, sharing one code path

Polling works when the laptop is off; the endpoint works when egress does not,
and it is the remediation path the tunnel-only hosts currently lack. Both call
the same `update.check()` and `update.apply()`.

- `POST /v1/update/check` → `200` with the block above, forced now.
- `POST /v1/update/apply` → `202 {"state":"applying","target":"v0.4.1"}`.
  Typed refusals: `409 update_not_available`, `409 jobs_running` with the
  holding job ids in `details`, `412 unsupervised`.

`apply` accepts at most an optional target tag, and only one the last
`ls-remote` actually returned.

### 4. Applying — a detached updater, a releases directory, an atomic swap

**The gateway never updates itself in-process.** It decides *when*; a
short-lived detached process (`bin/ab-update`) does the work. That split is
load-bearing rather than tidy: the thing that verifies the new version came up
cannot be the process being restarted.

Layout on the host:

```text
~/agent-bridge/
  releases/
    v0.4.0/                      checkout + its own .venv
    v0.4.1/
  current -> releases/v0.4.1     junction on Windows
  data/                          db, files, messages, .token
  config.toml
```

with `WorkingDirectory=%h/agent-bridge/current` and
`AGENT_BRIDGE_CONFIG=%h/agent-bridge/config.toml` in the unit. **Moving the data
dir and the config out of the checkout is a prerequisite, not a detail** — today
`gateway.db` sits in the repo root, where a release swap would strand it.

The sequence:

1. **Preflight.** A supervisor is present; free disk > 2× the tree; the remote
   resolves; the target tag exists.
2. **Stage.** Clone the tag into `releases/<tag>.tmp` — or fetch through a
   shared bare mirror, which is cheaper on a nearly-full filesystem — build its
   `.venv`, install requirements, `python -m compileall -q gateway client`, and
   run `pytest -q tests/backend -x` where the test extra is installed. Rename to
   `releases/<tag>`. **Nothing the running gateway touches has changed yet**; a
   failure here is a log line and a `last_error`.
3. **Drain.** Poll for `status=running` until empty, or until `max_defer_sec`.
   `awaiting_report` does not block. Without a quiet window, refuse — an
   operator can pass `--force` and accept the failed jobs.
4. **Swap.** `ln -sfn releases/<tag> current.new && mv -Tf current.new current`
   — one `rename`, atomic. Windows gets a delete-and-recreate junction with a
   sub-second window, or stays on in-place update; see open question 2.
5. **Restart** through the supervisor.
6. **Verify.** Poll `/health` for up to 90 s for `ok` *and* the expected commit,
   then one smoke call to `/v1/agents`.
7. **Roll back on failure.** Swap `current` back, restart, record
   `state: "failed"` with the reason, and leave the bad release on disk to be
   looked at.
8. **Post-install hooks**, `bin/install-skills` by default, so the host's skills
   match its code.
9. **Retain** `keep_releases` and delete the rest.

**Rollback works only because migrations are additive.**
`Database._migrate_locked` adds columns and never drops or rewrites, so an older
binary opens a newer database and ignores what it does not recognise. That is
currently an accident of implementation. This design promotes it to a rule, to
be written next to `_migrate_locked`: *a migration that is not an additive
column add breaks rollback, and needs a major version plus a manual runbook.*

### 5. Reporting — `ab fleet`

The thing wanted day to day, and it works today over `/health` alone:

```text
$ ab fleet
GATEWAY   REACHABLE  VERSION  COMMIT   UPSTREAM  STATE      JOBS
local     no         -        -        -         -          -
wsl       yes        0.4.1    a1b2c3d  a1b2c3d   current    0
midway3   yes        0.4.0    38f8c82  a1b2c3d   available  2 running
sophia    no         -        -        -         -          -
deltaai   no         -        -        -         -          -
```

`--output json` for agents. An unreachable gateway is a row, not an error: the
subject is the fleet, and a dead tunnel is a finding.

## What this deliberately does not do

**No in-process reload.** A process cannot replace its own modules and then be
honest about what is in memory. Update means restart, which is why drain is a
first-class step rather than a nicety.

**No self-update without a supervisor** — `412 unsupervised`. A gateway that
exits to be replaced and is never restarted is strictly worse than a stale one.

**`auto` is not the default; `notify` is.** On a cluster login node a bad
restart costs hours of agent work and a slice of a shared 5-hour Claude limit,
while being a day stale usually costs nothing. `local` and `wsl` are reasonable
`auto`; the cluster hosts should stay `notify` until the path has been exercised
by hand a few times.

**No arbitrary source.** The remote comes from the host's config file. A bearer
token already buys arbitrary code execution through `POST /v1/jobs`, so an
update endpoint is not a new class of privilege — but a URL *parameter* would
turn any token leak into a supply-chain foothold, and that is a new class.

**No signature verification initially.** Doing it properly means signed
annotated tags, `git tag -v`, and a pinned public key on each host. Worth it if
the repo becomes multi-writer; overhead now.

**No cross-host coordination.** Each host updates itself. Five machines is not
an orchestrator problem.

## Staging

**Phase 1 — identity and visibility.** Commit stamping, `commit` on `/health`,
`GET /v1/version`, `ab fleet`, and the first real tag. Zero operational risk,
useful the day it lands, and it is what finally measures how far the fleet has
drifted.

**Phase 2 — `bin/ab-update` as a manual command on the host.** No timer, no
endpoint. Run over ssh by hand, or — for the tunnel-only hosts — by an agent job
over the bridge itself: `ab submit "run bin/ab-update --to v0.4.1"`. That is a
working remediation path with no new API surface at all. Everything difficult
(staging, drain, swap, verify, rollback) lives in this phase and gets exercised
by a human before anything automates it.

**Phase 3 — the timer and the two endpoints.** Small, once phase 2 exists.

Phase 2 already solves the operational problem. Phase 3 is convenience; do not
let it block phases 1 and 2.

## Open questions

1. **Egress from sophia and deltaai is unverified** — both tunnels were down
   when this was written. If either sits behind a proxy with no HTTPS to
   github.com, its only route is push, and the timer is dead weight there.
   Answer before building phase 3; irrelevant to phases 1 and 2.
2. **The Windows swap.** No atomic directory rename with a junction. Options:
   run `local` from a fixed directory and update in place; or leave it on
   `mode = "off"` and keep using the `git pull` the developer already does. The
   second is probably right — `local` is the one host with a person at it.
3. **Per-release venv, or shared.** Per-release makes rollback complete and
   costs a few hundred MB each on a filesystem at 97%. Proposed: per-release
   with `keep_releases = 2`, and a `--shared-venv` escape for tight hosts.
4. **Moving the data dir out of the checkout** is a one-time manual migration on
   each host, and it blocks the releases layout. Worth doing in phase 1 while
   nothing depends on it yet.
5. **The client updates on its own schedule.** `client/ab.py` is stdlib-only and
   gets copied to laptops and compute nodes independently of any gateway. `ab
   fleet` reports gateway versions; a stale `ab` beside a batch script is a
   different problem, only partly covered by `install-skills` as a post-hook.
