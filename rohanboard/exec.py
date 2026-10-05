"""Executor abstraction for rohanboard.

Collectors take an `Executor` so the same code path can target the local box
(WSL/laptop dev), a slurm login node, or — once Phase 4e wires it — an LRZ
cluster via asyncssh.

Design constraints (from `plans/async_ssh_redesign_v2.md`):
  1. Async-only. No `subprocess.run`. No threads.
  2. Persistent connection per cluster (asyncssh side).
  3. No main-loop blocking — every `run()` yields cleanly.
  4. Cancellation-safe: catch BaseException, kill+`asyncio.shield(wait)`,
     re-raise. See `feedback_asyncio_subprocess_cancel_leaks.md`.

LocalExecutor spawns one subprocess per `run()`. AsyncSSHExecutor keeps one
persistent `SSHClientConnection` with one long-lived remote shell on it and
runs every command through that shell — see the section comment above the
class for why (2026-10-05 rohan session pile-up).
"""
from __future__ import annotations

import asyncio
import shlex
import time
import uuid
from collections import deque
from typing import Callable, Protocol, Sequence, runtime_checkable

# ──────────────────────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────────────────────

#: Default per-call timeout. Slurm collectors that need longer override.
DEFAULT_RUN_TIMEOUT: float = 15.0

#: SSH handshake budget. LRZ ProxyJump cold can be ~17 s; give it room.
DEFAULT_CONNECT_TIMEOUT: float = 25.0


# ──────────────────────────────────────────────────────────────────────────
# Protocol
# ──────────────────────────────────────────────────────────────────────────

@runtime_checkable
class Executor(Protocol):
    """Run argv on the target host. Implementations: LocalExecutor,
    AsyncSSHExecutor, FakeLocalExecutor (tests)."""

    async def run(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """Run argv. Returns (returncode, stdout, stderr) as decoded text.

        Raises `asyncio.TimeoutError` if the timeout is exceeded; the
        underlying subprocess / ssh session is killed before the exception
        propagates so no orphans are left behind.

        Cancellation-safe: if the caller's task is cancelled while `run` is
        in flight, the underlying child is killed and reaped under
        `asyncio.shield` before the CancelledError propagates.
        """
        ...

    async def aclose(self) -> None:
        """Close any persistent resources (e.g. ssh connection). Idempotent."""
        ...


# ──────────────────────────────────────────────────────────────────────────
# LocalExecutor
# ──────────────────────────────────────────────────────────────────────────

class LocalExecutor:
    """Thin wrapper around `asyncio.create_subprocess_exec`. No persistent
    state; `aclose()` is a no-op."""

    def __init__(self) -> None:
        self._whoami: str | None = None

    async def resolve_whoami(self, timeout: float = 5.0) -> str:
        """Run `whoami` once, cache the result. Used to substitute
        `"self"` → resolved-username in slurm user filters; the cache
        means rohanboard's per-tick refresh doesn't pay a fork+exec
        each time. Returns the empty string on failure (caller treats
        as "no user filter")."""
        if self._whoami is None:
            try:
                rc, out, _err = await self.run(["whoami"], timeout=timeout)
                self._whoami = out.strip() if rc == 0 else ""
            except Exception:
                self._whoami = ""
        return self._whoami

    async def run(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        if timeout is None:
            timeout = DEFAULT_RUN_TIMEOUT
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except BaseException:
            # TimeoutError, CancelledError, KeyboardInterrupt — all need
            # the same cleanup. See feedback_asyncio_subprocess_cancel_leaks.md.
            try:
                proc.kill()
            except ProcessLookupError:
                # Already gone.
                pass
            # Shield the reap so the cancellation that brought us here
            # doesn't also cancel the wait — otherwise we'd leak a zombie.
            await asyncio.shield(proc.wait())
            raise
        return proc.returncode or 0, stdout.decode(), stderr.decode()

    async def aclose(self) -> None:
        return None


# ──────────────────────────────────────────────────────────────────────────
# AsyncSSHExecutor — one connection, one long-lived remote shell
# ──────────────────────────────────────────────────────────────────────────
#
# Why a single persistent shell instead of one ssh session per run()
# (2026-10-05 rohan session pile-up, ~/workspace/rohan-ssh-issue/):
# the board refreshes every 5 s, and each refresh used to open a NEW
# session on the persistent connection. Every session start makes sshd do
# per-session work in the user's home on the server (xauth writing
# ~/.Xauthority when X11 is forwarded, chdir $HOME, bash reading ~/.bashrc).
# While rohan's /rhome was frozen that work hung, a tick's cancel never
# freed the session, sshd's MaxSessions filled after ~10 ticks, the refused
# channel open looked like a dead connection, and the reconnect orphaned
# all 10 and logged in again — ~12 stuck sessions/min, ~900 by the time
# the admins noticed.
#
# Now the executor opens ONE shell when the link comes up and feeds every
# command through its stdin, framed by sentinels. The rules that keep the
# server-side footprint bounded no matter how the remote misbehaves:
#   1. X11 and agent forwarding are off (whatever ~/.ssh/config says).
#   2. At most one remote command is ever in flight. A run() that times out
#      or is cancelled leaves its command running; the next run() waits for
#      it to finish instead of starting another one (RemoteBusyError if it
#      doesn't finish within that call's timeout).
#   3. A session is only replaced once the remote side has closed it or the
#      connection is gone — never because it is slow.
#   4. Link attempts (connect + shell start) run as executor-owned tasks, so
#      a tick being cancelled can't abort a handshake half-way and start
#      another one 5 s later. After a disconnect the first reconnect is
#      immediate; repeated failures back off up to RECONNECT_MAX_DELAY.

#: Command run on the remote side to start the persistent shell. `exec`
#: replaces the login shell; --norc/--noprofile keep bash from re-reading
#: dotfiles on every (re)start.
SHELL_COMMAND = "exec bash --noprofile --norc"

#: Failed link attempts that are retried at the normal pace (the next
#: run() call, but no sooner than RECONNECT_BASE_DELAY) before the delay
#: starts doubling, and the cap on that delay (seconds).
RECONNECT_FAST_RETRIES = 3
RECONNECT_BASE_DELAY = 5.0
RECONNECT_MAX_DELAY = 300.0

# End-of-output marker: `\n` + ASCII record separator + tag, then a header
# (`<token> <rc>` on stdout, `<token>` on stderr) and `\n`. The leading
# newline is ours, so output without a trailing newline still frames cleanly.
_FRAME_TAG = "rohanboard-end:"
_FRAME_MARK = "\n\x1e" + _FRAME_TAG
_READ_CHUNK = 65536


def reconnect_delay(failures: int) -> float:
    """Seconds to wait before the next link attempt after `failures`
    consecutive failed ones: 5, 5, 5, 10, 20, 40, 80, 160, 300, 300, …"""
    if failures <= 0:
        return 0.0
    doublings = min(max(0, failures - RECONNECT_FAST_RETRIES), 16)
    return min(RECONNECT_BASE_DELAY * (2 ** doublings), RECONNECT_MAX_DELAY)


class SSHUnavailableError(ConnectionError):
    """Raised without touching the network while reconnects are backing off."""


class RemoteBusyError(RuntimeError):
    """The previous command on the remote shell is still running, so no new
    command (and no new session) was started."""


class RemoteSessionLost(ConnectionError):
    """The remote shell or its connection went away mid-command."""


def _frame_command(cmd: str, token: str) -> str:
    """One input line for the persistent shell: run `cmd` with stdin detached
    (so it can't swallow the commands queued behind it), then mark the end of
    its stdout (with the exit status) and of its stderr."""
    return (
        f"{cmd} </dev/null; __rb_rc=$?; "
        f"printf '\\n\\036{_FRAME_TAG}%s %s\\n' {token} \"$__rb_rc\"; "
        f"printf '\\n\\036{_FRAME_TAG}%s\\n' {token} >&2\n"
    )


class _FrameReader:
    """Splits one output stream of the persistent shell into per-command
    frames `(header, body)`. Linear in the data size: each chunk is scanned
    once, with a short carry for a marker split across chunks."""

    def __init__(self) -> None:
        self.frames: deque[tuple[str, str]] = deque()
        self._parts: list[str] = []
        self._carry = ""

    def feed(self, chunk: str) -> None:
        data = self._carry + chunk
        start = 0
        while True:
            i = data.find(_FRAME_MARK, start)
            if i < 0:
                # Hold back a tail that could be the start of a marker.
                keep = max(start, len(data) - (len(_FRAME_MARK) - 1))
                break
            j = data.find("\n", i + len(_FRAME_MARK))
            if j < 0:
                keep = i  # marker seen, header line not complete yet
                break
            self._parts.append(data[start:i])
            self.frames.append((data[i + len(_FRAME_MARK):j], "".join(self._parts)))
            self._parts = []
            start = j + 1
        self._parts.append(data[start:keep])
        self._carry = data[keep:]


class _ShellSession:
    """The persistent remote shell: pumps stdout/stderr into frame readers
    and tracks the (at most one) command whose output hasn't been consumed."""

    def __init__(self, process: object) -> None:
        self.process = process
        self.out = _FrameReader()
        self.err = _FrameReader()
        self.closed = False
        #: Commands whose output was consumed. A session that closes with 0
        #: never worked, which counts as a failed link attempt.
        self.frames_done = 0
        #: Token of the command sent but not yet consumed, and when it was sent.
        self.inflight: str | None = None
        self.inflight_since = 0.0
        self._changed = asyncio.Event()
        self._pumps = [
            asyncio.ensure_future(self._pump(process.stdout, self.out)),  # type: ignore[attr-defined]
            asyncio.ensure_future(self._pump(process.stderr, self.err)),  # type: ignore[attr-defined]
        ]

    async def _pump(self, stream: object, reader: _FrameReader) -> None:
        try:
            while True:
                chunk = await stream.read(_READ_CHUNK)  # type: ignore[attr-defined]
                if not chunk:
                    break
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="replace")
                reader.feed(chunk)
                self._changed.set()
        except Exception:
            pass  # stream died with the channel/connection — same as EOF
        finally:
            # Either stream ending means the shell is gone.
            self.closed = True
            self._changed.set()

    def send(self, cmd: str, token: str, now: float) -> None:
        try:
            self.process.stdin.write(_frame_command(cmd, token))  # type: ignore[attr-defined]
        except Exception as exc:
            self.close()
            raise RemoteSessionLost(f"remote shell closed: {exc}") from exc
        self.inflight = token
        self.inflight_since = now

    async def wait_frame(self) -> tuple[int, str, str]:
        """Wait for the in-flight command's output. Cancellation-safe: the
        command stays in flight and a later call picks its output up."""
        token = self.inflight
        while True:
            if self.out.frames and self.err.frames:
                out_header, out = self.out.frames.popleft()
                err_header, err = self.err.frames.popleft()
                out_token, _, rc_text = out_header.partition(" ")
                if out_token != token or err_header != token:
                    self.close()
                    raise RemoteSessionLost("remote shell output out of sync")
                self.inflight = None
                self.frames_done += 1
                try:
                    rc = int(rc_text)
                except ValueError:
                    rc = -1
                return rc, out, err
            if self.closed:
                raise RemoteSessionLost("remote shell closed")
            self._changed.clear()
            await self._changed.wait()

    def close(self) -> None:
        """Close the channel without waiting: a remote shell stuck on a hung
        filesystem can't acknowledge the close until it unsticks."""
        self.closed = True
        self._changed.set()
        for pump in self._pumps:
            pump.cancel()
        try:
            self.process.close()  # type: ignore[attr-defined]
        except Exception:
            pass


class AsyncSSHExecutor:
    """One persistent `SSHClientConnection` per Executor instance, with ONE
    long-lived remote shell on it that runs every command in turn (see the
    section comment above for why).

    The link (connection + shell) is opened lazily on the first `run()`.
    `aclose()` closes it; calling `aclose` on a never-connected instance is
    a no-op.

    Cancellation-safe: a cancelled or timed-out `run()` leaves its remote
    command running and the link intact; the next `run()` waits for that
    command to finish before starting its own.
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str | None = None,
        known_hosts: object | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        client_keys: list[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        # asyncssh's `known_hosts` accepts None (default = ~/.ssh/known_hosts),
        # a path, or () to disable. We pass through whatever the caller gives.
        self.known_hosts = known_hosts
        self.connect_timeout = connect_timeout
        self.client_keys = client_keys
        # Lazily-opened. Type is asyncssh.SSHClientConnection but we avoid
        # importing asyncssh at module-load time so the LocalExecutor path
        # works on hosts without the dep installed (it IS in pyproject.toml,
        # but this future-proofs us).
        self._conn: object | None = None
        self._connect_lock = asyncio.Lock()
        self._session: _ShellSession | None = None
        # Serializes run() calls onto the one shell.
        self._run_lock = asyncio.Lock()
        # In-flight link attempt, shared by every run() waiting on it.
        self._link_task: asyncio.Future[_ShellSession] | None = None
        #: Consecutive failed link attempts; reset by any completed command.
        self.failures = 0
        self._next_attempt_at = 0.0
        self._clock = clock
        # whoami cache — see `resolve_whoami`. Resolved once over the
        # persistent connection at App startup so the per-tick squeue/sacct
        # filters substitute `"self"` → REMOTE username instead of
        # client-side $USER (which would mis-resolve to "hli" on a WSL
        # host running `exec = "ssh:lrz"` where the LRZ user is "di35dob").
        self._whoami: str | None = None

    async def resolve_whoami(self, timeout: float = 15.0) -> str:
        """Run `whoami` over the SSH connection, cache the result.
        Default timeout=15s tolerates LRZ's ~10.5s ProxyJump cold-connect."""
        if self._whoami is None:
            try:
                rc, out, _err = await self.run(["whoami"], timeout=timeout)
                self._whoami = out.strip() if rc == 0 else ""
            except Exception:
                self._whoami = ""
        return self._whoami

    async def connect(self) -> None:
        """Eagerly open the persistent connection. Idempotent — a second
        call on an open connection is a no-op."""
        if self._conn is not None:
            return
        # Single-flight: if N tasks call connect() simultaneously, only one
        # actually does the handshake; the rest return immediately.
        async with self._connect_lock:
            if self._conn is not None:
                return
            import asyncssh  # lazy
            kwargs: dict[str, object] = {
                "host": self.host,
                "port": self.port,
                # asyncssh sends NO keepalives by default (keepalive_interval=0),
                # so a persistent connection idling through TUM's NAT (~2 h idle
                # drop — see cluster_rohan.md) is silently killed. The
                # ServerAliveInterval in ~/.ssh/config is OpenSSH-subprocess-only
                # and does NOT apply to asyncssh. Send a keepalive after 30 s of
                # idle and tear the link down after 5 unanswered (~2.5 min) so
                # run()'s reconnect path rebuilds it on the next tick instead of
                # wedging forever. (2026-06-05 both-boards-wedged incident.)
                "keepalive_interval": 30,
                "keepalive_count_max": 5,
                # asyncssh DOES read ForwardX11Trusted / ForwardAgent from
                # ~/.ssh/config (`Host rohan` has ForwardX11Trusted yes), and
                # an X11 request makes sshd run xauth on ~/.Xauthority before
                # the command starts — the step that hung on frozen /rhome in
                # the 2026-10-05 pile-up. The board never needs either.
                "x11_forwarding": False,
                "agent_forwarding": False,
            }
            if self.username is not None:
                kwargs["username"] = self.username
            if self.known_hosts is not None:
                # Pass through ()=disabled, str=path. None means "let
                # asyncssh pick the default" (= ~/.ssh/known_hosts).
                kwargs["known_hosts"] = self.known_hosts
            if self.client_keys is not None:
                kwargs["client_keys"] = self.client_keys
            # asyncssh's connect() has had historical issues honoring its
            # own timeout (asyncssh#21); wrap in wait_for so we always
            # get a TimeoutError on slow handshakes.
            self._conn = await asyncio.wait_for(
                asyncssh.connect(**kwargs),
                timeout=self.connect_timeout,
            )

    def _conn_is_dead(self) -> bool:
        """True if the persistent connection is known-closed and must be
        rebuilt. Defensive: returns False when liveness can't be determined
        (e.g. a test stub without `is_closed`) so we never refuse to use an
        otherwise-fine connection."""
        conn = self._conn
        if conn is None:
            return True
        is_closed = getattr(conn, "is_closed", None)
        if is_closed is None:
            return False
        try:
            return bool(is_closed())
        except Exception:
            return False

    def _discard_conn(self) -> None:
        """Drop the (presumed-dead) persistent connection so the next call
        reconnects. Best-effort synchronous close; never raises."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        close = getattr(conn, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass

    def _record_failure(self) -> None:
        self.failures += 1
        self._next_attempt_at = self._clock() + reconnect_delay(self.failures)

    def _record_success(self) -> None:
        self.failures = 0
        self._next_attempt_at = 0.0

    async def _open_link(self) -> _ShellSession:
        """(Re)connect if needed, then start the persistent shell."""
        import asyncssh  # lazy
        try:
            if self._conn_is_dead():
                self._discard_conn()
            await self.connect()
            assert self._conn is not None
            process = await asyncio.wait_for(
                self._conn.create_process(  # type: ignore[attr-defined]
                    SHELL_COMMAND,
                    x11_forwarding=False,
                    encoding="utf-8",
                    errors="replace",
                ),
                timeout=self.connect_timeout,
            )
        except BaseException as exc:
            # A refused channel (e.g. sshd's MaxSessions) or a slow one says
            # nothing about the connection: keep it, so a retry doesn't log
            # in again and orphan whatever the server still holds for us.
            if isinstance(exc, (OSError, asyncssh.DisconnectError)) and not isinstance(
                exc, asyncio.TimeoutError
            ):
                self._discard_conn()
            self._record_failure()
            raise
        session = _ShellSession(process)
        try:
            # Startup probe: move the shell's cwd off $HOME, and let its frame
            # swallow anything the login shell printed before `exec`. The
            # first run() drains it like any unfinished command.
            session.send("cd /", uuid.uuid4().hex, self._clock())
        except RemoteSessionLost:
            self._record_failure()
            raise
        self._session = session
        return session

    def _start_link(self) -> asyncio.Future[_ShellSession]:
        task = asyncio.ensure_future(self._open_link())

        def _done(t: asyncio.Future[_ShellSession]) -> None:
            if self._link_task is t:
                self._link_task = None
            if not t.cancelled():
                t.exception()  # retrieved here; waiters re-raise it themselves

        task.add_done_callback(_done)
        self._link_task = task
        return task

    async def _ensure_session(self) -> _ShellSession:
        session = self._session
        if session is not None and not session.closed:
            return session
        if session is not None:
            # The shell is gone (remote exit, connection drop). One that never
            # completed a command counts as a failed attempt, so a shell that
            # dies on startup can't be restarted at tick rate.
            self._session = None
            session.close()
            if session.frames_done == 0:
                self._record_failure()
        task = self._link_task
        if task is None:
            wait = self._next_attempt_at - self._clock()
            if wait > 0:
                raise SSHUnavailableError(
                    f"ssh {self.host}: {self.failures} failed attempt(s), "
                    f"next reconnect in {wait:.0f}s"
                )
            task = self._start_link()
        # Shielded: a caller timing out or being cancelled must not abort the
        # handshake — that would strand a half-open login on the server and
        # start a fresh one on the next tick.
        return await asyncio.shield(task)

    async def _run(self, cmd: str, state: dict[str, float | None]) -> tuple[int, str, str]:
        async with self._run_lock:
            for attempt in (0, 1):
                session = await self._ensure_session()
                try:
                    if session.inflight is not None:
                        # An earlier run() timed out or was cancelled and its
                        # command hasn't finished. Wait for it and drop its
                        # output rather than start a second command beside it.
                        state["busy_since"] = session.inflight_since
                        await session.wait_frame()
                        state["busy_since"] = None
                        self._record_success()
                    session.send(cmd, uuid.uuid4().hex, self._clock())
                    result = await session.wait_frame()
                except RemoteSessionLost:
                    state["busy_since"] = None
                    # A link that had been working just died: reconnect and
                    # retry once right away. (Collector commands are reads.)
                    if attempt == 0 and session.frames_done > 0:
                        continue
                    raise
                self._record_success()
                return result
        raise AssertionError("unreachable")  # pragma: no cover

    async def run(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        if timeout is None:
            timeout = DEFAULT_RUN_TIMEOUT
        # asyncssh's run/create_process take a STRING command (passed to the
        # remote login shell), NOT an argv list. We shlex.join here so callers
        # can keep the list-of-args interface that LocalExecutor uses. This
        # quotes embedded whitespace/specials safely.
        cmd = shlex.join(argv)
        state: dict[str, float | None] = {"busy_since": None}
        try:
            return await asyncio.wait_for(self._run(cmd, state), timeout=timeout)
        except asyncio.TimeoutError:
            since = state["busy_since"]
            if since is not None:
                raise RemoteBusyError(
                    f"ssh {self.host}: previous command still running after "
                    f"{self._clock() - since:.0f}s; not starting another"
                ) from None
            raise

    async def aclose(self) -> None:
        task, self._link_task = self._link_task, None
        if task is not None and not task.done():
            task.cancel()
        session, self._session = self._session, None
        if session is not None:
            session.close()
        conn = self._conn
        if conn is None:
            return
        self._conn = None
        # asyncssh's connection has both close() (synchronous, requests
        # close) and wait_closed() (await full teardown). Use both, shielded,
        # so a parent cancel doesn't leak the connection.
        try:
            conn.close()  # type: ignore[attr-defined]
            await asyncio.shield(conn.wait_closed())  # type: ignore[attr-defined]
        except BaseException:
            # Best-effort close; if asyncssh raises mid-teardown there's
            # nothing we can do but log and move on. Don't mask the
            # original cancellation if any.
            raise
