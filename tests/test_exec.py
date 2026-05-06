import asyncio
import os
from unittest.mock import patch

import pytest

from rohanboard.exec import LocalExecutor, SSHExecutor


@pytest.mark.asyncio
async def test_local_executor_echo():
    ex = LocalExecutor()
    out = await ex.run(["/bin/echo", "hello"])
    assert out.strip() == "hello"


@pytest.mark.asyncio
async def test_local_executor_failure():
    ex = LocalExecutor()
    with pytest.raises(RuntimeError, match=r"rc=\d+"):
        await ex.run(["/bin/false"])


@pytest.mark.asyncio
async def test_local_executor_whoami_uses_env_user():
    """LocalExecutor.whoami returns $USER (no remote round-trip)."""
    ex = LocalExecutor()
    me = await ex.whoami()
    assert me == os.environ.get("USER", "")


# ──────────────────────────────────────────────────────────────────────
# SSHExecutor.whoami caching + downstream collector use
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ssh_executor_whoami_caches_first_call():
    """First whoami() shells out; second returns the cache without
    re-issuing a subprocess. This is the fix for the silent 1.5 s/tick
    'Invalid user id: hli' eat on LRZ."""
    spawn_count = {"n": 0}

    class FakeProc:
        returncode = 0

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        async def communicate(self):
            return (self.payload, b"")

        def kill(self): pass
        async def wait(self): return 0

    async def fake_create(*args, **kwargs):
        spawn_count["n"] += 1
        # First call is the master-warm `echo _rb_ssh_warm_`; subsequent
        # are the real argv. Distinguish by looking at the last argv element.
        last = args[-1] if args else ""
        if "_rb_ssh_warm_" in last:
            return FakeProc(b"_rb_ssh_warm_\n")
        return FakeProc(b"di35dob\n")

    ex = SSHExecutor(host="fake-lrz")
    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        a = await ex.whoami()
        b = await ex.whoami()
    assert a == "di35dob"
    assert b == "di35dob"
    # The whoami subprocess fired exactly once. (The master-warm fired
    # once too, on the FIRST call only — _master_ready becomes True.)
    # So spawn_count should be 2 total: 1 warm + 1 whoami. The second
    # whoami() call must be a pure cache hit.
    assert spawn_count["n"] == 2, f"expected 1 warm + 1 whoami; got {spawn_count['n']}"


@pytest.mark.asyncio
async def test_ssh_executor_whoami_used_by_fetch_jobs():
    """`fetch_jobs(['self'])` must construct `-u <remote_user>` from the
    executor's whoami cache, NOT from os.environ['USER'].

    This is the load-bearing test: when local $USER='hli' but remote
    user='di35dob', the squeue argv must contain `-u di35dob`.
    """
    from rohanboard.collectors import slurm

    captured: dict = {}

    class FakeExec:
        async def run(self, argv, timeout=15.0):
            captured["argv"] = list(argv)
            return ""  # parse_squeue handles empty input
        async def whoami(self):
            return "di35dob"

    # Force local $USER to differ from remote, to confirm we don't use it.
    with patch.dict(os.environ, {"USER": "hli"}):
        await slurm.fetch_jobs(FakeExec(), ["self"])
    argv = captured["argv"]
    assert "-u" in argv
    user_idx = argv.index("-u") + 1
    assert argv[user_idx] == "di35dob", f"expected -u di35dob; got {argv}"


@pytest.mark.asyncio
async def test_ssh_executor_whoami_used_by_fetch_recent_jobs():
    """Same as above but for sacct: `fetch_recent_jobs(['self'])` must
    use whoami, not $USER."""
    from rohanboard.collectors import slurm

    captured: dict = {}

    class FakeExec:
        async def run(self, argv, timeout=15.0):
            captured["argv"] = list(argv)
            return ""
        async def whoami(self):
            return "di35dob"

    with patch.dict(os.environ, {"USER": "hli"}):
        await slurm.fetch_recent_jobs(FakeExec(), ["self"])
    argv = captured["argv"]
    assert "-u" in argv, f"expected -u in argv; got {argv}"
    user_idx = argv.index("-u") + 1
    assert argv[user_idx] == "di35dob"


@pytest.mark.asyncio
async def test_ssh_executor_run_suppresses_rc255_on_cancel():
    """When `.run` is cancelled mid-flight, the killed ssh subprocess
    typically exits with rc=255 (OpenSSH's 'channel torn down' code).
    The previous behaviour surfaced this as `ssh ... failed rc=255`
    which leaked into snap.errors and stayed painted across cancel
    storms. Fix: on cancellation, propagate CancelledError, never
    fabricate a RuntimeError from the rc.
    """
    class HangProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(3600)
            return (b"", b"")

        def kill(self):
            # Simulate ssh's channel-torn-down rc=255.
            self.returncode = 255

        async def wait(self): return 255

    async def fake_create(*args, **kwargs):
        return HangProc()

    ex = SSHExecutor(host="cancel-rc255-host")
    ex._master_ready = True

    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        task = asyncio.create_task(ex.run(["scontrol", "show", "node", "--all"], timeout=60.0))
        await asyncio.sleep(0.05)
        task.cancel()
        # Must raise CancelledError, NOT RuntimeError(rc=255).
        with pytest.raises(asyncio.CancelledError):
            await task


# ──────────────────────────────────────────────────────────────────────
# Issue #3 — detect-and-reconnect: per-call timeout marks mux dead so
# next call re-warms instead of reusing a known-broken connection.
# ──────────────────────────────────────────────────────────────────────


def _make_ok_proc():
    """A subprocess that completes immediately with rc=0."""
    class OkProc:
        returncode = 0
        async def communicate(self):
            return (b"ok\n", b"")
        def kill(self): pass
        async def wait(self): return 0
    return OkProc()


def _make_hang_proc():
    """A subprocess whose communicate() never resolves (simulates a
    network blackhole — TCP handshake completed, payload reply never)."""
    class HangProc:
        returncode = None
        async def communicate(self):
            await asyncio.sleep(3600)
            return (b"", b"")
        def kill(self):
            self.returncode = 255  # ssh signal-killed exit code
        async def wait(self): return 255
    return HangProc()


@pytest.mark.asyncio
async def test_ssh_executor_run_marks_mux_dead_on_per_call_timeout():
    """When `.run`'s per-call wait_for fires, the executor must mark the
    mux dead (`_master_ready=False`) so the NEXT call re-warms from
    scratch. Without this, the next call's `_warm_master` would short-
    circuit on `ssh -O check` against the same dead socket.

    The mux-cleanup runs in `finally`, so `_master_ready` should be False
    by the time the awaiting coroutine sees the TimeoutError surface.
    """
    ex = SSHExecutor(host="timeout-detect-host")
    ex._master_ready = True   # pretend warm path already completed
    # Skip the warm-master ssh -O check round-trip; tests don't have a
    # real mux on disk.
    ex._mux_check_blocking = lambda *_a, **_k: True  # type: ignore

    # Patch BOTH the subprocess (so .run's actual ssh call hangs) AND
    # `_mark_mux_dead` to confirm it gets called. We don't actually want
    # the mark to ssh-O-exit anything — we just want to trace it.
    mark_calls = {"n": 0}

    def trace_mark():
        mark_calls["n"] += 1
        # Don't actually run ssh -O exit — just flip the flag.
        ex._master_ready = False

    ex._mark_mux_dead = trace_mark

    async def fake_create(*args, **kwargs):
        return _make_hang_proc()

    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        with pytest.raises(asyncio.TimeoutError):
            await ex.run(["squeue"], timeout=0.05)
    assert mark_calls["n"] >= 1, (
        "post-timeout cleanup must call _mark_mux_dead so the next call "
        "doesn't short-circuit to a known-broken mux"
    )
    assert ex._master_ready is False, (
        "_master_ready must be False after a per-call timeout"
    )


@pytest.mark.asyncio
async def test_ssh_executor_recovers_after_timeout_on_next_call():
    """End-to-end: first call hangs → TimeoutError; SECOND call succeeds.
    Confirms the semaphore is released, the mux is re-warmed, and no
    state leaks between calls. This is the durable fix for the LRZ
    'another probe is already in flight' wedge from issue #3.
    """
    ex = SSHExecutor(host="recover-host")
    ex._master_ready = True   # First call assumes warm
    # Test: skip the in-process ssh -O check round-trip; we're not
    # exercising the on-disk mux file.
    ex._mux_check_blocking = lambda *_a, **_k: True  # type: ignore

    # First subprocess hangs; second is fine.
    proc_idx = {"i": 0}

    async def fake_create(*args, **kwargs):
        proc_idx["i"] += 1
        # First call (the actual command) hangs → triggers per-call
        # timeout, which marks mux dead.
        if proc_idx["i"] == 1:
            return _make_hang_proc()
        # All subsequent spawns (warm + second actual command) succeed.
        return _make_ok_proc()

    # Stub the mark-mux-dead's subprocess.run so it doesn't actually try
    # to `ssh -O exit` against a real host (which would silently fail
    # but waste 2 s of the 30 s test budget).
    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create), \
         patch("rohanboard.exec.subprocess.run") as ssh_exit_run:
        ssh_exit_run.return_value = subprocess_completed_process()
        # First call: should TimeoutError after the per-call timeout.
        with pytest.raises(asyncio.TimeoutError):
            await ex.run(["squeue"], timeout=0.05)
        assert ex._master_ready is False, "mux should be marked dead"
        # Second call: master must be re-warmed, then the real call runs.
        # Both subprocess spawns are _make_ok_proc instances per fake_create.
        out = await ex.run(["squeue"], timeout=2.0)
        assert out.strip() == "ok"
        # Sanity: warmed back up.
        assert ex._master_ready is True


def subprocess_completed_process(rc: int = 0):
    """Helper to fabricate a subprocess.CompletedProcess for the patch."""
    class Done:
        returncode = rc
    return Done()


@pytest.mark.asyncio
async def test_ssh_executor_warm_master_shielded_from_outer_cancel():
    """`_warm_master` must complete (or hit its own timeout) even when
    the outer task is cancelled mid-handshake. This is the LRZ
    cold-ProxyJump path (10.5 s) crossing the 5 s tick boundary —
    `run_worker(exclusive=True)` cancels the outer; without the shield
    the warm gets torn down half-way and the next tick re-runs the same
    expensive path.

    Verification: when the outer is cancelled mid-warm, the inner warm
    subprocess MUST complete normally (its `communicate()` reaches its
    natural end) — not get its CancelledError-on-loop-cancel propagated
    in. We track whether the inner saw a cancel to confirm.
    """
    ex = SSHExecutor(host="shielded-warm-host")

    started = asyncio.Event()
    inner_finished_naturally = {"yes": False}
    inner_was_cancelled = {"yes": False}

    async def slow_communicate():
        started.set()
        # 0.3s — short enough to test fast, long enough that the outer
        # cancel arrives mid-flight.
        try:
            await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            inner_was_cancelled["yes"] = True
            raise
        inner_finished_naturally["yes"] = True
        return (b"_rb_ssh_warm_\n", b"")

    class SlowProc:
        returncode = 0
        async def communicate(self):
            return await slow_communicate()
        def kill(self):
            self.returncode = 255
        async def wait(self): return 255

    async def fake_create(*args, **kwargs):
        return SlowProc()

    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        task = asyncio.create_task(ex.run(["squeue"], timeout=2.0))
        await started.wait()
        # Outer cancel mid-warm. With the shield, the inner sleep should
        # NOT see a cancel — it finishes naturally and sets the flag.
        await asyncio.sleep(0.05)   # let warm get into the sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Give the shielded inner time to finish on its own.
        for _ in range(20):
            if inner_finished_naturally["yes"]:
                break
            await asyncio.sleep(0.05)

    assert inner_finished_naturally["yes"], (
        "shielded warm-master inner must run to completion despite outer "
        "cancel; instead it was torn down before reaching natural end"
    )
    assert not inner_was_cancelled["yes"], (
        "shielded warm-master inner must not see CancelledError from the "
        "outer task cancel"
    )
