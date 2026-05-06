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
async def test_ssh_executor_run_per_call_timeout_does_NOT_mark_mux_dead():
    """When `.run`'s per-call wait_for fires, the executor must NOT mark
    the mux dead — a slow-but-reachable cluster (LRZ scontrol slow on
    busy controller) can legitimately exceed the 15 s default timeout
    without the underlying mux being broken. Marking dead in this path
    would force a 10+ s re-warm on every subsequent tick, perpetually
    losing time before the next legitimate call.

    The warm-master path's pre-flight `ssh -O check` is what catches a
    truly-dead mux; the per-call timeout just propagates upward as a
    TimeoutError without touching mux state. The semaphore is released
    (always, in `finally`) so the next call can immediately try.
    """
    ex = SSHExecutor(host="timeout-detect-host")
    ex._master_ready = True   # pretend warm path already completed
    ex._mux_check_blocking = lambda *_a, **_k: True  # type: ignore

    mark_calls = {"n": 0}
    real_mark = ex._mark_mux_dead

    def trace_mark():
        mark_calls["n"] += 1
        real_mark()

    ex._mark_mux_dead = trace_mark

    async def fake_create(*args, **kwargs):
        return _make_hang_proc()

    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        with pytest.raises(asyncio.TimeoutError):
            await ex.run(["squeue"], timeout=0.05)

    assert mark_calls["n"] == 0, (
        "per-call TimeoutError must NOT call _mark_mux_dead — that would "
        "tear down a working mux on every slow-call tick"
    )
    # _master_ready is unchanged.
    assert ex._master_ready is True
    # Semaphore released so next call can proceed immediately.
    assert ex._inflight is not None
    assert ex._inflight._value == 1


@pytest.mark.asyncio
async def test_ssh_executor_recovers_after_timeout_on_next_call():
    """End-to-end: first call hangs → TimeoutError; SECOND call succeeds.
    Confirms the semaphore is released and no state leaks between calls.
    The per-call TimeoutError does NOT mark the mux dead (see
    `test_ssh_executor_run_per_call_timeout_does_NOT_mark_mux_dead`),
    so the second call goes straight through with the warm mux.
    """
    ex = SSHExecutor(host="recover-host")
    ex._master_ready = True   # First call assumes warm
    ex._mux_check_blocking = lambda *_a, **_k: True  # type: ignore

    # First subprocess hangs; second is fine.
    proc_idx = {"i": 0}

    async def fake_create(*args, **kwargs):
        proc_idx["i"] += 1
        if proc_idx["i"] == 1:
            return _make_hang_proc()
        return _make_ok_proc()

    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        # First call: should TimeoutError after the per-call timeout.
        with pytest.raises(asyncio.TimeoutError):
            await ex.run(["squeue"], timeout=0.05)
        # mux is NOT marked dead on per-call timeout (slow != broken).
        assert ex._master_ready is True
        # Second call: mux is still warm, command goes straight through.
        out = await ex.run(["squeue"], timeout=2.0)
        assert out.strip() == "ok"


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


# ──────────────────────────────────────────────────────────────────────
# Issue #2 — bulk_run coalesces per-cluster collector ssh fan-out
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_run_reduces_n_ssh_calls_to_one():
    """4 collector commands must be packed into ONE ssh subprocess call.

    Previous (broken) pattern: 4 × executor.run inside asyncio.gather
    serialised behind Semaphore(1) per host = 4 × per-call latency.
    For LRZ (~1.4 s warm-call) that's 5.6 s, busting the 5 s tick budget
    and getting cancelled by run_worker(exclusive=True).

    Fix: bulk_run packs all 4 into a sentinel-framed bash pipeline →
    ONE asyncio.create_subprocess_exec call → ONE Semaphore acquisition.
    """
    proc_count = {"n": 0}
    captured_argv = []

    class FrameProc:
        returncode = 0

        def __init__(self, argv):
            self.argv = argv

        async def communicate(self):
            # Synthesize a sentinel-framed response. argv[-1] is the
            # bash -c script; we extract the names after RBOUTBEGIN.
            import re as _re
            script = self.argv[-1]
            # Find the literal token via the first echo, then synthesize
            # a fake output for each name in order.
            m = _re.findall(r"---RBOUTBEGIN:([0-9a-f]+):([^-]+)---", script)
            out_parts = []
            for token, name in m:
                out_parts.append(f"---RBOUTBEGIN:{token}:{name}---")
                out_parts.append(f"<output for {name.strip()}>")
                out_parts.append(f"---RBOUTEND:{token}:{name}---")
            return ("\n".join(out_parts).encode(), b"")

        def kill(self): pass
        async def wait(self): return 0

    async def fake_create(*args, **kwargs):
        proc_count["n"] += 1
        captured_argv.append(list(args))
        last = args[-1] if args else ""
        # Master-warm probe: trivial OK.
        if "_rb_ssh_warm_" in last:
            class Warm:
                returncode = 0
                async def communicate(self): return (b"_rb_ssh_warm_\n", b"")
                def kill(self): pass
                async def wait(self): return 0
            return Warm()
        return FrameProc(args)

    ex = SSHExecutor(host="bulk-test-host")

    name_to_argv = {
        "jobs":    ["squeue", "-h"],
        "recent":  ["sacct", "-X", "-P", "-n"],
        "nodes":   ["scontrol", "show", "node", "--all"],
        "storage": ["bash", "-lc", "df -B1 /home"],
    }

    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        out = await ex.bulk_run(name_to_argv, timeout=5.0)

    # Master-warm + ONE bulk subprocess. Previous gather pattern would
    # have been 4 separate subprocess calls (or 5 with warm).
    assert proc_count["n"] == 2, (
        f"expected 1 warm + 1 bulk = 2 subprocess calls; got {proc_count['n']}"
    )
    # All 4 outputs returned.
    assert set(out.keys()) == {"jobs", "recent", "nodes", "storage"}
    for name in name_to_argv:
        assert f"<output for {name}>" in out[name], (
            f"slot {name!r} did not round-trip its output: {out[name]!r}"
        )


@pytest.mark.asyncio
async def test_bulk_run_acquires_semaphore_once_for_all_collectors():
    """The per-host Semaphore(1) must be acquired EXACTLY ONCE per
    bulk_run call, regardless of how many collectors are bundled.

    Verifies the fix for the issue-#2 trap: under the legacy
    asyncio.gather pattern, 4 collectors × Semaphore(1) = 4 sequential
    acquisitions; under bulk_run it's 1 acquisition.
    """
    acquire_count = {"n": 0}

    class FastProc:
        returncode = 0
        async def communicate(self):
            return (b"---RBOUTBEGIN:abc:s1---\nx\n---RBOUTEND:abc:s1---\n", b"")
        def kill(self): pass
        async def wait(self): return 0

    async def fake_create(*args, **kwargs):
        last = args[-1] if args else ""
        if "_rb_ssh_warm_" in last:
            class Warm:
                returncode = 0
                async def communicate(self): return (b"_rb_ssh_warm_\n", b"")
                def kill(self): pass
                async def wait(self): return 0
            return Warm()
        return FastProc()

    ex = SSHExecutor(host="sem-test-host")
    # Pre-create the semaphore and wrap acquire to count.
    ex._inflight = asyncio.Semaphore(1)
    real_acq = ex._inflight.acquire

    async def trace_acq():
        acquire_count["n"] += 1
        return await real_acq()

    ex._inflight.acquire = trace_acq  # type: ignore[assignment]

    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        await ex.bulk_run(
            {"a": ["true"], "b": ["true"], "c": ["true"], "d": ["true"]},
            timeout=5.0,
        )

    assert acquire_count["n"] == 1, (
        f"bulk_run must acquire the semaphore exactly once; got "
        f"{acquire_count['n']} acquisitions"
    )


@pytest.mark.asyncio
async def test_bulk_run_sentinels_unguessable_per_call():
    """The sentinel TOKEN must be different across calls so a collector's
    output that contains a literal "---RBOUTBEGIN:..." string can never
    confuse a SUBSEQUENT call's demux.
    """
    captured_scripts = []

    class FastProc:
        returncode = 0
        async def communicate(self):
            return (b"", b"")
        def kill(self): pass
        async def wait(self): return 0

    async def fake_create(*args, **kwargs):
        last = args[-1] if args else ""
        if "_rb_ssh_warm_" in last:
            class Warm:
                returncode = 0
                async def communicate(self): return (b"_rb_ssh_warm_\n", b"")
                def kill(self): pass
                async def wait(self): return 0
            return Warm()
        captured_scripts.append(last)
        return FastProc()

    ex = SSHExecutor(host="token-test-host")

    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        await ex.bulk_run({"a": ["true"]}, timeout=5.0)
        await ex.bulk_run({"a": ["true"]}, timeout=5.0)

    import re as _re
    tokens = [_re.search(r"RBOUTBEGIN:([0-9a-f]+):", s).group(1) for s in captured_scripts]
    assert len(tokens) == 2 and tokens[0] != tokens[1], (
        f"per-call tokens must differ; got {tokens}"
    )


@pytest.mark.asyncio
async def test_local_executor_bulk_run_runs_all_commands():
    """LocalExecutor.bulk_run must run every command and surface stdout."""
    ex = LocalExecutor()
    out = await ex.bulk_run({
        "a": ["/bin/echo", "alpha"],
        "b": ["/bin/echo", "bravo"],
    })
    assert out["a"].strip() == "alpha"
    assert out["b"].strip() == "bravo"


@pytest.mark.asyncio
async def test_local_executor_bulk_run_failed_command_returns_empty():
    """A non-zero rc command in bulk_run yields "" — does NOT raise.

    Caller must distinguish "" (failed) from "" (legitimately empty
    output) by knowing what its parser tolerates. Per #2's parse path
    each downstream collector's parser tolerates empty input.
    """
    ex = LocalExecutor()
    out = await ex.bulk_run({
        "ok":   ["/bin/echo", "ok"],
        "fail": ["/bin/false"],
    })
    assert out["ok"].strip() == "ok"
    assert out["fail"] == ""
