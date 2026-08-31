import { connect } from "node:net";
import type { ForwardHealth } from "../protocol.ts";

export interface ProbeResult {
  health: ForwardHealth;
  /** What answered, when something did — "agent-bridge 0.3.0". */
  serves?: string;
  error?: string;
}

const CONNECT_TIMEOUT = 1500;
const HTTP_TIMEOUT = 4000;

/** Does anything accept a connection on this local port? */
export function isListening(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = connect({ port, host: "127.0.0.1" });
    const done = (answer: boolean) => {
      socket.destroy();
      resolve(answer);
    };
    socket.setTimeout(CONNECT_TIMEOUT);
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false));
    socket.once("error", () => done(false));
  });
}

/**
 * How far a forward actually works.
 *
 * The rungs exist because "listening" lies. When the far end of an `ssh -L`
 * dies, the local socket keeps accepting connections and then resets them, so a
 * check that stops at `connect()` reports a healthy tunnel to a machine that is
 * no longer there. Only an answer from the far side is evidence, and only a
 * service that identifies itself as agent-bridge gets the top rung.
 */
export async function probeForward(
  forward: { kind: "local" | "dynamic"; localPort: number },
  expectGateway: boolean,
): Promise<ProbeResult> {
  if (!(await isListening(forward.localPort))) return { health: "closed" };

  // A SOCKS proxy has no address to ask and no health endpoint to read; that a
  // local port accepts is genuinely all that can be known from here.
  if (forward.kind === "dynamic" || !expectGateway) return { health: "listening" };

  try {
    const response = await fetch(`http://127.0.0.1:${forward.localPort}/health`, {
      signal: AbortSignal.timeout(HTTP_TIMEOUT),
      headers: { accept: "application/json" },
    });
    const body = (await response.json().catch(() => null)) as { ok?: boolean; version?: string } | null;
    if (body?.ok) return { health: "serving", serves: `agent-bridge ${body.version ?? "?"}` };
    // Something spoke HTTP through the tunnel, which is as much as this can
    // prove when the answer is not agent-bridge's.
    return { health: "reachable", serves: `HTTP ${response.status}` };
  } catch (err) {
    /*
     * This is the case the ladder exists for: the port accepted a connection
     * and then the request died. A tunnel whose far end has gone resets here,
     * which is a different fact from "nothing is listening" and reads very
     * differently to whoever is looking at the row.
     */
    return { health: "listening", error: describe(err) };
  }
}

function describe(err: unknown): string {
  const message = (err as Error)?.message ?? String(err);
  if (/aborted|timeout/i.test(message)) return "no answer through the tunnel";
  if (/ECONNRESET|socket hang up/i.test(message)) return "connection reset · far end gone";
  return message;
}
