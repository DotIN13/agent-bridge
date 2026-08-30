"""One ssh forward, supervised, on a pseudo-terminal.

The hard part is not spawning ssh; it is that the hosts this exists for want a
password *and* a Duo push, and a daemon has no terminal to be asked on. `ssh`
reads a password from `/dev/tty`, not stdin, so a plain pipe gets nothing --
the process just sits there looking healthy until it times out.

So the child gets a real pty. Whatever ssh writes goes into a bounded ring
buffer the UI reads, and when a line looks like a question the tunnel enters
`authenticating` and waits: the answer arrives from the browser, is written
straight to the master fd, and is never stored, echoed back, or logged. That is
the whole reason this is a pty and not a pipe.

Liveness is two independent facts, kept separate because they need opposite
fixes:

    process   is the ssh process alive?
    endpoint  does the local port actually answer? (`probe_gateway`)

"ssh alive, endpoint refused" is a forward that has not come up; "ssh alive,
endpoint reset" is a forward that is up with nothing serving behind it. Merging
them into one green light would hide the difference (see design/20).
"""
from __future__ import annotations

import errno
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

#: A ring, not a log. A tunnel left up for a week must not grow without bound,
#: and only the recent lines say anything about why it is unhappy.
MAX_LINES = 400

#: Lines that mean ssh is waiting for a human. Deliberately broad -- a missed
#: prompt is a tunnel that hangs looking fine, which is the failure this whole
#: module exists to avoid -- and each one is checked against the tail of the
#: output, since a prompt arrives with no trailing newline.
PROMPT_PATTERNS = (
    re.compile(r"password.*:\s*$", re.I),
    re.compile(r"passphrase.*:\s*$", re.I),
    re.compile(r"passcode.*:\s*$", re.I),
    re.compile(r"verification code.*:\s*$", re.I),
    re.compile(r"duo.*:\s*$", re.I),
    re.compile(r"\(yes/no(/\[fingerprint\])?\)\?\s*$", re.I),
    re.compile(r"enter a passcode or select one of the following options",
               re.I),
    re.compile(r"^\s*passcode or option \(\d+-\d+\):\s*$", re.I),
    re.compile(r"one-time password.*:\s*$", re.I),
)

#: A prompt whose answer is a secret. Used only to decide whether the UI masks
#: the input box -- the answer is withheld from the buffer either way.
SECRET_PATTERNS = (
    re.compile(r"password", re.I),
    re.compile(r"passphrase", re.I),
)

STATES = ("stopped", "starting", "authenticating", "up", "retrying", "failed")

#: Backoff between automatic restarts. Ends flat rather than growing forever: a
#: laptop that closed its lid should come back within a minute of waking, not
#: after an hour of doubling.
BACKOFF_SEC = (2, 5, 10, 30, 60)


@dataclass
class Line:
    """One chunk of the child's output, as the UI reads it."""

    seq: int
    at: float
    text: str
    #: `stderr` here means ssh's own diagnostics; on a pty the two streams are
    #: one, so this marks lines *we* wrote about the process instead.
    kind: str = "out"


@dataclass
class Snapshot:
    """Everything the UI shows for one tunnel, with no secrets in it."""

    name: str
    state: str
    pid: int | None
    since: float
    attempts: int
    prompt: str
    prompt_secret: bool
    last_error: str
    endpoint: dict = field(default_factory=dict)
    next_retry_in: float = 0.0

    def public(self) -> dict:
        return {
            "name": self.name, "state": self.state, "pid": self.pid,
            "since": self.since, "uptime_sec": round(max(0.0, time.time() - self.since), 1),
            "attempts": self.attempts, "prompt": self.prompt,
            "prompt_secret": self.prompt_secret, "last_error": self.last_error,
            "endpoint": self.endpoint,
            "next_retry_in": round(self.next_retry_in, 1),
        }


class Tunnel:
    """A supervised ssh child for one gateway.

    Owns its process and its output; knows nothing about HTTP. `supervisor.py`
    decides *when* it should be up, this decides *how*.
    """

    def __init__(self, name: str, argv: tuple[str, ...], *,
                 on_change=None, probe=None) -> None:
        self.name = name
        self.argv = argv
        self._on_change = on_change or (lambda *_: None)
        self._probe = probe
        self._lock = threading.RLock()
        self._lines: deque[Line] = deque(maxlen=MAX_LINES)
        self._seq = 0
        self._proc: subprocess.Popen | None = None
        self._master: int | None = None
        self._reader: threading.Thread | None = None
        self._state = "stopped"
        self._since = time.time()
        self._prompt = ""
        self._prompt_secret = False
        self._last_error = ""
        self._attempts = 0
        self._wanted = False
        self._retry_at = 0.0
        self._endpoint: dict = {}
        self._tail = ""

    # -- state ------------------------------------------------------------
    @property
    def wanted(self) -> bool:
        """Should this be up? Set by the UI, honoured by the supervisor."""
        with self._lock:
            return self._wanted

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                name=self.name, state=self._state,
                pid=self._proc.pid if self._proc and self._proc.poll() is None else None,
                since=self._since, attempts=self._attempts, prompt=self._prompt,
                prompt_secret=self._prompt_secret, last_error=self._last_error,
                endpoint=dict(self._endpoint),
                next_retry_in=max(0.0, self._retry_at - time.time()))

    def lines(self, after: int = 0) -> list[Line]:
        with self._lock:
            return [line for line in self._lines if line.seq > after]

    def _set_state(self, state: str, *, error: str = "") -> None:
        with self._lock:
            if state == self._state and not error:
                return
            self._state = state
            self._since = time.time()
            if error:
                self._last_error = error
            if state in ("up", "stopped"):
                self._prompt = ""
                self._prompt_secret = False
        self._on_change(self)

    def _say(self, text: str, kind: str = "note") -> None:
        """Put a line of *our own* into the buffer, so the UI's console reads as
        one story rather than ssh's half of it."""
        with self._lock:
            self._seq += 1
            self._lines.append(Line(seq=self._seq, at=time.time(), text=text,
                                    kind=kind))
        self._on_change(self)

    # -- lifecycle --------------------------------------------------------
    def request_up(self) -> None:
        with self._lock:
            self._wanted = True
            self._retry_at = 0.0
            self._attempts = 0

    def request_down(self) -> None:
        with self._lock:
            self._wanted = False
        self.kill("stopped by request")

    def start(self) -> None:
        """Spawn ssh on a pty. Safe to call when already running -- it no-ops.

        Imported lazily because `pty` is POSIX-only and the rest of this module
        is worth having on Windows for the config half.
        """
        import pty
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._attempts += 1
            attempt = self._attempts
        self._set_state("starting")
        self._say(f"$ {' '.join(self.argv)}", kind="cmd")
        try:
            master, slave = pty.openpty()
        except OSError as exc:
            self._set_state("failed", error=f"cannot allocate a pty: {exc}")
            return
        try:
            proc = subprocess.Popen(
                list(self.argv), stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True,
                # `start_new_session` alone is not enough. It calls `setsid()`,
                # which gives the child its own process group -- needed so
                # `kill` can take the whole tree -- but leaves it with *no*
                # controlling terminal, and `open("/dev/tty")` then fails.
                # OpenSSH asks for a password on /dev/tty first and only falls
                # back to stdin, and a wrapper like `sshpass` does not fall back
                # at all, so the tty has to be claimed explicitly.
                #
                # `preexec_fn` in a threaded process is normally to be avoided;
                # this is the one documented way to do it, and the body is a
                # single ioctl on a descriptor that is already open.
                preexec_fn=_claim_terminal,
                env={**os.environ,
                     # Never let ssh reach for a GUI asker: the answer has to
                     # come back through this pty or the UI cannot relay it.
                     "SSH_ASKPASS_REQUIRE": "never",
                     "DISPLAY": ""})
        except OSError as exc:
            os.close(master)
            os.close(slave)
            self._set_state("failed", error=f"cannot start: {exc}")
            return
        finally:
            try:
                os.close(slave)
            except OSError:
                pass
        # Echo off, immediately. The echo is the *pty's* doing, not the
        # child's: without this the line discipline copies whatever we write
        # back onto the master, and a password we relayed would appear in the
        # console the UI renders -- put there by us, not by ssh. ssh disables
        # echo for its own password prompts, but a wrapper may not, and this is
        # not a thing to leave to the child.
        _no_echo(master)
        with self._lock:
            self._proc = proc
            self._master = master
            self._tail = ""
        self._reader = threading.Thread(
            target=self._read_loop, args=(proc, master, attempt), daemon=True,
            name=f"tunnel-{self.name}")
        self._reader.start()

    def kill(self, why: str = "") -> None:
        """Stop the child, its group with it.

        The group matters: `ssh -N` with a `ProxyCommand`, or `autossh`, leaves
        children that would otherwise keep the local port bound and make the
        next start fail with "address already in use".
        """
        with self._lock:
            proc, master = self._proc, self._master
            self._proc = self._master = None
        if proc is not None and proc.poll() is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
                except (ProcessLookupError, PermissionError, OSError):
                    break
                try:
                    proc.wait(timeout=3)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        if why:
            self._say(why)
        self._set_state("stopped" if not self.wanted else "retrying")

    def answer(self, text: str) -> None:
        """Send a prompt's answer to the child, and keep no copy of it.

        Written raw plus a newline. Nothing is added to the ring buffer: on a pty
        ssh disables echo for a password, so the only way the secret could reach
        the UI's console is if this put it there.
        """
        with self._lock:
            master = self._master
            waiting = self._state == "authenticating"
        if master is None:
            raise TunnelError("this tunnel has no live process to answer")
        payload = (text + "\n").encode("utf-8", errors="replace")
        try:
            # Again here, in case the child turned echo back on for an earlier
            # non-secret prompt. Cheap, and the failure it prevents is a
            # password in a web page.
            _no_echo(master)
            os.write(master, payload)
        except OSError as exc:
            raise TunnelError(f"cannot write to the tunnel: {exc}") from exc
        finally:
            del payload
        self._say("(answered)" if waiting else "(sent input)")
        with self._lock:
            self._prompt = ""
            self._prompt_secret = False
        self._set_state("starting")

    # -- the reader -------------------------------------------------------
    def _read_loop(self, proc: subprocess.Popen, master: int,
                   attempt: int) -> None:
        """Drain the pty until the child goes, classifying what comes out."""
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                # EIO on a pty master is the normal "the slave side closed",
                # i.e. the child exited. Anything else is worth recording.
                if exc.errno not in (errno.EIO, errno.EBADF):
                    self._say(f"read error: {exc}")
                break
            if not chunk:
                break
            self._ingest(chunk.decode("utf-8", errors="replace"))
        code = proc.wait()
        self._on_exit(code, attempt)

    def _ingest(self, text: str) -> None:
        """Buffer output by line, and treat an unterminated tail as a prompt.

        The tail is the whole trick: `password: ` arrives with no newline, so a
        line-buffered reader would hold it until the login timed out. Every
        partial tail is tested against the prompt patterns.
        """
        with self._lock:
            self._tail += text.replace("\r\n", "\n").replace("\r", "\n")
            parts = self._tail.split("\n")
            self._tail = parts.pop()
            kept: list[str] = []
            for part in parts:
                line = part.rstrip()
                if not line.strip():
                    continue
                # A prompt reaches the buffer the moment it appears, as an
                # unterminated tail. When the newline finally arrives the same
                # text comes round again -- so drop it rather than showing every
                # question twice. `kept` then carries only the lines that were
                # genuinely new, which is also what may raise a prompt: without
                # that, the terminated copy of a question just answered re-asked
                # it, putting the tunnel back into `authenticating` for a prompt
                # nothing was waiting on.
                #
                # A real second ask survives this: ssh prints "Permission
                # denied, please try again." in between, so the repeat is no
                # longer among the recent lines.
                if any(seen.text == line and seen.kind == "prompt"
                       for seen in list(self._lines)[-4:]):
                    continue
                self._seq += 1
                self._lines.append(Line(seq=self._seq, at=time.time(),
                                        text=line))
                kept.append(line)
            tail = self._tail
        changed = False
        for part in kept:
            if self._looks_like_prompt(part):
                self._raise_prompt(part)
                changed = True
        if tail.strip() and self._looks_like_prompt(tail):
            self._raise_prompt(tail)
            changed = True
        if not changed:
            self._on_change(self)

    @staticmethod
    def _looks_like_prompt(text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        return any(pattern.search(stripped) for pattern in PROMPT_PATTERNS)

    def _raise_prompt(self, text: str) -> None:
        prompt = text.strip()
        secret = any(pattern.search(prompt) for pattern in SECRET_PATTERNS)
        with self._lock:
            self._prompt = prompt
            self._prompt_secret = secret
            if prompt not in [line.text for line in list(self._lines)[-3:]]:
                self._seq += 1
                self._lines.append(Line(seq=self._seq, at=time.time(),
                                        text=prompt, kind="prompt"))
        self._set_state("authenticating")

    def _on_exit(self, code: int, attempt: int) -> None:
        with self._lock:
            if attempt != self._attempts:
                return          # a newer start has already taken over
            self._proc = None
            master, self._master = self._master, None
            wanted = self._wanted
            tail = [line.text for line in list(self._lines)[-4:]
                    if line.kind == "out"]
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        # A negative code is a signal, and `code -9` reads like a mystery where
        # "killed by SIGKILL" reads like what happened. The last line of output
        # is labelled rather than appended: on a killed process it is whatever
        # ssh last said while healthy, and "exited: Success. Logging you in"
        # is a sentence that means the opposite of itself.
        if code < 0:
            reason = f"ssh was killed by signal {-code}"
        else:
            reason = f"ssh exited with code {code}"
        if tail:
            reason += f"; last output: {tail[-1][:200]}"
        self._say(reason)
        if not wanted:
            self._set_state("stopped")
            return
        delay = BACKOFF_SEC[min(attempt - 1, len(BACKOFF_SEC) - 1)]
        with self._lock:
            self._retry_at = time.time() + delay
        self._set_state("retrying", error=reason)
        self._say(f"retrying in {delay}s")

    # -- health -----------------------------------------------------------
    def check(self) -> None:
        """Refresh the endpoint fact, and promote `starting` to `up`.

        A forward that is serving is the only evidence that matters: ssh's own
        output says nothing once it has connected, and `-N` prints nothing at
        all on success.
        """
        if self._probe is None:
            return
        result = self._probe()
        # `state: "up"` from `probe_gateway` means a healthy agent-bridge
        # answered. `reachable` alone is weaker -- something is listening -- and
        # is not enough: an ssh forward to the wrong port will happily connect
        # to whatever is there.
        serving = result.get("state") == "up"
        with self._lock:
            self._endpoint = result
            state = self._state
            alive = self._proc is not None and self._proc.poll() is None
        # Deliberately not `authenticating`: while ssh is asking a question the
        # forward is not carrying anything yet, so whatever is answering on that
        # port is something else -- another tunnel, a stale process, a service
        # that happens to live there. Promoting on it would clear the prompt and
        # leave the login unanswered, which is the exact failure this module is
        # built to avoid.
        if serving and alive and state in ("starting", "retrying", "up"):
            self._set_state("up")
        elif state == "up" and not serving:
            self._set_state("starting",
                            error=result.get("detail")
                            or f"endpoint went {result.get('state')}")

    def due(self) -> bool:
        """Is a retry owed?"""
        with self._lock:
            return (self._wanted and self._state in ("retrying", "stopped",
                                                     "failed")
                    and time.time() >= self._retry_at)


def _no_echo(fd: int) -> None:
    """Stop the pty from copying input back to us.

    Failure is ignored: a pty that will not take termios settings is not a
    reason to refuse to run, and the states that matter (`prompt_secret`, and
    never writing the answer to the buffer ourselves) hold regardless.
    """
    import termios
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO          # lflag
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (OSError, termios.error, IndexError):
        pass


def _claim_terminal() -> None:
    """Make our stdin the controlling terminal. Runs in the forked child.

    `setsid()` has already happened (`start_new_session`), so this session has
    no controlling terminal and `TIOCSCTTY` can claim one. Failure is swallowed:
    a program that does not need /dev/tty should still start, and one that does
    will say so on the pty we are reading.
    """
    import fcntl
    import termios
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


class TunnelError(RuntimeError):
    """An operation on a tunnel could not be carried out."""
