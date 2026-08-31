import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { promisify } from "node:util";
import type { AuthPrompt } from "../protocol.ts";
import { AskpassBridge, helperPath } from "./askpass.ts";

const exec = promisify(execFile);

/** Somewhere to write the helper that is not the developer's real state dir. */
process.env.AB_WEBUI_STATE_DIR = mkdtempSync(path.join(tmpdir(), "ab-webui-state-"));

/** One line of JSON, the way the helper speaks. */
function ask(port: number, payload: unknown): Promise<string> {
  return new Promise((resolve, reject) => {
    const socket = connect(port, "127.0.0.1", () => socket.write(`${JSON.stringify(payload)}\n`));
    let answer = "";
    socket.setEncoding("utf8");
    socket.on("data", (chunk: string) => (answer += chunk));
    socket.on("end", () => resolve(answer));
    socket.on("error", reject);
  });
}

test("a question raised by a live session comes back with the answer it was given", async () => {
  const prompts: AuthPrompt[] = [];
  const closed: string[] = [];
  const bridge = new AskpassBridge((event) => {
    if (event.kind === "prompt" && event.prompt) prompts.push(event.prompt);
    else if (event.id) closed.push(event.id);
  });
  await bridge.start();
  const session = bridge.begin("midway5", () => ["Enter passphrase for key"]);

  const pending = ask(Number(session.env.AB_ASKPASS_PORT), {
    token: session.env.AB_ASKPASS_TOKEN,
    prompt: "somebody@midway5's password:",
  });

  // The prompt reaches the page before anything is answered — that is the whole
  // point of the bridge — and it carries the context ssh has printed so far.
  await waitFor(() => prompts.length === 1);
  assert.equal(prompts[0]!.gateway, "midway5");
  assert.equal(prompts[0]!.text, "somebody@midway5's password:");
  assert.deepEqual(prompts[0]!.context, ["Enter passphrase for key"]);
  assert.deepEqual(bridge.open().map((prompt) => prompt.id), [prompts[0]!.id]);

  bridge.answer(prompts[0]!.id, "hunter2");
  assert.equal(await pending, "hunter2\n");
  assert.deepEqual(closed, [prompts[0]!.id]);
  assert.deepEqual(bridge.open(), []);

  session.end();
  await bridge.stop();
});

test("a dismissed question ends the socket with nothing, which is how ssh is told nobody is there", async () => {
  const prompts: AuthPrompt[] = [];
  const bridge = new AskpassBridge((event) => {
    if (event.kind === "prompt" && event.prompt) prompts.push(event.prompt);
  });
  await bridge.start();
  const session = bridge.begin("gw", () => []);

  const pending = ask(Number(session.env.AB_ASKPASS_PORT), {
    token: session.env.AB_ASKPASS_TOKEN,
    prompt: "Password:",
  });
  await waitFor(() => prompts.length === 1);
  bridge.answer(prompts[0]!.id, null);

  assert.equal(await pending, "");
  session.end();
  await bridge.stop();
});

test("nobody asked for a connection, so nothing may ask for a credential", async () => {
  let raised = 0;
  const bridge = new AskpassBridge(() => raised++);
  await bridge.start();
  const session = bridge.begin("gw", () => []);
  const port = Number(session.env.AB_ASKPASS_PORT);

  // A wrong token, and a token from a session that has ended: both are some
  // other process on this machine trying its luck against the socket.
  assert.equal(await ask(port, { token: "0".repeat(48), prompt: "Password:" }), "");
  session.end();
  assert.equal(await ask(port, { token: session.env.AB_ASKPASS_TOKEN, prompt: "Password:" }), "");
  assert.equal(raised, 0);

  await bridge.stop();
});

test("ending a session cancels the question it left waiting", async () => {
  const prompts: AuthPrompt[] = [];
  const bridge = new AskpassBridge((event) => {
    if (event.kind === "prompt" && event.prompt) prompts.push(event.prompt);
  });
  await bridge.start();
  const session = bridge.begin("gw", () => []);

  const pending = ask(Number(session.env.AB_ASKPASS_PORT), {
    token: session.env.AB_ASKPASS_TOKEN,
    prompt: "Password:",
  });
  await waitFor(() => prompts.length === 1);

  // ssh exited while the box was still on screen. The dialog has to go, and the
  // promise behind it has to settle, or the next connect finds a stale prompt.
  session.end();
  assert.equal(await pending, "");
  assert.deepEqual(bridge.open(), []);

  await bridge.stop();
});

test("the helper ssh actually runs prints the answer on stdout and nothing else", async () => {
  const prompts: AuthPrompt[] = [];
  const bridge = new AskpassBridge((event) => {
    if (event.kind === "prompt" && event.prompt) prompts.push(event.prompt);
  });
  await bridge.start();
  const session = bridge.begin("gw", () => []);

  // This is the real path: ssh execs the wrapper with the prompt as an
  // argument, and reads one line from its stdout.
  const running = exec(helperPath(), ["Password:"], { env: { ...process.env, ...session.env } });
  await waitFor(() => prompts.length === 1);
  bridge.answer(prompts[0]!.id, "from-the-helper");

  const { stdout } = await running;
  assert.equal(stdout, "from-the-helper\n");

  session.end();
  await bridge.stop();
});

test("the helper exits non-zero when there is no answer, failing the authentication rather than hanging it", async () => {
  const prompts: AuthPrompt[] = [];
  const bridge = new AskpassBridge((event) => {
    if (event.kind === "prompt" && event.prompt) prompts.push(event.prompt);
  });
  await bridge.start();
  const session = bridge.begin("gw", () => []);

  const running = exec(helperPath(), ["Password:"], { env: { ...process.env, ...session.env } });
  await waitFor(() => prompts.length === 1);
  bridge.answer(prompts[0]!.id, null);

  await assert.rejects(running, (err: NodeJS.ErrnoException & { code?: number }) => err.code === 1);

  session.end();
  await bridge.stop();
});

test("a helper with no bridge in its environment refuses rather than prompting on a terminal", async () => {
  // ssh runs `SSH_ASKPASS` with whatever environment it has. Without our two
  // variables the helper must exit, not fall back to anything.
  await assert.rejects(
    exec(helperPath(), ["Password:"], { env: { PATH: process.env.PATH ?? "" } }),
    (err: NodeJS.ErrnoException & { code?: number }) => err.code === 1,
  );
});

async function waitFor(predicate: () => boolean, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("timed out waiting");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}
