import { execFile } from "node:child_process";
import { accessSync, constants } from "node:fs";
import { promisify } from "node:util";
import type {
  AppState,
  ForwardState,
  GatewayState,
  Job,
  JobEvent,
  ServerMessage,
  TunnelState,
} from "../protocol.ts";
import { AskpassBridge } from "./askpass.ts";
import {
  forwardPortFor,
  loadConfig,
  removeEntry,
  renameEntry,
  watchConfig,
  writeEntry,
  type GatewayEntry,
} from "./config.ts";
import { GatewayClient, GatewayError } from "./gateway-client.ts";
import { JobTracker } from "./jobs.ts";
import { probeForward, type ProbeResult } from "./probe.ts";
import { parseSshCommand } from "./ssh-command.ts";
import { Tunnel } from "./tunnel.ts";

const exec = promisify(execFile);

/** Probe cadences. Slow is the resting state; fast is somebody looking. */
const PROBE_IDLE_MS = 60_000;
const PROBE_WATCHED_MS = 15_000;

/** Two consecutive failures before a row goes red: login nodes drop one. */
const MISSES_BEFORE_RED = 2;

interface HealthEntry extends ProbeResult {
  checkedAt: string;
  misses: number;
}

interface GatewayHealth {
  status: GatewayState["status"];
  version?: string;
  agents?: string[];
  error?: string;
  misses: number;
}

/**
 * Tunnels, gateways, and the jobs behind them.
 *
 * One object owns all of it because all of it is one question — "can I reach
 * this cluster, and what is running on it" — asked at three different depths: a
 * process, a port, and an HTTP endpoint. Keeping them apart is what produced a
 * dashboard that said `up` about a tunnel to a machine that had gone.
 */
export class Manager {
  private readonly askpass: AskpassBridge;
  /** Keyed by ssh command text: identical commands are one connection. */
  private readonly tunnels = new Map<string, Tunnel>();
  private readonly clients = new Map<string, GatewayClient>();
  private readonly trackers = new Map<string, JobTracker>();
  private readonly forwardHealth = new Map<string, HealthEntry>();
  private readonly gatewayHealth = new Map<string, GatewayHealth>();

  private entries: GatewayEntry[] = [];
  private errors: string[] = [];
  private configFile = "";
  private readOnly = false;
  private sshAvailable = false;
  private probeTimer: NodeJS.Timeout | null = null;
  private unwatchConfig: (() => void) | null = null;
  private watching: string | null = null;
  private publishTimer: NodeJS.Timeout | null = null;

  /**
   * Where state goes. Set by `publishTo` once the HTTP server exists.
   *
   * The two are circular — the manager pushes to the sockets, and the socket
   * handlers call the manager — so one of them has to be wired after the fact.
   * A no-op default rather than an optional callback: every emit site would
   * otherwise carry a `?.`, and a manager with nowhere to publish is a
   * perfectly good manager in a test.
   */
  private emit: (message: ServerMessage) => void = () => {};

  constructor() {
    this.askpass = new AskpassBridge((event) => {
      const gateway = event.prompt?.gateway ?? event.gateway;
      const tunnel = gateway ? this.tunnelFor(gateway) : undefined;
      if (event.kind === "prompt") tunnel?.notePrompt();
      else {
        tunnel?.notePromptClosed();
        // An answered password is the moment the row is most wrong: the tunnel
        // is about to come up and the last probe said nothing was there. Waiting
        // out the resting cadence leaves it stale for a minute after a login
        // that worked.
        this.scheduleProbe(2500);
      }
      this.publish();
    });
  }

  publishTo(emit: (message: ServerMessage) => void): void {
    this.emit = emit;
  }

  async start(): Promise<void> {
    await this.askpass.start();
    this.sshAvailable = await hasSsh();
    this.reload();
    /*
     * `gateways.json` belongs to `ab`, so it changes without going through us:
     * a gateway added at the CLI, a token rotated, a `base_url` corrected. Read
     * once at startup, every one of those needs a restart to be seen.
     */
    this.unwatchConfig = watchConfig(() => this.reload());
    this.scheduleProbe(0);

    for (const entry of this.entries) {
      if (entry.enabled && entry.autoStart && entry.ssh) this.up(entry.name, false);
    }
  }

  async stop(): Promise<void> {
    this.unwatchConfig?.();
    this.unwatchConfig = null;
    if (this.probeTimer) clearTimeout(this.probeTimer);
    this.probeTimer = null;
    if (this.publishTimer) clearTimeout(this.publishTimer);
    this.publishTimer = null;
    for (const tracker of this.trackers.values()) tracker.stop();
    for (const tunnel of this.tunnels.values()) tunnel.down();
    await this.askpass.stop();
  }

  /**
   * Re-read the config and reconcile what is running against it.
   *
   * A tunnel that is up stays up: its record may have been re-read from disk,
   * but the process and the two-factor push that authenticated it are not
   * something to throw away because a file was touched.
   */
  reload(): void {
    const config = loadConfig();
    this.entries = config.gateways;
    this.errors = [
      ...config.errors,
      ...config.gateways.flatMap((entry) =>
        (entry.spec?.diagnostics ?? []).map((line) => `${entry.name}: ${line}`),
      ),
    ];
    this.configFile = config.configPath;
    this.readOnly = config.readOnly;

    for (const entry of this.entries) {
      if (entry.ssh && entry.spec) {
        const existing = this.tunnels.get(entry.ssh);
        if (existing) existing.update(entry.spec, entry.ssh, entry.autoStart);
        else {
          this.tunnels.set(
            entry.ssh,
            new Tunnel(entry.name, entry.spec, entry.ssh, entry.autoStart, this.askpass, () => this.publish()),
          );
        }
      }

      const client = this.clients.get(entry.name);
      if (client) client.update(entry.baseUrl, entry.token);
      else this.clients.set(entry.name, new GatewayClient(entry.name, entry.baseUrl, entry.token));

      if (!this.trackers.has(entry.name)) {
        const tracker = new JobTracker(
          this.clients.get(entry.name)!,
          {
            jobs: (jobs) => this.emit({ type: "jobs", gateway: entry.name, jobs }),
            job: (job) => this.emit({ type: "job", gateway: entry.name, job }),
            events: (jobId, events) => this.emit({ type: "events", gateway: entry.name, jobId, events }),
            follow: (jobId, state, error) =>
              this.emit({ type: "follow", gateway: entry.name, jobId, state, ...(error ? { error } : {}) }),
          },
          () => this.gatewayHealth.get(entry.name)?.status === "connected",
        );
        this.trackers.set(entry.name, tracker);
        if (entry.enabled) tracker.start();
      } else if (entry.enabled) {
        this.trackers.get(entry.name)!.start();
      } else {
        this.trackers.get(entry.name)!.stop();
      }
    }

    // Entries that have gone from the file. A running tunnel is stopped: the
    // alternative is an ssh process holding a port with nothing in the config
    // that explains it.
    const commands = new Set(this.entries.map((entry) => entry.ssh).filter(Boolean) as string[]);
    for (const [command, tunnel] of this.tunnels) {
      if (commands.has(command)) continue;
      tunnel.down();
      this.tunnels.delete(command);
    }
    const names = new Set(this.entries.map((entry) => entry.name));
    for (const [name, tracker] of this.trackers) {
      if (names.has(name)) continue;
      tracker.stop();
      this.trackers.delete(name);
      this.clients.delete(name);
      this.gatewayHealth.delete(name);
    }

    this.publish();
  }

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  state(): AppState {
    return {
      gateways: this.entries.map((entry) => this.gatewayState(entry)),
      tunnels: this.tunnelStates(),
      prompts: this.askpass.open(),
      errors: this.errors,
      configPath: this.configFile,
      sshAvailable: this.sshAvailable,
    };
  }

  get configIsReadOnly(): boolean {
    return this.readOnly;
  }

  private gatewayState(entry: GatewayEntry): GatewayState {
    const health = this.gatewayHealth.get(entry.name);
    const tunnel = entry.ssh ? this.tunnels.get(entry.ssh) : undefined;
    return {
      name: entry.name,
      baseUrl: entry.baseUrl,
      ssh: entry.ssh,
      exec: entry.exec,
      execCommand: entry.spec?.remoteCommand,
      enabled: entry.enabled,
      isDefault: entry.isDefault,
      tokenSource: entry.tokenSource,
      tokenName: entry.tokenName,
      tokenError: entry.tokenError,
      status: entry.enabled ? (health?.status ?? "unknown") : "disabled",
      version: health?.version,
      agents: health?.agents,
      error: entry.enabled ? health?.error : undefined,
      jobs: this.trackers.get(entry.name)?.counts() ?? { running: 0, waiting: 0, total: 0 },
      viaPort: forwardPortFor(entry.baseUrl, entry.spec),
      tunnel: tunnel?.name,
    };
  }

  private tunnelStates(): TunnelState[] {
    const states: TunnelState[] = [];
    for (const [command, tunnel] of this.tunnels) {
      const gateways = this.entries.filter((entry) => entry.ssh === command);
      const forwards: ForwardState[] = (gateways[0]?.spec?.forwards ?? []).map((forward) => {
        const key = `${tunnel.name}:${forward.localPort}`;
        const health = this.forwardHealth.get(key);
        return {
          ...forward,
          health: health?.health ?? "closed",
          serves: health?.serves,
          error: health?.error,
          checkedAt: health?.checkedAt,
        };
      });
      states.push(tunnel.state(forwards, gateways.map((entry) => entry.name), this.runnable(gateways[0]?.spec?.binary)));
    }
    return states;
  }

  /** Coalesced: a burst of ssh log lines is one repaint, not forty. */
  publish(): void {
    if (this.publishTimer) return;
    this.publishTimer = setTimeout(() => {
      this.publishTimer = null;
      this.emit({ type: "state", state: this.state() });
    }, 60);
    this.publishTimer.unref();
  }

  // -------------------------------------------------------------------------
  // Tunnels
  // -------------------------------------------------------------------------

  private tunnelFor(gateway: string): Tunnel | undefined {
    const entry = this.entries.find((candidate) => candidate.name === gateway);
    if (!entry?.ssh) return undefined;
    return this.tunnels.get(entry.ssh);
  }

  /**
   * Can the binary this command names be run?
   *
   * A bare `ssh` is a PATH question, asked once at startup. A path is a file
   * question, asked here — which is what lets a command spelling out
   * `/opt/homebrew/bin/ssh` work on a machine with no `ssh` on PATH at all.
   */
  private runnable(binary: string | undefined): boolean {
    if (!binary) return this.sshAvailable;
    try {
      accessSync(binary, constants.X_OK);
      return true;
    } catch {
      return false;
    }
  }

  up(gateway: string, interactive = true): void {
    const tunnel = this.tunnelFor(gateway);
    if (!tunnel) throw new Error(`gateway "${gateway}" has no ssh command`);
    const entry = this.entries.find((candidate) => candidate.name === gateway);
    if (!this.runnable(entry?.spec?.binary)) {
      throw new Error(
        entry?.spec?.binary
          ? `cannot run ${entry.spec.binary}`
          : "no ssh on PATH — install OpenSSH, or write the full path in the command",
      );
    }
    tunnel.up(interactive);
    // The click is the reason to look: probe as soon as the child has had a
    // chance to bind, rather than waiting out the resting cadence.
    this.scheduleProbe(2500);
    this.publish();
  }

  down(gateway: string): void {
    const tunnel = this.tunnelFor(gateway);
    if (!tunnel) throw new Error(`gateway "${gateway}" has no ssh command`);
    tunnel.down();
    this.scheduleProbe(500);
    this.publish();
  }

  answerPrompt(id: string, answer: string | null): boolean {
    return this.askpass.answer(id, answer);
  }

  // -------------------------------------------------------------------------
  // Config edits
  // -------------------------------------------------------------------------

  saveGateway(
    name: string,
    patch: {
      baseUrl?: string;
      ssh?: string | null;
      tokenEnv?: string | null;
      tokenFile?: string | null;
      enabled?: boolean;
      autoStart?: boolean;
      makeDefault?: boolean;
      rename?: string;
      exec?: true | string | false;
    },
  ): void {
    if (this.readOnly) throw new Error(`${this.configFile} is TOML; edit it by hand`);

    const entry: Record<string, unknown> = {};
    if (patch.baseUrl !== undefined) {
      const url = patch.baseUrl.trim().replace(/\/+$/, "");
      if (!url) throw new Error("base_url is required");
      assertUrl(url);
      entry.base_url = url;
    }
    if (patch.ssh !== undefined) entry.ssh = patch.ssh?.trim() ? patch.ssh.trim() : undefined;
    // `true` for the shipped default, a string for a script of their own, and
    // the key removed when nothing should run — three states, one field.
    if (patch.exec !== undefined) {
      entry.exec = patch.exec === false ? undefined : patch.exec;
    }
    // The three token forms are mutually exclusive, so setting one clears the
    // others: an entry carrying both `token_env` and `token_file` reads as two
    // sources of truth, and `ab` silently prefers one of them.
    if (patch.tokenEnv !== undefined) {
      entry.token_env = patch.tokenEnv?.trim() ? patch.tokenEnv.trim() : undefined;
      if (entry.token_env) entry.token_file = undefined;
    }
    if (patch.tokenFile !== undefined) {
      entry.token_file = patch.tokenFile?.trim() ? patch.tokenFile.trim() : undefined;
      if (entry.token_file) entry.token_env = undefined;
    }
    if (patch.enabled !== undefined) entry.enabled = patch.enabled;
    if (patch.autoStart !== undefined) entry.autostart = patch.autoStart || undefined;

    if (patch.rename && patch.rename !== name) {
      renameEntry(name, patch.rename);
      name = patch.rename;
    }
    writeEntry(name, entry, { makeDefault: patch.makeDefault });
    // The watch would catch this a quarter-second later; reloading now means
    // the answer to the request already carries the row the dialog just saved.
    this.reload();
  }

  removeGateway(name: string): void {
    if (this.readOnly) throw new Error(`${this.configFile} is TOML; edit it by hand`);
    removeEntry(name);
    this.reload();
  }

  parseSsh(command: string): ReturnType<typeof parseSshCommand> {
    return parseSshCommand(command);
  }

  // -------------------------------------------------------------------------
  // Jobs
  // -------------------------------------------------------------------------

  client(name: string): GatewayClient {
    const client = this.clients.get(name);
    if (!client) throw new Error(`unknown gateway "${name}"`);
    return client;
  }

  tracker(name: string): JobTracker {
    const tracker = this.trackers.get(name);
    if (!tracker) throw new Error(`unknown gateway "${name}"`);
    return tracker;
  }

  /** Which gateway's job list is on screen. Only one, since only one is. */
  setWatched(name: string | null): void {
    if (this.watching === name) return;
    if (this.watching) this.trackers.get(this.watching)?.setWatched(false);
    this.watching = name;
    if (name) this.trackers.get(name)?.setWatched(true);
    this.scheduleProbe(name ? 0 : PROBE_IDLE_MS);
  }

  follow(gateway: string, jobId: string): Promise<void> {
    return this.tracker(gateway).follow(jobId);
  }

  unfollow(gateway: string, jobId: string): void {
    this.trackers.get(gateway)?.unfollow(jobId);
  }

  events(gateway: string, jobId: string): JobEvent[] {
    return this.trackers.get(gateway)?.events(jobId) ?? [];
  }

  jobs(gateway: string): Job[] {
    return this.trackers.get(gateway)?.list() ?? [];
  }

  /** The refresh button: probe now, and ask every tracker to poll now. */
  refresh(): void {
    this.scheduleProbe(0);
    for (const tracker of this.trackers.values()) tracker.refresh();
  }

  // -------------------------------------------------------------------------
  // Probing
  // -------------------------------------------------------------------------

  private scheduleProbe(delay: number): void {
    if (this.probeTimer) clearTimeout(this.probeTimer);
    this.probeTimer = setTimeout(() => void this.probe(), delay);
    this.probeTimer.unref();
  }

  private async probe(): Promise<void> {
    const seen = new Set<string>();

    for (const [command, tunnel] of this.tunnels) {
      const gateways = this.entries.filter((entry) => entry.ssh === command);
      const spec = gateways[0]?.spec;
      for (const forward of spec?.forwards ?? []) {
        const key = `${tunnel.name}:${forward.localPort}`;
        seen.add(key);
        // Whether anything is expected to answer HTTP on this port: only the
        // one a gateway's base URL points at.
        const expectGateway = gateways.some((entry) => forwardPortFor(entry.baseUrl, entry.spec) === forward.localPort);
        const result = await probeForward(forward, expectGateway);
        const previous = this.forwardHealth.get(key);
        const misses = result.health === "closed" ? (previous?.misses ?? 0) + 1 : 0;
        // One miss keeps the previous rung: a login node drops a connection now
        // and then, and a row that flickers red teaches nobody anything. Its
        // `checkedAt` is kept with it — the row is showing an older reading, and
        // stamping it with now would say the opposite.
        this.forwardHealth.set(
          key,
          result.health === "closed" && misses < MISSES_BEFORE_RED && previous
            ? { ...previous, misses }
            : { ...result, misses, checkedAt: new Date().toISOString() },
        );
      }
    }
    for (const key of [...this.forwardHealth.keys()]) if (!seen.has(key)) this.forwardHealth.delete(key);

    await Promise.all(this.entries.map((entry) => this.probeGateway(entry)));

    this.publish();
    this.scheduleProbe(this.watching ? PROBE_WATCHED_MS : PROBE_IDLE_MS);
  }

  /**
   * The endpoint's own answer, which is the one that matters.
   *
   * A tunnel that is up in front of a gateway that refuses the token is not a
   * working gateway, and the row has to say which of the two it is: `/health`
   * needs no token, `/v1/agents` does, so asking both in order separates "the
   * port is dead" from "the token is wrong" without guessing.
   */
  private async probeGateway(entry: GatewayEntry): Promise<void> {
    if (!entry.enabled) {
      this.gatewayHealth.set(entry.name, { status: "disabled", misses: 0 });
      return;
    }

    const tunnel = entry.ssh ? this.tunnels.get(entry.ssh) : undefined;
    // Never promote out of `authenticating`: whatever is answering that local
    // port while ssh is still asking for a password is not this tunnel.
    if (tunnel?.authenticating) {
      const previous = this.gatewayHealth.get(entry.name);
      this.gatewayHealth.set(entry.name, {
        status: previous?.status === "connected" ? "connected" : "unknown",
        error: "waiting for the ssh prompt to be answered",
        misses: previous?.misses ?? 0,
        version: previous?.version,
        agents: previous?.agents,
      });
      return;
    }

    const client = this.clients.get(entry.name);
    if (!client) return;

    const previous = this.gatewayHealth.get(entry.name);
    try {
      const health = await client.health();
      if (!health.ok) throw new GatewayError("gateway did not answer ok", "unhealthy", 0);
      let agents: string[] | undefined;
      try {
        const listed = await client.agents();
        agents = listed.configured ?? [];
      } catch (err) {
        const error = err as GatewayError;
        if (error.status === 401 || error.status === 403) {
          this.gatewayHealth.set(entry.name, {
            status: "unauthorized",
            version: health.version,
            error: entry.tokenError ?? error.message,
            misses: 0,
          });
          return;
        }
        // Reachable, and something else went wrong: still connected, because
        // `/health` answered from the far end.
      }
      this.gatewayHealth.set(entry.name, {
        status: "connected",
        version: health.version,
        agents,
        misses: 0,
      });
      this.trackers.get(entry.name)?.refresh();
    } catch (err) {
      const misses = (previous?.misses ?? 0) + 1;
      if (previous?.status === "connected" && misses < MISSES_BEFORE_RED) {
        this.gatewayHealth.set(entry.name, { ...previous, misses });
        return;
      }
      this.gatewayHealth.set(entry.name, {
        status: "unreachable",
        error: (err as Error).message,
        misses,
      });
    }
  }
}

function assertUrl(value: string): void {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${value} is not a URL`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("base_url must be http or https");
}

/** Is there an ssh to run? Asked once: the answer does not change at runtime. */
async function hasSsh(): Promise<boolean> {
  try {
    await exec("ssh", ["-V"], { windowsHide: true });
    return true;
  } catch (err) {
    // ssh prints its version on *stderr* and exits 255 on some builds, which
    // execFile reports as a failure. Only ENOENT means there is no ssh.
    return (err as NodeJS.ErrnoException).code !== "ENOENT";
  }
}
