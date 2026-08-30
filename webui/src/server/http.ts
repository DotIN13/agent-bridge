import { timingSafeEqual } from "node:crypto";
import { createServer, type Server } from "node:http";
import path from "node:path";
import express from "express";
import { WebSocketServer, type WebSocket } from "ws";
import type { ClientMessage, ServerMessage } from "../protocol.ts";
import type { Manager } from "./manager.ts";
import { createRouter } from "./routes.ts";

export interface HttpOptions {
  manager: Manager;
  /**
   * The shared secret the page proves it has.
   *
   * Loopback is not on its own an authorization boundary: every process on this
   * machine can reach 127.0.0.1, including whatever a browser tab is running.
   */
  token: string;
  /** Where the built page lives. Absent in a test, which never asks for it. */
  webRoot?: string;
}

export interface Http {
  server: Server;
  /** Push to every open page. The manager's only way out to the browser. */
  broadcast: (message: ServerMessage) => void;
  close: () => Promise<void>;
}

export function createHttp(options: HttpOptions): Http {
  const { manager, token, webRoot } = options;
  const sockets = new Set<WebSocket>();

  const matches = (candidate: string | undefined): boolean => {
    if (!candidate) return false;
    const a = Buffer.from(candidate);
    const b = Buffer.from(token);
    return a.length === b.length && timingSafeEqual(a, b);
  };

  const app = express();
  app.use(express.json({ limit: "256kb" }));

  app.use("/api", (req, res, next) => {
    const header = req.header("authorization") ?? "";
    const bearer = header.startsWith("Bearer ") ? header.slice(7) : undefined;
    if (!matches(bearer)) {
      res.status(401).json({ error: "bad or missing token" });
      return;
    }
    next();
  });
  app.use("/api", createRouter(manager));

  /*
   * A page that may hold a credential prompt should not be able to reach
   * anything but this server. `connect-src 'self'` is the load-bearing one:
   * whatever an ssh password is typed into cannot ship it anywhere. Inline
   * styles are allowed because Kobalte positions its overlays with them.
   */
  app.use((_req, res, next) => {
    res.setHeader(
      "content-security-policy",
      "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'",
    );
    res.setHeader("referrer-policy", "no-referrer");
    res.setHeader("x-content-type-options", "nosniff");
    next();
  });

  if (webRoot) {
    app.use(express.static(webRoot, { index: "index.html" }));
    // One page, so anything that is not a file is still the page. `/api` is
    // handled above, and a 404 there must stay a 404 rather than becoming an
    // HTML document with a 200 on it.
    app.get(/^\/(?!api\/).*/, (_req, res) => {
      res.sendFile(path.join(webRoot, "index.html"), (err) => {
        if (err) res.status(404).type("text/plain").send("the web app has not been built - run `npm run build`");
      });
    });
  }

  const server = createServer(app);
  const wss = new WebSocketServer({ noServer: true });

  server.on("upgrade", (request, socket, head) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (url.pathname !== "/ws" || !matches(url.searchParams.get("token") ?? undefined)) {
      // A websocket handshake is HTTP until it is not, so a refusal is an HTTP
      // response - closing the socket bare shows up in the page as a mystery.
      socket.write("HTTP/1.1 401 Unauthorized\r\n\r\n");
      socket.destroy();
      return;
    }
    wss.handleUpgrade(request, socket, head, (ws) => wss.emit("connection", ws, request));
  });

  wss.on("connection", (socket: WebSocket) => {
    sockets.add(socket);
    /** What this socket is looking at, so letting go can be exact. */
    const following = new Set<string>();
    socket.send(JSON.stringify({ type: "state", state: manager.state() } satisfies ServerMessage));

    socket.on("message", (raw) => {
      let message: ClientMessage;
      try {
        message = JSON.parse(String(raw)) as ClientMessage;
      } catch {
        return;
      }

      if (message.type === "watch") {
        manager.setWatched(message.gateway);
        // What the tracker already holds, straight to the page that just asked:
        // the poll that would broadcast it is up to thirty seconds away, and an
        // empty table is indistinguishable from a gateway with no jobs.
        if (message.gateway) {
          const gateway = message.gateway;
          socket.send(JSON.stringify({ type: "jobs", gateway, jobs: manager.jobs(gateway) } satisfies ServerMessage));
        }
        return;
      }

      if (message.type === "follow") {
        const { gateway, jobId } = message;
        following.add(`${gateway} ${jobId}`);
        void manager
          .follow(gateway, jobId)
          // The ring, once the history behind it has landed. The tracker files a
          // fetched page without broadcasting it — the ring outlives a follow,
          // so a job opened twice has nothing new to announce — which means the
          // page that asked has to be handed it here.
          .then(() => {
            const events = manager.events(gateway, jobId);
            if (events.length) {
              socket.send(JSON.stringify({ type: "events", gateway, jobId, events } satisfies ServerMessage));
            }
          })
          .catch(() => {});
        return;
      }

      if (message.type === "unfollow") {
        following.delete(`${message.gateway} ${message.jobId}`);
        manager.unfollow(message.gateway, message.jobId);
      }
    });

    socket.on("close", () => {
      sockets.delete(socket);
      // Every follow this socket held, released. Without this a closed tab
      // leaves the server streaming a job nobody is reading - and the count
      // never reaches zero, so it streams it forever.
      for (const key of following) {
        const [gateway = "", jobId = ""] = key.split(" ");
        manager.unfollow(gateway, jobId);
      }
      if (sockets.size === 0) manager.setWatched(null);
    });
  });

  return {
    server,
    broadcast: (message) => {
      const payload = JSON.stringify(message);
      for (const socket of sockets) if (socket.readyState === socket.OPEN) socket.send(payload);
    },
    close: () =>
      new Promise<void>((resolve) => {
        for (const socket of sockets) socket.terminate();
        wss.close();
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}
