"""The client gateway file, read and written.

`~/.config/agent-bridge/gateways.json` already describes every gateway the `ab`
CLI can talk to. A tunnelled gateway's `base_url` is a *local* port that only
answers while an ssh forward is up, and until now keeping that forward alive was
the operator's problem, done by hand in a terminal that had to stay open
(README's "on a laptop, keep one tunnel alive").

This adds two optional keys per gateway, both ignored by the CLI:

    "ssh": "ssh -N -L 8787:localhost:8787 midway5"   # or a list of argv
    "autostart": false                                # bring it up on boot

Nothing else about the file changes, so an existing config keeps working and a
config written here stays readable by `ab`.

Writing it back is a real risk and is treated as one: the file names a command
this machine executes, and the editor is a web page. Hence `validate_ssh` --
argv only, no shell, and the program has to be one of a small allowlist. See
docs/design/20.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

#: Programs the daemon will exec. Not a general command runner: the UI can edit
#: this command, so an unrestricted argv would make a loopback web page a remote
#: shell. `sshpass` is here because people do use it; it is no worse than the
#: password it wraps, and refusing it would only push them to a wrapper script.
DEFAULT_PROGRAMS = ("ssh", "autossh", "sshpass")

#: Characters that mean the author expected a shell. They get an error naming
#: the alternative rather than a command that silently means something else --
#: `ssh a && rm -rf b` as argv passes `&&` to ssh, which is not what was meant.
SHELL_CHARS = ";&|<>$`\n\r*?(){}[]!~"

#: The search order `ab` itself uses, minus the explicit flag. Same order on
#: purpose: the daemon must manage the file the CLI reads, or the two disagree
#: about what `midway5` means.
def candidates(explicit: str | None = None) -> list[Path]:
    out = []
    for candidate in (explicit,
                      os.environ.get("AGENT_BRIDGE_CLIENT_CONFIG"),
                      str(Path.home() / ".config" / "agent-bridge" / "gateways.json"),
                      "gateways.json"):
        if candidate:
            out.append(Path(candidate).expanduser())
    return out


class ConfigError(ValueError):
    """The config cannot be read, or a proposed edit is not safe to write."""


@dataclass
class GatewayEntry:
    """One gateway as the daemon needs it: where it is, and how to reach it."""

    name: str
    base_url: str
    ssh: tuple[str, ...] = ()       # argv, already validated
    autostart: bool = False
    raw: dict = field(default_factory=dict)
    #: Non-fatal: the entry is usable, something about it will bite later.
    warning: str = ""

    @property
    def tunnelled(self) -> bool:
        return bool(self.ssh)

    @property
    def local_port(self) -> int | None:
        """The port `base_url` points at, for the "is anything listening" check.

        Read from the url rather than from the ssh command: the url is what the
        CLI will actually connect to, and a `-L` spec that disagrees with it is
        precisely the misconfiguration worth surfacing.
        """
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(self.base_url)
            return parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            return None

    def public(self) -> dict:
        """What the UI is allowed to see. No token, ever -- not even its value's
        length; whether one is configured is the useful part."""
        return {
            "name": self.name,
            "base_url": self.base_url,
            "ssh": list(self.ssh),
            "ssh_display": shlex.join(self.ssh) if self.ssh else "",
            "autostart": self.autostart,
            "tunnelled": self.tunnelled,
            "has_token": bool(self.raw.get("token") or self.raw.get("token_env")
                              or self.raw.get("token_file")),
            "token_source": ("inline" if self.raw.get("token") else
                             "env" if self.raw.get("token_env") else
                             "file" if self.raw.get("token_file") else ""),
            "warning": self.warning,
        }


def validate_ssh(spec, programs: tuple[str, ...] = DEFAULT_PROGRAMS
                 ) -> tuple[str, ...]:
    """Turn an `ssh` value into argv, or say exactly why it cannot be one.

    A string is split the way a shell would split it *for quoting purposes only*
    -- `shlex.split` -- and then executed without a shell. The difference
    matters: quotes and spaces work as expected, redirection and chaining do
    not, and are refused up front rather than passed to ssh as arguments.
    """
    if spec is None or spec == "" or spec == []:
        return ()
    if isinstance(spec, (list, tuple)):
        argv = [str(part) for part in spec]
    elif isinstance(spec, str):
        if any(char in spec for char in SHELL_CHARS):
            bad = sorted({c for c in spec if c in SHELL_CHARS})
            raise ConfigError(
                f"ssh command contains shell characters {bad!r}. This runs "
                f"without a shell, so they would be passed to the program as "
                f"literal arguments. Put the pieces in a list, or move the "
                f"shell part into a wrapper script and name that instead.")
        try:
            argv = shlex.split(spec)
        except ValueError as exc:
            raise ConfigError(f"ssh command does not parse: {exc}") from exc
    else:
        raise ConfigError("ssh must be a string or a list of arguments")
    if not argv:
        return ()
    program = Path(argv[0]).name
    if program not in programs:
        raise ConfigError(
            f"ssh command must start with one of {list(programs)}, not "
            f"{program!r}. The daemon executes this, and the web UI can edit "
            f"it, so it is deliberately not a general command runner.")
    return tuple(argv)


def program_warning(argv: tuple[str, ...]) -> str:
    """"`ssh` is not installed here" -- worth saying, not worth refusing.

    A config is often written on one machine and used on another, and a laptop
    that will have ssh in a minute should still be editable now. The daemon will
    report the real failure the moment it tries to run it; this just says so
    earlier.
    """
    if not argv:
        return ""
    if shutil.which(argv[0]) is None and not Path(argv[0]).expanduser().is_file():
        return f"{argv[0]!r} is not on PATH here; starting it will fail"
    return ""


class Store:
    """The gateway file: parsed, queryable, and writable back to the same path.

    Held as the raw document rather than a normalised copy, so a key this code
    knows nothing about -- `token_file`, or whatever the CLI grows next --
    survives a round trip through the UI untouched.
    """

    def __init__(self, path: Path, document: dict,
                 programs: tuple[str, ...] = DEFAULT_PROGRAMS) -> None:
        self.path = path
        self.document = document
        self.programs = programs
        self._problems: dict[str, str] = {}

    @classmethod
    def load(cls, explicit: str | None = None,
             programs: tuple[str, ...] = DEFAULT_PROGRAMS) -> "Store":
        tried = candidates(explicit)
        if explicit and not tried[0].exists():
            raise ConfigError(f"gateway config not found: {tried[0]}")
        for path in tried:
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot read {path}: {exc}") from exc
            if path.suffix == ".toml":
                # Readable, but not writable: round-tripping TOML without a
                # writer loses comments and ordering, and this file is one a
                # human maintains. Editing is refused with that reason.
                import tomllib
                try:
                    document = tomllib.loads(text)
                except Exception as exc:
                    raise ConfigError(f"cannot parse {path}: {exc}") from exc
            else:
                try:
                    document = json.loads(text or "{}")
                except json.JSONDecodeError as exc:
                    raise ConfigError(f"cannot parse {path}: {exc}") from exc
            return cls(path, document, programs)
        raise ConfigError(
            "no gateway config found (tried " +
            ", ".join(str(p) for p in tried) + ")")

    @property
    def writable(self) -> bool:
        return self.path.suffix != ".toml"

    def entries(self) -> list[GatewayEntry]:
        """Every gateway, in file order.

        A bad `ssh` value does not hide the gateway: the entry comes back with
        no command and the reason is available from `problems()`, because a
        gateway you cannot see is one you cannot fix from the UI.
        """
        self._problems = {}
        out: list[GatewayEntry] = []
        raw_gateways = self.document.get("gateways") or {}
        if not isinstance(raw_gateways, dict):
            raise ConfigError("'gateways' must be an object")
        for name, raw in raw_gateways.items():
            if not isinstance(raw, dict):
                self._problems[name] = "entry is not an object"
                raw = {}
            try:
                ssh = validate_ssh(raw.get("ssh"), self.programs)
            except ConfigError as exc:
                self._problems[name] = str(exc)
                ssh = ()
            out.append(GatewayEntry(
                name=name, base_url=(raw.get("base_url") or "").rstrip("/"),
                ssh=ssh, autostart=bool(raw.get("autostart")), raw=raw,
                warning=program_warning(ssh)))
        return out

    def problems(self) -> dict[str, str]:
        return dict(self._problems)

    def get(self, name: str) -> GatewayEntry | None:
        for entry in self.entries():
            if entry.name == name:
                return entry
        return None

    @property
    def default(self) -> str | None:
        value = self.document.get("default")
        return value if isinstance(value, str) else None

    # -- editing ----------------------------------------------------------
    def put(self, name: str, fields: dict) -> GatewayEntry:
        """Create or update one gateway, then write the file.

        Only the keys this understands are touched; anything else already in the
        entry is left alone, so a `token_file` the UI never shows is not lost by
        an edit that changed a port.
        """
        self._require_writable()
        if not name or "/" in name or name != name.strip():
            raise ConfigError("gateway name must be non-empty and have no slashes")
        base_url = str(fields.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ConfigError("base_url is required")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError("base_url must start with http:// or https://")
        ssh = validate_ssh(fields.get("ssh"), self.programs)

        gateways = self.document.setdefault("gateways", {})
        entry = dict(gateways.get(name) or {})
        entry["base_url"] = base_url
        if ssh:
            entry["ssh"] = list(ssh)
        else:
            entry.pop("ssh", None)
        if "autostart" in fields:
            entry["autostart"] = bool(fields["autostart"])
        for key in ("token", "token_env", "token_file"):
            if key in fields:
                # Setting one clears the others: three ways to name a token is
                # already the CLI's rule, and two set at once is a coin flip.
                for other in ("token", "token_env", "token_file"):
                    entry.pop(other, None)
                if fields[key]:
                    entry[key] = str(fields[key])
                break
        gateways[name] = entry
        self._ensure_default()
        self.save()
        return self.get(name)

    def delete(self, name: str) -> None:
        self._require_writable()
        gateways = self.document.get("gateways") or {}
        if name not in gateways:
            raise ConfigError(f"unknown gateway {name!r}")
        gateways.pop(name)
        if self.document.get("default") == name:
            self.document.pop("default", None)
        self._ensure_default()
        self.save()

    def set_default(self, name: str) -> None:
        self._require_writable()
        if name not in (self.document.get("gateways") or {}):
            raise ConfigError(f"unknown gateway {name!r}")
        self.document["default"] = name
        self.save()

    def _ensure_default(self) -> None:
        """Keep the file loadable by `ab`, which refuses more than one gateway
        with no default -- an edit that made the CLI unusable would be a poor
        way to find out."""
        gateways = self.document.get("gateways") or {}
        current = self.document.get("default")
        if current in gateways:
            return
        if len(gateways) == 1:
            self.document.pop("default", None)
        elif gateways:
            self.document["default"] = next(iter(gateways))

    def _require_writable(self) -> None:
        if not self.writable:
            raise ConfigError(
                f"{self.path} is TOML; this daemon only writes JSON, because a "
                f"TOML round trip would drop your comments and ordering. Edit "
                f"it in an editor, or convert it to JSON.")

    def save(self) -> None:
        """Write the file, keeping the previous copy.

        Temp-then-rename so a reader never sees half a document, and one `.bak`
        so a bad edit from a browser is recoverable without a version-control
        argument.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                shutil.copy2(self.path, self.path.with_suffix(
                    self.path.suffix + ".bak"))
            except OSError:
                pass
        tmp = self.path.with_name(self.path.name + ".tmp")
        text = json.dumps(self.document, indent=2, ensure_ascii=False) + "\n"
        tmp.write_text(text, encoding="utf-8")
        # The file can carry an inline token; it must not be world-readable.
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
