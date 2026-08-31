import { existsSync, mkdirSync, readFileSync, renameSync, watch, writeFileSync, type FSWatcher } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { parseSshCommand, type SshSpec } from "./ssh-command.ts";

/**
 * Where `ab-serve` is, said in a way that survives not knowing.
 *
 * `$AB_PATH` names the directory holding agent-bridge's console scripts, and the
 * obvious use of it — `$AB_PATH/ab-serve` — fails two ways. Unset, it expands to
 * nothing and the command becomes `/ab-serve`, which fails as "not found" and
 * names a path nobody configured. Set to the wrong directory, it fails the same
 * way with no fallback, even when `ab-serve` is on `PATH` a metre away.
 *
 * Prepending instead means the shell's own lookup does the work, and the answer
 * is right in all three cases: found in `$AB_PATH` when it is there, found on
 * `PATH` when `$AB_PATH` is unset *or* does not contain it. Measured across sh,
 * dash and bash, which agree.
 *
 * It also lands on `ab-serve`'s own environment, so the `shutil.which(
 * "agent-bridge")` it does next looks in the same directory it was found in.
 *
 * `exec` replaces the login shell rather than leaving it waiting: one process
 * fewer on the far side, and the signal that arrives when the connection drops
 * goes straight to `ab-serve`.
 */
export const DEFAULT_PATH_PREFIX = '"${AB_PATH:+$AB_PATH:}$PATH"';
export const DEFAULT_EXEC = `PATH=${DEFAULT_PATH_PREFIX}; exec ab-serve`;

/**
 * `ab`'s own `gateways.json`, read rather than copied.
 *
 * A gateway configured once for the CLI is configured for the dashboard, and
 * the two cannot drift because there is only one of them. The `ssh` key is the
 * dashboard's contribution: the command that makes `base_url` reachable, in the
 * form the user would type it. `ab` ignores keys it does not know, so an entry
 * carrying one still works at the CLI.
 */
export interface GatewayEntry {
  name: string;
  baseUrl: string;
  /** Resolved at use and never put in anything the browser can read. */
  token: string | null;
  tokenSource: "token" | "token_env" | "token_file" | "none";
  /** The variable name or file path, so the config dialog can show the pointer. */
  tokenName?: string;
  tokenError?: string;
  ssh?: string;
  /**
   * What to run on the far side when the tunnel comes up.
   *
   * `true` means the shipped default — start the gateway with `ab-serve` — and a
   * string is the user's own longer script. Absent means nothing runs and the
   * connection is a plain forward.
   */
  exec?: true | string;
  /** Parsed from `ssh`, with `exec` folded in. Drawn and probed from here. */
  spec?: SshSpec;
  enabled: boolean;
  autoStart: boolean;
  isDefault: boolean;
}

export interface LoadedConfig {
  gateways: GatewayEntry[];
  /** Problems with the file itself, which no row can carry. */
  errors: string[];
  /** Which file this came from, for the page to name. */
  configPath: string;
  /** True when the config lives in a format we will not write back. */
  readOnly: boolean;
}

/** `ab`'s discovery order, so the dashboard reads what the CLI reads. */
export function configPath(): string {
  const explicit = process.env.AGENT_BRIDGE_CLIENT_CONFIG;
  if (explicit) return expand(explicit);
  const home = path.join(homedir(), ".config", "agent-bridge", "gateways.json");
  if (existsSync(home)) return home;
  const cwd = path.resolve("gateways.json");
  if (existsSync(cwd)) return cwd;
  // Nothing yet: name the path the CLI would create, so the first write lands
  // where `ab` will look for it.
  return home;
}

function expand(value: string): string {
  if (value === "~") return homedir();
  if (value.startsWith("~/") || value.startsWith("~\\")) return path.join(homedir(), value.slice(2));
  return path.resolve(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * `token`, then `token_env`, then `token_file` — `ab`'s order, so a gateway
 * that works there works here.
 *
 * The reason for reporting the *source* rather than the value is that a token
 * that is missing and a token that is wrong are different problems, and the
 * page has to be able to say which without ever being handed the secret.
 */
function resolveToken(
  entry: Record<string, unknown>,
): Pick<GatewayEntry, "token" | "tokenSource" | "tokenName" | "tokenError"> {
  if (typeof entry.token === "string" && entry.token) return { token: entry.token, tokenSource: "token" };

  if (typeof entry.token_env === "string") {
    const name = entry.token_env;
    const value = process.env[name];
    if (value) return { token: value, tokenSource: "token_env", tokenName: name };
    return { token: null, tokenSource: "token_env", tokenName: name, tokenError: `$${name} is not set` };
  }

  if (typeof entry.token_file === "string") {
    // The path as written, not as expanded: it is what goes back in the file.
    const name = entry.token_file;
    const file = expand(name);
    try {
      const token = readFileSync(file, "utf8").trim();
      if (token) return { token, tokenSource: "token_file", tokenName: name };
      return { token: null, tokenSource: "token_file", tokenName: name, tokenError: `${file} is empty` };
    } catch (err) {
      return { token: null, tokenSource: "token_file", tokenName: name, tokenError: (err as Error).message };
    }
  }

  return { token: null, tokenSource: "none", tokenError: "no token, token_env or token_file" };
}

/** `true`, a command, or nothing. Anything else in the file is nothing. */
function readExec(raw: unknown): true | string | undefined {
  if (raw === true) return true;
  if (typeof raw === "string" && raw.trim()) return raw.trim();
  return undefined;
}

/**
 * Fold `exec` into the parsed command, and let the `ssh` line win.
 *
 * A command written into the line is the more specific and the more visible of
 * the two — it is right there in the field somebody is looking at — so `exec` is
 * what fills the gap when the line ends at the host. Both is a mistake worth a
 * diagnostic rather than a silent choice.
 */
export function withExec(spec: SshSpec, exec: true | string | undefined): SshSpec {
  if (exec === undefined) return spec;
  if (spec.remoteCommand) {
    return {
      ...spec,
      diagnostics: [
        ...spec.diagnostics,
        `The ssh line already ends in a command, so "exec" is not used: ${spec.remoteCommand}`,
      ],
    };
  }
  return { ...spec, remoteCommand: exec === true ? DEFAULT_EXEC : exec };
}

export function loadConfig(): LoadedConfig {
  const file = configPath();
  const errors: string[] = [];
  const gateways: GatewayEntry[] = [];
  let raw: Record<string, unknown> = {};

  if (existsSync(file)) {
    try {
      const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
      if (isRecord(parsed)) raw = parsed;
      else errors.push(`${file} must be a JSON object`);
    } catch (err) {
      errors.push(`${file} is not valid JSON: ${(err as Error).message}`);
    }
  } else {
    errors.push(`no gateway config yet — ${file} does not exist`);
  }

  const defaultName = typeof raw.default === "string" ? raw.default : null;
  const entries = isRecord(raw.gateways) ? raw.gateways : {};

  for (const [name, value] of Object.entries(entries)) {
    if (!isRecord(value)) {
      errors.push(`gateway "${name}" must be an object`);
      continue;
    }
    const baseUrl = typeof value.base_url === "string" ? value.base_url.replace(/\/+$/, "") : "";
    if (!baseUrl) {
      errors.push(`gateway "${name}" has no base_url`);
      continue;
    }
    const ssh = typeof value.ssh === "string" && value.ssh.trim() ? value.ssh.trim() : undefined;
    const exec = readExec(value.exec);
    gateways.push({
      name,
      baseUrl,
      ...resolveToken(value),
      ssh,
      exec,
      spec: ssh ? withExec(parseSshCommand(ssh), exec) : undefined,
      enabled: value.enabled === undefined ? true : Boolean(value.enabled),
      autoStart: Boolean(value.autostart ?? value.autoStart),
      isDefault: name === defaultName,
    });
  }

  return { gateways, errors, configPath: file, readOnly: file.endsWith(".toml") };
}

/**
 * Notice when the config changes underneath us.
 *
 * **The directory, not the file.** `gateways.json` is replaced rather than
 * written in place — `writeEntry` below does exactly that, and so does every
 * editor worth using — and on inotify a watch holds the inode, so it goes
 * quiet once the name points at a new one. The second reason is not about
 * portability at all: the token files the entries point at live beside it and
 * are read when the config is loaded, so a rotated token is as stale as an
 * unseen gateway, and a watch on one name would never see one.
 *
 * **Debounced**, because one edit is not one event: an atomic write lands as a
 * create and then a rename, and an editor that truncates before it writes
 * would otherwise be read halfway through.
 */
export function watchConfig(onChange: () => void, delayMs = 250): () => void {
  const dir = path.dirname(configPath());
  let timer: NodeJS.Timeout | null = null;
  let watcher: FSWatcher | null = null;

  try {
    // `persistent: false`: a watch on a config file is not a reason for the
    // process to stay alive, and in a test it is a reason for it not to exit.
    watcher = watch(dir, { persistent: false }, () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        onChange();
      }, delayMs);
    });
  } catch {
    // No directory: agent-bridge has never been configured on this machine.
    // `loadConfig` already says so, and the first write creates the directory.
    return () => {};
  }

  return () => {
    if (timer) clearTimeout(timer);
    timer = null;
    watcher?.close();
    watcher = null;
  };
}

/**
 * Write one gateway entry back, preserving everything else in the file.
 *
 * The dashboard is a guest in `ab`'s config: unknown keys survive verbatim, the
 * write is atomic, and the formatting stays two-space JSON so the diff is the
 * line that changed. A key set to `undefined` in the patch is removed, which is
 * how the dialog clears an `ssh` command without inventing an empty one.
 */
export function writeEntry(name: string, patch: Record<string, unknown>, options: { makeDefault?: boolean } = {}): void {
  const file = configPath();
  if (file.endsWith(".toml")) throw new Error(`${file} is TOML; edit it by hand`);

  const raw = readRaw(file);
  const gateways = isRecord(raw.gateways) ? { ...raw.gateways } : {};
  const existing = isRecord(gateways[name]) ? { ...(gateways[name] as Record<string, unknown>) } : {};
  const next = { ...existing, ...patch };
  for (const [key, value] of Object.entries(patch)) if (value === undefined) delete next[key];

  const merged: Record<string, unknown> = { ...raw, gateways: { ...gateways, [name]: next } };
  if (options.makeDefault) merged.default = name;
  else if (!merged.default) merged.default = name;
  save(file, merged);
}

/** Remove an entry, and the `default` pointer with it if it named this one. */
export function removeEntry(name: string): void {
  const file = configPath();
  if (file.endsWith(".toml")) throw new Error(`${file} is TOML; edit it by hand`);

  const raw = readRaw(file);
  const gateways = isRecord(raw.gateways) ? { ...raw.gateways } : {};
  if (!(name in gateways)) throw new Error(`no gateway "${name}" in ${file}`);
  delete gateways[name];

  const merged: Record<string, unknown> = { ...raw, gateways };
  if (merged.default === name) {
    const first = Object.keys(gateways)[0];
    if (first) merged.default = first;
    else delete merged.default;
  }
  save(file, merged);
}

/** Rename an entry in place, keeping its position in the file's order. */
export function renameEntry(from: string, to: string): void {
  const file = configPath();
  if (file.endsWith(".toml")) throw new Error(`${file} is TOML; edit it by hand`);
  if (from === to) return;

  const raw = readRaw(file);
  const gateways = isRecord(raw.gateways) ? (raw.gateways as Record<string, unknown>) : {};
  if (!(from in gateways)) throw new Error(`no gateway "${from}" in ${file}`);
  if (to in gateways) throw new Error(`a gateway called "${to}" already exists`);

  // Rebuilt in order rather than deleted and appended: the sidebar is drawn in
  // file order, and a rename that moved a row to the bottom of the list would
  // read as one entry vanishing and another appearing.
  const renamed = Object.fromEntries(
    Object.entries(gateways).map(([key, value]) => [key === from ? to : key, value]),
  );
  const merged: Record<string, unknown> = { ...raw, gateways: renamed };
  if (merged.default === from) merged.default = to;
  save(file, merged);
}

function readRaw(file: string): Record<string, unknown> {
  if (!existsSync(file)) return {};
  const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
  if (!isRecord(parsed)) throw new Error(`${file} must be a JSON object`);
  return parsed;
}

/**
 * Atomic, and `0600`.
 *
 * The file can hold a raw token — `token` is one of the three forms `ab`
 * accepts — so a mode that lets the rest of the machine read it is wrong even
 * when this particular entry uses `token_env`. A half-written config is worse
 * than a stale one, hence the rename.
 */
function save(file: string, value: Record<string, unknown>): void {
  mkdirSync(path.dirname(file), { recursive: true });
  const temp = `${file}.webui-tmp`;
  writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  renameSync(temp, file);
}

/** Which forward, if any, carries this base URL. Computed, never configured. */
export function forwardPortFor(baseUrl: string, spec: SshSpec | undefined): number | undefined {
  if (!spec) return undefined;
  let url: URL;
  try {
    url = new URL(baseUrl);
  } catch {
    return undefined;
  }
  const port = Number(url.port || (url.protocol === "https:" ? 443 : 80));
  const local = ["localhost", "127.0.0.1", "::1", "[::1]"].includes(url.hostname.toLowerCase());
  if (!local) return undefined;
  return spec.forwards.find((forward) => forward.localPort === port)?.localPort;
}
