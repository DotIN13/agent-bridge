"""Configuration loading: TOML file + environment overrides."""
from __future__ import annotations

import os
import secrets
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

# Fields kept from a [[agents.<name>.models]] entry. The catalog itself is NOT
# defined here — config.toml is its single source of truth, so a model can be
# added or repriced by editing config and restarting, with no code change and
# no second copy to drift. An agent with no models configured simply advertises
# none, and `model` on a job is then passed through unchecked.
_MODEL_FIELDS = ("id", "alias", "tier", "context", "input_per_mtok",
                 "output_per_mtok", "use", "note")

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
    "files": {
        "enabled": True,
        "dir": "",                 # "" -> per-user dir under $TMPDIR; abs or data_dir-relative otherwise
        "max_file_mb": 100,
        "max_request_mb": 512,
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
            "models": [],       # see config.example.toml; config is the catalog
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
    models: tuple[dict, ...] = ()

    def tiers(self) -> dict[str, str]:
        """complexity tier -> model id. First declared model of a tier wins."""
        out: dict[str, str] = {}
        for m in self.models:
            if m.get("tier") and m["tier"] not in out:
                out[m["tier"]] = m["id"]
        return out

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
    files_enabled: bool = True
    files_dir: str = ""
    files_max_file_mb: int = 100
    files_max_request_mb: int = 512
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    @property
    def default_agent(self) -> str:
        return "claude" if "claude" in self.agents else next(iter(self.agents))

    def allowed_bases(self) -> list[Path]:
        """Union of every agent's allowed_dirs, resolved. Used to sandbox file
        upload/download so nothing escapes what the agents can already touch."""
        bases: list[Path] = []
        for a in self.agents.values():
            for d in a.allowed_dirs:
                p = Path(d).expanduser().resolve()
                if p not in bases:
                    bases.append(p)
        return bases

    def within_allowed(self, path: str | os.PathLike) -> Path:
        """Resolve `path` and ensure it sits inside an allowed base or the file
        store (the store may live outside allowed_dirs, e.g. /tmp)."""
        p = Path(path).expanduser().resolve()
        bases = self.allowed_bases()
        if self.files_dir:
            bases.append(Path(self.files_dir).resolve())
        for b in bases:
            if p == b or b in p.parents:
                return p
        raise ValueError(f"path {p} is not under any allowed directory")


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
            models=_load_models(a.get("models", []), name),
        )

    cl = raw.get("cluster", {})
    fl = raw.get("files", {})
    files_dir = _resolve_files_dir(fl.get("dir", ""), data_dir)
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
        files_enabled=bool(fl.get("enabled", True)),
        files_dir=files_dir,
        files_max_file_mb=int(fl.get("max_file_mb", 100)),
        files_max_request_mb=int(fl.get("max_request_mb", 512)),
        agents=agents,
    )


def _load_models(raw: list, agent: str) -> tuple[dict, ...]:
    """Normalise [[agents.<name>.models]] into a stable shape for /v1/models.

    Only `id` is required; everything else is advertising copy. Unknown keys are
    dropped rather than passed through, so the endpoint's shape stays fixed no
    matter what an operator adds to the TOML.
    """
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise ValueError(
                f"agents.{agent}.models: every entry needs an `id` (got {entry!r})")
        out.append({k: entry[k] for k in _MODEL_FIELDS if entry.get(k) not in (None, "")})
    return tuple(out)


def _resolve_files_dir(cfg_dir: str, data_dir: Path) -> str:
    """Where uploads live:
        - [files].dir from config if set (absolute, or relative to the data dir)
        - otherwise a per-user dir under tempfile.gettempdir()
    The store is locked to 0700 — it may be world-accessible and uploads can be
    sensitive."""
    if cfg_dir:
        d = Path(cfg_dir) if Path(cfg_dir).is_absolute() else data_dir / cfg_dir
    else:
        d = Path(tempfile.gettempdir()) / f"agent-bridge-{os.getuid()}" / "files"
    return _prepare_store(d)


def _prepare_store(d: Path) -> str:
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)   # protect uploads even in world-accessible /tmp
    except OSError:
        pass
    return str(d.resolve())


def _ensure_token(data_dir: Path) -> str:
    token_file = data_dir / ".token"
    if token_file.exists():
        return token_file.read_text().strip()
    token = secrets.token_urlsafe(32)
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)
    return token
