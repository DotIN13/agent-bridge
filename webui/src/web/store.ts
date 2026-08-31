import { createStore } from "solid-js/store";
import type { AppState, FollowState, Job, JobEvent, ServerMessage } from "../protocol.ts";
import { api, del, post, put } from "./lib/api.ts";
import { connect, type Socket } from "./lib/socket.ts";
import { readToken } from "./lib/token.ts";

/** Where the page is. Three views, one at a time, no history to keep. */
export type Route =
  | { view: "home" }
  | { view: "gateway"; gateway: string }
  | { view: "job"; gateway: string; jobId: string };

interface Ui {
  /** The socket's own state, which is the only thing the page cannot ask for. */
  online: boolean;
  /** Null until the first `state` frame: "no gateways" and "not loaded yet"
      look identical otherwise, and one of them is worth a spinner. */
  app: AppState | null;
  readOnly: boolean;
  /** No token in the URL and none in this tab: nothing will work, say so. */
  needsToken: boolean;
  route: Route;
  jobs: Record<string, Job[]>;
  /** Keyed `gateway/jobId`, so one map covers every job page ever opened. */
  events: Record<string, JobEvent[]>;
  follow: Record<string, { state: FollowState; error?: string }>;
  /** The last thing that went wrong, shown as a strip under the header. */
  error: string | null;
  /**
   * The config dialog, when it is open. `name: null` is a new gateway.
   *
   * In the store rather than in a component because both the sidebar row and
   * the gateway page open it, and a second copy of the dialog behind the first
   * is the kind of bug that only shows up once someone uses both.
   */
  dialog: { name: string | null } | null;
}

const [state, setState] = createStore<Ui>({
  online: false,
  app: null,
  readOnly: false,
  needsToken: false,
  route: { view: "home" },
  jobs: {},
  events: {},
  follow: {},
  error: null,
  dialog: null,
});

export { state };

let socket: Socket | null = null;

export function key(gateway: string, jobId: string): string {
  return `${gateway}/${jobId}`;
}

export function start(): void {
  if (!readToken()) {
    setState("needsToken", true);
    return;
  }

  socket = connect({
    onMessage: receive,
    onOpen: () => {
      setState("online", true);
      // The server released everything this page was watching when the old
      // socket closed, so a reconnect has to say again what is on screen.
      const route = state.route;
      if (route.view === "gateway") socket?.send({ type: "watch", gateway: route.gateway });
      if (route.view === "job") {
        socket?.send({ type: "watch", gateway: route.gateway });
        socket?.send({ type: "follow", gateway: route.gateway, jobId: route.jobId });
      }
    },
    onClose: () => setState("online", false),
  });

  void api<{ readOnly: boolean }>("/state")
    .then((body) => setState("readOnly", body.readOnly))
    .catch((err: Error) => setState("error", err.message));
}

function receive(message: ServerMessage): void {
  if (message.type === "state") {
    setState("app", message.state);
    return;
  }
  if (message.type === "jobs") {
    setState("jobs", message.gateway, message.jobs);
    return;
  }
  if (message.type === "job") {
    // A whole new array rather than a mutation in place: the list may not exist
    // yet — an event can land for a gateway whose first poll has not answered —
    // and `produce` has no undefined to work with.
    const current = state.jobs[message.gateway] ?? [];
    const at = current.findIndex((job) => job.id === message.job.id);
    const next = at === -1 ? [message.job, ...current] : current.with(at, message.job);
    setState("jobs", message.gateway, next);
    return;
  }
  if (message.type === "events") {
    const id = key(message.gateway, message.jobId);
    setState("events", id, (previous: JobEvent[] | undefined) => merge(previous ?? [], message.events));
    return;
  }
  if (message.type === "follow") {
    setState("follow", key(message.gateway, message.jobId), {
      state: message.state,
      ...(message.error ? { error: message.error } : {}),
    });
  }
}

/**
 * Events, deduplicated by `seq`.
 *
 * The server hands a page the whole ring when it starts following and then
 * broadcasts each new event, so the two overlap by design — and a reconnect
 * replays from the last sequence *the server* saw, not this page. `seq` is
 * monotonic per job, which makes it the only join key needed.
 */
function merge(existing: JobEvent[], incoming: JobEvent[]): JobEvent[] {
  const seen = new Set(existing.map((event) => event.seq));
  const fresh = incoming.filter((event) => !seen.has(event.seq));
  if (!fresh.length) return existing;
  return [...existing, ...fresh].sort((a, b) => a.seq - b.seq);
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

export function openGateway(gateway: string): void {
  const previous = state.route;
  if (previous.view === "job") socket?.send({ type: "unfollow", gateway: previous.gateway, jobId: previous.jobId });
  setState("route", { view: "gateway", gateway });
  socket?.send({ type: "watch", gateway });
}

export function openJob(gateway: string, jobId: string): void {
  setState("route", { view: "job", gateway, jobId });
  socket?.send({ type: "follow", gateway, jobId });
}

/** The back button: to the job list, and let go of the job's stream. */
export function closeJob(): void {
  const route = state.route;
  if (route.view !== "job") return;
  socket?.send({ type: "unfollow", gateway: route.gateway, jobId: route.jobId });
  setState("route", { view: "gateway", gateway: route.gateway });
}

export function goHome(): void {
  const route = state.route;
  if (route.view === "job") socket?.send({ type: "unfollow", gateway: route.gateway, jobId: route.jobId });
  setState("route", { view: "home" });
  socket?.send({ type: "watch", gateway: null });
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

/**
 * Every action, with its failure put somewhere the page can draw it.
 *
 * A rejected promise in an event handler is a silent no-op on screen, which is
 * how a connect button that did nothing looked exactly like one that worked.
 */
async function act<T>(run: () => Promise<T>): Promise<T | null> {
  try {
    const value = await run();
    setState("error", null);
    return value;
  } catch (err) {
    setState("error", (err as Error).message);
    return null;
  }
}

export function dismissError(): void {
  setState("error", null);
}

export function openGatewayDialog(name: string | null): void {
  setState("dialog", { name });
}

export function closeGatewayDialog(): void {
  setState("dialog", null);
}

export function connectGateway(name: string): Promise<unknown> {
  return act(() => post(`/gateways/${encodeURIComponent(name)}/up`, { interactive: true }));
}

export function disconnectGateway(name: string): Promise<unknown> {
  return act(() => post(`/gateways/${encodeURIComponent(name)}/down`));
}

export function refresh(): Promise<unknown> {
  return act(() => post("/refresh"));
}

export function answerPrompt(id: string, answer: string | null): Promise<unknown> {
  return act(() => post(`/prompts/${encodeURIComponent(id)}`, { answer }));
}

export interface GatewayPatch {
  baseUrl?: string;
  /** `true` for the shipped default, a script of your own, `false` for none. */
  exec?: true | string | false;
  ssh?: string | null;
  tokenEnv?: string | null;
  tokenFile?: string | null;
  enabled?: boolean;
  autoStart?: boolean;
  makeDefault?: boolean;
  rename?: string;
}

export function saveGateway(name: string, patch: GatewayPatch): Promise<unknown> {
  return act(() => put(`/gateways/${encodeURIComponent(name)}`, patch));
}

export function removeGateway(name: string): Promise<unknown> {
  return act(async () => {
    await del(`/gateways/${encodeURIComponent(name)}`);
    if (state.route.view !== "home" && state.route.gateway === name) goHome();
  });
}

export function cancelJob(gateway: string, jobId: string): Promise<unknown> {
  return act(() =>
    post(`/gateways/${encodeURIComponent(gateway)}/jobs/${encodeURIComponent(jobId)}/cancel`),
  );
}

export function steerJob(gateway: string, jobId: string, prompt: string): Promise<unknown> {
  return act(() =>
    post(`/gateways/${encodeURIComponent(gateway)}/jobs/${encodeURIComponent(jobId)}/steer`, { prompt }),
  );
}

export interface ParsedSsh {
  binary?: string;
  destination: string;
  /** What will run on the far side when the tunnel comes up, if anything. */
  remoteCommand?: string;
  forwards: Array<{ kind: "local" | "dynamic"; localPort: number; remoteHost?: string; remotePort?: number }>;
  port?: number;
  jump?: string;
  identity?: string;
  diagnostics: string[];
}

export function parseSsh(command: string): Promise<ParsedSsh | null> {
  return act(() => post<ParsedSsh>("/ssh/parse", { command }));
}

// ---------------------------------------------------------------------------
// Selectors
// ---------------------------------------------------------------------------

export function gateway(name: string) {
  return state.app?.gateways.find((entry) => entry.name === name);
}

/** The tunnel carrying this gateway, which two gateways may share. */
export function tunnelFor(name: string) {
  const entry = gateway(name);
  if (!entry?.ssh) return undefined;
  return state.app?.tunnels.find((tunnel) => tunnel.gateways.includes(name) || tunnel.name === entry.tunnel);
}

export function jobs(name: string): Job[] {
  return state.jobs[name] ?? [];
}

export function job(gatewayName: string, jobId: string): Job | undefined {
  return jobs(gatewayName).find((entry) => entry.id === jobId);
}

export function events(gatewayName: string, jobId: string): JobEvent[] {
  return state.events[key(gatewayName, jobId)] ?? [];
}

export function followState(gatewayName: string, jobId: string) {
  return state.follow[key(gatewayName, jobId)];
}
