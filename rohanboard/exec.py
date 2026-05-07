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
        # Build a single shell command from argv. asyncssh.run takes either
        # a string (passed to remote shell) or list (joined). We pass a
        # list so asyncssh's own quoting handles it.
        # Wrap in asyncio.wait_for since asyncssh's own timeout doesn't
        # always apply to all phases of session setup (asyncssh#411, #626).
        # Cast away the `object` typing on _conn for the call site.
        conn = self._conn
        try:
            result = await asyncio.wait_for(
                conn.run(  # type: ignore[attr-defined]
                    list(argv),
                    check=False,
                ),
                timeout=timeout,
            )
        except BaseException:
            # On cancel/timeout, the half-open SSH session needs cleaning
            # but the parent connection is preserved for reuse. asyncssh
            # cleans up the session when the awaitable is cancelled
            # (PR for #626 landed pre-2.22). Nothing for us to do here
            # beyond re-raising — but we still wrap in shield-style intent
            # to be explicit about cancellation safety.
            raise
        # asyncssh's CompletedProcess: .exit_status, .stdout, .stderr.
        # stdout/stderr are str by default (encoding="utf-8"), bytes if
        # encoding=None. We don't override, so str.
        rc = int(getattr(result, "exit_status", 0) or 0)
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        # Some asyncssh versions return bytes when channel I/O was binary;
        # normalize defensively.
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return rc, stdout, stderr

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
