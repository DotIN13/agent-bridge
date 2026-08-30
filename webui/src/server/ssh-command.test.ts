import assert from "node:assert/strict";
import test from "node:test";
import { buildSshArgs, formatSshCommand, parseSshCommand, tokenize } from "./ssh-command.ts";

test("a quoted argument survives tokenizing as one token", () => {
  assert.deepEqual(tokenize(`ssh -o "ProxyCommand=nc -x host 22" gw`), [
    "ssh",
    "-o",
    "ProxyCommand=nc -x host 22",
    "gw",
  ]);
});

test("an empty quoted argument is a token, not nothing", () => {
  // `-o Something=` is a legal way to clear an option, and dropping the empty
  // value silently turns it into the *next* token's flag.
  assert.deepEqual(tokenize(`ssh -o "" gw`), ["ssh", "-o", "", "gw"]);
});

test("a local forward is read whether the flag is attached or not", () => {
  const attached = parseSshCommand("ssh -L8787:localhost:8787 midway5");
  const detached = parseSshCommand("ssh -L 8787:localhost:8787 midway5");
  assert.deepEqual(attached.forwards, detached.forwards);
  assert.deepEqual(attached.forwards, [
    { kind: "local", localPort: 8787, remoteHost: "localhost", remotePort: 8787 },
  ]);
  assert.equal(attached.destination, "midway5");
});

test("an explicit loopback bind is accepted and a public one is refused", () => {
  const loopback = parseSshCommand("ssh -L 127.0.0.1:8787:localhost:8787 gw");
  assert.equal(loopback.forwards.length, 1);
  assert.deepEqual(loopback.diagnostics, []);

  const public_ = parseSshCommand("ssh -L 0.0.0.0:8787:localhost:8787 gw");
  assert.deepEqual(public_.forwards, []);
  assert.match(public_.diagnostics[0]!, /not loopback/);
});

test("a reverse forward is refused, and says why", () => {
  const parsed = parseSshCommand("ssh -R 9000:localhost:9000 gw");
  assert.deepEqual(parsed.forwards, []);
  assert.match(parsed.diagnostics[0]!, /exposes this machine/);
});

test("the two ways of publishing a forward beyond this machine are both refused", () => {
  assert.match(parseSshCommand("ssh -g -L 8787:localhost:8787 gw").diagnostics[0]!, /beyond this machine/);
  assert.match(
    parseSshCommand("ssh -o GatewayPorts=yes -L 8787:localhost:8787 gw").diagnostics[0]!,
    /beyond this machine/,
  );
  // …and `GatewayPorts=no` is not a refusal: it is the default said out loud.
  const explicit = parseSshCommand("ssh -o GatewayPorts=no -L 8787:localhost:8787 gw");
  assert.deepEqual(explicit.diagnostics, []);
  assert.equal(explicit.options.GatewayPorts, "no");
});

test("an unrecognised boolean flag does not swallow the argument after it", () => {
  // `-4` takes no value. Advancing past it consumed the `-L`, and the forward
  // vanished with no diagnostic — the worst way to lose one.
  const parsed = parseSshCommand("ssh -4 -L 8787:localhost:8787 midway5");
  assert.equal(parsed.forwards.length, 1);
  assert.deepEqual(parsed.passthrough, ["-4"]);
  assert.equal(parsed.destination, "midway5");
});

test("a flag missing its value is reported rather than guessed at", () => {
  const parsed = parseSshCommand("ssh -L");
  assert.match(parsed.diagnostics.join(" "), /"-L" is missing its value/);
});

test("a command with no host says so", () => {
  assert.match(parseSshCommand("ssh -L 8787:localhost:8787").diagnostics.join(" "), /No host/);
});

test("a full path to ssh is kept as the binary, and a bare `ssh` is not", () => {
  assert.equal(parseSshCommand("/usr/bin/ssh gw").binary, "/usr/bin/ssh");
  assert.equal(parseSshCommand("ssh gw").binary, undefined);
});

test("a remote command after the destination is ignored, because -N carries none", () => {
  const parsed = parseSshCommand("ssh gw sleep 100");
  assert.equal(parsed.destination, "gw");
  assert.match(parsed.diagnostics.join(" "), /carries no remote command/);
});

test("the argv puts the user's own options last, so they win", () => {
  const parsed = parseSshCommand("ssh -o ConnectTimeout=90 -L 8787:localhost:8787 -p 2222 -i ~/.ssh/id gw");
  const args = buildSshArgs(parsed, { batch: false });

  assert.equal(args[0], "-N");
  assert.equal(args.at(-1), "gw");
  // Ours is present, theirs is later, and later is what ssh honours.
  const ours = args.indexOf("ConnectTimeout=15");
  const theirs = args.indexOf("ConnectTimeout=90");
  assert.ok(ours !== -1 && theirs > ours);
  assert.ok(args.includes("ExitOnForwardFailure=yes"));
  assert.deepEqual(args.slice(args.indexOf("-p"), args.indexOf("-p") + 2), ["-p", "2222"]);
  assert.ok(args.includes("-L") && args.includes("8787:localhost:8787"));
});

test("only an unattended attempt gets BatchMode, because only it cannot prompt", () => {
  const parsed = parseSshCommand("ssh gw");
  assert.ok(!buildSshArgs(parsed, { batch: false }).includes("BatchMode=yes"));
  assert.ok(buildSshArgs(parsed, { batch: true }).includes("BatchMode=yes"));
});

test("a dynamic forward comes back as -D and not as a broken -L", () => {
  const parsed = parseSshCommand("ssh -D 1080 gw");
  assert.deepEqual(parsed.forwards, [{ kind: "dynamic", localPort: 1080 }]);
  const args = buildSshArgs(parsed, { batch: false });
  assert.deepEqual(args.slice(args.indexOf("-D"), args.indexOf("-D") + 2), ["-D", "1080"]);
});

test("the formatted command quotes an option with a space in it", () => {
  const parsed = parseSshCommand(`ssh -o "ProxyCommand=nc host 22" gw`);
  assert.match(formatSshCommand(parsed, { batch: false }), /"ProxyCommand=nc host 22"/);
});
