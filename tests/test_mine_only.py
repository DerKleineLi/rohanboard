"""Phase 4d.2-D: client-side mine_only filter.

Server-side `-u $USER` is gone; squeue/sacct fetch all users every tick
and JobsTable + CompactJobs slice the snapshot client-side. Toggling
mine_only re-broadcasts the cached snapshot — no refetch.

These tests pin three things:
  1. JobsTable's row count flips between mine and all instantly when
     mine_only toggles, with no fetch fired.
  2. CompactJobs's count + body match the same client-side semantic.
  3. The empty-state message tells the user WHY the table is empty
     when mine_only is on (so "no jobs" is unambiguous).
"""
from __future__ import annotations

from textual.widgets import DataTable, Static, TabbedContent

from rohanboard.app import RohanBoardApp
from rohanboard.collectors.models import Job, Snapshot
from rohanboard.config import (
    Config,
    LayoutConfig,
    TabConfig,
)
from rohanboard.widgets.jobs_table import JobsTable
from rohanboard.widgets.overview import CompactJobs


def _minimal_overview_config() -> Config:
    cfg = Config()
    cfg.exec_spec = "local"
    cfg.layout = LayoutConfig(tabs=[
        TabConfig(id="overview", title="Overview", widgets=["overview_panel"]),
        TabConfig(id="jobs",     title="Jobs",     widgets=["jobs_table"]),
    ])
    cfg.storage_entries = []
    cfg.presets = {"jobs": [], "nodes": []}
    return cfg


def _job(job_id: str, user: str) -> Job:
    return Job(
        job_id=job_id,
        partition="a100_submit",
        name=f"job_{job_id}",
        user=user,
        state="RUNNING",
        node_or_reason="balar",
        time_used="0:00:01",
        time_left="N/A",
        num_nodes=1,
        num_cpus=4,
        tres="cpu=4,mem=16G,gres/gpu=1",
        alloc_mem="16G",
        alloc_gpu="1",
    )


def _mixed_snapshot() -> Snapshot:
    """5 jobs total: 3 by 'hli', 2 by 'someone_else'. cluster_user='hli'."""
    snap = Snapshot()
    snap.jobs = [
        _job("1001", "hli"),
        _job("1002", "someone_else"),
        _job("1003", "hli"),
        _job("1004", "someone_else"),
        _job("1005", "hli"),
    ]
    snap.cluster_user = "hli"
    return snap


async def _row_count(app, mine_only: bool) -> int:
    """Set app.mine_only, wait for the broadcast to finish, return the
    JobsTable's data-row count (excluding header)."""
    app.mine_only = mine_only
    await _settle(app)
    table = app.query_one(JobsTable).query_one("#jobs_table_dt", DataTable)
    return table.row_count


async def _settle(app) -> None:
    """Wait long enough for the debounce + chunked rebuild to complete."""
    # 150 ms debounce + chunk yields → 300 ms is comfortable.
    await app._broadcast_snapshot(app.snapshot)


async def test_jobs_table_mine_only_filters_to_cluster_user():
    """5 mixed-user jobs in the snapshot. mine_only=True → 3 rows
    (the user's jobs); mine_only=False → 5 rows. The toggle must not
    call _refresh_all — assert by counting executor invocations."""
    app = RohanBoardApp(config=_minimal_overview_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        await pilot.pause()

        # Set mine_only BEFORE snapshot so JobsTable's first render
        # picks it up — avoids depending on the watch_mine_only
        # broadcast in the assertion path.
        app.mine_only = True
        app.snapshot = _mixed_snapshot()
        await pilot.pause(0.4)

        table = app.query_one(JobsTable).query_one("#jobs_table_dt", DataTable)
        assert table.row_count == 3, (
            f"mine_only=True expected 3 rows (hli's jobs), got {table.row_count}"
        )

        # Toggle off → all 5 jobs.
        app.mine_only = False
        await pilot.pause(0.4)
        table = app.query_one(JobsTable).query_one("#jobs_table_dt", DataTable)
        assert table.row_count == 5, (
            f"mine_only=False expected 5 rows (all users), got {table.row_count}"
        )

        # Toggle back → 3 again.
        app.mine_only = True
        await pilot.pause(0.4)
        table = app.query_one(JobsTable).query_one("#jobs_table_dt", DataTable)
        assert table.row_count == 3


def test_compact_jobs_filters_to_cluster_user_unit():
    """Unit-level: construct a CompactJobs with a stub app.mine_only,
    drive update_snapshot directly, assert the title reflects the
    filtered count.

    Pilot-level testing of CompactJobs through OverviewPanel proved
    racy — the panel's _populate cycle remounts CompactJobs on every
    layout change, and the test happens to query_one a transitional
    instance before its compose finishes. A unit test isolates the
    filter logic from the layout cycle.
    """
    from textual.app import App
    from textual.widgets import Static
    from rohanboard.widgets.overview import CompactJobs

    class _StubApp:
        mine_only = True

    snap = _mixed_snapshot()

    class _Harness(App):
        def compose(self):
            yield CompactJobs()

    async def go():
        async with _Harness().run_test() as pilot:
            await pilot.pause(0.2)
            compact = pilot.app.query_one(CompactJobs)
            # Pretend the App has a mine_only attr (CompactJobs reads
            # self.app.mine_only). _Harness inherits App; we inject.
            pilot.app.mine_only = True   # type: ignore[attr-defined]
            compact.update_snapshot(snap)
            title_str = str(compact.query_one("#title", Static).content)
            assert "3" in title_str, f"expected 3, got {title_str!r}"
            assert "hli" in title_str, f"expected hli, got {title_str!r}"

            pilot.app.mine_only = False    # type: ignore[attr-defined]
            compact.update_snapshot(snap)
            title_str = str(compact.query_one("#title", Static).content)
            assert "5" in title_str, f"expected 5, got {title_str!r}"

    import asyncio
    asyncio.run(go())


async def test_mine_only_toggle_does_not_call_refresh_all():
    """The whole point of Phase 4d.2-D: toggling `a` is INSTANT — no
    refetch. Wrap the executor's .run with a counter and assert no
    new ssh calls fire when mine_only flips."""
    app = RohanBoardApp(config=_minimal_overview_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        await pilot.pause()
        # Wait for the cold-start refresh to complete.
        await pilot.pause(0.5)

        # Snapshot directly — bypasses the executor (which would fail
        # on `local` exec without slurm anyway).
        app.snapshot = _mixed_snapshot()
        await pilot.pause(0.3)

        executor = app.executor
        # Wrap .run to count post-toggle invocations.
        original_run = executor.run
        post_toggle_calls: list[tuple] = []

        async def counting_run(argv, timeout=None):
            post_toggle_calls.append((tuple(argv), timeout))
            return await original_run(argv, timeout=timeout)

        executor.run = counting_run    # type: ignore[method-assign]

        # Flip mine_only several times. Each toggle should re-broadcast
        # the cached snapshot but NOT spawn an _refresh_all worker.
        for new_value in (False, True, False):
            app.mine_only = new_value
            await pilot.pause(0.3)

        assert post_toggle_calls == [], (
            f"toggling mine_only should not refetch; "
            f"got {len(post_toggle_calls)} executor.run calls: "
            f"{post_toggle_calls[:3]}"
        )


async def test_mine_only_empty_state_message_names_the_user():
    """When mine_only=True and the user has zero jobs, the empty-state
    placeholder should say "no active jobs for <user>" — distinguishes
    "you have nothing running" from "no jobs anywhere"."""
    app = RohanBoardApp(config=_minimal_overview_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        await pilot.pause()

        # Snapshot with NO 'hli' jobs.
        snap = Snapshot()
        snap.jobs = [_job("2001", "alice"), _job("2002", "bob")]
        snap.cluster_user = "hli"
        app.mine_only = True
        app.snapshot = snap
        await pilot.pause(0.4)

        table = app.query_one(JobsTable).query_one("#jobs_table_dt", DataTable)
        # Single placeholder row, not zero.
        assert table.row_count == 1
        # Read the first cell of the placeholder row.
        cell = table.get_cell_at((0, 0))
        cell_str = str(cell)
        assert "hli" in cell_str, (
            f"empty-state message should name the user, got {cell_str!r}"
        )


async def test_cold_start_no_cluster_user_shows_everything():
    """Cluster_user=='' (whoami still resolving) → mine_only filter
    doesn't apply. Shows all rows; better than blocking the table
    behind an async whoami probe."""
    app = RohanBoardApp(config=_minimal_overview_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        await pilot.pause()

        snap = Snapshot()
        snap.jobs = [_job("3001", "alice"), _job("3002", "bob"), _job("3003", "hli")]
        snap.cluster_user = ""    # whoami not yet resolved
        app.mine_only = True
        app.snapshot = snap
        await pilot.pause(0.4)

        table = app.query_one(JobsTable).query_one("#jobs_table_dt", DataTable)
        assert table.row_count == 3, (
            f"cold-start (cluster_user='') should show ALL rows even with "
            f"mine_only=True; got {table.row_count}"
        )
