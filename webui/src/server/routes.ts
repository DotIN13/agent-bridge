import express, { type Request, type Response, type Router } from "express";
import type { Manager } from "./manager.ts";

/**
 * Everything the page can ask for.
 *
 * The browser never talks to a gateway directly: it asks here, and the server
 * adds the bearer token on the way out. That is the whole reason this process
 * exists rather than the page fetching `localhost:8787` itself — a token in a
 * page is a token in the devtools network tab, in the history, and in whatever
 * extension is reading the DOM.
 */
export function createRouter(manager: Manager): Router {
  const router = express.Router();

  const route =
    (handler: (req: Request, res: Response) => Promise<unknown> | unknown) => (req: Request, res: Response) => {
      void (async () => {
        try {
          await handler(req, res);
        } catch (err) {
          res.status(400).json({ error: (err as Error).message });
        }
      })();
    };

  router.get("/state", route((_req, res) => {
    res.json({ state: manager.state(), readOnly: manager.configIsReadOnly });
  }));

  router.post("/refresh", route((_req, res) => {
    manager.refresh();
    res.json({ ok: true });
  }));

  // ── Tunnels ──────────────────────────────────────────────────────────────

  router.post("/gateways/:name/up", route((req, res) => {
    manager.up(String(req.params.name), req.body?.interactive !== false);
    res.json({ state: manager.state() });
  }));

  router.post("/gateways/:name/down", route((req, res) => {
    manager.down(String(req.params.name));
    res.json({ state: manager.state() });
  }));

  /** Read an ssh command without saving it, so the dialog can show the parse. */
  router.post("/ssh/parse", route((req, res) => {
    const command = String(req.body?.command ?? "");
    if (!command.trim()) throw new Error("command is required");
    res.json(manager.parseSsh(command));
  }));

  /** An answer to a question ssh asked. `null` dismisses it. */
  router.post("/prompts/:id", route((req, res) => {
    const answer = typeof req.body?.answer === "string" ? req.body.answer : null;
    const delivered = manager.answerPrompt(String(req.params.id), answer);
    if (!delivered) throw new Error("that prompt is no longer waiting");
    res.json({ ok: true });
  }));

  // ── Config ───────────────────────────────────────────────────────────────

  router.put("/gateways/:name", route((req, res) => {
    const body = (req.body ?? {}) as Record<string, unknown>;
    manager.saveGateway(String(req.params.name), {
      baseUrl: str(body.baseUrl),
      ssh: nullable(body.ssh),
      tokenEnv: nullable(body.tokenEnv),
      tokenFile: nullable(body.tokenFile),
      enabled: bool(body.enabled),
      autoStart: bool(body.autoStart),
      makeDefault: bool(body.makeDefault),
      rename: str(body.rename),
    });
    res.json({ state: manager.state() });
  }));

  router.delete("/gateways/:name", route((req, res) => {
    manager.removeGateway(String(req.params.name));
    res.json({ state: manager.state() });
  }));

  // ── Jobs ─────────────────────────────────────────────────────────────────

  router.get("/gateways/:name/jobs", route(async (req, res) => {
    const gateway = String(req.params.name);
    const cursor = req.query.cursor ? String(req.query.cursor) : undefined;
    // No cursor means the live list the tracker already holds; a cursor is a
    // deliberate walk further back than the tracker keeps.
    if (!cursor) {
      res.json({ jobs: manager.jobs(gateway), error: manager.tracker(gateway).error() });
      return;
    }
    res.json(await manager.client(gateway).jobs(50, cursor));
  }));

  router.get("/gateways/:name/jobs/:id", route(async (req, res) => {
    res.json({ job: await manager.client(String(req.params.name)).job(String(req.params.id)) });
  }));

  /**
   * The ring the server is already holding, and nothing more.
   *
   * Deliberately free of side effects: following is asked for over the socket,
   * because the socket is the thing whose closing releases it. A GET that
   * started a follow leaked one per page load — the count went up and nothing
   * ever brought it down, so the server streamed a job nobody was reading.
   */
  router.get("/gateways/:name/jobs/:id/events", route((req, res) => {
    res.json({ events: manager.events(String(req.params.name), String(req.params.id)) });
  }));

  router.post("/gateways/:name/jobs/:id/cancel", route(async (req, res) => {
    res.json(await manager.client(String(req.params.name)).cancel(String(req.params.id)));
  }));

  router.post("/gateways/:name/jobs/:id/steer", route(async (req, res) => {
    const prompt = String(req.body?.prompt ?? "").trim();
    if (!prompt) throw new Error("prompt is required");
    res.json(await manager.client(String(req.params.name)).steer(String(req.params.id), prompt));
  }));

  return router;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

/** `""` means "clear this key", which is not the same as leaving it alone. */
function nullable(value: unknown): string | null | undefined {
  if (value === null) return null;
  return typeof value === "string" ? value : undefined;
}

function bool(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}
