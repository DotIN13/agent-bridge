/**
 * The vocabulary the server and the page share.
 *
 * One file, imported by both halves, because every drift bug this dashboard
 * could have is a field the server renamed and the page kept reading. The job
 * and event shapes are agent-bridge's own (`gateway/api_models.py`) with the
 * snake_case turned over at the boundary in `gateway-client.ts` — the page
 * never sees two spellings of the same field.
 */

// ---------------------------------------------------------------------------
// Tunnels
// ---------------------------------------------------------------------------

/**
 * `authenticating` is not cosmetic: it is the state in which a probe must not
 * promote the tunnel. Something else answering the forwarded port while ssh is
 * still asking for a password is not this tunnel working.
 */
export type TunnelStatus = "off" | "starting" | "authenticating" | "up" | "retrying" | "failed";

/** Why a tunnel stopped, when ssh said enough to tell. */
export type BlockedReason = "auth" | "port_in_use" | "unknown_host" | "no_ssh" | "refused";

export interface Forward {
  kind: "local" | "dynamic";
  localPort: number;
  /** Absent on a `-D` proxy, which has no single far end. */
  remoteHost?: string;
  remotePort?: number;
}

/**
 * How far a forward actually works.
 *
 * The rungs exist because "listening" lies: when the far end of an `ssh -L`
 * dies, the local socket keeps accepting connections and then resets them, so
 * a check that stops at `connect()` reports a healthy tunnel to a machine that
 * is gone.
 */
export type ForwardHealth = "closed" | "listening" | "reachable" | "serving";

export interface ForwardState extends Forward {
  health: ForwardHealth;
  /** What answered, when something did — "agent-bridge 0.3.0". */
  serves?: string;
  error?: string;
  checkedAt?: string;
}

export interface TunnelState {
  /** The first gateway that named this command; the tunnel is called after it. */
  name: string;
  /**
   * Every gateway riding this one connection.
   *
   * Two entries with the same `ssh` line are one tunnel, not two: spawning the
   * second would bind a port the first already holds, and ssh would refuse it
   * with `address already in use` — a failure that says nothing about what the
   * user actually configured.
   */
  gateways: string[];
  status: TunnelStatus;
  /** The command as written in the config, which is what we run. */
  command: string;
  destination: string;
  forwards: ForwardState[];
  pid?: number;
  since?: string;
  attempts: number;
  blocked?: BlockedReason;
  /** ssh's own stderr, capped and scanned for anything token-shaped. */
  log: string[];
  /** Refusals from reading the command — a `-R`, a non-loopback bind. */
  diagnostics: string[];
  /**
   * Whether the binary this command names can actually be run.
   *
   * Per tunnel rather than per machine: a command that spells out
   * `/opt/homebrew/bin/ssh` is runnable on a machine with nothing called `ssh`
   * on PATH, and a global "no ssh here" flag disabled its button anyway.
   */
  runnable: boolean;
}

// ---------------------------------------------------------------------------
// Gateways
// ---------------------------------------------------------------------------

export type GatewayStatus = "connected" | "unauthorized" | "unreachable" | "disabled" | "unknown";

export interface GatewayState {
  name: string;
  baseUrl: string;
  /** The ssh command that makes `baseUrl` reachable, when the entry names one. */
  ssh?: string;
  enabled: boolean;
  isDefault: boolean;
  /** Where the token comes from. The token itself never leaves the server. */
  tokenSource: "token" | "token_env" | "token_file" | "none";
  /**
   * The name of the variable or the path of the file it is read from.
   *
   * Not the token — the *pointer* to it, which the config dialog has to show or
   * an edit that only meant to change a base URL would save the entry back with
   * its token source blanked.
   */
  tokenName?: string;
  tokenError?: string;
  status: GatewayStatus;
  version?: string;
  agents?: string[];
  error?: string;
  /** Counts from the last poll, for the badge on the sidebar row. */
  jobs: { running: number; waiting: number; total: number };
  /** Which forward carries this base URL, when one does. */
  viaPort?: number;
  /** The tunnel that carries it, which may be named after another gateway. */
  tunnel?: string;
}

/** A question ssh asked, waiting on a human. */
export interface AuthPrompt {
  id: string;
  gateway: string;
  text: string;
  /** Recent ssh stderr, so the dialog can show what it is answering. */
  context: string[];
  createdAt: string;
  expiresAt: string;
}

export interface AppState {
  gateways: GatewayState[];
  tunnels: TunnelState[];
  prompts: AuthPrompt[];
  /** Problems with the config itself, which no row can carry. */
  errors: string[];
  configPath: string;
  /** False when there is no ssh on PATH — every connect button is dead. */
  sshAvailable: boolean;
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export type JobStatus =
  | "queued"
  | "running"
  | "waiting"
  | "canceling"
  | "succeeded"
  | "failed"
  | "canceled";

export const TERMINAL_STATUSES: readonly JobStatus[] = ["succeeded", "failed", "canceled"];

export interface Job {
  gateway: string;
  id: string;
  title: string | null;
  status: JobStatus;
  agent: string;
  cwd: string | null;
  model: string | null;
  session: string | null;
  costUsd: number | null;
  createdAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  lastEventAt: string | null;
  /** Detail-only, from `/v1/jobs/<id>`. */
  prompt?: string | null;
  result?: string | null;
  error?: string | null;
  /** When this row was last believed, so a cached list can say how old it is. */
  asOf: string;
}

export type JobEventType =
  | "status"
  | "assistant"
  | "thinking"
  | "tool_use"
  | "tool_result"
  | "steer"
  | "result"
  | "error"
  | "log"
  | "message";

export interface JobEvent {
  seq: number;
  /** The gateway's local clock, offset attached. */
  at: string | null;
  /** Seconds since the job's first event, computed by the gateway. */
  elapsed: number | null;
  /** `+HH:MM:SS`, which is the number worth reading on a long run. */
  elapsedHms: string | null;
  type: JobEventType;
  data: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// The socket
// ---------------------------------------------------------------------------

/** Whether a job's log is loading, live, or could not be reached. */
export type FollowState = "loading" | "live" | "error";

export type ServerMessage =
  | { type: "state"; state: AppState }
  | { type: "jobs"; gateway: string; jobs: Job[] }
  | { type: "job"; gateway: string; job: Job }
  | { type: "events"; gateway: string; jobId: string; events: JobEvent[] }
  | { type: "follow"; gateway: string; jobId: string; state: FollowState; error?: string };

export type ClientMessage =
  /** Which gateway's job list is on screen; `null` when none is. */
  | { type: "watch"; gateway: string | null }
  | { type: "follow"; gateway: string; jobId: string }
  | { type: "unfollow"; gateway: string; jobId: string };
