"""Keeps the tunnels the operator asked for, up.

One loop, one second apart. It does three things and nothing else: start what is
wanted and not running, probe what is running, and reconcile against the config
file when that changes on disk. Each `Tunnel` owns its own process; this owns the
decision about whether it should exist.

The event stream is here rather than in the server because a change can happen
with nobody watching -- a tunnel dropping at 3am is the case this whole thing is
for -- and the UI reconnecting should be able to read what it missed.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque

from .config import ConfigError, GatewayEntry, Store
from .tunnel import Tunnel, TunnelError

#: How often to reconcile and probe. A second is cheap (one TCP connect per
#: tunnel) and is the difference between the UI feeling live and feeling stale.
TICK_SEC = 1.0

#: Endpoint probes are the only part of a tick that touches the network, so they
#: get their own interval -- a second of `/health` requests per gateway is rude
#: to a gateway and pointless besides.
PROBE_EVERY_SEC = 3.0

MAX_EVENTS = 500


class Supervisor:
    def __init__(self, store: Store, *, probe=None) -> None:
        self.store = store
        self._probe_fn = probe or _default_probe
        self._lock = threading.RLock()
        self._tunnels: dict[str, Tunnel] = {}
        self._entries: dict[str, GatewayEntry] = {}
        self._events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self._seq = itertools.count(1)
        self._waiters: list[threading.Event] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_probe = 0.0
        self._mtime = 0.0
        self.reload()

    # -- config -----------------------------------------------------------
    def reload(self) -> None:
        """Re-read the file and reconcile the set of tunnels against it.

        A tunnel whose command changed is stopped and replaced rather than
        patched: the running process is the *old* command, and pretending
        otherwise is how a UI comes to show a green light for a forward nobody
        configured any more.
        """
        entries = {entry.name: entry for entry in self.store.entries()}
        try:
            self._mtime = self.store.path.stat().st_mtime
        except OSError:
            self._mtime = 0.0
        with self._lock:
            for name in list(self._tunnels):
                entry = entries.get(name)
                if entry is None or not entry.tunnelled:
                    self._tunnels.pop(name).request_down()
                    self._emit("removed", name)
                elif entry.ssh != self._tunnels[name].argv:
                    old = self._tunnels.pop(name)
                    wanted = old.wanted
                    old.request_down()
                    self._emit("command_changed", name)
                    tunnel = self._make(entry)
                    self._tunnels[name] = tunnel
                    if wanted:
                        tunnel.request_up()
            for name, entry in entries.items():
                if entry.tunnelled and name not in self._tunnels:
                    tunnel = self._make(entry)
                    self._tunnels[name] = tunnel
                    if entry.autostart:
                        tunnel.request_up()
                        self._emit("autostart", name)
            self._entries = entries

    def _make(self, entry: GatewayEntry) -> Tunnel:
        return Tunnel(entry.name, entry.ssh, on_change=self._on_change,
                      probe=lambda base=entry.base_url: self._probe_fn(base))

    def entry(self, name: str) -> GatewayEntry | None:
        with self._lock:
            return self._entries.get(name)

    # -- tunnels ----------------------------------------------------------
    def tunnel(self, name: str) -> Tunnel | None:
        with self._lock:
            return self._tunnels.get(name)

    def rows(self) -> list[dict]:
        """One row per configured gateway, tunnelled or not.

        A gateway with no `ssh` command still appears: it is reachable or it is
        not, and the answer belongs on the same page rather than in a second one.
        """
        problems = self.store.problems()
        with self._lock:
            entries = list(self._entries.values())
            tunnels = dict(self._tunnels)
        out = []
        for entry in entries:
            row = {"gateway": entry.public(),
                   "default": entry.name == self.store.default,
                   "problem": problems.get(entry.name, "")}
            tunnel = tunnels.get(entry.name)
            if tunnel is not None:
                row["tunnel"] = tunnel.snapshot().public()
                row["tunnel"]["wanted"] = tunnel.wanted
            else:
                row["tunnel"] = None
                row["endpoint"] = self._probe_fn(entry.base_url) \
                    if entry.base_url else {}
            out.append(row)
        return out

    def up(self, name: str) -> None:
        tunnel = self._require(name)
        tunnel.request_up()
        self._emit("up_requested", name)
        tunnel.start()

    def down(self, name: str) -> None:
        tunnel = self._require(name)
        tunnel.request_down()
        self._emit("down_requested", name)

    def restart(self, name: str) -> None:
        tunnel = self._require(name)
        tunnel.request_down()
        tunnel.request_up()
        self._emit("restart_requested", name)
        tunnel.start()

    def answer(self, name: str, text: str) -> None:
        self._require(name).answer(text)

    def _require(self, name: str) -> Tunnel:
        tunnel = self.tunnel(name)
        if tunnel is None:
            entry = self.entry(name)
            if entry is None:
                raise TunnelError(f"unknown gateway {name!r}")
            problem = self.store.problems().get(name)
            raise TunnelError(
                problem or f"gateway {name!r} has no ssh command to run; add "
                           f"one and it becomes a tunnel")
        return tunnel

    # -- events -----------------------------------------------------------
    def _emit(self, kind: str, name: str, **extra) -> None:
        event = {"seq": next(self._seq), "at": time.time(), "type": kind,
                 "name": name, **extra}
        with self._lock:
            self._events.append(event)
            waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            waiter.set()

    def _on_change(self, tunnel: Tunnel) -> None:
        snap = tunnel.snapshot()
        self._emit("state", tunnel.name, state=snap.state,
                   prompt=snap.prompt, prompt_secret=snap.prompt_secret)

    def events_after(self, seq: int) -> list[dict]:
        with self._lock:
            return [event for event in self._events if event["seq"] > seq]

    def wait_for_event(self, seq: int, timeout: float) -> list[dict]:
        """Block until there is something after `seq`, or the timeout.

        Long-polling rather than a per-client queue: a page that reconnects asks
        for everything after the last sequence it saw and cannot miss a
        transition in the gap, which a fan-out queue would have to be careful
        about.
        """
        pending = self.events_after(seq)
        if pending:
            return pending
        waiter = threading.Event()
        with self._lock:
            self._waiters.append(waiter)
        waiter.wait(timeout)
        with self._lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
        return self.events_after(seq)

    # -- the loop ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="bridge-supervisor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            tunnels = list(self._tunnels.values())
        for tunnel in tunnels:
            # Deliberate: the tunnels are this process's children, so leaving
            # them would leave orphaned ssh holding local ports that the next
            # run cannot bind.
            tunnel.request_down()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SEC):
            try:
                self.tick()
            except Exception as exc:                    # never die on a tick
                self._emit("error", "", detail=str(exc))

    def tick(self) -> None:
        self._reload_if_changed()
        with self._lock:
            tunnels = list(self._tunnels.values())
        for tunnel in tunnels:
            if tunnel.due():
                tunnel.start()
        now = time.time()
        if now - self._last_probe >= PROBE_EVERY_SEC:
            self._last_probe = now
            for tunnel in tunnels:
                tunnel.check()

    def _reload_if_changed(self) -> None:
        """Pick up an edit made in an editor, not just one made in the UI.

        The file is a human's, and the daemon holding a stale copy of it is the
        surprise worth avoiding -- `ab` would already be using the new one.
        """
        try:
            mtime = self.store.path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            fresh = Store.load(str(self.store.path), self.store.programs)
        except ConfigError as exc:
            self._mtime = mtime
            self._emit("config_error", "", detail=str(exc))
            return
        self.store.document = fresh.document
        self.reload()
        self._emit("config_reloaded", "")


def _default_probe(base_url: str) -> dict:
    """Is the local end of the forward answering?

    `probe_gateway` is the client's own reachability check, so the daemon and
    `ab gateways` agree about what "up" means -- including its classification of
    refused (forward down) against reset (forward up, gateway not serving),
    which is the distinction this UI most needs to show.
    """
    from client.abclient import probe_gateway
    return probe_gateway(base_url, timeout=2.0)
