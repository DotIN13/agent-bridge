"""`ab-serve`: the launcher the dashboard's ssh line runs on the far side.

The interesting assertions are all about restraint. It must not start a second
gateway in front of a working one, must not kill whatever else is holding the
port, and must not take the gateway down with it when the connection drops --
that last one is the difference between a closed laptop costing a tunnel and
costing every running job.
"""
from __future__ import annotations

import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gateway import serve  # noqa: E402


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Health(http.server.BaseHTTPRequestHandler):
    """A gateway's `/health`, and nothing else it does not need."""

    ok = True

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({"ok": self.ok, "version": "0.3.0"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def serving():
    """A gateway that is already up, on a port of its own."""
    server = http.server.HTTPServer(("127.0.0.1", 0), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def config_at(tmp_path: Path, port: int) -> Path:
    """The smallest config `gateway.config.load` accepts, on a chosen port."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\n'
        f'[auth]\ntoken = "test-token"\n', encoding="utf-8")
    return path


def fake_gateway(tmp_path: Path, body: str) -> Path:
    """A stand-in for `agent-bridge`, so a test can decide how it behaves."""
    path = tmp_path / "fake-gateway"
    path.write_text(f"#!/usr/bin/env python3\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_health_reads_the_gateway_s_own_answer_and_nothing_else(serving):
    assert serve.health(serving)["version"] == "0.3.0"
    # A port with nothing on it is `None`, not an exception: every caller here
    # treats "no answer" as a state rather than a failure.
    assert serve.health(free_port(), timeout=0.5) is None


def test_a_gateway_that_is_already_serving_is_left_alone(tmp_path, serving, capsys):
    # The `--agent-bridge` here would fail loudly if it were ever run, which is
    # the assertion: nothing starts a second gateway in front of a live one.
    never = fake_gateway(tmp_path, "raise SystemExit('ab-serve started a second gateway')\n")
    config = config_at(tmp_path, serving)

    code = serve.main(["--config", str(config), "--agent-bridge", str(never), "--no-park"])

    assert code == 0
    assert "already serving" in capsys.readouterr().out


def test_a_port_held_by_something_else_is_reported_and_not_killed(tmp_path, capsys):
    """The one case where doing nothing is the whole feature.

    Somebody's notebook on 8787 is somebody's notebook. A launcher that frees
    the port it wants is a launcher that eventually frees the wrong one.
    """
    squatter = subprocess.Popen(
        [sys.executable, "-c",
         "import socket, time\n"
         "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\n"
         "print(s.getsockname()[1], flush=True)\n"
         "time.sleep(30)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        port = int(squatter.stdout.readline().strip())
        config = config_at(tmp_path, port)
        never = fake_gateway(tmp_path, "raise SystemExit('started against a held port')\n")

        code = serve.main(["--config", str(config), "--agent-bridge", str(never), "--no-park"])

        assert code == 1
        assert "not touching it" in capsys.readouterr().out
        assert squatter.poll() is None, "ab-serve killed the process holding the port"
    finally:
        squatter.terminate()
        squatter.wait(timeout=10)


def test_a_gateway_that_starts_is_waited_for_and_announced(tmp_path, capsys):
    port = free_port()
    config = config_at(tmp_path, port)
    # Serves `/health` after a beat, the way a real one does once uvicorn binds.
    gateway = fake_gateway(tmp_path, f"""
import http.server, json, time
time.sleep(0.4)

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({{"ok": True, "version": "0.3.0"}}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

http.server.HTTPServer(("127.0.0.1", {port}), H).serve_forever()
""")

    code = serve.main(["--config", str(config), "--agent-bridge", str(gateway),
                       "--start-timeout", "20", "--no-park"])

    out = capsys.readouterr().out
    assert code == 0, out
    assert f"serving on {port}" in out
    # Started detached, so it is still there after the launcher has returned --
    # which is the property that keeps a closed laptop from killing jobs.
    assert serve.health(port) is not None
    _kill_listener(port)


def test_a_gateway_that_dies_exits_non_zero_with_its_own_log_tail(tmp_path, capsys):
    """The reason has to travel back over the ssh, or the row is red for nothing."""
    port = free_port()
    config = config_at(tmp_path, port)
    gateway = fake_gateway(tmp_path, """
import sys
print("Traceback (most recent call last):", file=sys.stderr)
print("ValueError: allowed_dirs is empty", file=sys.stderr)
raise SystemExit(1)
""")

    code = serve.main(["--config", str(config), "--agent-bridge", str(gateway),
                       "--start-timeout", "6", "--no-park"])

    out = capsys.readouterr().out
    assert code == 1
    assert "did not answer" in out
    # Not just "it failed": the line that says why, lifted out of the log.
    assert "allowed_dirs is empty" in out


def test_the_log_tail_is_this_attempt_s_and_not_an_older_one(tmp_path, capsys):
    port = free_port()
    config = config_at(tmp_path, port)
    log = tmp_path / "gateway.log"
    log.write_text("a failure from last week\n", encoding="utf-8")
    # `data_dir` is the config's parent, which is where the launcher looks.
    gateway = fake_gateway(tmp_path, """
import sys
print("today's problem", file=sys.stderr)
raise SystemExit(1)
""")

    serve.main(["--config", str(config), "--agent-bridge", str(gateway),
                "--start-timeout", "6", "--no-park"])

    out = capsys.readouterr().out
    assert "today's problem" in out
    assert "last week" not in out


def test_a_missing_binary_is_a_message_rather_than_a_traceback(tmp_path, capsys):
    config = config_at(tmp_path, free_port())
    code = serve.main(["--config", str(config),
                       "--agent-bridge", str(tmp_path / "not-here"), "--no-park"])
    assert code == 1
    assert "could not run" in capsys.readouterr().out


def test_a_config_that_is_not_there_fails_before_anything_starts(tmp_path, capsys):
    code = serve.main(["--config", str(tmp_path / "nope.toml"), "--no-park"])
    assert code == 2
    assert "config not found" in capsys.readouterr().err


def test_parking_holds_until_a_signal_and_leaves_the_gateway_running(tmp_path, serving, capsys):
    """What the ssh connection actually rides on.

    Run as a real child so the signal path is the real one: the holder gets a
    SIGTERM the way it would when ssh goes away, and has to exit 0 without
    taking the gateway with it.
    """
    config = config_at(tmp_path, serving)
    holder = subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "ab-serve"), "--config", str(config),
         "--interval", "1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        cwd=str(tmp_path))
    try:
        deadline = time.monotonic() + 15
        while holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
            if _reads(holder, "already serving"):
                break
        assert holder.poll() is None, "the holder exited instead of parking"

        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=15)
        assert holder.returncode == 0
        assert "leaving the gateway running" in holder.stdout.read()
        # Still there: the holder held the connection, it did not own the server.
        assert serve.health(serving) is not None
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


def test_a_gateway_that_goes_away_while_parked_is_restarted_then_given_up_on(tmp_path, capsys):
    """A restart is worth trying; an endless restart loop is worth exiting over.

    No gateway ever answers here, so the first health check fails, the restart
    fails, and the holder exits non-zero rather than flapping in silence.
    """
    port = free_port()
    config = config_at(tmp_path, port)
    # Answers once, so `ensure_serving` succeeds, then goes away for good.
    server = http.server.HTTPServer(("127.0.0.1", port), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = fake_gateway(tmp_path, "raise SystemExit(1)\n")

    def stop_serving():
        time.sleep(1.0)
        server.shutdown()
        server.server_close()

    threading.Thread(target=stop_serving, daemon=True).start()
    code = serve.main(["--config", str(config), "--agent-bridge", str(gateway),
                       "--interval", "0.5", "--start-timeout", "2",
                       "--max-restarts", "1"])

    out = capsys.readouterr().out
    assert code == 1, out
    assert "stopped answering" in out


def test_the_gateway_command_prefers_path_and_falls_back_to_this_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(serve.shutil, "which", lambda _name: None)
    assert serve.gateway_command(None, None)[:3] == [sys.executable, "-m", "gateway"]

    monkeypatch.setattr(serve.shutil, "which", lambda _name: "/usr/bin/agent-bridge")
    assert serve.gateway_command("/tmp/config.toml", None) == \
        ["/usr/bin/agent-bridge", "--config", "/tmp/config.toml"]

    # An explicit binary wins over both, which is how a venv is named.
    assert serve.gateway_command(None, "/opt/venv/bin/agent-bridge") == \
        ["/opt/venv/bin/agent-bridge"]


def _reads(process: subprocess.Popen, needle: str) -> bool:
    """Non-blocking-ish peek: the holder prints one line then goes quiet."""
    if process.stdout is None:
        return False
    os.set_blocking(process.stdout.fileno(), False)
    try:
        chunk = process.stdout.read() or ""
    except (BlockingIOError, TypeError):
        chunk = ""
    finally:
        os.set_blocking(process.stdout.fileno(), True)
    return needle in chunk


def _kill_listener(port: int) -> None:
    """Stop a detached fake gateway a test started, by port rather than pattern.

    Never by command line: a `pkill -f` here matches the test runner's own
    arguments, which is a way to kill the suite from inside it.
    """
    for pid_dir in Path("/proc").glob("[0-9]*"):
        try:
            for fd in (pid_dir / "fd").iterdir():
                if not os.readlink(fd).startswith("socket:"):
                    continue
                inode = os.readlink(fd)[8:-1]
                if inode in _listening_inodes(port):
                    os.kill(int(pid_dir.name), signal.SIGTERM)
                    return
        except (OSError, ValueError):
            continue


def _listening_inodes(port: int) -> set[str]:
    inodes = set()
    try:
        lines = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError:
        return inodes
    for line in lines:
        fields = line.split()
        if len(fields) > 9 and fields[3] == "0A" and int(fields[1].split(":")[1], 16) == port:
            inodes.add(fields[9])
    return inodes
