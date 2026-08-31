"""The delegate's own tools: what an agent running a job reaches for.

Two commands, both stdlib only and both writing into `$AB_JOB_DIR` rather than
calling the API -- no job id, no url, no token, and they work on a compute node
that cannot reach the gateway over HTTP:

    ab-notify    a milestone, while the work is still going
    ab-monitor   a watch on work that outlives the turn

They were in `client/` and did not belong there. `client/` is the *caller's*
side -- `ab` and the transport it drives from a laptop -- and these run on the
far side of the same connection, in the job. Nothing imported across the line,
so the split was already real; only the directory disagreed.

Not to be confused with `gateway/worker.py`, which is the gateway's own pool of
job runners. That one is the thing that *starts* an agent; this is the agent's
half of the conversation. Imports read `worker.notify` against `gateway.worker`,
which keeps the two apart at every use site.
"""
