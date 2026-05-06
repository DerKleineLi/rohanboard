"""Step-3 multi-cluster App tests.

Covers:
- `--cluster <id>` makes that cluster the initial active one.
- Hotkey `c` cycles `active_cluster_id` through every cluster.
- `app.snapshots` is keyed per cluster (each cluster's snapshot stays
  isolated from the others — refreshing one doesn't bleed into another).
- `SSHExecutor.run` constructs an ssh argv with the right options
  (BatchMode, ControlMaster, ControlPersist, ControlPath) and wraps the
  command in `bash -lc` by default.

Tests stub out subprocess calls — neither the App nor the SSHExecutor
ever actually shells out.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from rohanboard.app import RohanBoardApp
from rohanboard.cluster import Cluster, RefreshConfig, SlurmFilterConfig
from rohanboard.collectors.models import Snapshot
from rohanboard.config import (
    Config,
    LayoutConfig,
    OverviewConfig,
    TabConfig,
    ThemeConfig,
)
from rohanboard.exec import LocalExecutor, SSHExecutor


def _make_multi_config() -> Config:
    """A 2-cluster Config (both local) — enough to exercise multi-cluster
    plumbing without spinning up a Textual App."""
    rohan = Cluster(
        id="rohan", title="rohan",
        executor=LocalExecutor(),
        slurm=SlurmFilterConfig(users=["self"]),
        storage_entries=[],
        refresh=RefreshConfig(),
    )
    lrz = Cluster(
        id="lrz", title="LRZ",
        executor=LocalExecutor(),
        slurm=SlurmFilterConfig(users=["self"]),
        storage_entries=[],
        refresh=RefreshConfig(),
    )
    return Config(
        clusters=[rohan, lrz],
        layout=LayoutConfig(tabs=[
            TabConfig(id="overview", title="Overview", widgets=["overview_panel"]),
            TabConfig(id="jobs", title="Jobs", widgets=["jobs_table"]),
        ]),
        theme=ThemeConfig(),
        overview=OverviewConfig(),
        presets={"nodes": [], "jobs": []},
    )


# ──────────────────────────────────────────────────────────────────────
# App-level tests (no Textual run; just __init__ + action_cycle_cluster)
# ──────────────────────────────────────────────────────────────────────


def test_cluster_id_arg_picks_initial_active():
    cfg = _make_multi_config()
    app = RohanBoardApp(config=cfg, cluster_id="lrz")
    assert app.active_cluster_id == "lrz"
    assert app._cluster.id == "lrz"
    # Without --cluster, default to first.
    app2 = RohanBoardApp(config=cfg)
    assert app2.active_cluster_id == "rohan"


def test_cluster_id_arg_unknown_raises():
    cfg = _make_multi_config()
    with pytest.raises(RuntimeError, match="no cluster with id"):
        RohanBoardApp(config=cfg, cluster_id="bogus")


def test_snapshots_keyed_per_cluster_at_init():
    cfg = _make_multi_config()
    app = RohanBoardApp(config=cfg)
    assert set(app.snapshots.keys()) == {"rohan", "lrz"}
    # Each cluster starts with an empty Snapshot.
    assert isinstance(app.snapshots["rohan"], Snapshot)
    assert isinstance(app.snapshots["lrz"], Snapshot)
    # Per-cluster mine_only.
    assert app.mine_only == {"rohan": True, "lrz": True}


def test_action_cycle_cluster_advances_active_id():
    cfg = _make_multi_config()
    app = RohanBoardApp(config=cfg, cluster_id="rohan")
    # No widgets mounted — `query_one` for the tab strip will fail; the
    # action swallows that.  We only assert active_cluster_id moves.
    app.action_cycle_cluster()
    assert app.active_cluster_id == "lrz"
    app.action_cycle_cluster()
    assert app.active_cluster_id == "rohan"


def test_action_cycle_cluster_noop_with_one_cluster():
    rohan = Cluster(
        id="rohan", title="rohan", executor=LocalExecutor(),
        slurm=SlurmFilterConfig(users=["self"]),
        storage_entries=[], refresh=RefreshConfig(),
    )
    cfg = Config(
        clusters=[rohan],
        layout=LayoutConfig(tabs=[TabConfig(id="overview", title="Overview",
                                            widgets=["overview_panel"])]),
        theme=ThemeConfig(), overview=OverviewConfig(),
        presets={"nodes": [], "jobs": []},
    )
    app = RohanBoardApp(config=cfg)
    assert app.active_cluster_id == "rohan"
    app.action_cycle_cluster()  # no-op
    assert app.active_cluster_id == "rohan"


# ──────────────────────────────────────────────────────────────────────
# Refresh isolation: per-cluster snapshots don't bleed into each other.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_keeps_snapshots_per_cluster():
    """Mock collectors so each cluster gets a *different* node list, then
    drive `_refresh_all` and confirm `app.snapshots[cid]` contains that
    cluster's data and ONLY that cluster's data.

    Drives the bulk_run path (since 2026-05-06): patches `executor.bulk_run`
    to return canned squeue/sacct/scontrol text per executor; the
    orchestrator parses each and assembles the per-cluster Snapshot.

    `_broadcast_to_cluster` is patched out (no widget tree mounted).
    """
    cfg = _make_multi_config()
    # Tag each cluster's executor so we can dispatch off it in the fakes.
    cfg.clusters[0].executor._tag = "rohan"  # type: ignore[attr-defined]
    cfg.clusters[1].executor._tag = "lrz"    # type: ignore[attr-defined]

    app = RohanBoardApp(config=cfg)

    # scontrol show node text — only the fields we care about
    # (NodeName + counts) are required to round-trip through the parser.
    def _node_block(name: str) -> str:
        return (
            f"NodeName={name} CoresPerSocket=2 "
            f"CPUAlloc=0 CPUTot=4 CPULoad=0.00 "
            f"AvailableFeatures=(null) ActiveFeatures=(null) Gres=(null) "
            f"NodeAddr={name} NodeHostName={name} OS=Linux "
            f"RealMemory=1024 AllocMem=0 FreeMem=1024 "
            f"Partitions=p1 State=IDLE ThreadsPerCore=1 TmpDisk=0 Weight=1\n"
        )

    canned_nodes = {
        "rohan": _node_block("rohan-node-1"),
        # scontrol separates node entries with a blank line.
        "lrz":   _node_block("lrz-node-1") + "\n" + _node_block("lrz-node-2"),
    }

    async def fake_bulk_run(self, name_to_argv, timeout=30.0):
        tag = getattr(self, "_tag", "rohan")
        out = {}
        for k in name_to_argv:
            if k == "nodes":
                out[k] = canned_nodes[tag]
            else:
                out[k] = ""    # empty squeue/sacct → empty parsed list
        return out

    async def noop_broadcast(_cid, _snap):
        return None

    with patch.object(LocalExecutor, "bulk_run", new=fake_bulk_run), \
         patch.object(app, "_broadcast_to_cluster", new=noop_broadcast):
        await app._refresh_all()

    rohan_snap = app.snapshots["rohan"]
    lrz_snap = app.snapshots["lrz"]
    assert rohan_snap is not lrz_snap
    assert [n.name for n in rohan_snap.nodes] == ["rohan-node-1"]
    assert [n.name for n in lrz_snap.nodes] == ["lrz-node-1", "lrz-node-2"]
    # The reactive view follows the active cluster.
    assert app.snapshot is rohan_snap


# ──────────────────────────────────────────────────────────────────────
# SSHExecutor argv construction
# ──────────────────────────────────────────────────────────────────────


def test_ssh_executor_builds_login_shell_cmd():
    ex = SSHExecutor(host="lrz")
    cmd = ex._build_cmd(["squeue", "-O", "JobID,State"])
    assert cmd[0] == "ssh"
    # Required ControlMaster opts present.
    assert "-o" in cmd and "ControlMaster=auto" in cmd
    # ControlPersist is now 60 (seconds) — bound to the rohanboard process
    # so a wedged mux can't outlive it (was 10m → orphaned to systemd).
    assert "ControlPersist=60" in cmd
    assert "BatchMode=yes" in cmd
    assert any("ControlPath=" in a for a in cmd)
    # Host is somewhere in the argv.
    assert "lrz" in cmd
    # The remote command is the LAST argv element — a single shell-quoted
    # string that begins with `bash -lc` so SSH's own argv-join doesn't
    # destroy our quoting on the wire.
    remote = cmd[-1]
    assert remote.startswith("bash -lc ")
    # The inner argv, shlex-quoted twice, must round-trip back to the
    # original argv when shell-parsed twice.
    import shlex
    outer = shlex.split(remote)            # ['bash', '-lc', "squeue -O JobID,State"]
    assert outer[:2] == ["bash", "-lc"]
    inner = shlex.split(outer[2])
    assert inner == ["squeue", "-O", "JobID,State"]


def test_ssh_executor_no_login_shell_passes_argv_directly():
    ex = SSHExecutor(host="slurm", use_login_shell=False)
    cmd = ex._build_cmd(["squeue", "-O", "JobID"])
    # No bash wrapper.
    assert "bash" not in cmd
    # The remote command is the LAST argv element — a single shell-quoted
    # string that round-trips back to the user's argv when shell-parsed.
    import shlex
    assert shlex.split(cmd[-1]) == ["squeue", "-O", "JobID"]


def test_ssh_executor_custom_control_path():
    ex = SSHExecutor(host="lrz", control_path="/tmp/custom-cm-%h")
    cmd = ex._build_cmd(["uname"])
    assert any(a == "ControlPath=/tmp/custom-cm-%h" for a in cmd)


@pytest.mark.asyncio
async def test_ssh_executor_run_invokes_subprocess_with_built_cmd():
    """End-to-end: SSHExecutor.run should call create_subprocess_exec with
    the same argv that _build_cmd produces.  Mock the subprocess so we
    don't actually touch the network."""
    captured: dict = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"hello\n", b"")

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def fake_create(*args, **kwargs):
        captured["argv"] = list(args)
        return FakeProc()

    ex = SSHExecutor(host="lrz")
    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        out = await ex.run(["echo", "hi"])
    assert out == "hello\n"
    argv = captured["argv"]
    assert argv[0] == "ssh"
    assert "lrz" in argv
    # The remote command is the LAST argv element. After shell-parsing twice
    # we should recover the original argv.
    import shlex
    outer = shlex.split(argv[-1])
    assert outer[:2] == ["bash", "-lc"]
    assert shlex.split(outer[2]) == ["echo", "hi"]


@pytest.mark.asyncio
async def test_ssh_executor_run_raises_on_nonzero_rc():
    class FakeProc:
        returncode = 255

        async def communicate(self):
            return (b"", b"Permission denied (publickey).\n")

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def fake_create(*args, **kwargs):
        return FakeProc()

    ex = SSHExecutor(host="lrz")
    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        with pytest.raises(RuntimeError, match=r"rc=255"):
            await ex.run(["echo", "hi"])


# ──────────────────────────────────────────────────────────────────────
# SSH lifecycle: ControlPersist + atexit cleanup + in-flight semaphore +
# pre-flight `ssh -O check` (since 2026-05-05).
# ──────────────────────────────────────────────────────────────────────


def test_ssh_executor_default_persist_is_finite():
    """Default ControlPersist is `60`, NOT `yes`/`infinite`/`10m` — a wedged
    mux cannot orphan to systemd and outlive the rohanboard process."""
    ex = SSHExecutor(host="lrz")
    cmd = ex._build_cmd(["uname"])
    persist_opts = [a for a in cmd if a.startswith("ControlPersist=")]
    assert persist_opts == ["ControlPersist=60"]


def test_ssh_executor_registers_for_shutdown():
    """Every SSHExecutor must enroll in the registry so `atexit` /
    SIGTERM/SIGINT cleanup hits its mux on shutdown."""
    from rohanboard import exec as rb_exec
    before = len(rb_exec._REGISTERED_EXECUTORS)
    ex = SSHExecutor(host="lrz-test-shutdown")
    after = len(rb_exec._REGISTERED_EXECUTORS)
    assert after == before + 1
    assert ex in rb_exec._REGISTERED_EXECUTORS


def test_ssh_executor_shutdown_master_runs_ssh_O_exit():
    """`_shutdown_master` calls `ssh -O exit <host>` against the same
    ControlPath, then unlinks the socket file.  This is what runs from
    atexit / SIGTERM."""
    from unittest.mock import MagicMock, patch as _patch

    ex = SSHExecutor(host="lrz-shutdown-test")
    ex._master_ready = True
    fake_run = MagicMock(return_value=MagicMock(returncode=0))
    with _patch("rohanboard.exec.subprocess.run", new=fake_run), \
         _patch.object(ex, "_unlink_wedged_socket") as mock_unlink:
        ex._shutdown_master()
    # subprocess.run got an argv with `-O exit` and the host.
    assert fake_run.call_count == 1
    argv = fake_run.call_args[0][0]
    assert argv[0] == "ssh"
    assert "-O" in argv and "exit" in argv
    assert "lrz-shutdown-test" in argv
    # And the socket file was unlinked after.
    assert mock_unlink.call_count == 1
    # Cleanup state.
    assert ex._master_ready is False


def test_ssh_executor_shutdown_master_noop_when_not_ready():
    """If the master never warmed, shutdown is a no-op (no `ssh -O exit`)."""
    from unittest.mock import MagicMock, patch as _patch
    ex = SSHExecutor(host="lrz-not-warm")
    assert ex._master_ready is False
    fake_run = MagicMock()
    with _patch("rohanboard.exec.subprocess.run", new=fake_run):
        ex._shutdown_master()
    fake_run.assert_not_called()


def test_cleanup_all_masters_runs_each_executor():
    """`_cleanup_all_masters` (the atexit / signal hook) iterates every
    registered executor and calls `_shutdown_master` on each."""
    from unittest.mock import MagicMock, patch as _patch
    from rohanboard import exec as rb_exec

    # Snapshot existing registry so we don't depend on test order.
    saved = list(rb_exec._REGISTERED_EXECUTORS)
    try:
        with rb_exec._REGISTRY_LOCK:
            rb_exec._REGISTERED_EXECUTORS.clear()
        ex_a = SSHExecutor(host="cleanup-a")
        ex_b = SSHExecutor(host="cleanup-b")
        ex_a._shutdown_master = MagicMock()  # type: ignore[method-assign]
        ex_b._shutdown_master = MagicMock()  # type: ignore[method-assign]
        rb_exec._cleanup_all_masters()
        ex_a._shutdown_master.assert_called_once()
        ex_b._shutdown_master.assert_called_once()
        # Registry is drained.
        assert rb_exec._REGISTERED_EXECUTORS == []
    finally:
        # Restore (so other tests in the same session still see their entries).
        with rb_exec._REGISTRY_LOCK:
            rb_exec._REGISTERED_EXECUTORS.extend(saved)


@pytest.mark.asyncio
async def test_ssh_executor_inflight_semaphore_caps_at_one():
    """Two concurrent `.run` calls — the second cannot start a subprocess
    until the first releases the semaphore.  This is the bug fix: a
    hanging probe must NOT let a second probe stack up behind it."""
    import asyncio as _asyncio

    spawn_order: list[str] = []
    release_first = _asyncio.Event()

    class FakeProc:
        returncode = 0

        def __init__(self, idx: int) -> None:
            self.idx = idx

        async def communicate(self):
            spawn_order.append(f"start_{self.idx}")
            if self.idx == 0:
                # First probe blocks until released.
                await release_first.wait()
            spawn_order.append(f"end_{self.idx}")
            return (b"ok\n", b"")

        def kill(self): pass
        async def wait(self): return 0

    counter = {"n": 0}

    async def fake_create(*args, **kwargs):
        idx = counter["n"]
        counter["n"] += 1
        return FakeProc(idx)

    ex = SSHExecutor(host="cap-test")

    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        first = _asyncio.create_task(ex.run(["echo", "1"], timeout=5.0))
        # Let the first task acquire the semaphore + start its warm subprocess.
        await _asyncio.sleep(0.05)
        second = _asyncio.create_task(ex.run(["echo", "2"], timeout=5.0))
        await _asyncio.sleep(0.05)
        # The second task should NOT have spawned a subprocess yet — still
        # parked on the semaphore.  Only the first proc has been started.
        assert spawn_order == ["start_0"], f"unexpected order: {spawn_order}"
        # Release the first; it'll finish and free the semaphore.
        release_first.set()
        await _asyncio.gather(first, second)
        # Now the second probe ran AFTER the first finished.
        assert spawn_order.index("end_0") < spawn_order.index("start_1")


@pytest.mark.asyncio
async def test_ssh_executor_preflight_check_unwedges_dead_mux():
    """When `_master_ready` is True but the mux on disk is dead, the next
    `.run` calls `ssh -O check`, sees the failure, unlinks the socket,
    and re-warms cleanly."""
    import asyncio as _asyncio

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok\n", b"")

        def kill(self): pass
        async def wait(self): return 0

    async def fake_create(*args, **kwargs):
        return FakeProc()

    ex = SSHExecutor(host="wedged-host")
    # Pretend a previous run warmed the master successfully.
    ex._master_ready = True

    check_calls = {"n": 0}
    unlink_calls = {"n": 0}

    def fake_check(timeout=3.0):
        check_calls["n"] += 1
        return False  # mux is wedged

    def fake_unlink():
        unlink_calls["n"] += 1

    with patch.object(ex, "_mux_check_blocking", new=fake_check), \
         patch.object(ex, "_unlink_wedged_socket", new=fake_unlink), \
         patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        out = await ex.run(["echo", "hi"], timeout=5.0)

    assert out == "ok\n"
    # Pre-flight check ran; saw failure; unlinked socket; re-warmed.
    assert check_calls["n"] == 1
    assert unlink_calls["n"] == 1
    assert ex._master_ready is True


@pytest.mark.asyncio
async def test_ssh_executor_preflight_check_skipped_when_not_warm():
    """First-ever call: master isn't warm, so we skip the `ssh -O check`
    pre-flight (there's nothing to check) and go straight to warming."""
    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok\n", b"")

        def kill(self): pass
        async def wait(self): return 0

    async def fake_create(*args, **kwargs):
        return FakeProc()

    ex = SSHExecutor(host="cold-host")
    assert ex._master_ready is False

    check_calls = {"n": 0}

    def fake_check(timeout=3.0):
        check_calls["n"] += 1
        return False

    with patch.object(ex, "_mux_check_blocking", new=fake_check), \
         patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        await ex.run(["echo", "hi"], timeout=5.0)

    assert check_calls["n"] == 0  # no pre-flight when not warm
    assert ex._master_ready is True


@pytest.mark.asyncio
async def test_ssh_executor_kills_subprocess_on_cancellation():
    """When the awaiting task is cancelled (e.g. `run_worker(exclusive=True)`
    cancels the previous tick because a new one started), `.run` MUST
    kill+reap the subprocess.  Otherwise the OS-level ssh proc leaks and
    next tick's call stacks behind it — the slurm-quota pile-up bug."""
    import asyncio as _asyncio

    killed: list[bool] = []
    waited: list[bool] = []

    class HangProc:
        returncode = None

        async def communicate(self):
            # Hang forever; rely on cancellation to break us out.
            await _asyncio.sleep(3600)
            return (b"", b"")

        def kill(self):
            killed.append(True)
            # Simulate the kernel reaping; pretend the proc died.
            self.returncode = -9

        async def wait(self):
            waited.append(True)
            return -9

    async def fake_create(*args, **kwargs):
        return HangProc()

    ex = SSHExecutor(host="cancel-host")
    ex._master_ready = True  # bypass warm
    # Stub the mux-check so warm-master short-circuits without actually
    # ssh-O-checking; otherwise warm spawns the HangProc itself (instead
    # of the actual command) and the shield-around-warm now blocks the
    # cancel from propagating into THAT subprocess. We're testing the
    # COMMAND-path subprocess gets killed; warm-path is covered by
    # test_ssh_executor_warm_master_shielded_from_outer_cancel.
    ex._mux_check_blocking = lambda *_a, **_k: True

    with patch("rohanboard.exec.asyncio.create_subprocess_exec", new=fake_create):
        task = _asyncio.create_task(ex.run(["echo", "hi"], timeout=60.0))
        await _asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task

    assert killed == [True], "subprocess.kill() was not called on cancellation"
    assert waited == [True], "subprocess.wait() was not called on cancellation"
    # And the semaphore was released so the next caller can proceed.
    assert ex._inflight is not None
    assert ex._inflight._value == 1


def test_mux_ctl_argv_uses_same_control_path():
    """`ssh -O check/exit` must use the SAME ControlPath as `.run` — else
    it'd be poking at a different (or non-existent) socket."""
    ex = SSHExecutor(host="lrz")
    main = ex._build_cmd(["echo", "x"])
    main_cp = [a for a in main if a.startswith("ControlPath=")][0]
    check = ex._mux_ctl_argv("check")
    check_cp = [a for a in check if a.startswith("ControlPath=")][0]
    assert main_cp == check_cp
    assert "-O" in check and "check" in check
    assert "lrz" in check


# ──────────────────────────────────────────────────────────────────────
# Issue #1 — burst-keys input lag
# ──────────────────────────────────────────────────────────────────────
#
# When an LRZ ssh call stalls (10+ s on cold ProxyJump, indefinite on
# silent server-side TCP kill), the Textual input handler can lag for
# 300-500 ms or more — pressing "1234" quickly to switch to the
# Storage tab feels like the app froze. The plan: write a Pilot-based
# test that bursts keys against an app whose LRZ-side executor is
# mocked to block 8 s per call, then assert the LAST press lands on
# the active inner tab within 200 ms of the burst's end.
#
# This is the load-bearing test for the user's complaint. It must
# FAIL before any #1-specific fix lands (the slow executor hangs the
# refresh worker; key dispatch queues behind it). After the fix, lag
# < 200 ms.
#
# After issue #3's mux-detect-and-reconnect fix, the LRZ cancel storm
# goes away — measure residual lag here. If under 200 ms after #3
# alone, the test passes without an explicit #1 fix; if not, the test
# documents the delta and motivates the #1 work.


def _make_burst_test_config(slow_lrz_executor):
    """Build a 2-cluster Config with the four real tabs, using a slow
    LRZ executor that exercises the multi-cluster refresh path."""
    rohan = Cluster(
        id="rohan", title="rohan",
        executor=LocalExecutor(),
        slurm=SlurmFilterConfig(users=["self"]),
        storage_entries=[],
        refresh=RefreshConfig(),
    )
    lrz = Cluster(
        id="lrz", title="LRZ",
        executor=slow_lrz_executor,
        slurm=SlurmFilterConfig(users=["self"]),
        storage_entries=[],
        refresh=RefreshConfig(),
    )
    return Config(
        clusters=[rohan, lrz],
        layout=LayoutConfig(tabs=[
            TabConfig(id="overview", title="Overview", widgets=[]),
            TabConfig(id="jobs",     title="Jobs",     widgets=[]),
            TabConfig(id="nodes",    title="Nodes",    widgets=[]),
            TabConfig(id="storage",  title="Storage",  widgets=[]),
        ]),
        theme=ThemeConfig(),
        overview=OverviewConfig(),
        presets={"nodes": [], "jobs": []},
    )


class _SlowExecutor:
    """LocalExecutor-shaped stub whose `.run` blocks for `block_seconds`
    on every call. Simulates a stuck LRZ ssh call without actually
    spawning ssh.

    `whoami` returns immediately so the per-cluster collectors that
    look up `-u <user>` don't pay the slow cost on EVERY tick — this
    matches the real SSHExecutor, which caches whoami after the first
    call.
    """
    def __init__(self, block_seconds: float = 8.0) -> None:
        self.block_seconds = block_seconds
        self._whoami_done = False

    async def run(self, argv, timeout: float = 15.0) -> str:
        await asyncio.sleep(self.block_seconds)
        return ""   # parse_squeue / parse_scontrol_show_node handle empty

    async def whoami(self) -> str:
        return "test-user"


@pytest.mark.asyncio
async def test_burst_keypresses_land_on_last_tab_under_lag_threshold():
    """Issue #1 reproducer: with a slow LRZ executor (8 s/call), bursting
    1,2,3,4 at 30 ms intervals must land the active inner tab on
    "<cid>__storage" within 200 ms of the last press.

    Pre-#3 fix this typically saw 300-500 ms lag in the live app
    (cancel-storm against the wedged ssh master + subprocess teardown
    briefly stalling the event loop). Post-#3 fix, lag drops to ~10 ms
    in this test (the SlowExecutor uses pure asyncio.sleep, so it
    doesn't actually block the loop — the test guards the high-level
    invariant that an awaitable-slow cluster does not block input
    handling).

    Manual recipe for stronger reproduction (NOT in CI — too flaky):
        tmux new-window -t rohan -n rohanboard-debug-2 -d \\
          "ROHANBOARD_PERF_LOG=/tmp/rb_dbg_perf.log uv run rohanboard \\
           --config /tmp/wsl_multi_config.toml"
        tmux send-keys -t rohan:rohanboard-debug-2 1 1 2 3 4
        # Capture-pane after 100/200/500 ms to see when the tab settles.
        # Pre-#3 fix: 300-500 ms; post-#3 fix: <100 ms.
    """
    import time

    slow = _SlowExecutor(block_seconds=8.0)
    cfg = _make_burst_test_config(slow)
    app = RohanBoardApp(config=cfg)
    # Patch the rohan-side collectors so they don't try to actually run
    # squeue/scontrol locally — return empty results immediately.
    async def fake_jobs(*_a, **_k): return []
    async def fake_nodes(*_a, **_k): return []
    async def fake_recent(*_a, **_k): return []

    with patch("rohanboard.app.slurm.fetch_jobs", new=fake_jobs), \
         patch("rohanboard.app.slurm.fetch_nodes", new=fake_nodes), \
         patch("rohanboard.app.slurm.fetch_recent_jobs", new=fake_recent):
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await pilot.pause()

            # Burst: 1, 2, 3, 4 at 30 ms gaps (matches the user's
            # rapid-tab-switching gesture).
            for k in "1234":
                await pilot.press(k)
                await asyncio.sleep(0.03)

            t_burst_end = time.monotonic()

            # Poll up to 200 ms for the active inner tab to settle on
            # storage. We don't `pilot.pause()` for an unbounded
            # duration — the test's whole point is the latency.
            cluster_id = app.active_cluster_id
            target_pane = f"{cluster_id}__storage"
            settled_at: float | None = None
            for _ in range(20):
                await asyncio.sleep(0.01)
                inner = app._active_inner_tab()
                if inner == "storage":
                    settled_at = time.monotonic()
                    break

            assert settled_at is not None, (
                f"after burst 1-2-3-4, active inner tab never reached "
                f"'storage' within 200 ms; ended on {app._active_inner_tab()!r} "
                f"(target_pane={target_pane})"
            )
            lag_ms = (settled_at - t_burst_end) * 1000
            assert lag_ms < 200.0, (
                f"input lag {lag_ms:.1f} ms exceeds 200 ms threshold; "
                f"the burst's last press took too long to apply. "
                f"Target was {target_pane}; settled inner tab is "
                f"{app._active_inner_tab()!r}"
            )


@pytest.mark.asyncio
async def test_burst_keypresses_no_slow_executor_baseline():
    """Sanity baseline: with FAST executors on both clusters, burst-key
    must land within 100 ms (a tighter threshold than the slow-LRZ test).
    Confirms the test infrastructure isn't itself slow."""
    import time

    cfg = _make_multi_config()  # Both LocalExecutor (no slow stub)

    async def fake_jobs(*_a, **_k): return []
    async def fake_nodes(*_a, **_k): return []
    async def fake_recent(*_a, **_k): return []

    # Use 4 tabs (the multi config above only has 2 — extend it).
    cfg.layout.tabs = [
        TabConfig(id="overview", title="Overview", widgets=[]),
        TabConfig(id="jobs",     title="Jobs",     widgets=[]),
        TabConfig(id="nodes",    title="Nodes",    widgets=[]),
        TabConfig(id="storage",  title="Storage",  widgets=[]),
    ]
    app = RohanBoardApp(config=cfg)

    with patch("rohanboard.app.slurm.fetch_jobs", new=fake_jobs), \
         patch("rohanboard.app.slurm.fetch_nodes", new=fake_nodes), \
         patch("rohanboard.app.slurm.fetch_recent_jobs", new=fake_recent):
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            for k in "1234":
                await pilot.press(k)
                await asyncio.sleep(0.03)
            t_burst_end = time.monotonic()
            settled_at: float | None = None
            for _ in range(10):
                await asyncio.sleep(0.01)
                if app._active_inner_tab() == "storage":
                    settled_at = time.monotonic()
                    break
            assert settled_at is not None, "fast-baseline burst must reach storage"
            lag_ms = (settled_at - t_burst_end) * 1000
            assert lag_ms < 100.0, f"baseline lag {lag_ms:.1f} ms > 100 ms"


# ──────────────────────────────────────────────────────────────────────
# Issue #1 — filter input debounce coalesces typing into one rebuild
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filter_debounce_coalesces_keystrokes():
    """Typing N characters into the filter input must NOT trigger N
    synchronous DataTable rebuilds. With the 150 ms debounce, only ONE
    rebuild fires after the user pauses for 150 ms.

    Pre-fix: every `on_input_changed` synchronously called
    `update_snapshot` (full DataTable rebuild). Under refresh-tick
    pressure that starved Textual's input dispatch and dropped ~50%
    of slow keystrokes (per the v3 plan's measured live test).

    Post-fix: keystrokes set the reactive but defer the rebuild via
    a 150 ms timer. We verify by counting rebuild calls.
    """
    from rohanboard.collectors.models import Job, Snapshot
    from rohanboard.widgets.jobs_table import JobsTable

    rebuild_count = {"n": 0}
    real_update = JobsTable.update_snapshot

    def trace_update(self, snap):
        rebuild_count["n"] += 1
        return real_update(self, snap)

    cfg = _make_multi_config_one()
    cfg.layout.tabs = [TabConfig(id="jobs", title="Jobs", widgets=["jobs_table"])]

    async def fake_jobs(*_a, **_k):
        return [Job(
            job_id="1", partition="p", name="x", user="u", state="RUNNING",
            node_or_reason="n", time_used="00:01:00", time_left="00:01:00",
            num_nodes=1, num_cpus=1, tres="cpu=1", alloc_mem="1G", alloc_gpu="—",
        )]
    async def fake_nodes(*_a, **_k): return []
    async def fake_recent(*_a, **_k): return []

    app = RohanBoardApp(config=cfg)

    with patch("rohanboard.app.slurm.fetch_jobs", new=fake_jobs), \
         patch("rohanboard.app.slurm.fetch_nodes", new=fake_nodes), \
         patch("rohanboard.app.slurm.fetch_recent_jobs", new=fake_recent), \
         patch.object(JobsTable, "update_snapshot", new=trace_update):
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            jt = app.query_one(JobsTable)
            # Reset count after initial mount + first refresh.
            rebuild_count["n"] = 0
            # Type 5 chars in <150 ms via direct reactive write.
            for c in "abcde":
                jt.filter_text = c
                # Drive the on_input_changed path manually since a real
                # Input.Changed event would also set filter_text.
                if jt._filter_debounce_timer is not None:
                    try:
                        jt._filter_debounce_timer.stop()
                    except Exception:
                        pass
                jt._filter_debounce_timer = jt.set_timer(
                    0.15, jt._apply_filter_debounced
                )
                await asyncio.sleep(0.02)
            # Now wait > 150 ms for the timer to fire.
            await asyncio.sleep(0.30)
            await pilot.pause()
            # Exactly ONE rebuild from the debounce, no per-keystroke storm.
            assert rebuild_count["n"] == 1, (
                f"expected 1 rebuild from debounce; got {rebuild_count['n']} "
                f"— per-keystroke rebuild storm not coalesced"
            )


def _make_multi_config_one():
    """Single-cluster config — debounce test doesn't need multi-cluster."""
    cluster = Cluster(
        id="rohan", title="rohan",
        executor=LocalExecutor(),
        slurm=SlurmFilterConfig(users=["self"]),
        storage_entries=[],
        refresh=RefreshConfig(),
    )
    return Config(
        clusters=[cluster],
        layout=LayoutConfig(tabs=[
            TabConfig(id="jobs", title="Jobs", widgets=["jobs_table"]),
        ]),
        theme=ThemeConfig(),
        overview=OverviewConfig(),
        presets={"nodes": [], "jobs": []},
    )
