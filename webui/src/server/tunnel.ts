import { spawn, type ChildProcess } from "node:child_process";
import type { BlockedReason, ForwardState, TunnelState, TunnelStatus } from "../protocol.ts";
import type { AskpassBridge } from "./askpass.ts";
import { buildSshArgs, type SshSpec } from "./ssh-command.ts";

/** Lines of ssh's own stderr kept per tunnel. It is the only thing ssh says. */
const LOG_LINES = 60;

/** Backoff for a connection that dropped on its own, capped so it stays sane. */
const BACKOFF_MS = [2_000, 5_000, 15_000, 30_000, 60_000];

/**
 * How long a live child has to stay alive before it counts as up.
 *
 * `ssh -N` prints nothing when it works, so "up" is inferred from not having
 * exited. The probe ladder is what actually decides whether the ports carry
 * anything; this only says the process survived long enough to have opened them.
 */
const SETTLE_MS = 1_500;

/**
 * What went wrong, from what ssh printed.
 *
 * `auth` is the one that matters: it stops the supervisor rather than slowing it
 * down. Retrying a two-factor host on a timer is a stream of push notifications
 * to somebody's phone and a plausible route to a locked account.
 */
export function classify(stderr: string): BlockedReason | null {
  if (/permission denied|too many authentication failures|authentication failed|no supported authentication/i.test(stderr)) {
    return "auth";
  }
  if (/address already in use|cannot listen to port|bind: /i.test(stderr)) return "port_in_use";
  if (/could not resolve hostname|name or service not known|nodename nor servname/i.test(stderr)) return "unknown_host";
  if (/connection refused|connection timed out|no route to host|network is unreachable/i.test(stderr)) return "refused";
  return null;
}

/**
 * How a child died, in words.
 *
 * Node reports a signal death as a negative `code` on some paths and as a
 * `signal` on others, and the negative number went out to the page as "ssh
 * exited with code -9: Success" — a status line that is wrong twice over.
 */
export function exitReason(code: number | null, signal: NodeJS.Signals | null): string {
  if (signal) return `killed by ${signal}`;
  if (code === null) return "exited";
  if (code < 0) return `killed by signal ${-code}`;
  return `exited with code ${code}`;
}

/**
 * One ssh process per gateway, carrying every forward its command names.
 *
 * The command is the user's own, so the supervisor's whole job is to run it, to
 * keep it alive when the network drops it, and to get its questions in front of
 * somebody — never to decide what a working connection to their cluster looks
 * like.
 */
export class Tunnel {
  private child: ChildProcess | null = null;
  private retryTimer: NodeJS.Timeout | null = null;
  private settleTimer: NodeJS.Timeout | null = null;
  private stopping = false;
  private log: string[] = [];
  private status: TunnelStatus = "off";
  private attempts = 0;
  private since: string | undefined;
  private blocked: BlockedReason | undefined;
  /** Whether this attempt could prompt at all, and whether it ever did. */
  private interactive = false;
  private askedThisAttempt = 0;
  /**
   * Open prompts for this tunnel.
   *
   * Load-bearing, not bookkeeping: while ssh is still asking for a credential
   * the tunnel is *not* up, however long the process has been alive. Promoting
   * on the timer alone reported a connection whose password box was still on
   * screen — and if something else happened to be answering that local port,
   * the row went green for somebody else's tunnel.
   */
  private prompts = 0;

  constructor(
    readonly name: string,
    private spec: SshSpec,
    private command: string,
    private autoStart: boolean,
    private readonly askpass: AskpassBridge,
    private readonly onChange: () => void,
  ) {}

  update(spec: SshSpec, command: string, autoStart: boolean): void {
    this.spec = spec;
    this.command = command;
    this.autoStart = autoStart;
  }

  /** True while we own a live process for this gateway. */
  get running(): boolean {
    return this.child !== null;
  }

  get authenticating(): boolean {
    return this.prompts > 0;
  }

  state(forwards: ForwardState[], gateways: string[], runnable = true): TunnelState {
    return {
      name: this.name,
      gateways,
      runnable,
      status: this.status,
      command: this.command,
      destination: this.spec.destination,
      forwards,
      pid: this.child?.pid,
      since: this.since,
      attempts: this.attempts,
      blocked: this.blocked,
      log: [...this.log],
      diagnostics: [...this.spec.diagnostics],
    };
  }

  /**
   * Bring the tunnel up.
   *
   * `interactive` is the difference between a click and a retry. A click may
   * raise a prompt — that is the whole of the askpass bridge — and a retry runs
   * with `BatchMode=yes` so it cannot.
   */
  up(interactive = true): void {
    if (this.child) return;
    if (!this.spec.destination) {
      this.push("no host in the ssh command — nothing to connect to");
      this.setStatus("failed");
      return;
    }
    this.stopping = false;
    this.clearRetry();
    this.blocked = undefined;
    this.interactive = interactive;
    this.askedThisAttempt = 0;
    this.setStatus("starting");

    const args = buildSshArgs(this.spec, { batch: !interactive });
    const session = interactive ? this.askpass.begin(this.name, () => [...this.log]) : null;
    this.push(`$ ${this.spec.binary ?? "ssh"} ${args.join(" ")}`);

    let child: ChildProcess;
    try {
      child = spawn(this.spec.binary ?? "ssh", args, {
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
        env: { ...process.env, ...(session?.env ?? {}) },
      });
    } catch (err) {
      session?.end();
      this.push((err as Error).message);
      this.blocked = "no_ssh";
      this.setStatus("failed");
      return;
    }

    this.child = child;
    this.since = new Date().toISOString();

    for (const stream of [child.stderr, child.stdout]) {
      stream?.setEncoding("utf8");
      stream?.on("data", (chunk: string) => {
        for (const line of chunk.split(/\r?\n/)) if (line.trim()) this.push(line.trim());
        const reason = classify(chunk);
        if (reason) this.blocked = reason;
      });
    }

    child.once("error", (err: NodeJS.ErrnoException) => {
      this.push(err.message);
      if (err.code === "ENOENT") this.blocked = "no_ssh";
    });

    child.once("exit", (code, signal) => {
      session?.end();
      this.child = null;
      this.clearSettle();
      const clean = this.stopping || signal === "SIGTERM";
      if (clean) {
        this.setStatus("off");
        return;
      }
      this.push(`ssh ${exitReason(code, signal)}`);
      this.noteSilentAskpass();
      this.retry();
    });

    this.scheduleSettle(SETTLE_MS);
  }

  /** Take it down, and mean it: no retry follows a deliberate stop. */
  down(): void {
    this.stopping = true;
    this.clearRetry();
    this.clearSettle();
    const child = this.child;
    if (child) {
      child.kill("SIGTERM");
      // An ssh holding a connection can outlive a polite ask; give it a moment,
      // then insist.
      setTimeout(() => {
        if (this.child === child) child.kill("SIGKILL");
      }, 3000).unref();
    }
    this.setStatus("off");
  }

  /** ssh has asked something. Until it is answered this tunnel is not up. */
  notePrompt(): void {
    this.prompts++;
    this.askedThisAttempt++;
    this.clearSettle();
    this.setStatus("authenticating");
  }

  /**
   * A prompt closed, answered or not.
   *
   * The settle timer starts again only when nothing else is waiting: a
   * two-factor login asks twice, and the first answer is not the end of the
   * conversation.
   */
  notePromptClosed(): void {
    this.prompts = Math.max(0, this.prompts - 1);
    if (this.prompts === 0 && this.child) this.scheduleSettle(SETTLE_MS);
    else this.onChange();
  }

  /**
   * A credential was refused, and ssh never asked us for one.
   *
   * The interesting case is not a wrong password — it is an ssh that ignored
   * `SSH_ASKPASS` and so had no way to ask. Some Windows builds do that
   * (`askpass.ts` has the references), and from the outside it is
   * indistinguishable from a rejected password: the same exit code, the same
   * "Permission denied". Saying which it was is the difference between "try
   * again" and "this ssh cannot ask through this channel".
   */
  private noteSilentAskpass(): void {
    if (!this.interactive || this.blocked !== "auth" || this.askedThisAttempt > 0) return;
    this.push("ssh never asked for a credential through the askpass helper, so nothing could be answered.");
    this.push(
      process.platform === "win32"
        ? "Windows ssh honours SSH_ASKPASS on some builds and not others. Try Git for Windows' ssh.exe named in full, or a key in the OpenSSH agent."
        : "Check that this ssh honours SSH_ASKPASS, or use a key or an agent.",
    );
  }

  /**
   * A dropped connection comes back on its own; a rejected one does not.
   *
   * An authentication failure ends the supervisor here rather than backing off,
   * for the reason in `classify`. Everything else is worth another go, slower
   * each time — and only when the entry asked to be kept up, since a tunnel
   * somebody started by hand should not come back without them.
   */
  private retry(): void {
    if (this.stopping) {
      this.setStatus("off");
      return;
    }
    if (this.blocked === "auth" || this.blocked === "no_ssh") {
      this.setStatus("failed");
      return;
    }
    if (!this.autoStart) {
      this.setStatus(this.blocked ? "failed" : "off");
      return;
    }

    const delay = BACKOFF_MS[Math.min(this.attempts, BACKOFF_MS.length - 1)]!;
    this.attempts++;
    this.setStatus("retrying");
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      // Unattended: no prompt can appear, because nobody clicked anything.
      this.up(false);
    }, delay);
    this.retryTimer.unref();
  }

  private scheduleSettle(delay: number): void {
    this.clearSettle();
    this.settleTimer = setTimeout(() => {
      this.settleTimer = null;
      if (!this.child || this.prompts > 0) return;
      if (this.status !== "starting" && this.status !== "authenticating") return;
      this.attempts = 0;
      this.setStatus("up");
    }, delay);
    this.settleTimer.unref();
  }

  private clearSettle(): void {
    if (this.settleTimer) clearTimeout(this.settleTimer);
    this.settleTimer = null;
  }

  private clearRetry(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private setStatus(status: TunnelStatus): void {
    if (this.status === status) return;
    this.status = status;
    this.onChange();
  }

  /** Capped, and scanned: a token must never reach the browser in a log line. */
  private push(line: string): void {
    const safe = line.replace(/(bearer\s+|token[=:]\s*)\S+/gi, "$1[redacted]");
    this.log.push(safe);
    if (this.log.length > LOG_LINES) this.log = this.log.slice(-LOG_LINES);
    this.onChange();
  }
}
