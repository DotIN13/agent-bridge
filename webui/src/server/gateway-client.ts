import type { Job, JobEvent, JobEventType, JobStatus } from "../protocol.ts";

/** agent-bridge's one error envelope, unwrapped into a message worth showing. */
export class GatewayError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
  ) {
    super(message);
  }
}

const STATUSES: readonly string[] = [
  "queued", "running", "waiting", "canceling", "succeeded", "failed", "canceled",
];

const EVENT_TYPES: readonly string[] = [
  "status", "assistant", "thinking", "tool_use", "tool_result", "steer", "result", "error", "log",
  "message",
];

/**
 * A client for one agent-bridge gateway.
 *
 * Speaks the HTTP API directly rather than shelling out to `ab`: no Python on
 * the path, no argument-quoting layer between the dashboard and the gateway.
 * The token is held here and nowhere the browser can reach — every field the
 * page reads has been through `toJob`/`toEvent` below, which is the one place
 * the API's snake_case is turned over.
 */
export class GatewayClient {
  constructor(
    readonly name: string,
    private baseUrl: string,
    private token: string | null,
  ) {}

  update(baseUrl: string, token: string | null): void {
    this.baseUrl = baseUrl;
    this.token = token;
  }

  private async request<T>(path: string, init: RequestInit = {}, timeoutMs = 10_000): Promise<T> {
    const response = await this.fetch(`${this.baseUrl}${path}`, {
      ...init,
      signal: AbortSignal.timeout(timeoutMs),
      headers: {
        accept: "application/json",
        ...(this.token ? { authorization: `Bearer ${this.token}` } : {}),
        ...(init.body ? { "content-type": "application/json" } : {}),
        ...init.headers,
      },
    });

    const text = await response.text();
    let body: unknown = {};
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = { error: { message: text.slice(0, 300) } };
      }
    }

    if (!response.ok) {
      const error = (body as { error?: { code?: string; message?: string } }).error;
      throw new GatewayError(
        error?.message ?? `HTTP ${response.status}`,
        error?.code ?? String(response.status),
        response.status,
      );
    }
    return body as T;
  }

  /**
   * `fetch`, with the transport failure translated on the way out.
   *
   * Node says `fetch failed` for everything from a refused connection to a dead
   * tunnel, and that string is what ends up on screen. Whoever reads it needs
   * to know whether the local port is dead or the far end is: those are
   * different fixes, and the difference is exactly what a forwarded port hides.
   */
  private async fetch(url: string, init: RequestInit): Promise<Response> {
    try {
      return await globalThis.fetch(url, init);
    } catch (err) {
      const message = (err as Error)?.message ?? String(err);
      const cause = errnoOf(err);
      let where = this.baseUrl;
      try {
        const parsed = new URL(this.baseUrl);
        where = `${parsed.hostname}:${parsed.port || "80"}`;
      } catch {
        /* an unparseable base URL is its own answer */
      }
      if (/ECONNREFUSED/i.test(cause) || /ECONNREFUSED/i.test(message)) {
        throw new GatewayError(`nothing listening on ${where}`, "unreachable", 0);
      }
      if (/ECONNRESET/i.test(cause) || /ECONNRESET|socket hang up/i.test(message)) {
        throw new GatewayError("connection reset · far end gone", "unreachable", 0);
      }
      if (/timeout|aborted/i.test(message)) throw new GatewayError(`no answer from ${where}`, "timeout", 0);
      throw new GatewayError(message, "transport", 0);
    }
  }

  /** Unauthenticated, so it answers even when the token is wrong. */
  health(): Promise<{ ok: boolean; version?: string }> {
    return this.request<{ ok: boolean; version?: string }>("/health", {}, 5000);
  }

  agents(): Promise<{ configured?: string[]; default?: string }> {
    return this.request("/v1/agents");
  }

  async jobs(limit = 50, cursor?: string): Promise<{ jobs: Job[]; nextCursor: string | null; hasMore: boolean }> {
    const query = new URLSearchParams({ limit: String(limit) });
    if (cursor) query.set("cursor", cursor);
    const page = await this.request<{ jobs: unknown[]; next_cursor?: string | null; has_more?: boolean }>(
      `/v1/jobs?${query}`,
    );
    return {
      jobs: page.jobs.map((raw) => this.toJob(raw as Record<string, unknown>)),
      nextCursor: page.next_cursor ?? null,
      hasMore: Boolean(page.has_more),
    };
  }

  async job(id: string): Promise<Job> {
    const raw = await this.request<Record<string, unknown>>(`/v1/jobs/${encodeURIComponent(id)}`);
    return {
      ...this.toJob(raw),
      prompt: typeof raw.prompt === "string" ? raw.prompt : null,
      result: typeof raw.result === "string" ? raw.result : null,
      error: typeof raw.error === "string" ? raw.error : null,
    };
  }

  async events(
    id: string,
    query: { after?: number; tail?: number; limit?: number },
  ): Promise<{ events: JobEvent[]; status: string; terminal: boolean; nextAfter: number }> {
    const search = new URLSearchParams();
    // `tail` and `after` cannot be combined — anchoring from both ends has no
    // single sensible reading, and the gateway rejects it.
    if (query.tail !== undefined) search.set("tail", String(query.tail));
    else search.set("after", String(query.after ?? 0));
    if (query.limit) search.set("limit", String(query.limit));

    const page = await this.request<{
      events: Array<Record<string, unknown>>;
      status: string;
      terminal: boolean;
      next_after: number;
    }>(`/v1/jobs/${encodeURIComponent(id)}/events?${search}`, {}, 20_000);

    return {
      events: page.events.map((raw) => this.toEvent(raw)),
      status: page.status,
      terminal: page.terminal,
      nextAfter: page.next_after,
    };
  }

  cancel(id: string): Promise<unknown> {
    return this.request(`/v1/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: "{}" });
  }

  steer(id: string, prompt: string): Promise<unknown> {
    return this.request(`/v1/jobs/${encodeURIComponent(id)}/steer`, {
      method: "POST",
      body: JSON.stringify({ prompt }),
    });
  }

  /**
   * Follow one job's events until it goes terminal or the caller lets go.
   *
   * Resumable by `Last-Event-ID`, so a reconnect continues from the last event
   * seen rather than replaying the log from the beginning.
   */
  async follow(id: string, from: number, signal: AbortSignal, onEvent: (event: JobEvent) => void): Promise<void> {
    const response = await this.fetch(
      `${this.baseUrl}/v1/jobs/${encodeURIComponent(id)}/events?after=${from}`,
      {
        signal,
        headers: {
          accept: "text/event-stream",
          ...(this.token ? { authorization: `Bearer ${this.token}` } : {}),
          ...(from > 0 ? { "last-event-id": String(from) } : {}),
        },
      },
    );

    if (!response.ok || !response.body) {
      throw new GatewayError(`could not follow ${id} (HTTP ${response.status})`, "stream_failed", response.status);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line; a `: ping` comment keeps an
      // idle stream alive and parses to nothing, which is what we want.
      let split = buffer.indexOf("\n\n");
      while (split !== -1) {
        const event = this.parseFrame(buffer.slice(0, split));
        buffer = buffer.slice(split + 2);
        if (event) onEvent(event);
        split = buffer.indexOf("\n\n");
      }
    }
  }

  private parseFrame(frame: string): JobEvent | null {
    let type = "";
    let id = "";
    const data: string[] = [];

    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "event") type = value;
      else if (field === "id") id = value;
      else if (field === "data") data.push(value);
    }

    if (!data.length) return null;
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(data.join("\n")) as Record<string, unknown>;
    } catch {
      return null;
    }
    return this.toEvent({ ...payload, type: payload.type ?? type, seq: payload.seq ?? Number(id) });
  }

  private toEvent(raw: Record<string, unknown>): JobEvent {
    const type = String(raw.type ?? "log");
    return {
      seq: Number(raw.seq ?? 0),
      at: typeof raw.ts === "string" ? raw.ts : null,
      elapsed: typeof raw.elapsed === "number" ? raw.elapsed : null,
      // The gateway computes this: seconds since the job's *first* event, which
      // is not something a reader could work out from the row.
      elapsedHms: typeof raw.elapsed_hms === "string" ? raw.elapsed_hms : null,
      type: (EVENT_TYPES.includes(type) ? type : "log") as JobEventType,
      data: (raw.data as Record<string, unknown>) ?? {},
    };
  }

  private toJob(raw: Record<string, unknown>): Job {
    const status = String(raw.status ?? "queued");
    const text = (key: string): string | null => (typeof raw[key] === "string" ? (raw[key] as string) : null);

    return {
      gateway: this.name,
      id: String(raw.id ?? ""),
      title: text("title"),
      status: (STATUSES.includes(status) ? status : "queued") as JobStatus,
      agent: String(raw.agent ?? ""),
      cwd: text("cwd"),
      model: text("model"),
      // The gateway overwrites `session` with the one the run actually used, so
      // this is always the id to pass back on the next job.
      session: text("session"),
      costUsd: typeof raw.cost_usd === "number" ? raw.cost_usd : null,
      createdAt: text("created_at"),
      startedAt: text("started_at"),
      finishedAt: text("finished_at"),
      lastEventAt: text("last_event_at"),
      asOf: new Date().toISOString(),
    };
  }
}

/**
 * The errno buried under a `fetch` failure.
 *
 * Node's fetch says `fetch failed` for a refused connection, a reset one and a
 * dead tunnel alike, so the only usable signal is `cause` — and a bare "fetch
 * failed" on screen is the single least useful thing this dashboard could say
 * about its most common failure.
 *
 * Measured on Node 22, `cause.code` carries the errno directly. The walk is
 * there for the case that does not reproduce here: a host resolving to several
 * addresses (`localhost` is 127.0.0.1 *and* ::1) can come back as an
 * `AggregateError` whose own `code` is undefined and whose `errors` hold the
 * real one, depending on the undici version underneath.
 */
function errnoOf(err: unknown): string {
  const seen = new Set<unknown>();
  const walk = (value: unknown, depth: number): string => {
    if (!value || typeof value !== "object" || depth > 4 || seen.has(value)) return "";
    seen.add(value);
    const candidate = value as { code?: unknown; cause?: unknown; errors?: unknown };
    if (typeof candidate.code === "string") return candidate.code;
    if (Array.isArray(candidate.errors)) {
      for (const entry of candidate.errors) {
        const found = walk(entry, depth + 1);
        if (found) return found;
      }
    }
    return walk(candidate.cause, depth + 1);
  };
  return walk(err, 0);
}
