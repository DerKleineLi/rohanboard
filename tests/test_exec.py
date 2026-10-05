"""Tests for rohanboard.exec — Executor Protocol + LocalExecutor +
AsyncSSHExecutor + FakeLocalExecutor (collector-test fixture)."""
from __future__ import annotations

import asyncio
import os
from typing import Sequence

import pytest

from rohanboard.exec import (
    AsyncSSHExecutor,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RUN_TIMEOUT,
    Executor,
    LocalExecutor,
    RemoteBusyError,
    RemoteSessionLost,
    SSHUnavailableError,
    reconnect_delay,
)


# ──────────────────────────────────────────────────────────────────────────
# FakeLocalExecutor — in-memory canned-response Executor for collector tests
# ──────────────────────────────────────────────────────────────────────────

class FakeLocalExecutor:
    """Map argv tuples to canned `(rc, stdout, stderr)` responses.  Used in
    collector tests (Phase 4c+) to drive `slurm.fetch_*` / `storage.fetch_*`
    without spawning subprocesses or hitting a cluster.

    Example:
        fake = FakeLocalExecutor({
            ("squeue", "-h", "-O", ...): (0, squeue_text, ""),
            ("scontrol", "show", "node", "--all"): (0, scontrol_text, ""),
        })
        jobs = await slurm.fetch_jobs(fake, ["self"])
    """

    def __init__(
        self,
        canned: dict[tuple[str, ...], tuple[int, str, str]] | None = None,
        default: tuple[int, str, str] = (0, "", ""),
    ) -> None:
        self._canned = canned or {}
        self._default = default
        self.calls: list[tuple[str, ...]] = []
        self._whoami: str | None = None

    async def run(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        key = tuple(argv)
        self.calls.append(key)
        return self._canned.get(key, self._default)

    async def resolve_whoami(self, timeout: float = 5.0) -> str:
        if self._whoami is None:
            rc, out, _err = await self.run(["whoami"], timeout=timeout)
            self._whoami = out.strip() if rc == 0 else ""
        return self._whoami

    async def aclose(self) -> None:
        return None


# ──────────────────────────────────────────────────────────────────────────
# Protocol conformance
# ──────────────────────────────────────────────────────────────────────────

def test_local_executor_satisfies_protocol():
    assert isinstance(LocalExecutor(), Executor)


def test_async_ssh_executor_satisfies_protocol():
    assert isinstance(AsyncSSHExecutor(host="example.invalid"), Executor)


def test_fake_local_executor_satisfies_protocol():
    assert isinstance(FakeLocalExecutor(), Executor)


def test_module_defaults_present():
    # Sanity: constants we want callers to be able to import.
    assert DEFAULT_RUN_TIMEOUT > 0
    assert DEFAULT_CONNECT_TIMEOUT > 0


# ──────────────────────────────────────────────────────────────────────────
# LocalExecutor — happy path
# ──────────────────────────────────────────────────────────────────────────

async def test_local_run_echo():
    ex = LocalExecutor()
    rc, out, err = await ex.run(["echo", "hello"])
    assert rc == 0
    assert out.strip() == "hello"
    assert err == ""


async def test_local_run_nonzero_rc():
    """Non-zero rc is reported, NOT raised — matches storage.py's prior _run
    semantics (the more-general shape collectors expect).  slurm.py callers
    check rc themselves via _run_checked."""
    ex = LocalExecutor()
    # `false` always exits 1.
    rc, out, err = await ex.run(["false"])
    assert rc == 1


async def test_local_run_captures_stderr():
    ex = LocalExecutor()
    rc, out, err = await ex.run(["sh", "-c", "echo to-stderr 1>&2; exit 0"])
    assert rc == 0
    assert "to-stderr" in err


async def test_local_aclose_is_noop_and_idempotent():
    ex = LocalExecutor()
    await ex.aclose()
    await ex.aclose()  # idempotent


# ──────────────────────────────────────────────────────────────────────────
# resolve_whoami — substitute `users = ["self"]` with REMOTE whoami
# ──────────────────────────────────────────────────────────────────────────

async def test_local_resolve_whoami_caches_result():
    """resolve_whoami runs once, caches, second call doesn't re-run."""
    ex = LocalExecutor()
    user = await ex.resolve_whoami()
    assert user, "whoami should return non-empty for the test runner"
    # Tamper with the cache; second call should NOT run subprocess.
    ex._whoami = "sentinel"
    user2 = await ex.resolve_whoami()
    assert user2 == "sentinel"


async def test_fake_resolve_whoami_uses_canned_response():
    """FakeLocalExecutor's resolve_whoami honors canned ('whoami',) entry."""
    fake = FakeLocalExecutor(canned={("whoami",): (0, "di35dob\n", "")})
    user = await fake.resolve_whoami()
    assert user == "di35dob"


async def test_fake_resolve_whoami_empty_on_failure():
    """rc != 0 → empty string, NOT exception. Caller treats as 'no filter'."""
    fake = FakeLocalExecutor(default=(127, "", "command not found"))
    user = await fake.resolve_whoami()
    assert user == ""


# ──────────────────────────────────────────────────────────────────────────
# LocalExecutor — timeout reaps the child cleanly
# ──────────────────────────────────────────────────────────────────────────

async def test_local_run_timeout_raises_and_reaps():
    ex = LocalExecutor()
    # `sleep 30` would normally block well past our 0.2 s timeout.
    with pytest.raises(asyncio.TimeoutError):
        await ex.run(["sleep", "30"], timeout=0.2)
    # Give the OS a tick to actually reap; pytest-asyncio's loop is fast,
    # but in CI a sleep(0) is sometimes not enough.
    await asyncio.sleep(0.05)
    # `pgrep -f 'sleep 30'` should find none of OUR children.  We can't
    # easily reach into the proc table without false positives, but we CAN
    # check that the `proc.wait()` reap completed by spawning another and
    # confirming the loop is healthy.
    rc, out, _ = await ex.run(["echo", "post-timeout"])
    assert rc == 0
    assert "post-timeout" in out


# ──────────────────────────────────────────────────────────────────────────
# LocalExecutor — cancellation kills the child
# ──────────────────────────────────────────────────────────────────────────

async def test_local_run_cancellation_kills_child():
    """If the calling task is cancelled mid-run, the child must be killed.
    This is the cancel-leak pattern from
    feedback_asyncio_subprocess_cancel_leaks.md — without catch-BaseException
    the OS subprocess outlives the python parent."""
    ex = LocalExecutor()

    # Run a sleep in a background task.  We'll cancel it from outside.
    # Use a `python -c` so we get a unique cmdline pattern that won't
    # collide with the user's other shells.
    sentinel_marker = f"rohanboard_test_marker_{os.getpid()}"
    payload = (
        f"import time, sys; sys.stdout.write('{sentinel_marker} ready\\n'); "
        f"sys.stdout.flush(); time.sleep(60)"
    )
    task = asyncio.create_task(
        ex.run(["python", "-c", payload], timeout=120)
    )
    # Give the child time to actually spawn before we cancel.
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # After cancellation, the child must be reaped.  Re-spawn an `echo` to
    # confirm the loop and the executor are still healthy.
    rc, _, _ = await ex.run(["echo", "ok"])
    assert rc == 0
    # And confirm the marker process didn't survive: pgrep returns rc=1
    # when nothing matches.
    rc_pgrep, out_pgrep, _ = await ex.run(["pgrep", "-fa", sentinel_marker])
    # rc=1 = no match (good); rc=0 + match line = bad (leaked).
    assert rc_pgrep == 1, f"orphan child survived cancellation: {out_pgrep!r}"


# ──────────────────────────────────────────────────────────────────────────
# FakeLocalExecutor — used by collector tests in 4c+
# ──────────────────────────────────────────────────────────────────────────

async def test_fake_executor_returns_canned():
    fake = FakeLocalExecutor(
        canned={
            ("echo", "hi"): (0, "hi\n", ""),
            ("false",):     (1, "", "err"),
        },
    )
    rc, out, err = await fake.run(["echo", "hi"])
    assert (rc, out, err) == (0, "hi\n", "")
    rc, out, err = await fake.run(["false"])
    assert (rc, out, err) == (1, "", "err")


async def test_fake_executor_unmapped_returns_default():
    fake = FakeLocalExecutor(default=(127, "", "command not found"))
    rc, out, err = await fake.run(["definitely-not-a-real-command"])
    assert (rc, out, err) == (127, "", "command not found")


async def test_fake_executor_records_calls():
    fake = FakeLocalExecutor()
    await fake.run(["a", "b"])
    await fake.run(["c"])
    assert fake.calls == [("a", "b"), ("c",)]


# ──────────────────────────────────────────────────────────────────────────
# AsyncSSHExecutor
# ──────────────────────────────────────────────────────────────────────────
#
# The persistent-shell tests run a REAL local bash behind a fake connection,
# so the framing (sentinels, exit status, stderr, quoting) is exercised
# against an actual shell rather than a mock of one.

class _LocalReader:
    def __init__(self, stream: asyncio.StreamReader) -> None:
        self._stream = stream

    async def read(self, n: int) -> str:
        return (await self._stream.read(n)).decode()


class _LocalWriter:
    def __init__(self, stream: asyncio.StreamWriter) -> None:
        self._stream = stream

    def write(self, data: str) -> None:
        if self._stream.is_closing():
            raise BrokenPipeError("stdin closed")
        self._stream.write(data.encode())


class _LocalShellProcess:
    """asyncssh-process look-alike backed by a local subprocess."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.stdin = _LocalWriter(proc.stdin)
        self.stdout = _LocalReader(proc.stdout)
        self.stderr = _LocalReader(proc.stderr)

    def close(self) -> None:
        if self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass

    async def wait_closed(self) -> None:
        await self.proc.wait()


class _LocalShellConn:
    """Stands in for an `SSHClientConnection`: every `create_process` starts
    a local `bash -c <command>`. `preamble` runs before the command, like a
    login shell that prints something before `exec`."""

    def __init__(self, preamble: str = "") -> None:
        self.preamble = preamble
        self.processes: list[_LocalShellProcess] = []
        self.session_kwargs: list[dict] = []
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def create_process(self, cmd: str, **kwargs) -> _LocalShellProcess:
        self.session_kwargs.append(kwargs)
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", self.preamble + cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        process = _LocalShellProcess(proc)
        self.processes.append(process)
        return process

    def drop(self) -> None:
        """Simulate the connection dying under the executor."""
        self.closed = True
        for p in self.processes:
            p.close()

    def close(self) -> None:
        self.drop()

    async def wait_closed(self) -> None:
        for p in self.processes:
            await p.wait_closed()


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def local_ssh():
    """An AsyncSSHExecutor whose connection is a `_LocalShellConn`."""
    ex = AsyncSSHExecutor(host="example.invalid")
    conn = _LocalShellConn()
    ex._conn = conn
    yield ex, conn
    await ex.aclose()


def test_async_ssh_executor_instantiation():
    ex = AsyncSSHExecutor(
        host="example.invalid",
        port=2222,
        username="me",
        connect_timeout=5.0,
    )
    assert ex.host == "example.invalid"
    assert ex.port == 2222
    assert ex.username == "me"
    assert ex.connect_timeout == 5.0
    assert ex._conn is None


async def test_async_ssh_executor_aclose_no_connect_is_noop():
    """`aclose()` on an instance that never opened a connection is a no-op
    and must not raise — matters because App.on_unmount calls aclose on
    every shutdown path including those where connect() never succeeded."""
    ex = AsyncSSHExecutor(host="example.invalid")
    await ex.aclose()
    await ex.aclose()  # idempotent
    assert ex._conn is None


async def test_connect_disables_x11_and_agent_forwarding(monkeypatch):
    """asyncssh reads ForwardX11Trusted / ForwardAgent from ~/.ssh/config, and
    `Host rohan` there has ForwardX11Trusted yes. An X11 request makes sshd
    run xauth on ~/.Xauthority before every command — what hung on frozen
    /rhome in the 2026-10-05 pile-up. connect() must turn both off."""
    import asyncssh

    seen: dict = {}

    async def fake_connect(**kwargs):
        seen.update(kwargs)
        return _LocalShellConn()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    ex = AsyncSSHExecutor(host="example.invalid")
    try:
        assert await ex.run(["echo", "hi"]) == (0, "hi\n", "")
        assert seen["x11_forwarding"] is False
        assert seen["agent_forwarding"] is False
        assert ex._conn.session_kwargs[0]["x11_forwarding"] is False
    finally:
        await ex.aclose()


async def test_run_returns_stdout_stderr_and_exit_status(local_ssh):
    ex, _conn = local_ssh
    assert await ex.run(["sh", "-c", "printf out; printf err >&2; exit 3"]) == (3, "out", "err")
    assert await ex.run(["echo", "hi"]) == (0, "hi\n", "")
    assert await ex.run(["true"]) == (0, "", "")
    script = "echo 'a  b'\necho \"it's $((1+1))\"\nprintf 'no newline'"
    assert await ex.run(["bash", "-c", script]) == (0, "a  b\nit's 2\nno newline", "")


async def test_runs_share_one_persistent_session(local_ssh):
    """Every run() goes through ONE remote shell: no per-command sessions."""
    ex, conn = local_ssh
    for i in range(5):
        assert await ex.run(["echo", str(i)]) == (0, f"{i}\n", "")
    assert len(conn.processes) == 1


async def test_concurrent_runs_are_serialized_on_one_session(local_ssh):
    ex, conn = local_ssh
    results = await asyncio.gather(*(ex.run(["echo", str(i)]) for i in range(5)))
    assert results == [(0, f"{i}\n", "") for i in range(5)]
    assert len(conn.processes) == 1


async def test_login_shell_output_is_not_mixed_into_first_command():
    ex = AsyncSSHExecutor(host="example.invalid")
    ex._conn = _LocalShellConn(preamble="echo motd; echo motd-err >&2; ")
    try:
        assert await ex.run(["echo", "first"]) == (0, "first\n", "")
    finally:
        await ex.aclose()


async def test_large_output_is_framed_intact(local_ssh):
    ex, _conn = local_ssh
    rc, out, err = await ex.run(["bash", "-c", "seq 1 200000"])
    assert rc == 0 and err == ""
    assert out == "".join(f"{i}\n" for i in range(1, 200001))


def test_frame_reader_handles_marker_split_across_chunks():
    """Feeding one character at a time gives the same frames as one chunk."""
    from rohanboard.exec import _FRAME_MARK, _FrameReader

    stream = f"abc{_FRAME_MARK}t1 0\n{_FRAME_MARK}t2 1\nx\ny{_FRAME_MARK}t3 2\n"
    whole, split = _FrameReader(), _FrameReader()
    whole.feed(stream)
    for ch in stream:
        split.feed(ch)
    expected = [("t1 0", "abc"), ("t2 1", ""), ("t3 2", "x\ny")]
    assert list(whole.frames) == expected
    assert list(split.frames) == expected


async def test_cancelled_run_does_not_open_another_session(local_ssh):
    """The board's 5 s tick cancels a slow refresh. The cancelled command
    keeps running in the one shell, and the next run() waits for it rather
    than opening a second session beside it (the pile-up mechanism)."""
    ex, conn = local_ssh
    task = asyncio.create_task(ex.run(["sh", "-c", "sleep 0.5; echo old"]))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await ex.run(["echo", "new"], timeout=5) == (0, "new\n", "")
    assert len(conn.processes) == 1


async def test_busy_shell_raises_instead_of_starting_another_command(local_ssh):
    ex, conn = local_ssh
    with pytest.raises(asyncio.TimeoutError):
        await ex.run(["sh", "-c", "sleep 1; echo slow"], timeout=0.2)
    with pytest.raises(RemoteBusyError):
        await ex.run(["echo", "x"], timeout=0.2)
    assert len(conn.processes) == 1
    # Once the slow command finishes, the shell is usable again.
    assert await ex.run(["echo", "x"], timeout=5) == (0, "x\n", "")
    assert len(conn.processes) == 1


async def test_exited_shell_is_replaced_on_the_same_connection(local_ssh, monkeypatch):
    ex, conn = local_ssh
    connects = {"n": 0}

    async def counting_connect():
        connects["n"] += 1

    monkeypatch.setattr(ex, "connect", counting_connect)
    assert await ex.run(["echo", "a"]) == (0, "a\n", "")
    conn.processes[0].close()
    await conn.processes[0].wait_closed()
    assert await ex.run(["echo", "b"]) == (0, "b\n", "")
    assert len(conn.processes) == 2
    assert ex._conn is conn
    assert ex.failures == 0


async def test_shell_lost_mid_command_retries_once_then_raises(local_ssh):
    """A working link that dies mid-command is rebuilt and the command
    retried ONCE; if it dies again the error propagates (no loop)."""
    ex, conn = local_ssh
    assert await ex.run(["true"]) == (0, "", "")
    with pytest.raises(RemoteSessionLost):
        await ex.run(["sh", "-c", "kill -9 $PPID"])  # kills the shell itself
    assert len(conn.processes) == 2  # original + the one retry's shell
    # The retry's shell was killed too; the next call starts a fresh one.
    assert await ex.run(["echo", "ok"]) == (0, "ok\n", "")
    assert len(conn.processes) == 3


async def test_dropped_connection_reconnects_immediately(monkeypatch):
    """After a disconnect the first reconnect happens right away (2026-06-05
    NAT idle-drop fix, kept), within the same run() call."""
    ex = AsyncSSHExecutor(host="example.invalid")
    first = _LocalShellConn()
    ex._conn = first
    conns = [first]

    async def fake_connect():
        if ex._conn is None:
            ex._conn = _LocalShellConn()
            conns.append(ex._conn)

    monkeypatch.setattr(ex, "connect", fake_connect)
    try:
        assert await ex.run(["echo", "a"]) == (0, "a\n", "")
        first.drop()
        await first.wait_closed()
        assert await ex.run(["echo", "b"]) == (0, "b\n", "")
        assert len(conns) == 2
    finally:
        await ex.aclose()


async def test_reconnect_backs_off_after_repeated_failures(monkeypatch):
    clock = _FakeClock()
    ex = AsyncSSHExecutor(host="example.invalid", clock=clock)
    attempts = {"n": 0}

    async def failing_connect():
        attempts["n"] += 1
        raise OSError("no route to host")

    monkeypatch.setattr(ex, "connect", failing_connect)

    async def attempt_at(t: float) -> type[BaseException]:
        clock.now = 1000.0 + t
        with pytest.raises((OSError, SSHUnavailableError)) as info:
            await ex.run(["true"])
        return info.type

    # Three fast retries 5 s apart, then the gap doubles: 10, 20, …
    assert await attempt_at(0) is OSError
    assert await attempt_at(1) is SSHUnavailableError
    assert await attempt_at(5) is OSError
    assert await attempt_at(10) is OSError
    assert await attempt_at(15) is OSError       # 4th failure → wait 10 s
    assert await attempt_at(24) is SSHUnavailableError
    assert await attempt_at(25) is OSError       # 5th failure → wait 20 s
    assert await attempt_at(44) is SSHUnavailableError
    assert await attempt_at(45) is OSError
    assert attempts["n"] == 6
    assert ex.failures == 6


def test_reconnect_delay_schedule():
    assert [reconnect_delay(n) for n in range(0, 11)] == [
        0, 5, 5, 5, 10, 20, 40, 80, 160, 300, 300,
    ]
    assert reconnect_delay(10**6) == 300


async def test_successful_command_resets_backoff(monkeypatch):
    clock = _FakeClock()
    ex = AsyncSSHExecutor(host="example.invalid", clock=clock)
    fail = {"on": True}

    async def flaky_connect():
        if fail["on"]:
            raise OSError("down")
        if ex._conn is None:
            ex._conn = _LocalShellConn()

    monkeypatch.setattr(ex, "connect", flaky_connect)
    try:
        for t in (0, 5, 10, 15):
            clock.now = 1000.0 + t
            with pytest.raises(OSError):
                await ex.run(["true"])
        assert ex.failures == 4
        fail["on"] = False
        clock.now = 1000.0 + 25
        assert await ex.run(["echo", "up"]) == (0, "up\n", "")
        assert ex.failures == 0
    finally:
        await ex.aclose()


async def test_refused_session_keeps_the_connection(monkeypatch):
    """A refused channel (sshd's MaxSessions) is not a dead connection: keep
    it instead of logging in again, and back off before the next try."""
    import asyncssh

    class _RefusingConn:
        def __init__(self) -> None:
            self.opens = 0

        def is_closed(self) -> bool:
            return False

        async def create_process(self, _cmd, **_kwargs):
            self.opens += 1
            raise asyncssh.ChannelOpenError(
                asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, "open failed")

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            return None

    clock = _FakeClock()
    ex = AsyncSSHExecutor(host="example.invalid", clock=clock)
    conn = _RefusingConn()
    ex._conn = conn
    connects = {"n": 0}

    async def counting_connect():
        connects["n"] += 1

    monkeypatch.setattr(ex, "connect", counting_connect)
    with pytest.raises(asyncssh.ChannelOpenError):
        await ex.run(["true"])
    assert ex._conn is conn
    assert ex.failures == 1
    with pytest.raises(SSHUnavailableError):
        await ex.run(["true"])
    assert conn.opens == 1
    assert connects["n"] == 1  # connect() is a no-op on the kept connection


async def test_shell_dying_on_startup_backs_off():
    """A shell that exits before completing anything counts as a failed
    attempt, so it can't be restarted at tick rate."""
    ex = AsyncSSHExecutor(host="example.invalid", clock=_FakeClock())
    conn = _LocalShellConn(preamble="exit 1; ")
    ex._conn = conn
    try:
        with pytest.raises(RemoteSessionLost):
            await ex.run(["true"])
        with pytest.raises(SSHUnavailableError):
            await ex.run(["true"])
        assert len(conn.processes) == 1
        assert ex.failures == 1
    finally:
        await ex.aclose()


async def test_aclose_closes_session_and_connection(local_ssh):
    ex, conn = local_ssh
    assert await ex.run(["true"]) == (0, "", "")
    await ex.aclose()
    assert conn.closed
    assert ex._conn is None and ex._session is None
    assert conn.processes[0].proc.returncode is not None
    await ex.aclose()  # idempotent
