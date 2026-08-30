import { For, Show, createSignal } from "solid-js";
import type { ForwardState, GatewayState, TunnelState } from "../../protocol.ts";
import {
  connectGateway,
  disconnectGateway,
  goHome,
  openGateway,
  openGatewayDialog,
  refresh,
  state,
  tunnelFor,
} from "../store.ts";
import { IconButton } from "./ui/button.tsx";
import { Icon } from "./ui/icon.tsx";
import { Spinner, Tooltip } from "./ui/primitives.tsx";

/**
 * One row per gateway in `gateways.json`, and its ports under it.
 *
 * The model keeps a tunnel and a gateway apart, and has to: a tunnel is an ssh
 * process that may carry several ports, a gateway is an HTTP endpoint that may
 * be reachable without this app having started anything. But that is a
 * distinction the *server* needs, not one worth making a reader hold — drawn as
 * two lists it puts one machine on screen twice.
 *
 * So a row is what the config calls an entry: its name, how far it is actually
 * working, what is running on it, and the one button that changes any of that.
 */
export function Sidebar() {
  const [busy, setBusy] = createSignal<string | null>(null);

  const act = async (name: string, run: () => Promise<unknown>) => {
    setBusy(name);
    try {
      await run();
    } finally {
      setBusy(null);
    }
  };

  return (
    <aside data-slot="sidebar">
      <header data-slot="sidebar-title">
        <button type="button" data-slot="brand" onClick={goHome}>
          agent-bridge
        </button>
        <Tooltip label={state.online ? "connected to the local server" : "the local server is not answering"} placement="bottom">
          <span data-slot="online-dot" data-online={state.online ? "" : undefined} />
        </Tooltip>
      </header>

      <div data-slot="sidebar-header">
        <span data-slot="section-title">Gateways</span>
        <IconButton icon="refresh" label="Re-check everything" size="small" variant="ghost-muted" onClick={() => void refresh()} />
        <Show when={!state.readOnly}>
          <IconButton
            icon="plus"
            label="Add a gateway"
            size="small"
            variant="ghost-muted"
            onClick={() => openGatewayDialog(null)}
          />
        </Show>
      </div>

      <div data-slot="sidebar-scroll">
        <Show when={state.app} fallback={<p data-slot="sidebar-empty">Loading…</p>}>
          <Show
            when={state.app!.gateways.length > 0}
            fallback={
              <p data-slot="sidebar-empty">
                No gateways yet. Add one, or point <code>ab</code> at a cluster and it will show up here.
              </p>
            }
          >
            <ul data-slot="gateway-list">
              <For each={state.app!.gateways}>
                {(entry) => {
                  const tunnel = () => tunnelFor(entry.name);
                  const selected = () => state.route.view !== "home" && state.route.gateway === entry.name;
                  return (
                    <li>
                      <div data-slot="gateway-row" data-selected={selected() ? "" : undefined}>
                        <button type="button" data-slot="gateway-open" onClick={() => openGateway(entry.name)}>
                          <span data-slot="status-dot" data-status={dot(entry, tunnel())} />
                          <span class="truncate">{entry.name}</span>
                          <Show when={entry.isDefault}>
                            <span data-slot="gateway-default" title="ab's default gateway">
                              default
                            </span>
                          </Show>
                          <Show when={entry.jobs.running > 0}>
                            <Tooltip label="running, queued or canceling" placement="right">
                              <span data-slot="gateway-count" data-tone="running">
                                {entry.jobs.running}
                              </span>
                            </Tooltip>
                          </Show>
                          <Show when={entry.jobs.waiting > 0}>
                            {/* Parked, not finished: the agent turn is over and
                                the batch work is not. */}
                            <Tooltip label="waiting for a report" placement="right">
                              <span data-slot="gateway-count" data-tone="waiting">
                                {entry.jobs.waiting}
                              </span>
                            </Tooltip>
                          </Show>
                        </button>

                        <div data-slot="gateway-actions">
                          <Show when={busy() === entry.name}>
                            <Spinner size={10} />
                          </Show>
                          <Show when={entry.ssh} fallback={<NoSsh />}>
                            <Show
                              when={running(tunnel())}
                              fallback={
                                <Tooltip label={connectLabel(entry, tunnel())} placement="bottom">
                                  <IconButton
                                    icon="plug"
                                    label={`Connect ${entry.name}`}
                                    size="small"
                                    variant="ghost-muted"
                                    disabled={busy() !== null || tunnel()?.runnable === false}
                                    onClick={() => void act(entry.name, () => connectGateway(entry.name))}
                                  />
                                </Tooltip>
                              }
                            >
                              <Tooltip label="Disconnect · ssh gets a SIGTERM" placement="bottom">
                                <IconButton
                                  icon="stop"
                                  label={`Disconnect ${entry.name}`}
                                  size="small"
                                  variant="ghost-muted"
                                  disabled={busy() !== null}
                                  onClick={() => void act(entry.name, () => disconnectGateway(entry.name))}
                                />
                              </Tooltip>
                            </Show>
                          </Show>
                          <IconButton
                            icon="settings"
                            label={`Configure ${entry.name}`}
                            size="small"
                            variant="ghost-muted"
                            onClick={() => openGatewayDialog(entry.name)}
                          />
                        </div>
                      </div>

                      {/* Only when there are ports: an empty list still carries
                          its margin, which puts 3px between every two rows. */}
                      <Show when={tunnel()?.forwards.length}>
                        <ul data-slot="forward-list">
                          <For each={tunnel()!.forwards}>{(forward) => <ForwardRow forward={forward} />}</For>
                        </ul>
                      </Show>
                    </li>
                  );
                }}
              </For>
            </ul>
          </Show>
        </Show>
      </div>

      <footer data-slot="sidebar-footer">
        <Show when={!state.app?.sshAvailable}>
          <p data-slot="sidebar-warning">No ssh on PATH — nothing here can connect.</p>
        </Show>
        <Show when={state.app?.configPath}>
          <Tooltip label={state.app!.configPath} placement="top">
            <p data-slot="config-path">{shorten(state.app!.configPath)}</p>
          </Tooltip>
        </Show>
      </footer>
    </aside>
  );
}

/** An entry with no `ssh` line is reachable or not on its own account. */
function NoSsh() {
  return (
    <Tooltip label="No ssh command — add one to supervise the forward from here" placement="bottom">
      <span data-slot="gateway-hint">
        <Icon name="link" size={11} />
      </span>
    </Tooltip>
  );
}

/**
 * One port, at whichever rung of the ladder it reached.
 *
 * `listening` is styled as a warning rather than a success on purpose: a forward
 * whose far end has died keeps accepting connections and then resets them, and
 * that is exactly the state this row exists to make visible.
 */
function ForwardRow(props: { forward: ForwardState }) {
  const label = () =>
    props.forward.kind === "dynamic"
      ? `SOCKS on ${props.forward.localPort}`
      : `${props.forward.localPort} → ${props.forward.remoteHost}:${props.forward.remotePort}`;

  return (
    <li data-slot="forward-row" data-health={props.forward.health}>
      <Tooltip label={props.forward.error ?? props.forward.serves ?? props.forward.health} placement="right">
        <span data-slot="forward-health" />
      </Tooltip>
      <span class="min-w-0 flex-1 truncate">{label()}</span>
    </li>
  );
}

function running(tunnel: TunnelState | undefined): boolean {
  const status = tunnel?.status;
  return status === "up" || status === "starting" || status === "retrying" || status === "authenticating";
}

function connectLabel(entry: GatewayState, tunnel: TunnelState | undefined): string {
  if (tunnel?.runnable === false) return "That ssh cannot be run on this machine";
  if (tunnel?.blocked === "auth") return "Last attempt was refused — try again";
  return `Connect · ${entry.ssh ?? ""}`;
}

/**
 * One dot for the whole row.
 *
 * The endpoint's own answer when there is one, because reachability is what the
 * row is about: a tunnel that is up in front of a gateway that refuses the token
 * is not working. `authenticating` gets its own colour — a question is waiting,
 * which is neither broken nor working.
 */
function dot(entry: GatewayState, tunnel: TunnelState | undefined): string {
  if (!entry.enabled) return "disabled";
  if (tunnel?.status === "authenticating") return "asking";
  if (entry.status === "connected") return "connected";
  if (entry.status === "unauthorized") return "unauthorized";
  if (entry.status === "unknown") return "unknown";
  return "error";
}

/** The tail of a path: the head is `/home/somebody/.config` every time. */
function shorten(value: string): string {
  const parts = value.split(/[\\/]/).filter(Boolean);
  return parts.length <= 3 ? value : `…/${parts.slice(-3).join("/")}`;
}
