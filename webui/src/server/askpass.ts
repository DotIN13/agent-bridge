import { chmodSync, mkdirSync, writeFileSync } from "node:fs";
import { randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { createServer, type Server, type Socket } from "node:net";
import path from "node:path";
import type { AuthPrompt } from "../protocol.ts";
import { stateDir } from "./paths.ts";

/** How long a human has to answer before ssh is told nobody is there. */
const PROMPT_TIMEOUT_MS = 120_000;

/**
 * The door ssh knocks on when it needs a human.
 *
 * OpenSSH will not read a password from a process with no tty, but it will run
 * `SSH_ASKPASS` and read the answer from that program's stdout — with
 * `SSH_ASKPASS_REQUIRE=force`, even with no `DISPLAY`. That is the whole
 * mechanism, and it is the reason this rebuild has no pty in it: a pty needs a
 * *controlling terminal* to carry a prompt (`setsid()` alone leaves the child
 * without one, so `/dev/tty` fails and the prompt never arrives), and its line
 * discipline echoes the answer back into the log the page is rendering. Askpass
 * has neither problem, and it carries a passphrase, a password and a
 * two-factor menu through one channel.
 *
 * The helper is spawned by ssh, not by us, so it authenticates itself with a
 * single-use token handed to it in its environment — not on its command line,
 * which every process on the machine can read.
 */
export class AskpassBridge {
  private server: Server | null = null;
  private port = 0;
  private readonly sessions = new Map<string, { gateway: string; context: () => string[] }>();
  private readonly pending = new Map<string, { prompt: AuthPrompt; resolve: (answer: string | null) => void; timer: NodeJS.Timeout }>();

  constructor(
    private readonly emit: (event: { kind: "prompt" | "closed"; prompt?: AuthPrompt; id?: string; gateway?: string }) => void,
  ) {}

  /** Loopback, an ephemeral port, and nothing listening until it is needed. */
  async start(): Promise<void> {
    if (this.server) return;
    const server = createServer((socket) => this.serve(socket));
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => resolve());
    });
    server.unref();
    this.server = server;
    this.port = (server.address() as { port: number }).port;
  }

  async stop(): Promise<void> {
    for (const id of [...this.pending.keys()]) this.answer(id, null);
    this.sessions.clear();
    this.server?.close();
    this.server = null;
  }

  /**
   * Open the window during which this gateway may ask something, and return the
   * environment ssh needs to find its way back here.
   *
   * A connect the user clicked is the only thing that may prompt: a retry runs
   * with no session at all, so its helper is refused at the socket.
   */
  begin(gateway: string, context: () => string[]): { env: Record<string, string>; end: () => void } {
    const token = randomBytes(24).toString("hex");
    this.sessions.set(token, { gateway, context });
    return {
      env: {
        SSH_ASKPASS: helperPath(),
        SSH_ASKPASS_REQUIRE: "force",
        AB_ASKPASS_PORT: String(this.port),
        AB_ASKPASS_TOKEN: token,
        // Some builds still consult DISPLAY before askpass; a value that is
        // never a real display keeps them satisfied without pointing anywhere.
        DISPLAY: process.env.DISPLAY ?? "agent-bridge:0",
      },
      end: () => {
        this.sessions.delete(token);
        for (const [id, entry] of this.pending) {
          if (entry.prompt.gateway === gateway) this.answer(id, null);
        }
      },
    };
  }

  /** The human's answer, or `null` when the dialog was dismissed or timed out. */
  answer(id: string, value: string | null): boolean {
    const entry = this.pending.get(id);
    if (!entry) return false;
    clearTimeout(entry.timer);
    this.pending.delete(id);
    entry.resolve(value);
    this.emit({ kind: "closed", id, gateway: entry.prompt.gateway });
    return true;
  }

  /** Prompts still waiting, so a page that loads late can draw them. */
  open(): AuthPrompt[] {
    return [...this.pending.values()].map((entry) => entry.prompt);
  }

  private serve(socket: Socket): void {
    socket.setEncoding("utf8");
    let buffer = "";

    socket.on("data", (chunk: string) => {
      buffer += chunk;
      const line = buffer.indexOf("\n");
      if (line === -1) return;

      let request: { token?: string; prompt?: string };
      try {
        request = JSON.parse(buffer.slice(0, line)) as { token?: string; prompt?: string };
      } catch {
        socket.end();
        return;
      }

      const session = this.sessionFor(request.token);
      if (!session) {
        // Nobody asked for a connection, so nothing may ask for a credential.
        socket.end();
        return;
      }

      void this.ask(session.gateway, session.context(), String(request.prompt ?? "")).then((answer) => {
        // Ending with nothing is how ssh is told there is no answer: the helper
        // exits non-zero, which fails the authentication instead of hanging it.
        if (answer === null) socket.end();
        else socket.end(`${answer}\n`);
      });
    });

    socket.on("error", () => socket.destroy());
  }

  /** Constant-time compare, so a token cannot be guessed a byte at a time. */
  private sessionFor(token: string | undefined): { gateway: string; context: () => string[] } | undefined {
    if (!token) return undefined;
    const candidate = Buffer.from(token);
    for (const [known, session] of this.sessions) {
      const buf = Buffer.from(known);
      if (buf.length === candidate.length && timingSafeEqual(buf, candidate)) return session;
    }
    return undefined;
  }

  private ask(gateway: string, context: string[], text: string): Promise<string | null> {
    const prompt: AuthPrompt = {
      id: randomUUID(),
      gateway,
      text,
      context,
      createdAt: new Date().toISOString(),
      expiresAt: new Date(Date.now() + PROMPT_TIMEOUT_MS).toISOString(),
    };

    return new Promise<string | null>((resolve) => {
      const timer = setTimeout(() => this.answer(prompt.id, null), PROMPT_TIMEOUT_MS);
      timer.unref();
      this.pending.set(prompt.id, { prompt, resolve, timer });
      this.emit({ kind: "prompt", prompt });
    });
  }
}

/**
 * What `SSH_ASKPASS` points at.
 *
 * Deliberately the smallest program here: read the prompt from `argv[1]`,
 * connect to the bridge, hand over the prompt and the one-time token from the
 * environment, print the reply on stdout, exit. It holds no configuration and
 * can answer nothing on its own — everything that decides whether a question is
 * legitimate lives on the other end of the socket.
 *
 * Written to disk at runtime rather than shipped as a file, so it is found the
 * same way under `tsx` in development and under `dist` in production, and so
 * the `node` that runs it is provably the one running the server.
 */
const HELPER = `import net from "node:net";

// ssh passes the prompt as the first argument. Some builds pass nothing at all
// for a passphrase, which is still a question worth showing.
const prompt = process.argv[2] ?? "";
const port = Number(process.env.AB_ASKPASS_PORT);
const token = process.env.AB_ASKPASS_TOKEN;

if (!port || !token) process.exit(1);

const socket = net.connect(port, "127.0.0.1", () => {
  socket.write(JSON.stringify({ token, prompt }) + "\\n");
});

let answer = "";
socket.setEncoding("utf8");
socket.on("data", (chunk) => (answer += chunk));
socket.on("error", () => process.exit(1));
socket.on("end", () => {
  if (!answer) process.exit(1);
  process.stdout.write(answer.replace(/\\r?\\n$/, "") + "\\n");
  process.exit(0);
});
`;

let cached: string | null = null;

/**
 * Materialize the helper and its wrapper, and return the path ssh should run.
 *
 * `SSH_ASKPASS` takes a program and no arguments, so `node helper.mjs` needs a
 * wrapper either way: a shell script with a shebang on Unix, and a `.cmd` on
 * Windows.
 *
 * **The Windows path is unverified, and there is reason to doubt it.** ssh's own
 * askpass support there is not reliable — `SSH_ASKPASS` was honoured by
 * `OpenSSH_for_Windows_8.1p1` and is ignored by `8.6p1`
 * (PowerShell/Win32-OpenSSH#2115), `SSH_ASKPASS_REQUIRE` was ignored outright in
 * 8.1 (#1726), and there is no native `ssh-askpass` on the platform at all.
 * Beyond that, ssh launches the helper with `CreateProcess`, which cannot
 * execute a batch file directly. So on Windows expect the prompt not to arrive:
 * `tunnel.ts` says so in the log when an interactive attempt fails without ever
 * asking, and the working configurations there are an agent, a key, or naming
 * Git for Windows' `ssh.exe` in the command.
 */
export function helperPath(): string {
  if (cached) return cached;

  const dir = path.join(stateDir(), "askpass");
  mkdirSync(dir, { recursive: true, mode: 0o700 });

  const script = path.join(dir, "helper.mjs");
  writeFileSync(script, HELPER, { encoding: "utf8", mode: 0o600 });

  if (process.platform === "win32") {
    const wrapper = path.join(dir, "helper.cmd");
    writeFileSync(wrapper, `@echo off\r\n"${process.execPath}" "${script}" %*\r\n`, "utf8");
    cached = wrapper;
    return wrapper;
  }

  const wrapper = path.join(dir, "helper.sh");
  writeFileSync(wrapper, `#!/bin/sh\nexec "${process.execPath}" "${script}" "$@"\n`, "utf8");
  chmodSync(wrapper, 0o700);
  cached = wrapper;
  return wrapper;
}
