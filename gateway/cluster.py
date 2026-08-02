"""Cluster capability probing.

Runs a set of cheap, read-only commands ONCE on startup (concurrently, in a
background thread) and caches a concise structured snapshot. `GET /v1/info`
returns the cache instantly; `?refresh=1` re-runs. Nothing here writes anything,
submits any job, or prints secret values (env vars are reported presence-only).

The probe *set* is generic (hostname, nvidia-smi, sinfo, an allocation-balance
command); the *values* are whatever this machine reports — so the same code
advertises any Slurm cluster.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_GPU_TYPES = ("gh200", "h200", "h100", "a100", "l40s", "l40", "a40",
              "v100", "a10", "rtx", "mi300", "mi250", "mi210")
_DEFAULT_ENV_PRESENCE = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def _run(cmd, timeout, shell=False) -> tuple[bool, str]:
    try:
        out = subprocess.run(cmd, shell=shell, text=True, capture_output=True,
                             timeout=timeout)
    except FileNotFoundError:
        return False, "command not found"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:  # never let a probe crash collection
        return False, str(e)[:200]
    txt = (out.stdout or "").strip() or (out.stderr or "").strip()
    return out.returncode == 0, txt


# -- individual probes (each returns a small JSON-able value) -------------

def _probe_host(timeout) -> dict:
    ok, host = _run(["hostname", "-f"], timeout)
    d: dict = {"hostname": host if ok else None}
    try:
        rel = {}
        for line in open("/etc/os-release"):
            if "=" in line:
                k, _, v = line.partition("=")
                rel[k] = v.strip().strip('"')
        d["os"] = rel.get("PRETTY_NAME")
    except OSError:
        d["os"] = None
    _, kern = _run(["uname", "-r"], timeout)
    d["kernel"] = kern or None
    ok, lscpu = _run(["lscpu"], timeout)
    if ok:
        def g(key):
            m = re.search(rf"^{re.escape(key)}:\s*(.+)$", lscpu, re.M)
            return m.group(1).strip() if m else None
        d["cpu_model"] = g("Model name")
        d["cpus"] = _int(g("CPU(s)"))
        d["sockets"] = _int(g("Socket(s)"))
        d["cores_per_socket"] = _int(g("Core(s) per socket"))
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                d["mem_gb"] = round(int(line.split()[1]) / 1024 / 1024)
                break
    except OSError:
        pass
    return d


def _probe_gpu_local(timeout) -> str:
    ok, out = _run(["nvidia-smi", "-L"], timeout)
    return out if (ok and out) else "NONE"


def _probe_scheduler(timeout) -> dict:
    ok, out = _run(["sinfo", "--version"], timeout)
    if not ok:
        return {"type": None}
    return {"type": out.split()[0] if out else "slurm", "version": out}


def _probe_partitions(timeout) -> list[dict]:
    ok, out = _run(["sinfo", "-s", "-h", "-o", "%P|%a|%F"], timeout)
    if not ok:
        return []
    parts = []
    for line in out.splitlines():
        f = line.split("|")
        if len(f) < 3:
            continue
        parts.append({"partition": f[0], "avail": f[1], "nodes_aiot": f[2]})
    return parts


def _probe_gpus(timeout) -> list[dict]:
    ok, out = _run(["sinfo", "-N", "-h", "-O", "Gres:24,Features:70,StateLong:14"],
                   timeout)
    if not ok:
        return []
    agg: dict[str, dict] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        gres, state = parts[0], parts[-1]
        feat = " ".join(parts[1:-1]).lower()
        if "gpu" not in gres.lower():
            continue
        m = re.search(r"gpu:(\d+)", gres)
        ngpu = int(m.group(1)) if m else 0
        gtype = next((g for g in _GPU_TYPES if g in feat), "unknown")
        a = agg.setdefault(gtype, {"type": gtype, "nodes": 0, "gpus": 0,
                                   "idle_nodes": 0})
        a["nodes"] += 1
        a["gpus"] += ngpu
        if state.startswith("idle"):
            a["idle_nodes"] += 1
    return sorted(agg.values(), key=lambda x: -x["gpus"])


def _probe_accounts(timeout) -> list[dict]:
    for cmd in (["accounts", "balance"], ["rcchelp", "balance"]):
        ok, out = _run(cmd, timeout)
        if not ok or not out:
            continue
        rows = []
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 4 and _int(f[1]) is not None and _int(f[-1]) is not None:
                rows.append({"account": f[0], "allocation": _int(f[1]),
                             "usage": _int(f[-2]), "balance": _int(f[-1])})
        if rows:
            return rows
    return []


_PROBES = {
    "host": _probe_host,
    "gpu_local": _probe_gpu_local,
    "scheduler": _probe_scheduler,
    "partitions": _probe_partitions,
    "gpus": _probe_gpus,
    "accounts": _probe_accounts,
}


def collect(timeout: int, env_names: tuple[str, ...]) -> dict:
    started = time.time()
    results: dict = {}
    meta: dict = {}
    with ThreadPoolExecutor(max_workers=len(_PROBES)) as ex:
        futs = {ex.submit(_timed, fn, timeout): key for key, fn in _PROBES.items()}
        for fut in as_completed(futs):
            key = futs[fut]
            value, took_ms, err = fut.result()
            results[key] = value
            meta[key] = {"took_ms": took_ms, "error": err}
    snap = {
        "ready": True,
        "collected_at": started,
        "took_ms": int((time.time() - started) * 1000),
        **results,
        "env_present": {n: (n in os.environ and bool(os.environ[n]))
                        for n in env_names},
        "_probes": meta,
    }
    snap["summary"] = _summary(snap)
    return snap


def _timed(fn, timeout):
    t0 = time.time()
    try:
        v = fn(timeout)
        return v, int((time.time() - t0) * 1000), None
    except Exception as e:
        return None, int((time.time() - t0) * 1000), str(e)[:200]


def _summary(s: dict) -> str:
    h = s.get("host") or {}
    bits = [h.get("hostname") or "?"]
    if h.get("os"):
        bits.append(h["os"].replace("Red Hat Enterprise Linux", "RHEL"))
    if h.get("cpus"):
        bits.append(f"{h['cpus']} CPU/{h.get('mem_gb','?')}GB")
    bits.append("no local GPU" if s.get("gpu_local") == "NONE" else "local GPU")
    sch = s.get("scheduler") or {}
    if sch.get("type"):
        bits.append(sch.get("version") or sch["type"])
    gpus = s.get("gpus") or []
    if gpus:
        bits.append("GPU nodes: " + ", ".join(
            f"{g['type']}×{g['gpus']}" for g in gpus[:6] if g["type"] != "unknown"))
    accts = s.get("accounts") or []
    if accts:
        top = max(accts, key=lambda a: a.get("balance") or 0)
        bits.append(f"balance {top['account']} {top['balance']} SU")
    return " · ".join(b for b in bits if b)


def _int(x):
    try:
        return int(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


class ClusterInfo:
    """Holds the latest snapshot; probes in the background so reads are instant."""

    def __init__(self, timeout: int, env_names: tuple[str, ...]) -> None:
        self._timeout = timeout
        self._env = env_names
        self._lock = threading.Lock()
        self._snap: dict = {"ready": False, "status": "probing"}
        self._inflight = False

    def start_async(self) -> None:
        self.refresh_async()

    def refresh_async(self) -> None:
        with self._lock:
            if self._inflight:
                return
            self._inflight = True
        threading.Thread(target=self._collect, name="cluster-probe",
                         daemon=True).start()

    def _collect(self) -> None:
        try:
            snap = collect(self._timeout, self._env)
        except Exception as e:  # keep last good snapshot on failure
            snap = None
            err = str(e)[:200]
        with self._lock:
            self._inflight = False
            if snap is not None:
                self._snap = snap
            elif not self._snap.get("ready"):
                self._snap = {"ready": False, "status": "error", "error": err}

    def get(self) -> dict:
        with self._lock:
            return dict(self._snap)
