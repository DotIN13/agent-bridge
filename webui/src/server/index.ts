import { randomBytes } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHttp } from "./http.ts";
import { Manager } from "./manager.ts";

const PORT = Number(process.env.AB_WEBUI_PORT ?? 8765);
/**
 * Loopback, always, and not configurable.
 *
 * This process can start ssh processes and it holds every gateway token on the
 * machine. There is no version of "bind it to the network" that is a good idea;
 * reach it from elsewhere with `ssh -L`, which is the thing it is for.
 */
const HOST = "127.0.0.1";

/**
 * The token, in the URL **fragment**.
 *
 * A fragment is the one part of a URL a browser never sends to a server, so the
 * token stays out of access logs and out of the `Referer` on anything the page
 * loads. `AB_WEBUI_TOKEN` pins it, which is what makes a dev server reload
 * without a new link every time.
 */
const TOKEN = process.env.AB_WEBUI_TOKEN ?? randomBytes(24).toString("hex");

const here = path.dirname(fileURLToPath(import.meta.url));
/** `dist/server/index.js` and `src/server/index.ts` are both two deep. */
const webRoot = path.resolve(here, "../web");

async function main(): Promise<void> {
  const manager = new Manager();
  const http = createHttp({ manager, token: TOKEN, webRoot });
  manager.publishTo(http.broadcast);

  await manager.start();
  await new Promise<void>((resolve, reject) => {
    http.server.once("error", reject);
    http.server.listen(PORT, HOST, () => resolve());
  });

  say(`agent-bridge webui on http://${HOST}:${PORT}/#t=${TOKEN}`);
  if (process.env.AB_WEBUI_DEV) say(`vite dev server (npm run dev:web): http://${HOST}:8766/#t=${TOKEN}`);

  const shutdown = (signal: NodeJS.Signals): void => {
    say(`${signal} - closing tunnels`);
    void manager
      .stop()
      .then(() => http.close())
      .then(() => process.exit(0));
  };
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

function say(line: string): void {
  process.stdout.write(`${line}\n`);
}

main().catch((err: Error) => {
  process.stderr.write(`${err.message}\n`);
  process.exit(1);
});
