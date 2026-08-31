import type { ClientMessage, ServerMessage } from "../../protocol.ts";
import { readToken } from "./token.ts";

/** Reconnect backoff, capped: the server is usually a restart away. */
const BACKOFF_MS = [500, 1_000, 2_000, 4_000, 8_000];

export interface Socket {
  send: (message: ClientMessage) => void;
  close: () => void;
}

/**
 * One socket, reconnected for as long as the page is open.
 *
 * The server pushes state, job lists and events down this; commands go over
 * REST. The split is not arbitrary — `fetch` can set an `Authorization` header
 * and a browser `WebSocket` cannot, so the socket authenticates with the token
 * in its query string and everything that carries a *body* stays on HTTP.
 *
 * Anything the page was watching has to be re-declared after a reconnect: the
 * server released those follows when the old socket closed. `onOpen` is where
 * the caller does that, which is why it is a callback rather than a promise.
 */
export function connect(handlers: {
  onMessage: (message: ServerMessage) => void;
  onOpen: () => void;
  onClose: () => void;
}): Socket {
  let socket: WebSocket | null = null;
  let attempts = 0;
  let timer: number | undefined;
  let closed = false;
  /** Sent as soon as the socket opens, so a command issued while it was down
      is not silently dropped. */
  let queue: ClientMessage[] = [];

  const open = (): void => {
    if (closed) return;
    const token = readToken() ?? "";
    const url = new URL("/ws", window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("token", token);
    socket = new WebSocket(url);

    socket.addEventListener("open", () => {
      attempts = 0;
      handlers.onOpen();
      const pending = queue;
      queue = [];
      for (const message of pending) socket?.send(JSON.stringify(message));
    });

    socket.addEventListener("message", (event: MessageEvent<string>) => {
      try {
        handlers.onMessage(JSON.parse(event.data) as ServerMessage);
      } catch {
        /* a frame we cannot read is not a reason to drop the socket */
      }
    });

    socket.addEventListener("close", () => {
      socket = null;
      handlers.onClose();
      if (closed) return;
      const delay = BACKOFF_MS[Math.min(attempts, BACKOFF_MS.length - 1)]!;
      attempts++;
      timer = window.setTimeout(open, delay);
    });

    socket.addEventListener("error", () => socket?.close());
  };

  open();

  return {
    send: (message) => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
      else queue.push(message);
    },
    close: () => {
      closed = true;
      window.clearTimeout(timer);
      socket?.close();
    },
  };
}
