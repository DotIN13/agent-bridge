import { Dynamic } from "solid-js/web";
import { splitProps, type ComponentProps } from "solid-js";

import Check from "lucide-solid/icons/check";
import ChevronDown from "lucide-solid/icons/chevron-down";
import ChevronLeft from "lucide-solid/icons/chevron-left";
import Clock from "lucide-solid/icons/clock";
import Copy from "lucide-solid/icons/copy";
import Eye from "lucide-solid/icons/eye";
import EyeOff from "lucide-solid/icons/eye-off";
import KeyRound from "lucide-solid/icons/key-round";
import Link2 from "lucide-solid/icons/link-2";
import Plug from "lucide-solid/icons/plug";
import Plus from "lucide-solid/icons/plus";
import RefreshCw from "lucide-solid/icons/refresh-cw";
import Search from "lucide-solid/icons/search";
import Settings from "lucide-solid/icons/settings";
import Square from "lucide-solid/icons/square";
import Terminal from "lucide-solid/icons/terminal";
import Trash2 from "lucide-solid/icons/trash-2";
import TriangleAlert from "lucide-solid/icons/triangle-alert";
import X from "lucide-solid/icons/x";

/**
 * Lucide, imported one icon at a time so the bundle only carries what is used.
 * The keys are product names rather than glyph names, so swapping a glyph never
 * touches a call site.
 */
const ICONS = {
  alert: TriangleAlert,
  back: ChevronLeft,
  check: Check,
  "chevron-down": ChevronDown,
  clock: Clock,
  close: X,
  copy: Copy,
  eye: Eye,
  "eye-off": EyeOff,
  key: KeyRound,
  link: Link2,
  plug: Plug,
  plus: Plus,
  refresh: RefreshCw,
  search: Search,
  settings: Settings,
  stop: Square,
  terminal: Terminal,
  trash: Trash2,
} as const;

export type IconName = keyof typeof ICONS;

export interface IconProps extends Omit<ComponentProps<"svg">, "children"> {
  name: IconName;
  size?: number;
  /**
   * With `absoluteStrokeWidth` this is device pixels, so a 12px and a 22px icon
   * keep the same hairline weight — the way opencode's set behaves.
   */
  strokeWidth?: number;
}

export function Icon(props: IconProps) {
  const [local, rest] = splitProps(props, ["name", "size", "strokeWidth"]);
  return (
    <Dynamic
      component={ICONS[local.name]}
      {...rest}
      data-slot="icon-svg"
      size={local.size ?? 16}
      strokeWidth={local.strokeWidth ?? 1.5}
      absoluteStrokeWidth
      aria-hidden="true"
    />
  );
}
