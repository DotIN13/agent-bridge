# 23 — The report is the result, and progress is a tool

Two changes to the reporting contract from design/15 and design/17, in the same
direction: fewer ways to say a thing, so the ways that remain cannot disagree.

1. **`$AB_JOB_DIR/report.md` is the job's `result`.** Its content is written to
   the row, so `ab job <ref>` prints the deliverable, and it still lands on the
   event stream as a `message`. A delegate states its findings once, in the file.
2. **Progress goes through `ab-notify`.** The equivalent `echo` into
   `progress/` still works and is no longer what the skills teach.

## Why the report and not the closing message

Before this, `result` held the turn's last message and `report.md` was one more
`message` event. So the skills asked for the findings twice — a comprehensive
report *and* a self-contained closing message — and a caller reading `ab job`
got the message while the artifact sat in the event stream.

Two answers to one question is a bug with a delay on it. They start identical,
then a delegate revises the report and not the message, or writes the report from
the sbatch output and the message from memory, and the one the caller reads first
is the one nobody updated. Worse, only one of them is *asked for*: the report is
required (a job with none fails with `report_missing`, design/17), and the
message is whatever the turn happened to end with.

So the file wins, for the reason it was made required in the first place: it is
the thing the delegate was told to produce, the thing that survives being read
months later, and the thing whose absence is already a failure. The turn's own
text is not discarded — it is on the event stream where it always was, and it
remains the `result` for a job that has no report, which is what a failed run
looks like.

The worker skill already claimed `ab job <ref>` printed the report. This makes
that true. Worth noticing as a class of defect: a document describing behaviour
nobody had implemented, sitting next to code that did something else, with
neither contradicting the other loudly enough to be caught by a test.

**Both channels, on purpose.** The row and the stream answer different
questions — *what was the answer* and *when did the work report* — and a reader
who has one wants the other about half the time. The row means no event paging
for the common case; the event means a live follower sees the report land.

## Where it is written

Two paths reach a finished job, and the report has to win on both:

| Path | Where |
|---|---|
| report already written when the turn ends (short jobs, and long ones that wrote a preliminary report) | `worker._run_job`, before the fields are saved |
| report lands while the row is `waiting` | `server._finish_if_reported`, before the status change |

The second orders the two writes deliberately: `result` first, then
`finish_reported`. A reader that sees `succeeded` never sees it without the
answer.

The first needed a smaller fix than it looks: `fields["result"]` was being set
from the report and then overwritten a few lines later by
`fields.update(status=…, result=result.result, …)` on the success branch. The
`result` key is no longer restated there, and a comment says why — it is exactly
the kind of line somebody re-adds for symmetry.

### Two bounds, because the report is not a note

`report.md` is read to `MAX_REPORT_BYTES` (2 MiB) and everything else in the job
dir to `MAX_FILE_BYTES` (64 KiB). It shared the smaller one at first, which was
the wrong bound for what the file had just become: a milestone is a line in a
log, and losing its tail costs a sentence, but the report is what the caller
reads back into its own context, and losing its tail costs the answer — silently,
in the one place a reader has no way to notice, since a truncated document ends
looking like a document.

Both copies get the same limit, deliberately: the `result` column and the
`message` event carry identical text, and a bounded row beside a whole event
would reintroduce the two-answers problem this whole change exists to remove.

Still bounded, at a number chosen for a real document with its tables, diffs and
log excerpts quoted in full. `report.md` fed from a training log is a mistake
rather than a large report, and it should meet a ceiling before the database
does; the truncation note on the event says which happened. Beyond that, a report
is a document and not an artifact store — the paths it names are how the big
things travel.

Writing the API-level test found a third thing, which is why it was worth writing
one: reads already ingested the job dir but did not settle the row, so
`GET /v1/jobs/<id>` could answer `waiting` in a body whose own event list carried
`report.md` — a response contradicting itself for up to one sweep interval. Both
read paths now settle before answering (`_ingest_and_settle`), which is also the
honest reading of why ingestion happens on a read at all: a read is the moment
somebody is asking.


## Why progress is a tool and not a path

`ab-notify` was already the recommended way and its own docstring undercut it:
*"a convenience rather than a dependency — the same milestone is `echo … >
$AB_JOB_DIR/progress/010-up.md`"*. True, and the reason to prefer the tool is
that the `echo` form has three requirements the shell does not enforce:

- the name has to **sort** in the order things happened (ingestion is by name,
  not mtime, because mtime on a shared filesystem is the less trustworthy of the
  two);
- the content has to stay under **64 KiB**, or the gateway truncates it — a
  milestone's bound, not the report's;
- a retried step wants the **same id** so its second note overwrites the first
  instead of piling up — dedup is by path *and* digest.

A brief that teaches the `echo` has to teach all three. `ab-notify --msg … --report-id sources` holds them, so the skills name the tool and the hand-written
form is a fallback for a host where it is missing. The ingestion is unchanged:
`progress/` is still read, because that is what `ab-notify` writes into.

`status` also stays readable and stays undocumented. It named a job's state back
when a row could park waiting to be closed; the turn's end decides that now
(design/16), so a delegate that writes one out of habit is heard and nothing
acts on it. Removing the read would turn a harmless habit into a silent loss.

## Tests

Five cases, each naming a way this could be got wrong rather than restating the
happy path. In `test_waiting.py`:

- a report written during the turn is what `ab job` prints, and the turn's text
  did **not** override it;
- a run with no report keeps its own answer, so nothing is lost on the failure
  path;
- a report landing during `waiting` replaces the interim answer the turn saved;
- the report is a `message` event *as well as* the result.

Two more in `test_job_dir.py` for the bounds, since a bound nobody measures is a
comment: a report eight times a milestone's limit comes back whole from *both*
the event and `read_report`, and one past 2 MiB is truncated to exactly that with
the limit named in the note. The milestone truncation test moved to a
`progress/` file, which is what it was always about.

And one through HTTP rather than the db, because the claim is about the answer a
caller gets: `GET /v1/jobs/<id>` returns `succeeded` with
the report as `result`, and the same report appears exactly once on the event
page. That is the test that found the read-path gap above.
