import { Button as Kobalte } from "@kobalte/core/button";
import { Show, splitProps, type ComponentProps } from "solid-js";
import { Icon, type IconName } from "./icon.tsx";

export interface ButtonProps extends ComponentProps<typeof Kobalte> {
  size?: "small" | "normal" | "large";
  variant?: "neutral" | "contrast" | "outline" | "ghost" | "ghost-muted" | "danger";
  icon?: IconName;
  class?: string;
}

export function Button(props: ButtonProps) {
  const [local, rest] = splitProps(props, ["variant", "size", "icon", "class", "children"]);
  return (
    <Kobalte
      {...rest}
      data-component="button"
      data-size={local.size ?? "normal"}
      data-variant={local.variant ?? "neutral"}
      data-icon={local.icon ? "" : undefined}
      class={local.class}
    >
      <Show when={local.icon}>{(name) => <Icon name={name()} />}</Show>
      {local.children}
    </Kobalte>
  );
}

export interface IconButtonProps extends ComponentProps<typeof Kobalte> {
  icon: IconName;
  size?: "small" | "normal";
  variant?: "neutral" | "ghost" | "ghost-muted" | "danger";
  label: string;
  class?: string;
}

/** Square, icon-only affordance. `label` is required — it becomes the a11y name. */
export function IconButton(props: IconButtonProps) {
  const [local, rest] = splitProps(props, ["icon", "size", "variant", "label", "class"]);
  return (
    <Kobalte
      {...rest}
      data-component="icon-button"
      data-size={local.size ?? "normal"}
      data-variant={local.variant ?? "ghost"}
      aria-label={local.label}
      title={local.label}
      class={local.class}
    >
      <Icon name={local.icon} />
    </Kobalte>
  );
}
