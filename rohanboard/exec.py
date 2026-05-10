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

Phase 4b ships LocalExecutor end-to-end. AsyncSSHExecutor is importable and
instantiable; it lazily opens a persistent `SSHClientConnection` on first
`run()`. Phase 4e wires it to LRZ for real (and adds the smoke test).
"""
from __future__ import annotations

import asyncio
import shlex
from typing import Protocol, Sequence, runtime_checkable

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
# AsyncSSHExecutor (minimal viable; Phase 4e wires to LRZ for real)
# ──────────────────────────────────────────────────────────────────────────

class AsyncSSHExecutor:
    """One persistent `SSHClientConnection` per Executor instance. Multiple
    `run()` calls multiplex sessions over the same connection (asyncssh's
    native model — no ControlMaster, no socket files, no atexit gymnastics).

    The connection is opened lazily on the first `run()` call (or eagerly
    via `await connect()`). `aclose()` closes it; calling `aclose` on a
    never-connected instance is a no-op.

    Cancellation-safe: if a `run()` is cancelled, the in-flight session is
    closed but the persistent connection is preserved for the next call.
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str | None = None,
        known_hosts: object | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        client_keys: list[str] | None = None,
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

    async def run(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        if timeout is None:
            timeout = DEFAULT_RUN_TIMEOUT
        if self._conn is None:
            await self.connect()
        assert self._conn is not None
        # Use create_process so we can explicitly close() the half-open
        # session on cancel/timeout. `conn.run()` is a higher-level wrapper
        # that doesn't expose the channel, so cleanup is implicit there.
        # We want explicit control to mirror LocalExecutor's kill+shielded-wait
        # pattern (feedback_asyncio_subprocess_cancel_leaks.md) — even though
        # asyncssh's connection is persistent, an in-flight session that's
        # been cancelled mid-execution can leave a remote process running
        # until the connection is dropped.
        #
        # asyncssh's run/create_process take a STRING command (passed to the
        # remote login shell), NOT an argv list. We shlex.join here so callers
        # can keep the list-of-args interface that LocalExecutor uses. This
        # quotes embedded whitespace/specials safely.
        conn = self._conn
        process: object | None = None
        cmd = shlex.join(argv)
        try:
            process = await conn.create_process(cmd)  # type: ignore[attr-defined]
            # Wrap the wait in asyncio.wait_for since asyncssh's own timeout
            # doesn't always apply to all phases of session I/O
            # (asyncssh#411, #626).
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),  # type: ignore[attr-defined]
                timeout=timeout,
            )
            rc = int(process.exit_status or 0)  # type: ignore[attr-defined]
        except BaseException:
            # TimeoutError, CancelledError, KeyboardInterrupt — clean up
            # the half-open session before propagating. The parent
            # connection (`self._conn`) is preserved for reuse.
            if process is not None:
                try:
                    process.close()  # type: ignore[attr-defined]
                except Exception:
                    pass
                # Shield the wait so the cancellation that brought us here
                # doesn't also cancel the cleanup. asyncssh's process.wait()
                # is the analogue of subprocess.wait().
                try:
                    await asyncio.shield(process.wait_closed())  # type: ignore[attr-defined]
                except BaseException:
                    pass
            raise
        # Some asyncssh versions return bytes when channel I/O was binary;
        # normalize defensively.
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return rc, stdout or "", stderr or ""

    async def aclose(self) -> None:
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
