import { Show, createEffect, createSignal, onCleanup } from "solid-js";
import type { AuthPrompt as Prompt } from "../../protocol.ts";
import { answerPrompt, state } from "../store.ts";
import { Button, IconButton } from "./ui/button.tsx";
import { Dialog } from "./ui/primitives.tsx";

/**
 * What ssh is asking, put in front of a human.
 *
 * The first waiting prompt only: a two-factor login asks twice, and drawing both
 * at once invites answering the second question in the first box. When this one
 * is answered the next arrives on its own.
 *
 * Keyed on the **id**, not the prompt object. `keyed` remounts the body
 * whenever its value changes identity, and the server publishes a fresh state
 * frame every time anything moves — a probe, a line of ssh output — so keying
 * on the object threw the dialog away and rebuilt it mid-sentence, wiping a
 * half-typed password. Measured: the box was empty two seconds after typing
 * into it. The id changes only when it is genuinely a different question.
 */
export function AuthPromptDialog() {
  const id = () => state.app?.prompts[0]?.id;
  return (
    <Show when={id()} keyed>
      {(promptId) => (
        <Show when={state.app?.prompts.find((entry) => entry.id === promptId)}>
          {(prompt) => <PromptBody prompt={prompt()} />}
        </Show>
      )}
    </Show>
  );
}

/**
 * One of these per question, which is the point.
 *
 * The draft, the reveal toggle and the focus all belong to *this* question. A
 * single long-lived component reused across prompts carried the previous
 * answer's text into the next box.
 */
function PromptBody(props: { prompt: Prompt }) {
  const [draft, setDraft] = createSignal("");
  const [reveal, setReveal] = createSignal(false);
  const [sending, setSending] = createSignal(false);
  let input: HTMLInputElement | undefined;

  /** Focus once, when the box appears — not on every repaint. */
  createEffect(() => {
    if (input) input.focus();
  });

  /** A prompt has a deadline; a box that is about to stop working should say so. */
  const [now, setNow] = createSignal(Date.now());
  const timer = setInterval(() => setNow(Date.now()), 1000);
  onCleanup(() => clearInterval(timer));
  const secondsLeft = () => Math.max(0, Math.round((Date.parse(props.prompt.expiresAt) - now()) / 1000));

  const send = async () => {
    setSending(true);
    try {
      await answerPrompt(props.prompt.id, draft());
    } finally {
      setSending(false);
    }
  };

  const dismiss = () => void answerPrompt(props.prompt.id, null);

  return (
    <Dialog
      open
      insistent
      onOpenChange={() => {}}
      width="520px"
      title={`${props.prompt.gateway} is asking`}
      description="ssh wants an answer. It goes to the ssh process on this machine and is not stored."
      footer={
        <>
          <Button size="normal" variant="contrast" disabled={sending()} onClick={() => void send()}>
            Answer
          </Button>
          <Button size="normal" variant="ghost-muted" disabled={sending()} onClick={dismiss}>
            Nobody is here
          </Button>
          <span data-slot="prompt-countdown">{secondsLeft()}s</span>
        </>
      }
    >
      <div data-slot="prompt-body">
        <p data-slot="prompt-question">{props.prompt.text || "Password:"}</p>

        <div data-slot="secret-field">
          {/*
           * A masked *text* input, not `type="password"`.
           *
           * Chrome's password manager offers to save anything typed into a
           * password field, and its bubble steals the focus — which on a
           * two-factor prompt means the next question arrives while a
           * "save password?" dialog is over the box. `-webkit-text-security`
           * masks the characters without the field ever claiming to be a
           * credential, and the `data-*` attributes turn off the three
           * third-party managers that ignore `autocomplete`.
           */}
          <input
            ref={input}
            type="text"
            data-slot="secret-input"
            data-reveal={reveal() ? "" : undefined}
            autocomplete="off"
            autocapitalize="off"
            autocorrect="off"
            spellcheck={false}
            data-1p-ignore
            data-lpignore="true"
            data-bwignore
            value={draft()}
            onInput={(event) => setDraft(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void send();
            }}
          />
          <IconButton
            icon={reveal() ? "eye-off" : "eye"}
            label={reveal() ? "Hide" : "Show"}
            size="small"
            variant="ghost-muted"
            onClick={() => setReveal(!reveal())}
          />
        </div>

        {/* What ssh has said so far. A prompt with no context is a prompt you
            have to guess at — "Password:" for which host, and after what. */}
        <Show when={props.prompt.context.length}>
          <pre data-slot="console">{props.prompt.context.slice(-8).join("\n")}</pre>
        </Show>
      </div>
    </Dialog>
  );
}
