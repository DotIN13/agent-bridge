import { For, Show, createMemo, createSignal } from "solid-js";
import type { GatewayState, JobStatus, TunnelState } from "../../protocol.ts";
import {
  connectGateway,
  disconnectGateway,
  gateway,
  jobs,
  openGatewayDialog,
  openJob,
  tunnelFor,
} from "../store.ts";
import { Button } from "./ui/button.tsx";
import { Icon } from "./ui/icon.tsx";
import { Copyable, Tag, Tooltip } from "./ui/primitives.tsx";
import { timeAgo } from "../lib/time.ts";

/**
 * One gateway's jobs.
 *
 * A window onto work happening somewhere else: what is running, what is parked
 * waiting for a batch report, and — when none of that is reachable — why not,
 * with the ssh console that explains it one click away.
 */
export function GatewayPage(props: { gateway: string }) {
  const [filter, setFilter] = createSignal("");
  const [console_, setConsole] = createSignal(false);

  const entry = () => gateway(props.gateway);
  const tunnel = () => tunnelFor(props.gateway);
  const rows = createMemo(() => {
    const query = filter().trim().toLowerCase();
    const all = jobs(props.gateway);
    if (!query) return all;
    return all.filter((job) => `${job.title ?? ""} ${job.id} ${job.cwd ?? ""}`.toLowerCase().includes(query));
  });

  /** How old the list is, which is the whole point when nothing is reachable. */
  const asOf = () => rows()[0]?.asOf;

  return (
    <div data-slot="page">
      <header data-slot="page-header">
        <div data-slot="page-title-row">
          <h1 data-slot="page-title">{props.gateway}</h1>
          <Show when={entry()}>
            {(detail) => (
              <>
                <Tag tone={statusTone(detail())}>{detail().status}</Tag>
                <Show when={detail().version}>{(version) => <span data-slot="meta">agent-bridge {version()}</span>}</Show>
                <span data-slot="meta">{detail().baseUrl}</span>
                {/* Not the port: it is the base URL's by definition, and saying
                    it twice reads as two facts. What is worth knowing is that an
                    ssh forward is what carries it. */}
                <Show when={detail().viaPort}>
                  <span data-slot="meta">via ssh</span>
                </Show>
                <Show when={detail().agents?.length}>
                  <span data-slot="meta">{detail().agents!.join(", ")}</span>
                </Show>
              </>
            )}
          </Show>

          <div data-slot="page-actions">
            <Show when={entry()?.ssh}>
              <Show
                when={live(tunnel())}
                fallback={
                  <Button
                    size="small"
                    variant="contrast"
                    icon="plug"
                    disabled={tunnel()?.runnable === false}
                    onClick={() => void connectGateway(props.gateway)}
                  >
                    Connect
                  </Button>
                }
              >
                <Button size="small" variant="outline" icon="stop" onClick={() => void disconnectGateway(props.gateway)}>
                  Disconnect
                </Button>
              </Show>
            </Show>
            <Button size="small" variant="neutral" icon="settings" onClick={() => openGatewayDialog(props.gateway)}>
              Configure
            </Button>
          </div>
        </div>

        <Show when={entry() && entry()!.status !== "connected" && entry()!.enabled}>
          <div data-slot="banner" data-tone="warning">
            <span data-slot="banner-mark">
              <Icon name="alert" size={12} />
            </span>
            <span>
              {entry()!.error ?? "not reachable yet"}
              <Show when={asOf()}> {" · "}cached, last seen {timeAgo(asOf())}</Show>
            </span>
          </div>
        </Show>

        <Show when={entry()?.tokenError && entry()!.status !== "connected"}>
          {(problem) => (
            <div data-slot="banner" data-tone="danger">
              <span data-slot="banner-mark">
                <Icon name="key" size={12} />
              </span>
              <span>{problem()}</span>
            </div>
          )}
        </Show>

        <Show when={tunnel()?.diagnostics.length}>
          <div data-slot="banner" data-tone="warning">
            <span data-slot="banner-mark">
              <Icon name="alert" size={12} />
            </span>
            <span>{tunnel()!.diagnostics.join(" ")}</span>
          </div>
        </Show>

        <div data-slot="page-tools">
          <div data-slot="search">
            <Icon name="search" size={13} />
            <input
              type="search"
              placeholder="Filter jobs…"
              value={filter()}
              onInput={(event) => setFilter(event.currentTarget.value)}
            />
          </div>
          <Show when={tunnel()}>
            {(detail) => (
              <button type="button" data-slot="console-toggle" onClick={() => setConsole(!console_())}>
                <Icon name="terminal" size={12} />
                ssh · {detail().status}
                <Show when={detail().pid}>{(pid) => <span data-slot="meta">pid {pid()}</span>}</Show>
              </button>
            )}
          </Show>
        </div>

        {/* ssh says nothing when it works, so this is empty on a good day — and
            it is the only place an answer lives on a bad one. */}
        <Show when={console_() && tunnel()}>
          <pre data-slot="console">
            <Show when={tunnel()!.log.length} fallback="(ssh has said nothing)">
              {tunnel()!.log.join("\n")}
            </Show>
          </pre>
        </Show>
      </header>

      <div data-slot="page-scroll">
        <Show
          when={rows().length > 0}
          fallback={
            <p data-slot="empty">
              {entry()?.status === "connected" ? "No jobs on this gateway yet." : "No jobs cached for this gateway."}
            </p>
          }
        >
          <table data-slot="job-table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Title</th>
                <th>Agent</th>
                <th>Directory</th>
                <th>Session</th>
                <th>Age</th>
                <th>Last event</th>
              </tr>
            </thead>
            <tbody>
              <For each={rows()}>
                {(job) => (
                  <tr data-slot="job-row" onClick={() => openJob(job.gateway, job.id)}>
                    <td>
                      <StatusPill status={job.status} />
                    </td>
                    <td class="truncate">{job.title ?? job.id.slice(0, 8)}</td>
                    <td>
                      <span data-slot="job-agent">
                        <span>{job.agent}</span>
                        <Show when={job.model}>{(model) => <span data-slot="job-model">{model()}</span>}</Show>
                      </span>
                    </td>
                    <td data-slot="job-path" title={job.cwd ?? ""}>
                      {shortPath(job.cwd)}
                    </td>
                    <td onClick={(event) => event.stopPropagation()}>
                      <Show when={job.session} fallback={<span data-slot="meta">—</span>}>
                        {(session) => (
                          // Eight characters is enough to tell two rows apart
                          // and never enough to *use* — the whole id is what
                          // goes into `ab submit --session`, so one click puts
                          // it on the clipboard.
                          <Copyable value={session()} display={session().slice(0, 8)} />
                        )}
                      </Show>
                    </td>
                    <td>{timeAgo(job.createdAt)}</td>
                    <td>{timeAgo(job.lastEventAt)}</td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </Show>
      </div>
    </div>
  );
}

/**
 * Seven states, and `waiting` is the one worth drawing differently.
 *
 * The agent turn finished and the work did not: it is where every job that
 * submits sbatch spends most of its life, and reading it as "done" is the single
 * most misleading thing this table could do.
 */
export function StatusPill(props: { status: JobStatus }) {
  return (
    <Tooltip label={statusText(props.status)} placement="top">
      <span>
        <Tag tone={jobTone(props.status)}>{props.status}</Tag>
      </span>
    </Tooltip>
  );
}

export function jobTone(status: JobStatus): "neutral" | "success" | "warning" | "danger" | "info" {
  switch (status) {
    case "running":
      return "info";
    case "queued":
      return "neutral";
    case "waiting":
    case "canceling":
      return "warning";
    case "succeeded":
      return "success";
    default:
      return "danger";
  }
}

function statusText(status: JobStatus): string {
  switch (status) {
    case "waiting":
      return "the turn ended without a report · the agent is still alive, waiting to write one";
    case "running":
      return "agent turn live";
    case "canceling":
      return "interrupt sent";
    case "queued":
      return "waiting for a worker";
    default:
      return status;
  }
}

function statusTone(entry: GatewayState): "neutral" | "success" | "warning" | "danger" | "info" {
  if (!entry.enabled) return "neutral";
  if (entry.status === "connected") return "success";
  if (entry.status === "unknown") return "warning";
  return "danger";
}

function live(tunnel: TunnelState | undefined): boolean {
  const status = tunnel?.status;
  return status === "up" || status === "starting" || status === "retrying" || status === "authenticating";
}

/**
 * The last two segments of a remote path.
 *
 * `/project/somebody/one_app` and its neighbour differ in the tail, and the
 * column is never wide enough for the head. The full path stays in `title`.
 */
function shortPath(cwd: string | null): string {
  if (!cwd) return "";
  const parts = cwd.split(/[\\/]/).filter(Boolean);
  if (parts.length <= 2) return cwd;
  return `…/${parts.slice(-2).join("/")}`;
}
