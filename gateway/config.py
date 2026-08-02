"""Configuration loading: TOML file + environment overrides."""
from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

_DEFAULTS: dict = {
    "server": {"host": "127.0.0.1", "port": 8787},
    "auth": {"token": ""},
    "worker": {"concurrency": 2},
    "db": {"path": "gateway.db"},
    "cluster": {
        "enabled": True,
        "probe_timeout_sec": 15,
        # env vars reported presence-only (never their values) at /v1/info
        "env_presence": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
    },
    "agents": {
        "claude": {
            "bin": "claude",
            "dispatch_mode": "agent_exec",
            "permission_mode": "bypassPermissions",
            "model": "",
            "default_cwd": str(Path.cwd()),
            "allowed_dirs": [str(Path.home())],
            "timeout_sec": 0,
            "max_sessions_in_index": 40,
        }
    },
}


@dataclass(frozen=True)
class AgentConfig:
    name: str
    bin: str
    dispatch_mode: str
    permission_mode: str
    model: str
    default_cwd: str
    allowed_dirs: tuple[str, ...]
    timeout_sec: int
    max_sessions_in_index: int

    def resolve_cwd(self, requested: str | None) -> str:
        """Return an allowed absolute cwd, or raise ValueError."""
        target = Path(requested).expanduser() if requested else Path(self.default_cwd)
        target = target.resolve()
        for base in self.allowed_dirs:
            b = Path(base).expanduser().resolve()
            if target == b or b in target.parents:
                return str(target)
        raise ValueError(
            f"cwd {target} is not under any allowed_dirs {list(self.allowed_dirs)}"
        )


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    token: str
    concurrency: int
    db_path: str          # absolute
    data_dir: str         # absolute
    cluster_enabled: bool = True
    cluster_probe_timeout: int = 15
    cluster_env_presence: tuple[str, ...] = ()
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    @property
    def default_agent(self) -> str:
        return "claude" if "claude" in self.agents else next(iter(self.agents))


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides(cfg: dict) -> dict:
    """AGENT_BRIDGE_SERVER_PORT=9000 -> cfg['server']['port']=9000 (scalars only)."""
    for env_key, raw in os.environ.items():
        if not env_key.startswith("AGENT_BRIDGE_"):
            continue
        parts = env_key[len("AGENT_BRIDGE_"):].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, key = parts
        if section in cfg and isinstance(cfg[section], dict) and key in cfg[section]:
            cur = cfg[section][key]
            cfg[section][key] = _coerce(raw, cur)
    return cfg


def _coerce(raw: str, like):
    if isinstance(like, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        return int(raw)
    if isinstance(like, float):
        return float(raw)
    return raw


def load(path: str | os.PathLike | None) -> Config:
    raw = dict(_DEFAULTS)
    data_dir = Path.cwd()
    if path:
        p = Path(path).expanduser().resolve()
        with open(p, "rb") as fh:
            raw = _deep_merge(raw, tomllib.load(fh))
        data_dir = p.parent
    raw = _env_overrides(raw)

    data_dir = Path(os.environ.get("AGENT_BRIDGE_DATA_DIR", data_dir)).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    token = raw["auth"]["token"] or _ensure_token(data_dir)

    db_path = Path(raw["db"]["path"])
    if not db_path.is_absolute():
        db_path = data_dir / db_path

    agents: dict[str, AgentConfig] = {}
    for name, a in raw.get("agents", {}).items():
        agents[name] = AgentConfig(
            name=name,
            bin=a["bin"],
            dispatch_mode=a.get("dispatch_mode", "agent_exec"),
            permission_mode=a.get("permission_mode", "bypassPermissions"),
            model=a.get("model", ""),
            default_cwd=str(Path(a["default_cwd"]).expanduser().resolve()),
            allowed_dirs=tuple(a.get("allowed_dirs", [str(Path.home())])),
            timeout_sec=int(a.get("timeout_sec", 0)),
            max_sessions_in_index=int(a.get("max_sessions_in_index", 40)),
        )

    cl = raw.get("cluster", {})
    return Config(
        host=raw["server"]["host"],
        port=int(raw["server"]["port"]),
        token=token,
        concurrency=int(raw["worker"]["concurrency"]),
        db_path=str(db_path),
        data_dir=str(data_dir),
        cluster_enabled=bool(cl.get("enabled", True)),
        cluster_probe_timeout=int(cl.get("probe_timeout_sec", 15)),
        cluster_env_presence=tuple(cl.get("env_presence", [])),
        agents=agents,
    )


def _ensure_token(data_dir: Path) -> str:
    token_file = data_dir / ".token"
    if token_file.exists():
        return token_file.read_text().strip()
    token = secrets.token_urlsafe(32)
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)
    return token
