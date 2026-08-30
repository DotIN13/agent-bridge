"""ssh on a pty, because a pipe cannot answer a Duo push.

`ssh` reads a password from `/dev/tty`, not from stdin, so a daemon that spawns
it with pipes gets nothing: the process sits there looking healthy until the
login times out. The fake below behaves the same way on purpose — it opens
`/dev/tty` to prompt — so these tests fail if the pty is ever quietly replaced
with a pipe.
"""
from __future__ import annotations

import os
import stat
import time

import pytest

from bridge.tunnel import BACKOFF_SEC, Tunnel, TunnelError

pytestmark = pytest.mark.skipif(os.name == "nt",
                                reason="pty is POSIX-only, and so is ssh here")

#: Prompts on its controlling terminal, then holds the "forward" open. Writes
#: what it was told to a file so a test can prove the answer arrived.
FAKE_SSH = r'''#!/usr/bin/env python3
import os, sys, time
answers = os.environ["FAKE_SSH_ANSWERS"]
mode = os.environ.get("FAKE_SSH_MODE", "prompt")

if mode == "die":
    sys.stdout.write("ssh: connect to host midway5 port 22: Connection refused\n")
    sys.stdout.flush()
    raise SystemExit(255)

# Raw fd rather than `open(...)`: a tty is not seekable, so Python's buffered
# reader refuses it. Real ssh uses read(2)/write(2) here too.
fd = os.open("/dev/tty", os.O_RDWR)


def say(text):
    os.write(fd, text.encode())


def listen():
    out = b""
    while not out.endswith(b"\n"):
        chunk = os.read(fd, 1)
        if not chunk:
            break
        out += chunk
    return out.decode().rstrip("\n").rstrip("\r")


if mode == "prompt":
    say("tzhang3@midway5.rcc.uchicago.edu's password: ")
    open(answers, "a").write("password=" + listen() + "\n")
    say("\r\nDuo two-factor login for tzhang3\r\n\r\n")
    say("Passcode or option (1-3): ")
    open(answers, "a").write("duo=" + listen() + "\n")
    say("\r\nSuccess. Logging you in...\r\n")

open(answers + ".up", "w").write("up\n")
while True:
    time.sleep(0.05)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    path = tmp_path / "ssh"
    path.write_text(FAKE_SSH, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    answers = tmp_path / "answers"
    monkeypatch.setenv("FAKE_SSH_ANSWERS", str(answers))
    return Fake(path=path, answers=answers)


class Fake:
    def __init__(self, path, answers):
        self.path, self.answers = path, answers

    @property
    def argv(self):
        return (str(self.path), "-N", "-L", "8787:localhost:8787", "midway5")

    def said(self) -> list[str]:
        if not self.answers.is_file():
            return []
        return self.answers.read_text(encoding="utf-8").splitlines()

    @property
    def connected(self) -> bool:
        return (self.answers.parent / (self.answers.name + ".up")).exists()


def _wait(predicate, timeout=10.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_password_prompt_becomes_a_question_the_ui_can_answer(fake):
    """The whole reason this is a pty: with a pipe, nothing would arrive here."""
    tunnel = Tunnel("midway5", fake.argv)
    tunnel.request_up()
    tunnel.start()
    try:
        _wait(lambda: tunnel.state == "authenticating", what="the password prompt")
        snap = tunnel.snapshot()
        assert "password" in snap.prompt.lower()
        assert snap.prompt_secret is True, "the UI must mask this one"

        tunnel.answer("hunter2")
        _wait(lambda: "password=hunter2" in fake.said(), what="ssh to read it")
    finally:
        tunnel.request_down()


def test_a_duo_passcode_prompt_is_recognised_and_is_not_masked(fake):
    """`Passcode or option (1-3):` is the real prompt; the "Duo two-factor
    login" line above it is a banner and must not be mistaken for one."""
    tunnel = Tunnel("midway5", fake.argv)
    tunnel.request_up()
    tunnel.start()
    try:
        _wait(lambda: tunnel.state == "authenticating", what="the password prompt")
        tunnel.answer("hunter2")
        _wait(lambda: "option" in tunnel.snapshot().prompt.lower(),
              what="the Duo prompt")
        assert tunnel.snapshot().prompt_secret is False

        tunnel.answer("1")
        _wait(lambda: "duo=1" in fake.said(), what="ssh to read the passcode")
        _wait(lambda: fake.connected, what="the fake to report a connection")
    finally:
        tunnel.request_down()


def test_an_answer_never_reaches_the_output_buffer(fake):
    """A password in the console the UI renders would be a leak we put there
    ourselves — ssh does not echo it."""
    tunnel = Tunnel("midway5", fake.argv)
    tunnel.request_up()
    tunnel.start()
    try:
        _wait(lambda: tunnel.state == "authenticating")
        tunnel.answer("correct-horse-battery-staple")
        _wait(lambda: "password=correct-horse-battery-staple" in fake.said())
        time.sleep(0.2)
        blob = "\n".join(line.text for line in tunnel.lines())
        assert "correct-horse-battery-staple" not in blob
        assert "(answered)" in blob, "the fact of it is worth showing"
        # This passes because `_no_echo` turns the pty's echo off, not because
        # the fake is polite: it never touches termios. Re-enable echo and the
        # secret is in the buffer, which is what the assertion is guarding.
    finally:
        tunnel.request_down()


def test_answering_a_tunnel_with_no_process_is_a_typed_error(fake):
    tunnel = Tunnel("midway5", fake.argv)
    with pytest.raises(TunnelError) as exc:
        tunnel.answer("hunter2")
    assert "no live process" in str(exc.value)


def test_ssh_dying_schedules_a_retry_with_its_last_words(fake, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "die")
    tunnel = Tunnel("midway5", fake.argv)
    tunnel.request_up()
    tunnel.start()
    try:
        _wait(lambda: tunnel.state == "retrying", what="the retry decision")
        snap = tunnel.snapshot()
        assert "code 255" in snap.last_error
        assert "Connection refused" in snap.last_error, "ssh's own words"
        assert "last output:" in snap.last_error, \
            "labelled, not appended — on a killed process the last line is " \
            "whatever ssh said while it was still healthy"
        assert 0 < snap.next_retry_in <= BACKOFF_SEC[0]
        assert tunnel.due() is False, "not yet — the backoff has not elapsed"
    finally:
        tunnel.request_down()


def test_a_stopped_tunnel_is_not_restarted(fake, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "die")
    tunnel = Tunnel("midway5", fake.argv)
    tunnel.request_up()
    tunnel.start()
    _wait(lambda: tunnel.state == "retrying")
    tunnel.request_down()
    _wait(lambda: tunnel.state == "stopped", what="the stop")
    assert tunnel.due() is False


def test_stopping_takes_the_process_group_with_it(fake):
    """An orphaned `ssh -N` keeps the local port bound, and the next start then
    fails with "address already in use"."""
    tunnel = Tunnel("midway5", fake.argv)
    tunnel.request_up()
    tunnel.start()
    _wait(lambda: tunnel.snapshot().pid is not None, what="a pid")
    pid = tunnel.snapshot().pid
    tunnel.request_down()
    _wait(lambda: not _alive(pid), what="the process to be gone")
    assert tunnel.snapshot().pid is None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_the_endpoint_check_is_what_promotes_a_tunnel_to_up(fake):
    """`ssh -N` prints nothing on success, so its output cannot say the forward
    is working. Only the port answering does."""
    serving = {"state": "refused", "reachable": False}
    tunnel = Tunnel("midway5", fake.argv, probe=lambda: serving)
    tunnel.request_up()
    tunnel.start()
    try:
        _wait(lambda: tunnel.state == "authenticating")
        tunnel.answer("hunter2")
        _wait(lambda: "option" in tunnel.snapshot().prompt.lower())
        tunnel.answer("1")
        _wait(lambda: fake.connected)

        tunnel.check()
        assert tunnel.state != "up", "nothing is answering yet"

        serving.update({"state": "up", "reachable": True, "version": "0.3.0"})
        tunnel.check()
        assert tunnel.state == "up"
        assert tunnel.snapshot().endpoint["version"] == "0.3.0"
    finally:
        tunnel.request_down()


def test_something_listening_is_not_enough_to_be_up(fake):
    """A forward pointed at the wrong port connects happily to whatever is
    there. `reachable` alone would call that success."""
    result = {"state": "http_error", "reachable": True,
              "detail": "HTTP 404 from /health; is this an agent-bridge?"}
    tunnel = Tunnel("midway5", fake.argv, probe=lambda: result)
    tunnel.request_up()
    tunnel.start()
    try:
        _wait(lambda: tunnel.state == "authenticating")
        tunnel.check()
        assert tunnel.state != "up"
    finally:
        tunnel.request_down()


def test_an_endpoint_that_stops_answering_drops_a_tunnel_out_of_up(fake):
    result = {"state": "up", "reachable": True}
    tunnel = Tunnel("midway5", fake.argv, probe=lambda: result)
    tunnel.request_up()
    tunnel.start()
    try:
        _wait(lambda: tunnel.state == "authenticating")
        tunnel.answer("x")
        tunnel.check()
        _wait(lambda: tunnel.state == "up", what="up")
        result.update({"state": "reset", "reachable": False,
                       "detail": "forward is up but the gateway is not serving"})
        tunnel.check()
        assert tunnel.state == "starting"
        assert "not serving" in tunnel.snapshot().last_error
    finally:
        tunnel.request_down()


def test_a_command_that_cannot_be_executed_fails_without_raising():
    tunnel = Tunnel("nope", ("/nonexistent/ssh", "host"))
    tunnel.request_up()
    tunnel.start()
    assert tunnel.state == "failed"
    assert "cannot start" in tunnel.snapshot().last_error


def test_the_command_line_is_the_first_thing_in_the_console(fake):
    tunnel = Tunnel("midway5", fake.argv)
    tunnel.request_up()
    tunnel.start()
    try:
        _wait(lambda: tunnel.lines(), what="any output")
        first = tunnel.lines()[0]
        assert first.kind == "cmd"
        assert "8787:localhost:8787" in first.text
    finally:
        tunnel.request_down()


def test_a_signal_death_is_named_as_one(fake):
    """`ssh exited with code -9` reads like a mystery; "killed by signal 9"
    reads like a dropped connection, which is what it usually is."""
    tunnel = Tunnel("midway5", fake.argv)
    tunnel.request_up()
    tunnel.start()
    try:
        _wait(lambda: tunnel.snapshot().pid is not None, what="a pid")
        os.kill(tunnel.snapshot().pid, 9)
        _wait(lambda: tunnel.state == "retrying", what="the retry decision")
        assert "killed by signal 9" in tunnel.snapshot().last_error
    finally:
        tunnel.request_down()


def test_a_prompt_is_not_shown_twice(fake):
    """It reaches the buffer as an unterminated tail, then again when its
    newline arrives — the console should read as one question, not two."""
    tunnel = Tunnel("midway5", fake.argv)
    tunnel.request_up()
    tunnel.start()
    try:
        _wait(lambda: tunnel.state == "authenticating")
        tunnel.answer("pw")
        _wait(lambda: "option" in tunnel.snapshot().prompt.lower())
        tunnel.answer("1")
        _wait(lambda: fake.connected)
        time.sleep(0.2)
        texts = [line.text for line in tunnel.lines()]
        for prompt in ("tzhang3@midway5.rcc.uchicago.edu's password:",
                       "Passcode or option (1-3):"):
            assert texts.count(prompt) == 1, texts
    finally:
        tunnel.request_down()
