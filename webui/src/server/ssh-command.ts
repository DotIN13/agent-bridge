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
  /**
   * A command to run on the far side, when the line names one.
   *
   * `ssh -L … host 'ab-serve'` is the shape that makes a gateway start itself:
   * the command holds the connection open for as long as it is serving, so the
   * tunnel and the service go up and down together. Its presence is what drops
   * `-N` from the argv — the two are exclusive, since `-N` is *no command*.
   */
  remoteCommand?: string;
  /** Refusals and problems, in the words the user should read. */
  diagnostics: string[];
}

/** Binds that keep a forward on this machine. Anything else is refused. */
const LOOPBACK = new Set(["", "localhost", "127.0.0.1", "::1", "[::1]"]);

/**
 * Flags that take a value in the next argument.
 *
 * Arity is the one thing this parser cannot infer: an unknown flag is assumed
 * to take none, because assuming otherwise made `-4 -L …` swallow the forward.
 * The cost is that a *wrapper's* valued flag reads as a boolean and its value
 * becomes the destination — `autossh -M 0 -L … midway5` came out with a host of
 * `0`. So `-M`, autossh's monitoring port, is named here alongside ssh's own.
 * Anything else with an argument needs the attached form (`-Mn0`) or a
 * diagnostic will not save it.
 */
const VALUED = new Set([
  "-L", "-R", "-D", "-p", "-i", "-J", "-o", "-l", "-F", "-b", "-c", "-m", "-w", "-e", "-B", "-I",
  "-S", "-Q", "-W",
  "-M",
]);

/**
 * Flags we set ourselves, dropped silently rather than fought with. `-f` and
 * `-n` in particular would detach the process we are trying to supervise.
 */
const OURS = new Set(["-N", "-f", "-n", "-T", "-t", "-v", "-vv", "-vvv", "-q"]);

/** One token, and where it sat in the line it came from. */
interface Token {
  value: string;
  /** Index of the first character, opening quote included. */
  start: number;
  /** Index just past the last character consumed. */
  end: number;
}

/**
 * Split a command line, respecting quotes. No shell is involved anywhere.
 *
 * The spans are what let a remote command be lifted out *as written* rather
 * than rebuilt from tokens: `'systemctl --user start x && ab-serve'` has to
 * reach the far side with its own quoting intact, and reassembling it from
 * unquoted pieces is how a `&&` ends up as an argument to `systemctl`.
 */
function scan(command: string): Token[] {
  const out: Token[] = [];
  let current = "";
  let quote: '"' | "'" | null = null;
  let started = false;
  let start = 0;

  const push = (end: number) => {
    out.push({ value: current, start, end });
    current = "";
    started = false;
  };

  for (let i = 0; i < command.length; i++) {
    const ch = command[i]!;
    if (quote) {
      if (ch === quote) quote = null;
      else current += ch;
      continue;
    }
    if (ch === '"' || ch === "'") {
      if (!started && !current) start = i;
      quote = ch;
      started = true;
      continue;
    }
    if (/\s/.test(ch)) {
      if (started || current) push(i);
      continue;
    }
    if (!started && !current) start = i;
    current += ch;
  }
  if (started || current) push(command.length);
  return out;
}

export function tokenize(command: string): string[] {
  return scan(command).map((token) => token.value);
}

/** One enclosing layer of quotes, removed. What ssh would have been handed. */
function unwrap(text: string): string {
  const trimmed = text.trim();
  const first = trimmed[0];
  if ((first === '"' || first === "'") && trimmed.endsWith(first) && trimmed.length > 1) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
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
  const spans = scan(command);
  const tokens = spans.map((span) => span.value);

  const parsed: SshSpec = { destination: "", forwards, options, passthrough, diagnostics };

  let i = 0;
  /*
   * The first word is the program, whatever it is called.
   *
   * Matching only `/ssh$/` here looked safer and was not: a line starting
   * `autossh -M 0 -L …` had `autossh` taken as the *destination*, so the real
   * host became a remote command and the connect button reported "no ssh on
   * PATH" — three symptoms, none of them naming the cause. Found by a live run
   * against a fake called `ssh-exec`. A bare `ssh` stays `undefined` so the
   * argv keeps using whatever is on PATH.
   */
  if (tokens.length && !tokens[0]!.startsWith("-")) {
    const first = tokens[0]!;
    if (first.toLowerCase() !== "ssh") parsed.binary = first;
    i = 1;
  }

  for (; i < tokens.length; i++) {
    const token = tokens[i]!;

    if (!token.startsWith("-")) {
      if (!parsed.destination) {
        parsed.destination = token;
        continue;
      }
      /*
       * The rest of the line is a command for the far side, taken raw.
       *
       * ssh itself stops reading options at the destination, so being faithful
       * would mean treating *everything* after it as the command — except that
       * `ssh midway5 -L 8787:localhost:8787` used to parse here as a forward,
       * and turning somebody's working config into a remote `-L` silently is a
       * worse failure than not being faithful. So a token starting with `-` is
       * still read as a flag, and anything else opens the command.
       */
      parsed.remoteCommand = unwrap(command.slice(spans[i]!.start));
      break;
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
    // `-N` is "no command", so it goes exactly when there is no command. With
    // one, the connection lives as long as that command runs — which is the
    // whole mechanism behind `ab-serve`: it holds the ssh open while the
    // gateway serves, and exits when it cannot.
    ...(spec.remoteCommand ? [] : ["-N"]),
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
  // One argument, not several: ssh joins its remaining arguments with spaces
  // before handing them to the remote shell, so passing the command whole is
  // the same thing said once — and it keeps the user's own quoting inside it.
  if (spec.remoteCommand) args.push(spec.remoteCommand);
  return args;
}

/** The command as it would be typed, for the copy button in the config dialog. */
export function formatSshCommand(spec: SshSpec, options: { batch: boolean }): string {
  const quote = (arg: string) => (/\s/.test(arg) ? `"${arg}"` : arg);
  return [spec.binary ?? "ssh", ...buildSshArgs(spec, options).map(quote)].join(" ");
}
