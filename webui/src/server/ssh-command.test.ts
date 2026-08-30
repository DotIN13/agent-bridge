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

test("the first word is the program, whatever it is called", () => {
  assert.equal(parseSshCommand("/usr/bin/ssh gw").binary, "/usr/bin/ssh");
  // A bare `ssh` stays undefined, so the argv uses whatever is on PATH.
  assert.equal(parseSshCommand("ssh gw").binary, undefined);
  assert.equal(parseSshCommand("ssh.exe gw").binary, "ssh.exe");

  /*
   * The one that cost a live run: matching only `/ssh$/` made `autossh` the
   * *destination*, which turned the real host into a remote command and left
   * the connect button reporting "no ssh on PATH".
   */
  // `-M 0` is autossh's monitoring port. Arity is the one thing this parser
  // cannot infer, so it is named in VALUED — without that its `0` became the
  // destination and `midway5` a remote command.
  const autossh = parseSshCommand("autossh -M 0 -L 8787:localhost:8787 midway5");
  assert.equal(autossh.binary, "autossh");
  assert.equal(autossh.destination, "midway5");
  assert.equal(autossh.remoteCommand, undefined);
  assert.deepEqual(autossh.forwards, [
    { kind: "local", localPort: 8787, remoteHost: "localhost", remotePort: 8787 },
  ]);
});

test("a command after the host is kept, and takes -N off the argv with it", () => {
  const parsed = parseSshCommand("ssh -L 8787:localhost:8787 midway5 ab-serve");
  assert.equal(parsed.destination, "midway5");
  assert.equal(parsed.remoteCommand, "ab-serve");
  assert.deepEqual(parsed.diagnostics, []);

  const args = buildSshArgs(parsed, { batch: false });
  // `-N` is "no command", so the two are exclusive.
  assert.ok(!args.includes("-N"));
  assert.deepEqual(args.slice(-2), ["midway5", "ab-serve"]);
});

test("a quoted command reaches the far side as one argument, quoting intact", () => {
  // Rebuilt from tokens, `&&` becomes an argument to systemctl and the second
  // half never runs. It has to travel as written.
  const parsed = parseSshCommand(
    `ssh -L 8787:localhost:8787 gw 'systemctl --user start agent-bridge && exec ab-serve --interval 30'`,
  );
  assert.equal(parsed.remoteCommand, "systemctl --user start agent-bridge && exec ab-serve --interval 30");
  const args = buildSshArgs(parsed, { batch: false });
  assert.equal(args.at(-1), "systemctl --user start agent-bridge && exec ab-serve --interval 30");
  assert.equal(args.filter((arg) => arg === "systemctl --user start agent-bridge && exec ab-serve --interval 30").length, 1);
});

test("an unquoted command keeps every word, including its own flags", () => {
  const parsed = parseSshCommand("ssh gw ab-serve --config ~/.config/agent-bridge/config.toml");
  assert.equal(parsed.remoteCommand, "ab-serve --config ~/.config/agent-bridge/config.toml");
  // The command's own `--config` is not read as ssh's: everything past the
  // first non-flag word belongs to the far side.
  assert.deepEqual(parsed.options, {});
});

test("a flag after the destination is still a flag, so an old config keeps working", () => {
  /*
   * ssh itself stops reading options at the destination, so `-L` here is
   * strictly a remote command. Being faithful would silently turn
   * `ssh midway5 -L 8787:localhost:8787` — which this parser used to accept —
   * into a remote `-L` and drop the forward, which is a worse failure than not
   * being faithful.
   */
  const parsed = parseSshCommand("ssh midway5 -L 8787:localhost:8787");
  assert.equal(parsed.destination, "midway5");
  assert.equal(parsed.remoteCommand, undefined);
  assert.deepEqual(parsed.forwards, [
    { kind: "local", localPort: 8787, remoteHost: "localhost", remotePort: 8787 },
  ]);
  assert.ok(buildSshArgs(parsed, { batch: false }).includes("-N"));
});

test("quoting the command changes nothing here, because there is no local shell", () => {
  /*
   * The question this settles: does `$AB_PATH` get expanded on the way out?
   *
   * No — there is nothing to expand it. `spawn` runs ssh without a shell, so the
   * command crosses as one literal argv element and the *remote* login shell
   * does the expanding, which is the only correct answer: this machine's
   * `$AB_PATH` says nothing about a cluster's layout.
   *
   * So single quotes, double quotes and none at all come out the same. They are
   * still worth writing: the same line pasted into a terminal goes through a
   * local shell first, and there the quotes are what stop it expanding
   * `$AB_PATH` before ssh ever sees it.
   */
  const command = 'PATH="${AB_PATH:+$AB_PATH:}$PATH"; exec ab-serve';
  const forms = [
    `ssh -L 8787:localhost:8787 midway5 '${command}'`,
    `ssh -L 8787:localhost:8787 midway5 ${command}`,
  ];

  for (const line of forms) {
    const parsed = parseSshCommand(line);
    assert.equal(parsed.destination, "midway5");
    assert.equal(parsed.remoteCommand, command);
    // Unexpanded, and one argument: what ssh hands the far side verbatim.
    assert.equal(buildSshArgs(parsed, { batch: false }).at(-1), command);
  }

  // A double-quoted one loses its outer pair the same way, and its `$` survives.
  const doubled = parseSshCommand('ssh gw "exec $AB_PATH/ab-serve"');
  assert.equal(doubled.remoteCommand, "exec $AB_PATH/ab-serve");
});

test("the command is the whole tail, so a flag inside it is not lifted out", () => {
  const parsed = parseSshCommand("ssh gw bash -lc 'exec ab-serve'");
  assert.equal(parsed.remoteCommand, "bash -lc 'exec ab-serve'");
  assert.deepEqual(parsed.passthrough, []);
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

test("the formatted command quotes the remote command as the one argument it is", () => {
  const parsed = parseSshCommand("ssh -L 1:localhost:1 gw 'ab-serve --interval 30'");
  assert.match(formatSshCommand(parsed, { batch: false }), /gw "ab-serve --interval 30"$/);
});

test("the formatted command quotes an option with a space in it", () => {
  const parsed = parseSshCommand(`ssh -o "ProxyCommand=nc host 22" gw`);
  assert.match(formatSshCommand(parsed, { batch: false }), /"ProxyCommand=nc host 22"/);
});
