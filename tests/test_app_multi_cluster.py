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

    The collectors are dispatched by the executor passed in — we tag the
    LocalExecutor instances with a sentinel attribute and inspect it.

    `_broadcast_to_cluster` is patched out (no widget tree mounted).
    """
    from rohanboard.collectors.models import Node

    cfg = _make_multi_config()
    # Tag each cluster's executor so we can dispatch off it in the fakes.
    cfg.clusters[0].executor._tag = "rohan"  # type: ignore[attr-defined]
    cfg.clusters[1].executor._tag = "lrz"    # type: ignore[attr-defined]

    app = RohanBoardApp(config=cfg)

    def make_node(name: str) -> Node:
        return Node(
            name=name, partitions=[], state="IDLE",
            cpu_total=4, cpu_alloc=0, cpu_load=0.0,
            mem_total_mb=1024, mem_alloc_mb=0, mem_free_mb=1024,
            gpus=[],
        )

    fake_nodes = {
        "rohan": [make_node("rohan-node-1")],
        "lrz":   [make_node("lrz-node-1"), make_node("lrz-node-2")],
    }

    async def fake_fetch_jobs(executor, users):
        return []

    async def fake_fetch_nodes(executor):
        tag = getattr(executor, "_tag", "rohan")
        return list(fake_nodes[tag])

    async def fake_fetch_recent(executor, users):
        return []

    async def noop_broadcast(_cid, _snap):
        return None

    with patch("rohanboard.app.slurm.fetch_jobs", new=fake_fetch_jobs), \
         patch("rohanboard.app.slurm.fetch_nodes", new=fake_fetch_nodes), \
         patch("rohanboard.app.slurm.fetch_recent_jobs", new=fake_fetch_recent), \
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
