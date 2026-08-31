import { Dialog as KDialog } from "@kobalte/core/dialog";
import { Switch as KSwitch } from "@kobalte/core/switch";
import { Tooltip as KTooltip } from "@kobalte/core/tooltip";
import { TextField as KTextField } from "@kobalte/core/text-field";
import { Show, splitProps, type ComponentProps, type JSX, type ParentProps } from "solid-js";
import { Icon } from "./icon.tsx";
import { IconButton } from "./button.tsx";

/**
 * The primitives, from picone's — Kobalte for behaviour, `data-component` and
 * `data-slot` for styling, so the CSS in `ui/*.css` is the same file in both
 * projects and stays diffable.
 */

// ---------------------------------------------------------------------------
// Tooltip
// ---------------------------------------------------------------------------

export function Tooltip(props: ParentProps<{ label: JSX.Element; placement?: "top" | "bottom" | "left" | "right" }>) {
  return (
    <KTooltip openDelay={400} closeDelay={80} placement={props.placement ?? "bottom"} gutter={6}>
      <KTooltip.Trigger as="span" data-slot="tooltip-trigger">
        {props.children}
      </KTooltip.Trigger>
      <KTooltip.Portal>
        <KTooltip.Content data-component="tooltip">{props.label}</KTooltip.Content>
      </KTooltip.Portal>
    </KTooltip>
  );
}

// ---------------------------------------------------------------------------
// Dialog
// ---------------------------------------------------------------------------

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: JSX.Element;
  description?: JSX.Element;
  width?: string;
  children: JSX.Element;
  footer?: JSX.Element;
  /**
   * A dialog that must be answered rather than dismissed.
   *
   * Only the ssh prompt uses it, and it still closes — with a button that says
   * what dismissing means (ssh is told nobody is there). What it refuses is the
   * *accidental* close: Escape or a click on the scrim, with a password half
   * typed, cancels an authentication that may have cost a two-factor push.
   */
  insistent?: boolean;
}

export function Dialog(props: DialogProps) {
  return (
    <KDialog open={props.open} onOpenChange={props.onOpenChange} modal preventScroll>
      <KDialog.Portal>
        <KDialog.Overlay data-component="dialog-overlay" />
        <div data-slot="dialog-positioner">
          <KDialog.Content
            data-component="dialog"
            style={{ width: props.width ?? "560px" }}
            // Kobalte has no `closeOnEscape` on the root, so an insistent
            // dialog refuses the two gestures on the content itself.
            onEscapeKeyDown={(event: KeyboardEvent) => {
              if (props.insistent) event.preventDefault();
            }}
            onInteractOutside={(event: Event) => {
              if (props.insistent) event.preventDefault();
            }}
          >
            <div data-slot="dialog-header">
              <div data-slot="dialog-heading">
                <KDialog.Title data-slot="dialog-title">{props.title}</KDialog.Title>
                <Show when={props.description}>
                  <KDialog.Description data-slot="dialog-description">{props.description}</KDialog.Description>
                </Show>
              </div>
              <Show when={!props.insistent}>
                <KDialog.CloseButton as="div">
                  <IconButton icon="close" label="Close" variant="ghost-muted" />
                </KDialog.CloseButton>
              </Show>
            </div>
            <div data-slot="dialog-body">{props.children}</div>
            <Show when={props.footer}>
              <div data-slot="dialog-footer">{props.footer}</div>
            </Show>
          </KDialog.Content>
        </div>
      </KDialog.Portal>
    </KDialog>
  );
}

// ---------------------------------------------------------------------------
// Switch
// ---------------------------------------------------------------------------

export function Switch(props: { checked: boolean; onChange: (checked: boolean) => void; label: JSX.Element }) {
  return (
    <KSwitch checked={props.checked} onChange={props.onChange} data-component="switch">
      <KSwitch.Label data-slot="switch-label">{props.label}</KSwitch.Label>
      <KSwitch.Input />
      <KSwitch.Control data-slot="switch-control">
        <KSwitch.Thumb data-slot="switch-thumb" />
      </KSwitch.Control>
    </KSwitch>
  );
}

// ---------------------------------------------------------------------------
// Text input
// ---------------------------------------------------------------------------

export interface TextInputProps extends Omit<ComponentProps<"input">, "onInput" | "value"> {
  value: string;
  onValue: (value: string) => void;
  label?: JSX.Element;
  hint?: JSX.Element;
  size?: "small" | "normal";
}

export function TextInput(props: TextInputProps) {
  const [local, rest] = splitProps(props, ["value", "onValue", "label", "hint", "size"]);
  return (
    <KTextField value={local.value} onChange={local.onValue} data-component="text-field">
      <Show when={local.label}>
        <KTextField.Label data-slot="field-label">{local.label}</KTextField.Label>
      </Show>
      <KTextField.Input {...rest} data-slot="text-input" data-size={local.size ?? "normal"} />
      <Show when={local.hint}>
        <KTextField.Description data-slot="field-hint">{local.hint}</KTextField.Description>
      </Show>
    </KTextField>
  );
}

export interface TextAreaProps extends Omit<ComponentProps<"textarea">, "onInput" | "value"> {
  value: string;
  onValue: (value: string) => void;
  label?: JSX.Element;
  hint?: JSX.Element;
}

export function TextArea(props: TextAreaProps) {
  const [local, rest] = splitProps(props, ["value", "onValue", "label", "hint"]);
  return (
    <KTextField value={local.value} onChange={local.onValue} data-component="text-field">
      <Show when={local.label}>
        <KTextField.Label data-slot="field-label">{local.label}</KTextField.Label>
      </Show>
      <KTextField.TextArea {...rest} data-slot="textarea" />
      <Show when={local.hint}>
        <KTextField.Description data-slot="field-hint">{local.hint}</KTextField.Description>
      </Show>
    </KTextField>
  );
}

// ---------------------------------------------------------------------------
// Tag
// ---------------------------------------------------------------------------

export function Tag(props: ParentProps<{ tone?: "neutral" | "success" | "warning" | "danger" | "info" }>) {
  return (
    <span data-component="tag" data-tone={props.tone ?? "neutral"}>
      {props.children}
    </span>
  );
}

export function Spinner(props: { size?: number }) {
  return <span data-component="spinner" style={{ width: `${props.size ?? 10}px`, height: `${props.size ?? 10}px` }} />;
}

// ---------------------------------------------------------------------------
// Copyable
// ---------------------------------------------------------------------------

/** A monospace value with a copy button, for ids and ports. */
export function Copyable(props: { value: string; display?: string; label?: string }) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(props.value);
    } catch {
      /* a denied clipboard is not worth an error dialog */
    }
  };
  return (
    <button type="button" data-slot="copyable" aria-label={props.label ?? `Copy ${props.value}`} onClick={() => void copy()}>
      <code>{props.display ?? props.value}</code>
      <Icon name="copy" size={11} />
    </button>
  );
}
