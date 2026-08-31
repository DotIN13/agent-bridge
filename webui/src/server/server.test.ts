import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createServer, type Server } from "node:http";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { WebSocket } from "ws";
import type { ServerMessage } from "../protocol.ts";
import { createHttp, type Http } from "./http.ts";
import { Manager } from "./manager.ts";

process.env.AB_WEBUI_STATE_DIR = mkdtempSync(path.join(tmpdir(), "ab-webui-state-"));

const TOKEN = "a".repeat(48);

/** A gateway that answers agent-bridge's shape, on a port of our choosing. */
function fakeGateway(port: number, jobs: unknown[]): Promise<Server> {
  const server = createServer((req, res) => {
    const url = req.url ?? "";
    const send = (body: unknown, status = 200) => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(body));
    };
    if (url === "/health") return send({ ok: true, version: "0.3.0" });
    if (url === "/v1/agents") return send({ configured: ["claude"], default: "claude" });
    if (url.startsWith("/v1/jobs?")) return send({ jobs, next_cursor: null, has_more: false });
    if (/\/v1\/jobs\/[^/]+\/events/.test(url)) {
      return send({ events: [{ seq: 1, type: "assistant", data: { text: "hi" }, elapsed: 0, elapsed_hms: "+00:00:00" }], status: "running", terminal: false, next_after: 1 });
    }
    if (/\/v1\/jobs\/[^/]+$/.test(url)) return send({ id: "job-1", status: "running", agent: "claude", prompt: "do a thing" });
    return send({ error: { message: "not here" } }, 404);
  });
  return new Promise((resolve) => server.listen(port, "127.0.0.1", () => resolve(server)));
}

function writeConfig(entries: Record<string, unknown>, defaultName?: string): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ab-webui-cfg-"));
  const file = path.join(dir, "gateways.json");
  writeFileSync(file, JSON.stringify({ ...(defaultName ? { default: defaultName } : {}), gateways: entries }, null, 2));
  process.env.AGENT_BRIDGE_CLIENT_CONFIG = file;
  return file;
}

interface Running {
  http: Http;
  manager: Manager;
  base: string;
  stop: () => Promise<void>;
}

async function run(): Promise<Running> {
  const manager = new Manager();
  const http = createHttp({ manager, token: TOKEN });
  manager.publishTo(http.broadcast);
  await manager.start();
  await new Promise<void>((resolve) => http.server.listen(0, "127.0.0.1", () => resolve()));
  const port = (http.server.address() as { port: number }).port;
  return {
    http,
    manager,
    base: `http://127.0.0.1:${port}`,
    stop: async () => {
      await manager.stop();
      await http.close();
    },
  };
}

function call(base: string, path_: string, init: RequestInit = {}, token = TOKEN): Promise<Response> {
  return fetch(`${base}/api${path_}`, {
    ...init,
    headers: {
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(init.body ? { "content-type": "application/json" } : {}),
      ...init.headers,
    },
  });
}

test.afterEach(() => {
  delete process.env.AGENT_BRIDGE_CLIENT_CONFIG;
});

test("the api is shut to anything without the token, including the socket", async () => {
  writeConfig({});
  const app = await run();

  // Loopback is not an authorization boundary: every process on this machine
  // can reach 127.0.0.1, and this one holds every gateway token there is.
  assert.equal((await call(app.base, "/state", {}, "")).status, 401);
  assert.equal((await call(app.base, "/state", {}, "b".repeat(48))).status, 401);
  assert.equal((await call(app.base, "/state")).status, 200);

  const refused = new WebSocket(`${app.base.replace("http", "ws")}/ws?token=wrong`);
  const failure = await new Promise<string>((resolve) => {
    refused.on("error", (err: Error) => resolve(err.message));
    refused.on("open", () => resolve("opened"));
  });
  assert.match(failure, /401/);

  await app.stop();
});

test("a reachable gateway reaches the page as connected, with its jobs on the socket", async () => {
  const port = 46101;
  const gateway = await fakeGateway(port, [
    { id: "job-1", status: "running", agent: "claude", title: "one" },
    { id: "job-2", status: "waiting", agent: "claude", title: "two" },
  ]);
  writeConfig({ gw: { base_url: `http://127.0.0.1:${port}` } }, "gw");
  const app = await run();

  await waitFor(() => app.manager.state().gateways[0]?.status === "connected", 8000);
  const entry = app.manager.state().gateways[0]!;
  assert.equal(entry.version, "0.3.0");
  assert.deepEqual(entry.agents, ["claude"]);
  assert.equal(entry.isDefault, true);

  await waitFor(() => app.manager.jobs("gw").length === 2, 8000);
  // Running first, then parked: the order the sidebar counts are drawn from.
  assert.deepEqual(app.manager.jobs("gw").map((job) => job.id), ["job-1", "job-2"]);
  assert.deepEqual(entry.jobs.running >= 0 ? true : false, true);

  const socket = new WebSocket(`${app.base.replace("http", "ws")}/ws?token=${TOKEN}`);
  const received: ServerMessage[] = [];
  socket.on("message", (raw) => received.push(JSON.parse(String(raw)) as ServerMessage));
  await new Promise<void>((resolve) => socket.on("open", () => resolve()));

  // The state frame arrives unasked, because a page that has just loaded has
  // nothing to draw until it does.
  await waitFor(() => received.some((message) => message.type === "state"), 4000);

  socket.send(JSON.stringify({ type: "watch", gateway: "gw" }));
  await waitFor(() => received.some((message) => message.type === "jobs"), 4000);
  const jobsFrame = received.find((message) => message.type === "jobs") as { jobs: unknown[] };
  assert.equal(jobsFrame.jobs.length, 2);

  socket.close();
  await app.stop();
  gateway.close();
});

test("a gateway nothing is listening for says so, in words with the port in them", async () => {
  writeConfig({ gw: { base_url: "http://127.0.0.1:46102" } });
  const app = await run();

  // Two misses before a row goes red, so this needs a second probe.
  await waitFor(() => app.manager.state().gateways[0]?.status === "unreachable", 8000);
  assert.match(app.manager.state().gateways[0]!.error!, /nothing listening on 127\.0\.0\.1:46102/);

  await app.stop();
});

test("a token the gateway refuses is unauthorized, which is not the same as unreachable", async () => {
  const port = 46103;
  const server = createServer((req, res) => {
    if (req.url === "/health") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true, version: "0.3.0" }));
      return;
    }
    res.writeHead(401, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { code: "unauthorized", message: "bad token" } }));
  });
  await new Promise<void>((resolve) => server.listen(port, "127.0.0.1", () => resolve()));

  writeConfig({ gw: { base_url: `http://127.0.0.1:${port}`, token: "wrong" } });
  const app = await run();

  // `/health` needs no token and `/v1/agents` does, which is what separates
  // "the port is dead" from "the token is wrong" without guessing.
  await waitFor(() => app.manager.state().gateways[0]?.status === "unauthorized", 8000);
  assert.equal(app.manager.state().gateways[0]!.version, "0.3.0");

  await app.stop();
  server.close();
});

test("the config dialog's save lands in ab's own file and is read back at once", async () => {
  const file = writeConfig({});
  const app = await run();

  const response = await call(app.base, "/gateways/new-one", {
    method: "PUT",
    body: JSON.stringify({
      baseUrl: "http://localhost:8787/",
      ssh: "ssh -N -L 8787:localhost:8787 midway5",
      tokenEnv: "AGENT_BRIDGE_TOKEN",
      enabled: true,
      makeDefault: true,
    }),
  });
  assert.equal(response.status, 200);

  const raw = JSON.parse(await readFile(file)) as Record<string, any>;
  assert.equal(raw.gateways["new-one"].base_url, "http://localhost:8787");
  assert.equal(raw.gateways["new-one"].token_env, "AGENT_BRIDGE_TOKEN");
  assert.equal(raw.default, "new-one");

  // Read back without waiting for the file watcher: the answer to the request
  // already carries the row the dialog just saved.
  const entry = app.manager.state().gateways[0]!;
  assert.equal(entry.name, "new-one");
  assert.equal(entry.tokenSource, "token_env");
  assert.equal(entry.tunnel, "new-one");
  // No `ssh` on PATH in this container, and the command names a bare one — so
  // the row knows its own button would not work.
  assert.equal(app.manager.state().tunnels[0]!.runnable, false);

  assert.equal((await call(app.base, "/gateways/new-one", { method: "DELETE" })).status, 200);
  assert.deepEqual(app.manager.state().gateways, []);

  await app.stop();
});

test("the on-connect switch writes exec, and the tunnel picks up the default command", async () => {
  const file = writeConfig({});
  const app = await run();

  await call(app.base, "/gateways/gw", {
    method: "PUT",
    body: JSON.stringify({
      baseUrl: "http://localhost:8787",
      ssh: "ssh -L 8787:localhost:8787 midway5",
      exec: true,
    }),
  });

  let raw = JSON.parse(await readFile(file)) as Record<string, any>;
  assert.equal(raw.gateways.gw.exec, true);
  // `true` in the file, the expansion in the argv: the page never has to know
  // what the default is, and the file never has to carry a copy of it.
  const command = app.manager.state().tunnels[0]!.command;
  assert.equal(command, "ssh -L 8787:localhost:8787 midway5");
  assert.match(app.manager.state().gateways[0]!.execCommand!, /ab-serve/);

  // A script of their own replaces it, and turning it off removes the key
  // rather than writing `false` — one absent state, not two falsy ones.
  await call(app.base, "/gateways/gw", { method: "PUT", body: JSON.stringify({ exec: "~/bin/mine.sh" }) });
  raw = JSON.parse(await readFile(file)) as Record<string, any>;
  assert.equal(raw.gateways.gw.exec, "~/bin/mine.sh");

  await call(app.base, "/gateways/gw", { method: "PUT", body: JSON.stringify({ exec: false }) });
  raw = JSON.parse(await readFile(file)) as Record<string, any>;
  assert.ok(!("exec" in raw.gateways.gw));
  assert.equal(app.manager.state().gateways[0]!.execCommand, undefined);

  await app.stop();
});

test("a base URL that is not one is refused before it reaches the file", async () => {
  const file = writeConfig({});
  const app = await run();

  // Two ways of not being a base URL: a scheme that is not http, and a string
  // that is not a URL at all.
  const scheme = await call(app.base, "/gateways/gw", { method: "PUT", body: JSON.stringify({ baseUrl: "midway5:8787" }) });
  assert.equal(scheme.status, 400);
  assert.match(await errorOf(scheme), /must be http or https/);

  const nonsense = await call(app.base, "/gateways/gw", { method: "PUT", body: JSON.stringify({ baseUrl: "not a url" }) });
  assert.equal(nonsense.status, 400);
  assert.match(await errorOf(nonsense), /not a URL/);

  const raw = JSON.parse(await readFile(file)) as Record<string, any>;
  assert.deepEqual(raw.gateways, {});

  await app.stop();
});

test("the ssh parse the dialog draws comes from the same reader that runs the command", async () => {
  writeConfig({});
  const app = await run();

  const response = await call(app.base, "/ssh/parse", {
    method: "POST",
    body: JSON.stringify({ command: "ssh -L 0.0.0.0:8787:localhost:8787 -R 9:localhost:9 midway5" }),
  });
  const parsed = (await response.json()) as { destination: string; forwards: unknown[]; diagnostics: string[] };
  assert.equal(parsed.destination, "midway5");
  assert.deepEqual(parsed.forwards, []);
  assert.equal(parsed.diagnostics.length, 2);

  await app.stop();
});

test("connecting a gateway with no ssh command is an error, not a silent no-op", async () => {
  writeConfig({ gw: { base_url: "http://127.0.0.1:46104" } });
  const app = await run();

  const response = await call(app.base, "/gateways/gw/up", { method: "POST", body: "{}" });
  assert.equal(response.status, 400);
  assert.match(await errorOf(response), /no ssh command/);

  await app.stop();
});

test("something else answering the port does not make an unauthenticated tunnel connected", async () => {
  /*
   * The one that is worth a test of its own.
   *
   * ssh is still asking for a password, and meanwhile *something* starts
   * answering the forwarded port — an old tunnel of somebody else's, a service
   * bound to the same number. Promoting the row then reports a working
   * connection to a machine this dashboard has not authenticated to.
   */
  const port = 46105;
  const fake = fakeSshThatAsks();
  writeConfig({ gw: { base_url: `http://127.0.0.1:${port}`, ssh: `${fake} -N -L ${port}:localhost:8787 host` } });
  const app = await run();

  await waitFor(() => app.manager.state().gateways[0]?.status === "unreachable", 8000);

  await call(app.base, "/gateways/gw/up", { method: "POST", body: JSON.stringify({ interactive: true }) });
  await waitFor(() => app.manager.state().prompts.length === 1, 8000);
  assert.equal(app.manager.state().tunnels[0]!.status, "authenticating");

  // The impostor: a perfectly healthy agent-bridge, and not ours.
  const impostor = await fakeGateway(port, []);
  await call(app.base, "/refresh", { method: "POST" });
  await new Promise((resolve) => setTimeout(resolve, 600));

  const entry = app.manager.state().gateways[0]!;
  assert.notEqual(entry.status, "connected");
  assert.match(entry.error!, /waiting for the ssh prompt/);

  // Answered, and now the tunnel may speak for the port.
  const prompt = app.manager.state().prompts[0]!;
  await call(app.base, `/prompts/${prompt.id}`, { method: "POST", body: JSON.stringify({ answer: "hunter2" }) });
  await waitFor(() => app.manager.state().gateways[0]?.status === "connected", 10_000);

  await app.stop();
  impostor.close();
});

/**
 * An ssh that asks once, through the askpass bridge, and then stays alive.
 *
 * `node --check` on what was written, because a broken fake does not fail its
 * test — it fails quietly and wrongly. One of these was a syntax error for a
 * while and its test passed anyway, on the words in the traceback.
 */
function fakeSshThatAsks(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ab-fake-ssh-"));
  const file = path.join(dir, "ssh");
  writeFileSync(
    file,
    `#!/usr/bin/env node
import { execFile } from "node:child_process";
const helper = process.env.SSH_ASKPASS;
if (!helper) { process.stderr.write("no askpass and no tty\\n"); process.exit(255); }
execFile(helper, ["somebody@host's password:"], (err, stdout) => {
  if (err || (stdout ?? "").trim() !== "hunter2") {
    process.stderr.write("Permission denied, please try again.\\n");
    process.exit(255);
  }
  process.stderr.write("Authenticated to host ([127.0.0.1]:22).\\n");
  setInterval(() => {}, 1000);
});
`,
    { encoding: "utf8", mode: 0o755 },
  );
  execFileSync(process.execPath, ["--check", file]);
  return file;
}

async function errorOf(response: Response): Promise<string> {
  return String(((await response.json()) as { error?: unknown }).error ?? "");
}

async function readFile(file: string): Promise<string> {
  const { readFile: read } = await import("node:fs/promises");
  return read(file, "utf8");
}

async function waitFor(predicate: () => boolean, timeoutMs = 4000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("timed out waiting");
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}
