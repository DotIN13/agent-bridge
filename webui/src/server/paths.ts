import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

/**
 * Where the dashboard keeps the little it has to put on disk: the askpass
 * helper it writes out at runtime, and nothing else.
 *
 * `0700`, because the helper is a program ssh will execute — a directory the
 * rest of the machine can write to is a directory the rest of the machine can
 * replace it in.
 */
export function stateDir(): string {
  const base =
    process.env.AB_WEBUI_STATE_DIR ??
    (process.platform === "win32"
      ? path.join(process.env.LOCALAPPDATA ?? path.join(homedir(), "AppData", "Local"), "agent-bridge")
      : path.join(process.env.XDG_STATE_HOME ?? path.join(homedir(), ".local", "state"), "agent-bridge"));
  const dir = path.join(base, "webui");
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  return dir;
}
