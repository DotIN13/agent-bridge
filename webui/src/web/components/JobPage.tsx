import { For, Show, createEffect, createResource, createSignal, onCleanup, onMount } from "solid-js";
import type { Job, JobEvent } from "../../protocol.ts";
import { TERMINAL_STATUSES } from "../../protocol.ts";
import { api } from "../lib/api.ts";
import { elapsed } from "../lib/time.ts";
import { cancelJob, closeJob, events, followState, gateway, job, steerJob } from "../store.ts";
import { Button, IconButton } from "./ui/button.tsx";
import { Icon } from "./ui/icon.tsx";
import { Copyable, Spinner, Tag, Tooltip } from "./ui/primitives.tsx";
import { StatusPill } from "./GatewayPage.tsx";

const TERMINAL = new Set<string>(TERMINAL_STATUSES);

/** 120px of slack: near enough the end that you meant to be at it. */
const NEAR_BOTTOM = 120;

/**
 * One job's event log.
 *
 * Not a chat, and it must not pretend to be one. Two actions, because they are
 * the two things worth doing to a run you are watching go wrong: interrupt it,
 * or tell it something.
 */
export function JobPage(props: { gateway: string; jobId: string }) {
  const [draft, setDraft] = createSignal("");
  const [sending, setSending] = createSignal(false);
  let scroller: HTMLDivElement | undefined;
  /** Whether to keep the log pinned to its end, which scrolling up gives up. */
  let stuck = true;

  const row = () => job(props.gateway, props.jobId);
  const log = () => events(props.gateway, props.jobId);
  const follow = () => followState(props.gateway, props.jobId);
  const entry = () => gateway(props.gateway);
  const running = () => row()?.status === "running";

  /**
   * The prompt and the result, which the list rows do not carry.
   *
   * Fetched once per job rather than polled: `prompt` never changes, and the
   * `result` that matters arrives as an event anyway.
   */
  const [detail] = createResource(
    () => [props.gateway, props.jobId] as const,
    ([g, id]) =>
      api<{ job: Job }>(`/gateways/${encodeURIComponent(g)}/jobs/${encodeURIComponent(id)}`)
        .then((body) => body.job)
        .catch(() => null),
  );

  /**
   * Why the log is empty, when it is.
   *
   * Three empties that look identical and are not: the fetch has not answered,
   * the gateway cannot be reached — the usual one, since the job list survives
   * in the server's cache and the log does not — and a job that said nothing.
   */
  const unreachable = () => follow()?.state === "error" || Boolean(entry() && entry()!.status !== "connected");
  const loading = () => !follow() || follow()!.state === "loading";

  onMount(() => {
    const element = scroller;
    if (!element) return;
    const onScroll = () => {
      stuck = element.scrollHeight - element.scrollTop - element.clientHeight <= NEAR_BOTTOM;
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    onCleanup(() => element.removeEventListener("scroll", onScroll));
  });

  /*
   * An event arriving re-pins the log.
   *
   * Counting is enough: an event is appended and never edited, and on a job
   * that emits a line every few minutes the answer is always the newest one —
   * so a log that had to be scrolled by hand was one that was out of date
   * whenever you looked away.
   */
  createEffect((previous: number | undefined) => {
    const count = log().length;
    if (count !== previous && stuck) {
      queueMicrotask(() => {
        if (scroller) scroller.scrollTop = scroller.scrollHeight;
      });
    }
    return count;
  });

  const send = async () => {
    const text = draft().trim();
    if (!text) return;
    setSending(true);
    try {
      await steerJob(props.gateway, props.jobId, text);
      setDraft("");
    } finally {
      setSending(false);
    }
  };

  return (
    <div data-slot="page">
      <header data-slot="page-header">
        <div data-slot="page-title-row">
          <Tooltip label="Back to the job list" placement="bottom">
            <IconButton icon="back" label="Back to the job list" size="normal" variant="ghost" onClick={closeJob} />
          </Tooltip>
          <h1 data-slot="page-title">{row()?.title ?? detail()?.title ?? props.jobId.slice(0, 8)}</h1>
          <Show when={row()}>
            {(detail_) => (
              <>
                <StatusPill status={detail_().status} />
                <span data-slot="meta">{props.gateway}</span>
                <Show when={detail_().model}>{(model) => <span data-slot="meta">{model()}</span>}</Show>
                <Show when={detail_().cwd}>{(cwd) => <span data-slot="meta">{cwd()}</span>}</Show>
              </>
            )}
          </Show>
          <div data-slot="page-actions">
            <Copyable value={props.jobId} display={props.jobId.slice(0, 8)} label="Copy the job id" />
            <Show when={row() && !TERMINAL.has(row()!.status)}>
              <Tooltip label="Interrupt · SIGINT, then escalate" placement="bottom">
                <Button size="small" variant="danger" onClick={() => void cancelJob(props.gateway, props.jobId)}>
                  Cancel
                </Button>
              </Tooltip>
            </Show>
          </div>
        </div>

        <Show when={unreachable()}>
          <div data-slot="banner" data-tone="warning">
            <span data-slot="banner-mark">
              <Icon name="alert" size={12} />
            </span>
            <span>{follow()?.error ?? entry()?.error ?? "the gateway is not reachable"}</span>
          </div>
        </Show>

        {/* A parked job is the normal end of anything that submitted sbatch,
            and the report that closes it is the only progress it has. */}
        <Show when={row()?.status === "waiting"}>
          <div data-slot="banner" data-tone="warning">
            <span data-slot="banner-mark">
              <Icon name="clock" size={12} />
            </span>
            <span>the turn ended without a report · the agent is alive, waiting to write one</span>
          </div>
        </Show>

        <Show when={detail()?.error}>
          {(problem) => (
            <div data-slot="banner" data-tone="danger">
              <span data-slot="banner-mark">
                <Icon name="alert" size={12} />
              </span>
              <span>{problem()}</span>
            </div>
          )}
        </Show>

        <Show when={detail()?.prompt}>
          {(prompt) => (
            <details data-slot="prompt">
              <summary>prompt</summary>
              <p>{prompt()}</p>
            </details>
          )}
        </Show>
      </header>

      <div data-slot="page-scroll" ref={scroller}>
        <Show
          when={log().length > 0}
          fallback={
            <Show when={!unreachable()}>
              <Show
                when={!loading()}
                fallback={
                  <p data-slot="empty" class="flex items-center gap-2">
                    <Spinner size={11} />
                    Loading the log…
                  </p>
                }
              >
                <p data-slot="empty">No events yet.</p>
              </Show>
            </Show>
          }
        >
          <ol data-slot="event-log">
            <For each={log()}>{(event) => <EventRow event={event} />}</For>
          </ol>
        </Show>
      </div>

      <footer data-slot="steer">
        <input
          type="text"
          placeholder={running() ? "Steer this run…" : "No running turn to steer"}
          disabled={!running() || sending()}
          value={draft()}
          onInput={(event) => setDraft(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void send();
          }}
        />
        <Tooltip label="delivered at the next tool boundary" placement="top">
          <Button size="small" variant="contrast" disabled={!running() || sending() || !draft().trim()} onClick={() => void send()}>
            Steer
          </Button>
        </Tooltip>
      </footer>
    </div>
  );
}

/**
 * One event.
 *
 * The types with a shape worth drawing get one — assistant text, a tool call, a
 * batch report — and everything else is a log line. Leading with the elapsed
 * time rather than the clock is deliberate: on a run that has been going for
 * hours, *where in the run* something happened is the actual question.
 */
function EventRow(props: { event: JobEvent }) {
  const data = () => props.event.data;
  const text = (key: string): string => (typeof data()[key] === "string" ? (data()[key] as string) : "");

  return (
    <li data-slot="event-row" data-type={props.event.type}>
      <Tooltip label={props.event.at ?? "no timestamp"} placement="right">
        <span data-slot="event-when">{elapsed(props.event)}</span>
      </Tooltip>
      <span data-slot="event-body">
        <EventContent event={props.event} text={text} data={data} />
      </span>
    </li>
  );
}

/**
 * Never call one of these `Switch`, `Show`, `For` or `Match`.
 *
 * Solid's JSX transform auto-imports a control-flow component when it sees the
 * name in JSX and the file has not imported it — so a local component with one
 * of those names is silently replaced by Solid's, which then receives the wrong
 * props. It throws in the dev server and not in the production build, because
 * only one of the two auto-imports.
 */
function EventContent(props: {
  event: JobEvent;
  text: (key: string) => string;
  data: () => Record<string, unknown>;
}) {
  return (
    <Show when={props.event.type === "message"} fallback={<Rendered {...props} />}>
      {/* A milestone from `ab-notify` or a monitor: for a parked job, the only
          progress there is. */}
      <span data-slot="event-report">
        <Tag tone={reportTone(String(props.data().status ?? ""))}>{String(props.data().status ?? "report")}</Tag>
        <Show when={props.data().slurm_job_id}>{(id) => <code>slurm {String(id())}</code>}</Show>
        <Show when={props.data().host}>{(host) => <code>{String(host())}</code>}</Show>
        <Show when={props.text("msg")}>{(msg) => <span>{msg()}</span>}</Show>
      </span>
    </Show>
  );
}

function Rendered(props: {
  event: JobEvent;
  text: (key: string) => string;
  data: () => Record<string, unknown>;
}) {
  const kind = () => props.event.type;
  return (
    <Show when={kind() !== "tool_use" && kind() !== "tool_result"} fallback={<ToolLine {...props} />}>
      <Show when={kind() !== "status"} fallback={<StatusLine {...props} />}>
        <span data-slot="event-text">
          {/* `prompt` is the steering one: without it a steer event rendered as
              the raw `{"prompt": …}` object, which is the payload rather than
              the thing somebody said. */}
          {props.text("text") ||
            props.text("message") ||
            props.text("prompt") ||
            props.text("result") ||
            JSON.stringify(props.data())}
        </span>
      </Show>
    </Show>
  );
}

/** The init record is where the model actually running is finally stated. */
function StatusLine(props: { data: () => Record<string, unknown> }) {
  return (
    <span data-slot="event-status">
      <strong>{String(props.data().subtype ?? props.data().stage ?? "status")}</strong>
      <Show when={props.data().model}>{(model) => <code>{String(model())}</code>}</Show>
      <Show when={props.data().session_id}>{(id) => <code>{String(id()).slice(0, 8)}</code>}</Show>
      <Show when={props.data().reason}>{(reason) => <span>{String(reason())}</span>}</Show>
    </span>
  );
}

function ToolLine(props: {
  event: JobEvent;
  text: (key: string) => string;
  data: () => Record<string, unknown>;
}) {
  const name = () => String(props.data().name ?? "tool");
  const input = () => {
    const raw = props.data().input;
    if (!raw) return "";
    const value = raw as Record<string, unknown>;
    const first =
      typeof value.command === "string" ? value.command : typeof value.file_path === "string" ? value.file_path : "";
    return first || JSON.stringify(raw);
  };

  return (
    <Show
      when={props.event.type === "tool_use"}
      fallback={<span data-slot="event-tool-result">{clip(props.text("text") || props.text("content"))}</span>}
    >
      <span data-slot="event-tool">
        <Icon name="terminal" size={11} />
        <strong>{name()}</strong>
        <code class="truncate">{clip(input())}</code>
      </span>
    </Show>
  );
}

function reportTone(status: string): "neutral" | "success" | "warning" | "danger" | "info" {
  if (status === "finished" || status === "succeeded") return "success";
  if (status === "failed" || status === "error") return "danger";
  if (status === "running" || status === "started") return "info";
  return "warning";
}

/** The gateway stores tool results whole; a log line is not where to read one. */
function clip(value: string, limit = 400): string {
  const flat = value.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}
