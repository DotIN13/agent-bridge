# 25 — The delegate's tools are not the client: `worker/`

`ab-notify` and `ab-monitor` lived in `client/` and ran on the other side of the
connection from everything else in it. They are now `worker/notify.py` and
`worker/monitor.py`.

## The line that was already there

`client/` is the caller's side: `ab` on a laptop, and `abclient.py`, the
transport it drives — base urls, bearer tokens, SSE, retries, uploads. The two
tools that moved do none of that. They run *inside a job* on the gateway host,
write a file into `$AB_JOB_DIR`, and exit:

| | `client/` | `worker/` |
|---|---|---|
| Runs on | the caller's machine | the machine the job runs on |
| Talks to | the gateway's HTTP API | the filesystem |
| Needs | base url, token, job id | `$AB_JOB_DIR`, and nothing else |
| Fails when | the tunnel is down | the job dir is gone |

Nothing imported across that line before the move — no `abclient` in either
file, no `ab_notify` in `ab.py` — so the separation was already complete in the
code. Only the directory disagreed, and a directory that disagrees with the code
is a directory that teaches the wrong thing: it invited a future edit to reach
for `abclient` from inside a job, which is exactly the dependency design/15
removed when it made reporting a directory instead of an HTTP call.

The stdlib-only constraint now reads as two facts rather than one blurred
one. `client/` is stdlib-only so a laptop needs no install; `worker/` is
stdlib-only because a compute node may have no network at all and cannot be
assumed to have anything installed. Same rule, different reasons, and the second
is the stricter of the two.

## The name, and the collision to be careful about

`gateway/worker.py` is the gateway's pool of job runners — the thing that
*starts* an agent. `worker/` is that agent's own half. Two different senses of
one word in one repository, which is a real cost, accepted for two reasons: the
delegate-facing skill is already called `agent-bridge-worker`, so this is the
vocabulary the briefs use; and the import forms keep them apart at every use
site — `worker.notify` against `gateway.worker`, never a bare `worker`.

The module names lost their `ab_` prefix on the way, because it was standing in
for the missing directory. `worker/notify.py` says what `client/ab_notify.py`
was trying to.

## What moved with it

The two console scripts point at the new modules (`ab-notify =
worker.notify:main`), `worker*` is in the packages list, and the `bin/` shims
import from `worker`. The command names are unchanged, which is what matters:
`ab-notify` is written into briefs, skills and old sbatch scripts, and none of
those know where the module lives.

Tests moved to `tests/worker/`. `test_monitor.py` keeps both halves of the
monitor story in one file even though the code is now split — the asymmetry
between the delegate's file drop and the caller's API read *is* the subject
there, and a test that can only see one half stops being able to state it.

Design records 15, 16 and todo/13 still name `client/ab_notify.py`. They are
records of what particular commits touched, and rewriting them to a path that
did not exist then would make them less true, not more.
