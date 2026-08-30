import type { Forward } from "../protocol.ts";

/**
 * An ssh command line, read rather than invented.
 *
 * The user already has a command that works — it is the `ssh` key on a gateway
 * in `gateways.json`, the same line they would paste into a terminal. This
 * dashboard's job is to run it and to understand enough of it to draw the
 * ports and probe them. So this parses for *comprehension*: it never
 * re-serializes the result over the top of what was written, and a flag it does
 * not recognise is handed to ssh as it arrived.
 *
 * Ported from picone's `remote/ssh-command.ts`, minus its multiplexing support:
 * that exists so a port can be added to a live connection, which nothing here
 * asks for.
 */
export interface SshSpec {
  /** Present when the command names one, e.g. `/usr/bin/ssh -L …`. */
  binary?: string;
  destination: string;
  forwards: Forward[];
  port?: number;
  jump?: string;
  identity?: string;
  options: Record<string, string>;
  /** Everything else, in order, handed to ssh untouched. */
  passthrough: string[];
  /** Refusals and problems, in the words the user should read. */
  diagnostics: string[];
}

/** Binds that keep a forward on this machine. Anything else is refused. */
const LOOPBACK = new Set(["", "localhost", "127.0.0.1", "::1", "[::1]"]);

/** Flags that take a value in the next argument. */
const VALUED = new Set([
  "-L", "-R", "-D", "-p", "-i", "-J", "-o", "-l", "-F", "-b", "-c", "-m", "-w", "-e", "-B", "-I",
  "-S", "-Q", "-W",
]);

/**
 * Flags we set ourselves, dropped silently rather than fought with. `-f` and
 * `-n` in particular would detach the process we are trying to supervise.
 */
const OURS = new Set(["-N", "-f", "-n", "-T", "-t", "-v", "-vv", "-vvv", "-q"]);

/** Split a command line, respecting quotes. No shell is involved anywhere. */
export function tokenize(command: string): string[] {
  const out: string[] = [];
  let current = "";
  let quote: '"' | "'" | null = null;
  let started = false;

  for (let i = 0; i < command.length; i++) {
    const ch = command[i]!;
    if (quote) {
      if (ch === quote) quote = null;
      else current += ch;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      started = true;
      continue;
    }
    if (/\s/.test(ch)) {
      if (started || current) out.push(current);
      current = "";
      started = false;
      continue;
    }
    current += ch;
  }
  if (started || current) out.push(current);
  return out;
}

/**
 * One `-L`/`-D` spec.
 *
 * `-L` is `[bind:]port:host:hostport`, and the bind is the part that matters
 * here: a forward bound to anything but loopback publishes a route into the
 * cluster to the whole network, which is never what someone pasting a command
 * into a local dashboard meant.
 */
function parseForward(kind: "local" | "dynamic", spec: string, diagnostics: string[]): Forward | null {
  const parts = spec.split(":");

  if (kind === "dynamic") {
    // `-D [bind:]port`
    const bind = parts.length > 1 ? parts[0]! : "";
    const port = Number(parts[parts.length - 1]);
    if (!Number.isInteger(port) || port <= 0) {
      diagnostics.push(`Could not read the port in "-D ${spec}".`);
      return null;
    }
    if (!LOOPBACK.has(bind.toLowerCase())) {
      diagnostics.push(`Refused "-D ${spec}": it binds ${bind}, which is not loopback.`);
      return null;
    }
    return { kind: "dynamic", localPort: port };
  }

  if (parts.length < 3) {
    diagnostics.push(`Could not read "-L ${spec}" — expected [bind:]port:host:hostport.`);
    return null;
  }

  const bind = parts.length >= 4 ? parts[0]! : "";
  const rest = parts.length >= 4 ? parts.slice(1) : parts;
  if (!LOOPBACK.has(bind.toLowerCase())) {
    diagnostics.push(`Refused "-L ${spec}": it binds ${bind}, which is not loopback.`);
    return null;
  }

  const localPort = Number(rest[0]);
  const remoteHost = rest[1]!;
  const remotePort = Number(rest[2]);
  if (!Number.isInteger(localPort) || !Number.isInteger(remotePort)) {
    diagnostics.push(`Could not read the ports in "-L ${spec}".`);
    return null;
  }

  return { kind: "local", localPort, remoteHost, remotePort };
}

export function parseSshCommand(command: string): SshSpec {
  const diagnostics: string[] = [];
  const forwards: Forward[] = [];
  const options: Record<string, string> = {};
  const passthrough: string[] = [];
  const tokens = tokenize(command);

  const parsed: SshSpec = { destination: "", forwards, options, passthrough, diagnostics };

  let i = 0;
  // The leading `ssh`, or a path to one. Anything else is a destination-first
  // command, which is not a form ssh accepts, so treat it as the binary.
  if (tokens.length && !tokens[0]!.startsWith("-")) {
    const first = tokens[0]!;
    if (/(^|[\\/])ssh(\.exe)?$/i.test(first)) {
      if (first.toLowerCase() !== "ssh") parsed.binary = first;
      i = 1;
    }
  }

  for (; i < tokens.length; i++) {
    const token = tokens[i]!;

    if (!token.startsWith("-")) {
      if (!parsed.destination) parsed.destination = token;
      // Anything after the destination is a remote command, which `-N` forbids.
      else diagnostics.push(`Ignored "${token}": the connection runs with -N, so it carries no remote command.`);
      continue;
    }

    if (OURS.has(token)) continue;

    // `-g` is GatewayPorts by another name, and would undo the bind check.
    if (token === "-g") {
      diagnostics.push(`Refused "-g": it would publish the forwards beyond this machine.`);
      continue;
    }

    // Attached forms — `-L8787:localhost:8787`, `-p2222` — are legal ssh.
    const attached = token.length > 2 && VALUED.has(token.slice(0, 2));
    const flag = attached ? token.slice(0, 2) : token;
    // Only a flag that takes a value consumes the next token. Advancing for an
    // unrecognised boolean swallows whatever followed it — `-4 -L …` loses the
    // forward, silently, which is the worst way to lose one.
    const value = attached ? token.slice(2) : VALUED.has(flag) ? tokens[++i] : undefined;

    if (VALUED.has(flag) && value === undefined) {
      diagnostics.push(`"${flag}" is missing its value.`);
      break;
    }

    switch (flag) {
      case "-L":
      case "-D": {
        const forward = parseForward(flag === "-L" ? "local" : "dynamic", value!, diagnostics);
        if (forward) forwards.push(forward);
        break;
      }
      case "-R":
        diagnostics.push(`Refused "-R ${value}": a reverse forward exposes this machine to the remote host.`);
        break;
      case "-p":
        parsed.port = Number(value);
        break;
      case "-i":
        parsed.identity = value;
        break;
      case "-J":
        parsed.jump = value;
        break;
      case "-o": {
        const [key = "", ...restOfValue] = value!.split("=");
        const name = key.trim();
        if (/^gatewayports$/i.test(name) && !/^no$/i.test(restOfValue.join("=").trim())) {
          diagnostics.push(`Refused "-o ${value}": it would publish the forwards beyond this machine.`);
          break;
        }
        options[name] = restOfValue.join("=");
        break;
      }
      default:
        passthrough.push(flag);
        if (VALUED.has(flag)) passthrough.push(value!);
    }
  }

  if (!parsed.destination) diagnostics.push("No host in the command.");
  return parsed;
}

/**
 * The argv we actually run.
 *
 * The user's own `-o`s go on last so they win: this list is a set of defaults
 * that make a supervised tunnel behave, not a policy about somebody's cluster.
 * `ExitOnForwardFailure` is the one that is not negotiable in spirit — without
 * it a forward that cannot bind is a warning and ssh connects anyway, which
 * looks exactly like success.
 */
export function buildSshArgs(spec: SshSpec, options: { batch: boolean }): string[] {
  const args = [
    "-N",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "ConnectTimeout=15",
  ];

  // An unattended retry must not hang on a prompt nobody can see: a credential
  // dialog nobody asked for is a credential dialog nobody can trust.
  if (options.batch) args.push("-o", "BatchMode=yes");

  if (spec.port) args.push("-p", String(spec.port));
  if (spec.identity) args.push("-i", spec.identity);
  if (spec.jump) args.push("-J", spec.jump);

  for (const forward of spec.forwards) {
    if (forward.kind === "dynamic") args.push("-D", String(forward.localPort));
    else args.push("-L", `${forward.localPort}:${forward.remoteHost}:${forward.remotePort}`);
  }

  args.push(...spec.passthrough);
  for (const [key, value] of Object.entries(spec.options)) args.push("-o", `${key}=${value}`);

  args.push(spec.destination);
  return args;
}

/** The command as it would be typed, for the copy button in the config dialog. */
export function formatSshCommand(spec: SshSpec, options: { batch: boolean }): string {
  const quote = (arg: string) => (/\s/.test(arg) ? `"${arg}"` : arg);
  return [spec.binary ?? "ssh", ...buildSshArgs(spec, options).map(quote)].join(" ");
}
