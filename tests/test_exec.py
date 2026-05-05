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
