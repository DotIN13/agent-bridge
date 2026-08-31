import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import test from "node:test";
import { GatewayClient, GatewayError } from "./gateway-client.ts";

/** A stand-in for one agent-bridge gateway, answering only what is asked of it. */
async function fakeGateway(
  handler: (path: string, headers: Record<string, string | string[] | undefined>) => {
    status?: number;
    body?: unknown;
    sse?: string;
  },
): Promise<{ url: string; close: () => Promise<void>; server: Server }> {
  const server = createServer((req, res) => {
    const answer = handler(req.url ?? "", req.headers);
    if (answer.sse !== undefined) {
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write(answer.sse);
      res.end();
      return;
    }
    res.writeHead(answer.status ?? 200, { "content-type": "application/json" });
    res.end(JSON.stringify(answer.body ?? {}));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const port = (server.address() as { port: number }).port;
  return {
    url: `http://127.0.0.1:${port}`,
    server,
    close: () =>
      new Promise<void>((resolve) => {
        // `close` waits for open connections, and undici keeps them alive — so
        // without this the callback never fires and the test file hangs rather
        // than failing.
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}

test("a job row arrives in the page's spelling, with the status vocabulary checked", async () => {
  const gateway = await fakeGateway(() => ({
    body: {
      jobs: [
        {
          id: "job-1",
          status: "waiting",
          agent: "claude",
          title: "a title",
          cwd: "/project/somebody/app",
          session: "abcdef0123456789",
          model: "a-model",
          cost_usd: 0.42,
          created_at: "2026-08-30T10:00:00.000+00:00",
          last_event_at: "2026-08-30T10:05:00.000+00:00",
        },
        // A status this build has never heard of: kept as a row, read as
        // `queued`, because dropping the row loses the job entirely.
        { id: "job-2", status: "from-the-future", agent: "opencode" },
      ],
      next_cursor: "c2",
      has_more: true,
    },
  }));

  const client = new GatewayClient("gw", gateway.url, "shh");
  const page = await client.jobs();

  assert.equal(page.jobs.length, 2);
  const [first, second] = page.jobs;
  assert.equal(first!.status, "waiting");
  assert.equal(first!.costUsd, 0.42);
  assert.equal(first!.createdAt, "2026-08-30T10:00:00.000+00:00");
  assert.equal(first!.lastEventAt, "2026-08-30T10:05:00.000+00:00");
  assert.equal(first!.gateway, "gw");
  assert.equal(second!.status, "queued");
  assert.equal(page.nextCursor, "c2");
  assert.equal(page.hasMore, true);

  await gateway.close();
});

test("the token goes on the request and the health check goes without one", async () => {
  const seen: Array<{ path: string; auth?: string }> = [];
  const gateway = await fakeGateway((path, headers) => {
    seen.push({ path, auth: headers.authorization as string | undefined });
    return { body: { ok: true, version: "0.3.0", jobs: [] } };
  });

  const client = new GatewayClient("gw", gateway.url, "shh");
  await client.health();
  await client.jobs();

  // `/health` needs no token, which is what makes it able to tell "the port is
  // dead" apart from "the token is wrong". Sending one anyway is harmless and
  // keeps one code path.
  assert.equal(seen[0]!.path, "/health");
  assert.equal(seen[0]!.auth, "Bearer shh");
  assert.match(seen[1]!.path, /^\/v1\/jobs\?/);
  assert.equal(seen[1]!.auth, "Bearer shh");

  await gateway.close();
});

test("the gateway's error envelope becomes the message on screen", async () => {
  const gateway = await fakeGateway(() => ({
    status: 401,
    body: { error: { code: "unauthorized", message: "bad token" } },
  }));

  const client = new GatewayClient("gw", gateway.url, "wrong");
  await assert.rejects(client.jobs(), (err: GatewayError) => {
    assert.equal(err.message, "bad token");
    assert.equal(err.code, "unauthorized");
    assert.equal(err.status, 401);
    return true;
  });

  await gateway.close();
});

test("a body that is not JSON is still an error somebody can read", async () => {
  // An HTML 502 from a proxy in front of the gateway used to surface as
  // "Unexpected token < in JSON at position 0", which names the wrong problem.
  const server = createServer((_req, res) => {
    res.writeHead(502, { "content-type": "text/html" });
    res.end("<html>Bad Gateway</html>");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const port = (server.address() as { port: number }).port;

  const client = new GatewayClient("gw", `http://127.0.0.1:${port}`, null);
  await assert.rejects(client.jobs(), (err: GatewayError) => {
    assert.match(err.message, /Bad Gateway/);
    assert.equal(err.status, 502);
    return true;
  });

  await new Promise<void>((resolve) => server.close(() => resolve()));
});

test("nothing listening is said as nothing listening, with the port in it", async () => {
  // Node says `fetch failed` for everything from a refused connection to a dead
  // tunnel, and that string is what ends up on screen. A high port nothing has
  // any business holding — and not a low one, which fetch refuses as a "bad
  // port" before it ever tries to connect.
  const client = new GatewayClient("gw", "http://127.0.0.1:45999", null);
  await assert.rejects(client.jobs(), (err: GatewayError) => {
    assert.match(err.message, /nothing listening on 127\.0\.0\.1:45999/);
    assert.equal(err.code, "unreachable");
    return true;
  });
});

test("events carry the gateway's own +HH:MM:SS rather than a number to format twice", async () => {
  const gateway = await fakeGateway(() => ({
    body: {
      events: [
        { seq: 1, ts: "2026-08-30T10:00:00.000+00:00", type: "status", data: { subtype: "init" }, elapsed: 0, elapsed_hms: "+00:00:00" },
        { seq: 2, ts: "2026-08-30T11:02:03.000+00:00", type: "assistant", data: { text: "hello" }, elapsed: 3723, elapsed_hms: "+01:02:03" },
        // A type this build does not know becomes a log line rather than a gap.
        { seq: 3, ts: null, type: "invented", data: {}, elapsed: null, elapsed_hms: null },
      ],
      status: "running",
      terminal: false,
      next_after: 3,
    },
  }));

  const client = new GatewayClient("gw", gateway.url, "shh");
  const page = await client.events("job-1", { tail: 10 });

  assert.equal(page.events[1]!.elapsedHms, "+01:02:03");
  assert.equal(page.events[1]!.elapsed, 3723);
  assert.equal(page.events[1]!.type, "assistant");
  assert.equal(page.events[2]!.type, "log");
  assert.equal(page.events[2]!.at, null);
  assert.equal(page.status, "running");
  assert.equal(page.nextAfter, 3);

  await gateway.close();
});

test("tail and after are never sent together, because the gateway rejects both", async () => {
  const queries: string[] = [];
  const gateway = await fakeGateway((path) => {
    queries.push(path);
    return { body: { events: [], status: "running", terminal: false, next_after: 0 } };
  });

  const client = new GatewayClient("gw", gateway.url, null);
  await client.events("j", { tail: 200 });
  await client.events("j", { after: 12 });
  await client.events("j", { tail: 5, after: 12 });

  assert.match(queries[0]!, /tail=200/);
  assert.ok(!queries[0]!.includes("after="));
  assert.match(queries[1]!, /after=12/);
  // Both asked for: `tail` wins and `after` is dropped, rather than sending a
  // pair that comes back as a 400.
  assert.match(queries[2]!, /tail=5/);
  assert.ok(!queries[2]!.includes("after="));

  await gateway.close();
});

test("a followed stream is read frame by frame, and a keep-alive comment parses to nothing", async () => {
  const frames = [
    ": ping",
    "",
    "event: assistant",
    "id: 7",
    'data: {"seq": 7, "type": "assistant", "data": {"text": "one"}}',
    "",
    // A frame whose type is only in the `event:` field, and whose id is only in
    // `id:` — both are legal SSE and both have to land on the record.
    "event: tool_use",
    "id: 8",
    'data: {"data": {"name": "Bash"}}',
    "",
    // Multi-line data, joined with newlines before it is parsed.
    "event: assistant",
    "id: 9",
    'data: {"seq": 9, "type": "assistant",',
    'data:  "data": {"text": "two"}}',
    // Two, so the last frame is terminated: SSE ends a frame with a blank line,
    // and a frame with no blank line after it is one nobody has finished
    // sending.
    "",
    "",
  ].join("\n");

  const seen: Array<{ seq: number; type: string }> = [];
  const gateway = await fakeGateway((path, headers) => {
    assert.match(path, /\/v1\/jobs\/job-1\/events\?after=6/);
    assert.equal(headers["last-event-id"], "6");
    return { sse: frames };
  });

  const client = new GatewayClient("gw", gateway.url, "shh");
  await client.follow("job-1", 6, new AbortController().signal, (event) => {
    seen.push({ seq: event.seq, type: event.type });
  });

  assert.deepEqual(seen, [
    { seq: 7, type: "assistant" },
    { seq: 8, type: "tool_use" },
    { seq: 9, type: "assistant" },
  ]);

  await gateway.close();
});

test("a stream that cannot be opened is an error, not an empty log", async () => {
  const gateway = await fakeGateway(() => ({ status: 404, body: { error: { message: "no such job" } } }));
  const client = new GatewayClient("gw", gateway.url, "shh");

  await assert.rejects(
    client.follow("nope", 0, new AbortController().signal, () => {}),
    (err: GatewayError) => {
      assert.match(err.message, /could not follow nope/);
      assert.equal(err.code, "stream_failed");
      return true;
    },
  );

  await gateway.close();
});
