import { TERMINAL_STATUSES, type Job, type JobEvent, type JobStatus } from "../protocol.ts";
import { GatewayClient } from "./gateway-client.ts";

/** Events kept per followed job. Older pages come from the gateway on demand. */
const RING = 2000;

/** Poll cadences: a job list in front of you, versus a count in the sidebar. */
const WATCHED_MS = 5_000;
const BACKGROUND_MS = 30_000;
/** A `waiting` job's report lands long after its event stream has closed. */
const REPORT_POLL_MS = 20_000;

const TERMINAL = new Set<string>(TERMINAL_STATUSES);

interface Follow {
  /** How many sockets are watching. The stream closes when the last lets go. */
  count: number;
  abort: AbortController;
  lastSeq: number;
}

/**
 * One gateway's jobs: the list, the followed streams, and the rings.
 *
 * The tracker is what lets a browser open a job that has been running for an
 * hour and see the whole run — the server holds the stream, accumulates while
 * nobody is looking, and fans it out to whichever pages are.
 */
export class JobTracker {
  private readonly jobs = new Map<string, Job>();
  private readonly rings = new Map<string, JobEvent[]>();
  private readonly follows = new Map<string, Follow>();
  private timer: NodeJS.Timeout | null = null;
  private watched = false;
  private failures = 0;
  private stopped = true;
  private lastError: string | null = null;

  constructor(
    private readonly client: GatewayClient,
    private readonly emit: {
      jobs: (jobs: Job[]) => void;
      job: (job: Job) => void;
      events: (jobId: string, events: JobEvent[]) => void;
      follow: (jobId: string, state: "loading" | "live" | "error", error?: string) => void;
    },
    /** Only poll while the gateway behind it is actually answering. */
    private readonly reachable: () => boolean,
  ) {}

  get name(): string {
    return this.client.name;
  }

  list(): Job[] {
    return [...this.jobs.values()].sort(byInterest);
  }

  events(jobId: string): JobEvent[] {
    return this.rings.get(jobId) ?? [];
  }

  /**
   * What the sidebar badges count.
   *
   * `running` includes `queued` and `canceling`: from the outside they are all
   * "this gateway is busy with something", and three badges on a row 280px wide
   * is a row nobody reads. `waiting` stays its own number because it is the one
   * that means the opposite of what it looks like — the turn is over and the
   * work is not.
   */
  counts(): { running: number; waiting: number; total: number } {
    let running = 0;
    let waiting = 0;
    for (const job of this.jobs.values()) {
      if (job.status === "running" || job.status === "canceling" || job.status === "queued") running++;
      if (job.status === "waiting") waiting++;
    }
    return { running, waiting, total: this.jobs.size };
  }

  error(): string | null {
    return this.lastError;
  }

  /** A job list is open on this gateway, so poll like someone is watching. */
  setWatched(watched: boolean): void {
    if (this.watched === watched) return;
    this.watched = watched;
    if (!this.stopped) this.schedule(watched ? 0 : BACKGROUND_MS);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.schedule(0);
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    for (const [id, follow] of this.follows) {
      follow.abort.abort();
      this.follows.delete(id);
    }
  }

  /** Ask now, whatever the cadence says — the refresh button. */
  refresh(): void {
    if (!this.stopped) this.schedule(0);
  }

  private schedule(delay: number): void {
    if (this.stopped) return;
    if (this.timer) clearTimeout(this.timer);
    // Jittered, so several gateways do not wake in lockstep every five seconds.
    const jitter = delay ? delay * 0.15 * Math.random() : 0;
    this.timer = setTimeout(() => void this.poll(), delay + jitter);
    this.timer.unref();
  }

  private interval(): number {
    if (!this.watched) return BACKGROUND_MS;
    const busy = [...this.jobs.values()].some((job) => !TERMINAL.has(job.status));
    return busy ? WATCHED_MS : WATCHED_MS * 3;
  }

  private async poll(): Promise<void> {
    if (this.stopped) return;

    // A gateway whose tunnel is down is not asked: the row already says what is
    // wrong, and a stack of identical failures says it worse.
    if (!this.reachable()) {
      this.schedule(BACKGROUND_MS);
      return;
    }

    try {
      const page = await this.client.jobs(50);
      this.failures = 0;
      this.lastError = null;
      let changed = page.jobs.length !== this.jobs.size;
      for (const job of page.jobs) {
        const previous = this.jobs.get(job.id);
        if (!previous || previous.status !== job.status || previous.lastEventAt !== job.lastEventAt) changed = true;
        this.jobs.set(job.id, job);
      }
      if (changed) this.emit.jobs(this.list());
      this.schedule(this.interval());
    } catch (err) {
      this.failures++;
      this.lastError = (err as Error).message;
      // Back off, but never past a minute: the usual cause is a tunnel that is
      // about to come back.
      this.schedule(Math.min(BACKGROUND_MS * Math.min(this.failures, 2), 60_000));
    }
  }

  /**
   * One upstream stream per job, however many browsers are looking at it.
   *
   * The history it fetches is filed silently: the ring outlives a follow, so a
   * job opened, closed and opened again has nothing new to broadcast, and a
   * page relying on that broadcast would draw an empty log. Whoever asked gets
   * the ring from the route instead, which is true in both cases.
   */
  async follow(jobId: string): Promise<void> {
    const existing = this.follows.get(jobId);
    if (existing) {
      existing.count++;
      // Already streaming: the ring is what the new watcher needs, and the
      // route hands that over directly.
      this.emit.follow(jobId, "live");
      return;
    }

    const follow: Follow = { count: 1, abort: new AbortController(), lastSeq: 0 };
    this.follows.set(jobId, follow);
    this.emit.follow(jobId, "loading");

    // The history first, so a page opens on the run so far rather than on
    // whatever happens next.
    try {
      const page = await this.client.events(jobId, { tail: 400 });
      this.absorb(jobId, page.events, { announce: false });
      follow.lastSeq = page.events.at(-1)?.seq ?? 0;
      this.emit.follow(jobId, "live");
    } catch (err) {
      // Said rather than swallowed: an empty log and an unreachable one look
      // identical on screen, and only one of them is the job's fault.
      this.emit.follow(jobId, "error", (err as Error).message);
    }

    void this.stream(jobId, follow);
  }

  unfollow(jobId: string): void {
    const follow = this.follows.get(jobId);
    if (!follow) return;
    follow.count--;
    if (follow.count > 0) return;
    follow.abort.abort();
    this.follows.delete(jobId);
  }

  private async stream(jobId: string, follow: Follow): Promise<void> {
    while (!follow.abort.signal.aborted) {
      try {
        await this.client.follow(jobId, follow.lastSeq, follow.abort.signal, (event) => {
          follow.lastSeq = Math.max(follow.lastSeq, event.seq);
          this.absorb(jobId, [event]);
        });
      } catch (err) {
        if (follow.abort.signal.aborted) return;
        this.emit.follow(jobId, "error", (err as Error).message);
        // A dropped stream is a reconnect, from the last sequence seen.
        await sleep(3000, follow.abort.signal);
        continue;
      }

      /*
       * The stream ended because the *turn* is done. That is not the same as
       * the job being done: a run that submitted sbatch is parked in `waiting`,
       * and the report that closes it arrives over minutes or hours through a
       * channel the closed stream will never carry. So polling takes over until
       * the job is really terminal.
       */
      const job = this.jobs.get(jobId);
      if (job && !TERMINAL.has(job.status)) {
        await sleep(REPORT_POLL_MS, follow.abort.signal);
        if (follow.abort.signal.aborted) return;
        await this.catchUp(jobId, follow);
        continue;
      }
      return;
    }
  }

  private async catchUp(jobId: string, follow: Follow): Promise<void> {
    try {
      const page = await this.client.events(jobId, { after: follow.lastSeq });
      if (page.events.length) {
        follow.lastSeq = page.events.at(-1)!.seq;
        this.absorb(jobId, page.events);
      }
      const job = this.jobs.get(jobId);
      if (job && job.status !== page.status) {
        const next: Job = { ...job, status: page.status as JobStatus, asOf: new Date().toISOString() };
        this.jobs.set(jobId, next);
        this.emit.job(next);
      }
    } catch {
      // Next tick.
    }
  }

  private absorb(jobId: string, events: JobEvent[], options: { announce?: boolean } = {}): void {
    if (!events.length) return;

    const ring = this.rings.get(jobId) ?? [];
    const seen = new Set(ring.map((event) => event.seq));
    const fresh = events.filter((event) => !seen.has(event.seq));
    if (!fresh.length) return;

    const next = [...ring, ...fresh].sort((a, b) => a.seq - b.seq);
    this.rings.set(jobId, next.length > RING ? next.slice(-RING) : next);
    if (options.announce !== false) {
      this.emit.events(jobId, fresh);
      // Events arriving *is* the stream working, which is how a follow that
      // failed earlier stops saying so once the tunnel comes back.
      this.emit.follow(jobId, "live");
    }
  }
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    timer.unref();
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

/** Running first, then parked, then whatever moved most recently. */
function byInterest(a: Job, b: Job): number {
  const rank = (job: Job): number => {
    if (job.status === "running" || job.status === "canceling") return 0;
    if (job.status === "queued") return 1;
    if (job.status === "waiting") return 2;
    return 3;
  };
  const byRank = rank(a) - rank(b);
  if (byRank !== 0) return byRank;
  return (b.lastEventAt ?? b.createdAt ?? "").localeCompare(a.lastEventAt ?? a.createdAt ?? "");
}
