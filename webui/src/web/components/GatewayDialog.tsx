import { For, Show, createEffect, createSignal, on, onCleanup } from "solid-js";
import {
  closeGatewayDialog,
  gateway,
  parseSsh,
  removeGateway,
  saveGateway,
  state,
  type ParsedSsh,
} from "../store.ts";
import { Button } from "./ui/button.tsx";
import { Icon } from "./ui/icon.tsx";
import { Dialog, Switch, TextArea, TextInput } from "./ui/primitives.tsx";

/**
 * Add, edit or remove one entry in `gateways.json`.
 *
 * Writing to `ab`'s own config rather than a copy is the whole point: a gateway
 * added here is a gateway `ab submit` can use from a terminal a second later.
 * The file is edited as a guest — unknown keys survive, the write is atomic —
 * and the one thing this dialog will not do is put a raw token in it. A token
 * belongs in an environment variable or a file; both are named here, neither is
 * read here.
 */
export function GatewayDialog() {
  return (
    <Show when={state.dialog} keyed>
      {(dialog) => <Body name={dialog.name} />}
    </Show>
  );
}

function Body(props: { name: string | null }) {
  const existing = () => (props.name ? gateway(props.name) : undefined);
  const isNew = () => props.name === null;

  const [name, setName] = createSignal(props.name ?? "");
  const [baseUrl, setBaseUrl] = createSignal(existing()?.baseUrl ?? "");
  const [ssh, setSsh] = createSignal(existing()?.ssh ?? "");
  const [tokenEnv, setTokenEnv] = createSignal(
    existing()?.tokenSource === "token_env" ? (existing()!.tokenName ?? "") : "",
  );
  const [tokenFile, setTokenFile] = createSignal(
    existing()?.tokenSource === "token_file" ? (existing()!.tokenName ?? "") : "",
  );
  const [enabled, setEnabled] = createSignal(existing()?.enabled ?? true);
  const [makeDefault, setMakeDefault] = createSignal(existing()?.isDefault ?? false);
  const [parsed, setParsed] = createSignal<ParsedSsh | null>(null);
  const [confirming, setConfirming] = createSignal(false);
  const [saving, setSaving] = createSignal(false);

  /**
   * The parse, debounced.
   *
   * Reading the command back is what makes this dialog worth opening: it says
   * which ports it will bind and which flags it refused *before* an ssh process
   * exists to argue with. It runs for the value the field opened with too, so
   * an entry configured months ago shows its parse without being retyped.
   */
  createEffect(
    on(ssh, (command) => {
      if (!command.trim()) {
        setParsed(null);
        return;
      }
      const timer = setTimeout(() => {
        void parseSsh(command).then((result) => {
          setParsed(result);
          // A base URL nobody has typed yet, from the port the command
          // forwards: it is right almost every time, and wrong in a way that is
          // obvious on screen.
          const first = result?.forwards.find((forward) => forward.kind === "local");
          if (first && !baseUrl().trim()) setBaseUrl(`http://localhost:${first.localPort}`);
        });
      }, 300);
      onCleanup(() => clearTimeout(timer));
    }),
  );

  const save = async () => {
    setSaving(true);
    try {
      const target = isNew() ? name().trim() : props.name!;
      if (!target) return;
      const patch = {
        baseUrl: baseUrl(),
        ssh: ssh().trim() ? ssh().trim() : null,
        tokenEnv: tokenEnv().trim() ? tokenEnv().trim() : null,
        tokenFile: tokenFile().trim() ? tokenFile().trim() : null,
        enabled: enabled(),
        makeDefault: makeDefault(),
        ...(isNew() || name().trim() === props.name ? {} : { rename: name().trim() }),
      };
      const saved = await saveGateway(target, patch);
      if (saved !== null) closeGatewayDialog();
    } finally {
      setSaving(false);
    }
  };

  const drop = async () => {
    if (!props.name) return;
    const done = await removeGateway(props.name);
    if (done !== null) closeGatewayDialog();
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) closeGatewayDialog();
      }}
      width="620px"
      title={isNew() ? "Add a gateway" : `Configure ${props.name}`}
      description={state.app?.configPath}
      footer={
        <>
          <Button size="normal" variant="contrast" disabled={saving() || !name().trim() || !baseUrl().trim()} onClick={() => void save()}>
            Save
          </Button>
          <Button size="normal" variant="ghost-muted" onClick={closeGatewayDialog}>
            Cancel
          </Button>
          <Show when={!isNew()}>
            <span class="ml-auto">
              <Show
                when={confirming()}
                fallback={
                  <Button size="normal" variant="ghost-muted" icon="trash" onClick={() => setConfirming(true)}>
                    Remove
                  </Button>
                }
              >
                {/* Two clicks, because this edits a file `ab` reads and there is
                    no undo in a JSON file. */}
                <Button size="normal" variant="danger" onClick={() => void drop()}>
                  Remove {props.name} from the config
                </Button>
              </Show>
            </span>
          </Show>
        </>
      }
    >
      <div data-slot="form">
        <Show when={state.readOnly}>
          <div data-slot="banner" data-tone="warning">
            <span data-slot="banner-mark">
              <Icon name="alert" size={12} />
            </span>
            <span>This config is TOML, which this dialog will not rewrite. Edit it by hand.</span>
          </div>
        </Show>

        <TextInput label="Name" value={name()} onValue={setName} placeholder="midway5" spellcheck={false} />

        <TextInput
          label="Base URL"
          value={baseUrl()}
          onValue={setBaseUrl}
          placeholder="http://localhost:8787"
          spellcheck={false}
          hint="Where the gateway answers on this machine — the local end of the forward."
        />

        <TextArea
          label="ssh command"
          value={ssh()}
          onValue={setSsh}
          rows={2}
          spellcheck={false}
          placeholder="ssh -N -L 8787:localhost:8787 midway5"
          hint="The command you would type. It is run as written, with -N and a few keep-alives added."
        />

        <Show when={parsed()}>
          {(result) => (
            <div data-slot="parse">
              <div data-slot="parse-row">
                <span data-slot="field-label">Host</span>
                <code>{result().destination || "—"}</code>
              </div>
              <div data-slot="parse-row">
                <span data-slot="field-label">Forwards</span>
                <span data-slot="parse-forwards">
                  <Show when={result().forwards.length} fallback={<code>none</code>}>
                    <For each={result().forwards}>
                      {(forward) => (
                        <code>
                          {forward.kind === "dynamic"
                            ? `SOCKS :${forward.localPort}`
                            : `${forward.localPort} → ${forward.remoteHost}:${forward.remotePort}`}
                        </code>
                      )}
                    </For>
                  </Show>
                </span>
              </div>
              <Show when={result().diagnostics.length}>
                <ul data-slot="parse-problems">
                  <For each={result().diagnostics}>{(line) => <li>{line}</li>}</For>
                </ul>
              </Show>
            </div>
          )}
        </Show>

        <div data-slot="form-split">
          <TextInput
            label="Token from $ENV"
            value={tokenEnv()}
            onValue={(value) => {
              setTokenEnv(value);
              if (value.trim()) setTokenFile("");
            }}
            placeholder="AGENT_BRIDGE_TOKEN"
            spellcheck={false}
          />
          <TextInput
            label="…or from a file"
            value={tokenFile()}
            onValue={(value) => {
              setTokenFile(value);
              if (value.trim()) setTokenEnv("");
            }}
            placeholder="~/.config/agent-bridge/midway5.token"
            spellcheck={false}
          />
        </div>
        <p data-slot="field-hint">
          One or the other. The token itself is never typed here and never leaves the server — this names where to read
          it from, exactly as <code>ab</code> does.
        </p>

        <div data-slot="form-switches">
          <Switch checked={enabled()} onChange={setEnabled} label="Enabled" />
          <Switch checked={makeDefault()} onChange={setMakeDefault} label="ab's default gateway" />
        </div>
      </div>
    </Dialog>
  );
}
