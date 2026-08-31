import { For, Show } from "solid-js";
import { dismissError, openGatewayDialog, state } from "./store.ts";
import { AuthPromptDialog } from "./components/AuthPrompt.tsx";
import { GatewayDialog } from "./components/GatewayDialog.tsx";
import { GatewayPage } from "./components/GatewayPage.tsx";
import { JobPage } from "./components/JobPage.tsx";
import { Sidebar } from "./components/Sidebar.tsx";
import { Button, IconButton } from "./components/ui/button.tsx";
import { Icon } from "./components/ui/icon.tsx";

/**
 * The shell: a list on the left, one thing at a time on the right.
 *
 * Three views and no router. The right pane is the gateway you clicked, or a job
 * on it, and the way back from a job is the button on its own header — this is a
 * dashboard for a handful of machines, not a site, and a URL for each pane would
 * be one more thing to keep in step with a socket that reconnects.
 */
export function App() {
  return (
    <Show when={!state.needsToken} fallback={<NoToken />}>
      <div data-slot="shell">
        <Sidebar />
        <main data-slot="main">
          <Show when={state.error}>
            {(message) => (
              <div data-slot="error-strip">
                <Icon name="alert" size={13} />
                <span class="min-w-0 flex-1">{message()}</span>
                <IconButton icon="close" label="Dismiss" size="small" variant="ghost-muted" onClick={dismissError} />
              </div>
            )}
          </Show>

          <Show when={state.route.view === "gateway"}>
            <GatewayPage gateway={(state.route as { gateway: string }).gateway} />
          </Show>
          <Show when={state.route.view === "job"}>
            <JobPage
              gateway={(state.route as { gateway: string }).gateway}
              jobId={(state.route as { jobId: string }).jobId}
            />
          </Show>
          <Show when={state.route.view === "home"}>
            <Home />
          </Show>
        </main>

        <AuthPromptDialog />
        <GatewayDialog />
      </div>
    </Show>
  );
}

/**
 * The landing pane.
 *
 * Config problems live here rather than on a gateway row, because a file that
 * will not parse has no rows: the sidebar would be empty with no explanation.
 */
function Home() {
  return (
    <div data-slot="home">
      <div data-slot="home-inner">
        <h1 data-slot="home-title">agent-bridge</h1>
        <p data-slot="home-lede">
          The ssh forwards on this machine, the gateways behind them, and what is running on each. Pick a gateway on the
          left, or add one.
        </p>

        <Show when={state.app?.errors.length}>
          <div data-slot="banner" data-tone="danger">
            <span data-slot="banner-mark">
              <Icon name="alert" size={12} />
            </span>
            <ul>
              <For each={state.app!.errors}>{(line) => <li>{line}</li>}</For>
            </ul>
          </div>
        </Show>

        <Show when={!state.readOnly}>
          <Button size="normal" variant="contrast" icon="plus" onClick={() => openGatewayDialog(null)}>
            Add a gateway
          </Button>
        </Show>

        <Show when={state.app?.configPath}>
          <p data-slot="home-path">
            Reading <code>{state.app!.configPath}</code> — the same file <code>ab</code> reads.
          </p>
        </Show>
      </div>
    </div>
  );
}

/**
 * Opened without the token.
 *
 * No box to paste one into: it belongs in the URL the server printed, and a
 * field for typing a credential into a page is a field for phishing one out of
 * somebody.
 */
function NoToken() {
  return (
    <div data-slot="home">
      <div data-slot="home-inner">
        <h1 data-slot="home-title">One link short</h1>
        <p data-slot="home-lede">
          This page needs the token the server printed when it started. Open the URL from that line — it ends with{" "}
          <code>#t=…</code> — rather than this address on its own.
        </p>
      </div>
    </div>
  );
}
