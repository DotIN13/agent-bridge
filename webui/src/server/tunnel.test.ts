import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import type { AuthPrompt } from "../protocol.ts";
import { AskpassBridge } from "./askpass.ts";
import { classify, exitReason, Tunnel } from "./tunnel.ts";
import { parseSshCommand } from "./ssh-command.ts";

process.env.AB_WEBUI_STATE_DIR = mkdtempSync(path.join(tmpdir(), "ab-webui-state-"));

/**
 * An ssh that behaves like ssh where it matters.
 *
 * Specifically: it does not read a password from its own stdin. It runs
 * `$SSH_ASKPASS` with the prompt as an argument and reads one line from that
 * program's stdout — which is the contract the whole bridge is built on, and the
 * only part of ssh worth imitating here. With `BatchMode=yes` it refuses
 * without asking, as ssh does.
 *
 * Called `ssh` on purpose: `parseSshCommand` only treats a leading token as a
 * binary when it is named like one.
 */
function fakeSsh(password: string): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ab-fake-ssh-"));
  const file = path.join(dir, "ssh");
  writeFileSync(
    file,
    `#!/usr/bin/env node
import { execFile } from "node:child_process";

const args = process.argv.slice(2);
const batch = args.includes("BatchMode=yes");

if (batch) {
  process.stderr.write("somebody@host: Permission denied (publickey,password).\\n");
  process.exit(255);
}

const helper = process.env.SSH_ASKPASS;
if (!helper) {
  process.stderr.write("no askpass and no tty\\n");
  process.exit(255);
}

execFile(helper, ["somebody@host's password:"], (err, stdout) => {
  const answer = (stdout ?? "").replace(/\\r?\\n$/, "");
  if (err || answer !== ${JSON.stringify(password)}) {
    process.stderr.write("Permission denied, please try again.\\n");
    process.exit(255);
  }
  process.stderr.write("Authenticated to host ([127.0.0.1]:22).\\n");
  // ssh -N says nothing more and stays alive holding the forwards.
  setInterval(() => {}, 1000);
});
`,
    { encoding: "utf8", mode: 0o755 },
  );
  return file;
}

/**
 * An ssh that ignores `SSH_ASKPASS`, which is the bundled Windows client.
 *
 * It refuses exactly as a wrong password does — same exit code, same words — so
 * the only thing that separates the two from outside is whether a question was
 * ever asked.
 */
function fakeSshThatIgnoresAskpass(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ab-fake-ssh-"));
  const file = path.join(dir, "ssh");
  writeFileSync(
    file,
    `#!/usr/bin/env node
process.stderr.write("somebody@host: Permission denied (publickey,password).\n");
process.exit(255);
`,
    { encoding: "utf8", mode: 0o755 },
  );
  return file;
}

interface Harness {
  tunnel: Tunnel;
  bridge: AskpassBridge;
  prompts: AuthPrompt[];
  status: () => string;
  stop: () => Promise<void>;
}

async function harness(password: string, answered: string | null): Promise<Harness> {
  const prompts: AuthPrompt[] = [];
  let tunnel: Tunnel | undefined;

  const bridge = new AskpassBridge((event) => {
    if (event.kind === "prompt" && event.prompt) {
      prompts.push(event.prompt);
      tunnel?.notePrompt();
      // Answer on the next tick, so a test can look at the state while the
      // question is still open.
      if (answered !== null) setTimeout(() => bridge.answer(event.prompt!.id, answered), 30);
    } else {
      tunnel?.notePromptClosed();
    }
  });
  await bridge.start();

  const command = `${fakeSsh(password)} -N -L 18787:localhost:8787 host`;
  tunnel = new Tunnel("gw", parseSshCommand(command), command, false, bridge, () => {});

  return {
    tunnel,
    bridge,
    prompts,
    status: () => tunnel!.state([], ["gw"]).status,
    stop: async () => {
      tunnel!.down();
      await bridge.stop();
    },
  };
}

test("a signal death is named, not reported as a negative exit code", () => {
  // "ssh exited with code -9: Success" went out to the page once. It is wrong
  // twice over, and both halves came from formatting this badly.
  assert.equal(exitReason(null, "SIGKILL"), "killed by SIGKILL");
  assert.equal(exitReason(-9, null), "killed by signal 9");
  assert.equal(exitReason(255, null), "exited with code 255");
  assert.equal(exitReason(null, null), "exited");
});

test("what ssh printed decides whether the supervisor tries again", () => {
  assert.equal(classify("somebody@host: Permission denied (publickey)."), "auth");
  assert.equal(classify("bind [127.0.0.1]:8787: Address already in use"), "port_in_use");
  assert.equal(classify("ssh: Could not resolve hostname nope: Name or service not known"), "unknown_host");
  assert.equal(classify("ssh: connect to host x port 22: Connection refused"), "refused");
  assert.equal(classify("Authenticated to host ([127.0.0.1]:22)."), null);
});

test("the answer typed into the page is what ssh authenticates with", async () => {
  const h = await harness("hunter2", "hunter2");
  h.tunnel.up(true);

  await waitFor(() => h.status() === "up", 6000);
  assert.equal(h.prompts.length, 1);
  assert.equal(h.prompts[0]!.text, "somebody@host's password:");
  assert.equal(h.tunnel.running, true);

  await h.stop();
});

test("a tunnel with a question waiting is not up, however long the process has been alive", async () => {
  // The bug this is here for: the process survives, the settle timer fires, and
  // the row goes green while the password box is still on screen. Worse, if
  // something else happens to be answering that local port, the probe agrees.
  const h = await harness("hunter2", null);
  h.tunnel.up(true);

  await waitFor(() => h.prompts.length === 1, 6000);
  assert.equal(h.status(), "authenticating");
  assert.equal(h.tunnel.authenticating, true);

  // Well past the settle window, with the question still open.
  await new Promise((resolve) => setTimeout(resolve, 2200));
  assert.equal(h.status(), "authenticating");

  // Answered — wrongly, since the fake wants `hunter2` — and now it may move.
  h.bridge.answer(h.prompts[0]!.id, "nope");
  await waitFor(() => h.status() === "failed", 6000);
  assert.equal(h.tunnel.authenticating, false);

  await h.stop();
});

test("a refused password stops the supervisor rather than retrying it", async () => {
  // Retrying a two-factor host on a timer is a stream of push notifications to
  // somebody's phone and a plausible route to a locked account.
  const h = await harness("hunter2", "wrong");
  h.tunnel.up(true);

  await waitFor(() => h.status() === "failed", 6000);
  const state = h.tunnel.state([], ["gw"]);
  assert.equal(state.blocked, "auth");
  assert.equal(state.attempts, 0);
  assert.equal(h.tunnel.running, false);
  assert.match(state.log.join(" "), /Permission denied/);

  await h.stop();
});

test("an unattended attempt cannot raise a prompt, so it fails instead of waiting", async () => {
  const h = await harness("hunter2", "hunter2");
  h.tunnel.up(false);

  await waitFor(() => h.status() === "failed", 6000);
  assert.equal(h.prompts.length, 0);
  assert.equal(h.tunnel.state([], ["gw"]).blocked, "auth");

  await h.stop();
});

test("a deliberate stop is not a dropped connection, so nothing comes back", async () => {
  const h = await harness("hunter2", "hunter2");
  h.tunnel.up(true);
  await waitFor(() => h.status() === "up", 6000);

  h.tunnel.down();
  await waitFor(() => h.tunnel.running === false, 6000);
  assert.equal(h.status(), "off");
  // A SIGTERM we sent is a clean exit; a retry after one would fight the user.
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(h.status(), "off");

  await h.stop();
});

test("a token-shaped string in ssh's output never reaches the page", () => {
  // The log is what the page renders, so it is the last place a credential can
  // leak from — and a verbose ssh, or one carrying `-o SendEnv`, will happily
  // print one. No process is started here: the redaction is in the line, not in
  // the run.
  const bridge = new AskpassBridge(() => {});
  const tunnel = new Tunnel("gw", parseSshCommand("ssh host"), "ssh host", false, bridge, () => {});
  const push = (tunnel as unknown as { push: (line: string) => void }).push.bind(tunnel);

  push("debug1: authorization: Bearer abc123secret");
  push("token=abc123secret");

  const log = tunnel.state([], ["gw"]).log.join(" ");
  assert.match(log, /Bearer \[redacted\]/);
  assert.match(log, /token=\[redacted\]/);
  assert.ok(!log.includes("abc123secret"));
});

test("an ssh that ignores SSH_ASKPASS is told apart from a wrong password", async () => {
  const bridge = new AskpassBridge(() => {});
  await bridge.start();
  const command = `${fakeSshThatIgnoresAskpass()} -N -L 18788:localhost:8787 host`;
  const tunnel = new Tunnel("gw", parseSshCommand(command), command, false, bridge, () => {});

  tunnel.up(true);
  await waitFor(() => tunnel.state([], ["gw"]).status === "failed", 6000);

  const state = tunnel.state([], ["gw"]);
  assert.equal(state.blocked, "auth");
  // The log has to say which of the two happened, because the exit code cannot.
  assert.match(state.log.join(" "), /never asked for a credential through the askpass helper/);

  tunnel.down();
  await bridge.stop();
});

test("a password that was asked for and refused says nothing about askpass", async () => {
  // The other half of the pair: a real refusal must not carry the Windows
  // advice, or the advice means nothing.
  const h = await harness("hunter2", "wrong");
  h.tunnel.up(true);

  await waitFor(() => h.status() === "failed", 6000);
  const log = h.tunnel.state([], ["gw"]).log.join(" ");
  assert.match(log, /Permission denied/);
  assert.ok(!log.includes("never asked for a credential"));

  await h.stop();
});

/**
 * An ssh that reports what it was handed and then behaves as told.
 *
 * `hold` decides the half being tested: staying alive is a remote command
 * holding the connection open (`ab-serve`), and exiting 0 is one that finished.
 */
function fakeSshEchoingArgs(hold: boolean): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ab-fake-ssh-"));
  const file = path.join(dir, "ssh");
  writeFileSync(
    file,
    `#!/usr/bin/env node
const args = process.argv.slice(2);
process.stderr.write("argc=" + args.length + "\\n");
process.stderr.write("last=" + args[args.length - 1] + "\\n");
${hold ? "setInterval(() => {}, 1000);" : "process.exit(0);"}
`,
    { encoding: "utf8", mode: 0o755 },
  );
  return file;
}

test("a remote command reaches ssh as one argument and holds the tunnel up", async () => {
  const bridge = new AskpassBridge(() => {});
  await bridge.start();
  const command = `${fakeSshEchoingArgs(true)} -L 18789:localhost:8787 host 'ab-serve --interval 30'`;
  const tunnel = new Tunnel("gw", parseSshCommand(command), command, false, bridge, () => {});

  tunnel.up(true);
  await waitFor(() => tunnel.state([], ["gw"]).status === "up", 6000);

  const log = tunnel.state([], ["gw"]).log.join("\n");
  // One argument, with its own spacing — not three words ssh would have to
  // reassemble, and not a `-N` that would forbid a command at all.
  assert.match(log, /last=ab-serve --interval 30/);
  assert.ok(!log.includes(" -N "));

  tunnel.down();
  await bridge.stop();
});

test("a remote command that finishes is reported as that, not as a bare exit 0", async () => {
  const bridge = new AskpassBridge(() => {});
  await bridge.start();
  const command = `${fakeSshEchoingArgs(false)} -L 18790:localhost:8787 host 'ab-serve'`;
  const tunnel = new Tunnel("gw", parseSshCommand(command), command, false, bridge, () => {});

  tunnel.up(true);
  // No autostart, so a finished command leaves the tunnel off rather than
  // retrying — and `ab-serve` exiting means the gateway would not serve.
  await waitFor(() => tunnel.running === false, 6000);
  assert.match(tunnel.state([], ["gw"]).log.join("\n"), /the remote command finished.*ab-serve/);

  await bridge.stop();
});

async function waitFor(predicate: () => boolean, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("timed out waiting");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}
