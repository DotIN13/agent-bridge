import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { execFileSync } from "node:child_process";
import { DEFAULT_EXEC, DEFAULT_EXEC_TARGET, loadConfig, removeEntry, renameEntry, writeEntry } from "./config.ts";

/** A config file of our own, pointed at the way `ab` points at one. */
function withConfig(contents: unknown): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ab-webui-"));
  const file = path.join(dir, "gateways.json");
  writeFileSync(file, `${JSON.stringify(contents, null, 2)}\n`, "utf8");
  process.env.AGENT_BRIDGE_CLIENT_CONFIG = file;
  return file;
}

test.afterEach(() => {
  delete process.env.AGENT_BRIDGE_CLIENT_CONFIG;
  delete process.env.AB_TEST_TOKEN;
});

test("an entry is read with its ssh command parsed and its ports drawn", () => {
  withConfig({
    default: "midway5",
    gateways: {
      midway5: {
        base_url: "http://localhost:8787/",
        token_env: "AB_TEST_TOKEN",
        ssh: "ssh -N -L 8787:localhost:8787 midway5",
      },
    },
  });
  process.env.AB_TEST_TOKEN = "shh";

  const config = loadConfig();
  const entry = config.gateways[0]!;
  assert.equal(entry.name, "midway5");
  // The trailing slash is stripped, or every path built from it has two.
  assert.equal(entry.baseUrl, "http://localhost:8787");
  assert.equal(entry.token, "shh");
  assert.equal(entry.tokenSource, "token_env");
  assert.equal(entry.tokenName, "AB_TEST_TOKEN");
  assert.equal(entry.isDefault, true);
  assert.equal(entry.enabled, true);
  assert.deepEqual(entry.spec?.forwards, [
    { kind: "local", localPort: 8787, remoteHost: "localhost", remotePort: 8787 },
  ]);
  assert.deepEqual(config.errors, []);
});

test("a token that is named but not set is a stated problem, not a silent empty string", () => {
  withConfig({ gateways: { gw: { base_url: "http://localhost:1", token_env: "AB_TEST_TOKEN" } } });
  const entry = loadConfig().gateways[0]!;
  assert.equal(entry.token, null);
  assert.equal(entry.tokenName, "AB_TEST_TOKEN");
  assert.match(entry.tokenError!, /\$AB_TEST_TOKEN is not set/);
});

test("a token file is read, and its path is published as written", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "ab-webui-"));
  const tokenFile = path.join(dir, "gw.token");
  // Trailing whitespace and a newline: what an `echo > file` leaves behind, and
  // a token with a newline on the end is a token the gateway rejects.
  writeFileSync(tokenFile, "  from-a-file\n", "utf8");
  withConfig({ gateways: { gw: { base_url: "http://localhost:1", token_file: tokenFile } } });

  const entry = loadConfig().gateways[0]!;
  assert.equal(entry.token, "from-a-file");
  assert.equal(entry.tokenSource, "token_file");
  // The path as written, because that is what goes back in the file.
  assert.equal(entry.tokenName, tokenFile);
});

test("an entry with no base_url is an error against that name, not a crash", () => {
  withConfig({ gateways: { ok: { base_url: "http://localhost:1" }, broken: { ssh: "ssh gw" } } });
  const config = loadConfig();
  assert.equal(config.gateways.length, 1);
  assert.match(config.errors.join(" "), /"broken" has no base_url/);
});

test("a config that is not JSON is reported with the parse error in it", () => {
  const file = withConfig({});
  writeFileSync(file, "{ nope", "utf8");
  assert.match(loadConfig().errors.join(" "), /is not valid JSON/);
});

test("exec true means the shipped default, and a string means your own script", () => {
  withConfig({
    gateways: {
      plain: { base_url: "http://localhost:1", ssh: "ssh gw" },
      standard: { base_url: "http://localhost:2", ssh: "ssh gw2", exec: true },
      custom: { base_url: "http://localhost:3", ssh: "ssh gw3", exec: "~/bin/start.sh --wait" },
      // A key the file may carry and this reader turns into "absent", so the
      // three states downstream stay three.
      off: { base_url: "http://localhost:4", ssh: "ssh gw4", exec: false },
    },
  });

  const byName = Object.fromEntries(loadConfig().gateways.map((entry) => [entry.name, entry]));
  assert.equal(byName.plain!.spec!.remoteCommand, undefined);
  assert.equal(byName.standard!.spec!.remoteCommand, DEFAULT_EXEC);
  assert.equal(byName.custom!.spec!.remoteCommand, "~/bin/start.sh --wait");
  assert.equal(byName.off!.exec, undefined);
  assert.equal(byName.off!.spec!.remoteCommand, undefined);
});

test("a command in the ssh line wins over exec, and the clash is said out loud", () => {
  // The line is the more specific and the more visible of the two — it is in the
  // field somebody is looking at — so `exec` fills the gap rather than taking
  // over. Silently preferring either one is how a config lies.
  withConfig({
    gateways: { gw: { base_url: "http://localhost:1", ssh: "ssh host 'my-own-thing'", exec: true } },
  });
  const entry = loadConfig().gateways[0]!;
  assert.equal(entry.spec!.remoteCommand, "my-own-thing");
  assert.match(entry.spec!.diagnostics.join(" "), /already ends in a command/);
});

test("exec with no ssh command carries nothing, because there is no connection to carry it", () => {
  withConfig({ gateways: { gw: { base_url: "http://localhost:1", exec: true } } });
  const entry = loadConfig().gateways[0]!;
  assert.equal(entry.exec, true);
  assert.equal(entry.spec, undefined);
});

test("the default command finds ab-serve with or without $AB_BIN_PATH", () => {
  /*
   * The whole point of the expansion, checked against a real shell rather than
   * read. `$AB_BIN_PATH/ab-serve` on its own is the trap: unset, it expands to
   * `/ab-serve` and fails as "not found", naming nothing useful.
   */
  const expand = (env: Record<string, string>) =>
    execFileSync("/bin/sh", ["-c", `echo ${DEFAULT_EXEC_TARGET}`], {
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", ...env },
    }).trim();

  assert.equal(expand({}), "ab-serve");
  assert.equal(expand({ AB_BIN_PATH: "/opt/ab/bin" }), "/opt/ab/bin/ab-serve");
  // A path with a space survives, because the expansion is quoted.
  assert.equal(expand({ AB_BIN_PATH: "/opt/my tools" }), "/opt/my tools/ab-serve");
  // And it replaces the shell, so the signal from a dropped connection lands on
  // ab-serve rather than on a shell waiting for it.
  assert.ok(DEFAULT_EXEC.startsWith("exec "));
});

test("a diagnostic from the ssh command is attached to the entry that carries it", () => {
  withConfig({
    gateways: { gw: { base_url: "http://localhost:1", ssh: "ssh -L 0.0.0.0:1:localhost:1 host" } },
  });
  assert.match(loadConfig().gateways[0]!.spec!.diagnostics.join(" "), /not loopback/);
});

test("writing an entry leaves every other key in the file alone", () => {
  const file = withConfig({
    default: "one",
    // A key this dashboard knows nothing about, which `ab` or a future version
    // may. Rewriting the file must not be how somebody loses it.
    experiment: { keep: true },
    gateways: {
      one: { base_url: "http://localhost:1", token_env: "A", note: "mine" },
      two: { base_url: "http://localhost:2" },
    },
  });

  writeEntry("one", { ssh: "ssh -N -L 1:localhost:1 host" });

  const raw = JSON.parse(readFileSync(file, "utf8")) as Record<string, any>;
  assert.deepEqual(raw.experiment, { keep: true });
  assert.equal(raw.gateways.one.note, "mine");
  assert.equal(raw.gateways.one.token_env, "A");
  assert.equal(raw.gateways.one.ssh, "ssh -N -L 1:localhost:1 host");
  assert.equal(raw.gateways.two.base_url, "http://localhost:2");
  assert.equal(raw.default, "one");
});

test("a key set to undefined in a patch is removed rather than written as null", () => {
  const file = withConfig({ gateways: { one: { base_url: "http://localhost:1", ssh: "ssh host" } } });
  writeEntry("one", { ssh: undefined });
  const raw = JSON.parse(readFileSync(file, "utf8")) as Record<string, any>;
  assert.ok(!("ssh" in raw.gateways.one));
});

test("the file is written 0600, because one of its three token forms is a raw token", () => {
  const file = withConfig({ gateways: { one: { base_url: "http://localhost:1" } } });
  writeEntry("one", { base_url: "http://localhost:2" });
  assert.equal(statSync(file).mode & 0o777, 0o600);
});

test("a rename keeps the entry where it was in the file, and moves the default with it", () => {
  const file = withConfig({
    default: "two",
    gateways: {
      one: { base_url: "http://localhost:1" },
      two: { base_url: "http://localhost:2" },
      three: { base_url: "http://localhost:3" },
    },
  });

  renameEntry("two", "renamed");

  const raw = JSON.parse(readFileSync(file, "utf8")) as Record<string, any>;
  // Order matters: the sidebar is drawn in file order, and a rename that moved
  // a row to the bottom reads as one entry vanishing and another appearing.
  assert.deepEqual(Object.keys(raw.gateways), ["one", "renamed", "three"]);
  assert.equal(raw.default, "renamed");
  assert.equal(raw.gateways.renamed.base_url, "http://localhost:2");
});

test("renaming onto a name that exists is refused before anything is written", () => {
  const file = withConfig({
    gateways: { one: { base_url: "http://localhost:1" }, two: { base_url: "http://localhost:2" } },
  });
  assert.throws(() => renameEntry("one", "two"), /already exists/);
  const raw = JSON.parse(readFileSync(file, "utf8")) as Record<string, any>;
  assert.deepEqual(Object.keys(raw.gateways), ["one", "two"]);
});

test("removing the default entry hands the pointer to another rather than dangling", () => {
  const file = withConfig({
    default: "one",
    gateways: { one: { base_url: "http://localhost:1" }, two: { base_url: "http://localhost:2" } },
  });

  removeEntry("one");

  const raw = JSON.parse(readFileSync(file, "utf8")) as Record<string, any>;
  assert.deepEqual(Object.keys(raw.gateways), ["two"]);
  assert.equal(raw.default, "two");
});

test("removing the last entry drops the default rather than naming a gateway that is gone", () => {
  const file = withConfig({ default: "one", gateways: { one: { base_url: "http://localhost:1" } } });
  removeEntry("one");
  const raw = JSON.parse(readFileSync(file, "utf8")) as Record<string, any>;
  assert.deepEqual(raw.gateways, {});
  assert.ok(!("default" in raw));
});

test("a TOML config is read-only, and says so instead of writing JSON over it", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "ab-webui-"));
  const file = path.join(dir, "gateways.toml");
  writeFileSync(file, "# ab also accepts TOML\n", "utf8");
  process.env.AGENT_BRIDGE_CLIENT_CONFIG = file;

  const config = loadConfig();
  assert.equal(config.readOnly, true);
  assert.throws(() => writeEntry("one", { base_url: "http://localhost:1" }), /TOML/);
  assert.equal(readFileSync(file, "utf8"), "# ab also accepts TOML\n");
});

test("a first write creates the file, and names the entry as the default", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "ab-webui-"));
  const file = path.join(dir, "nested", "gateways.json");
  process.env.AGENT_BRIDGE_CLIENT_CONFIG = file;

  // Nothing there yet is a stated condition, not an empty list.
  assert.match(loadConfig().errors.join(" "), /does not exist/);

  writeEntry("first", { base_url: "http://localhost:8787" });
  const raw = JSON.parse(readFileSync(file, "utf8")) as Record<string, any>;
  assert.equal(raw.default, "first");
  assert.equal(loadConfig().gateways[0]!.isDefault, true);
});
