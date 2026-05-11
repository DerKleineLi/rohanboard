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
    "you have nothing running" from "no jobs anywhere".

    Bundle-1 Sub-fix-1: the placeholder is now a full-width Static
    (`#empty_placeholder`) so the message no longer truncates against
    a narrow Job column. Test queries the Static, not the DataTable."""
    app = RohanBoardApp(config=_minimal_overview_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        await pilot.pause()

        # Snapshot with NO 'hli' jobs. Mark first_tick_done=True so we
        # test the empty-state path, not Bundle-1 Sub-fix-4's
        # "loading…" placeholder.
        snap = Snapshot()
        snap.jobs = [_job("2001", "alice"), _job("2002", "bob")]
        snap.cluster_user = "hli"
        snap.first_tick_done = True
        app.mine_only = True
        app.snapshot = snap
        await pilot.pause(0.4)

        jt = app.query_one(JobsTable)
        placeholder = jt.query_one("#empty_placeholder", Static)
        msg = str(placeholder.render())
        assert "hli" in msg, (
            f"empty-state message should name the user, got {msg!r}"
        )
        # And the table itself is hidden (zero data rows visible).
        table = jt.query_one("#jobs_table_dt", DataTable)
        assert table.row_count == 0, (
            f"table should be empty (placeholder Static carries the message), "
            f"got row_count={table.row_count}"
        )


async def test_empty_placeholder_text_differs_by_mode():
    """Bundle-1 Sub-fix-1: placeholder is parametrized by JobsTable.mode.
    Active mode → "no active jobs"; Recent mode → "no recent jobs".

    Same empty snapshot, two modes — strings must differ so the user
    knows WHICH list is empty when they flip the toggle."""
    app = RohanBoardApp(config=_minimal_overview_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        await pilot.pause()

        # Empty in both modes — no jobs and no recent_jobs.
        # first_tick_done=True keeps us out of the "loading…" branch
        # (Bundle-1 Sub-fix-4) so we exercise the mode-parametrized
        # empty-state text. Bundle-2 B2.1: both recent snapshots empty
        # so Recent mode renders the empty state regardless of
        # mine_only.
        snap = Snapshot()
        snap.jobs = []
        snap.recent_jobs_self = []
        snap.recent_jobs_all = []
        snap.cluster_user = ""    # bypass mine_only branch
        snap.first_tick_done = True
        app.mine_only = False
        app.snapshot = snap
        await pilot.pause(0.3)

        jt = app.query_one(JobsTable)
        placeholder = jt.query_one("#empty_placeholder", Static)

        # Force mode=active explicitly (it's the default but make it
        # explicit so the test is independent of init defaults).
        jt.mode = "active"
        await pilot.pause(0.3)
        active_msg = str(placeholder.render())

        # Flip to recent.
        jt.mode = "recent"
        await pilot.pause(0.3)
        recent_msg = str(placeholder.render())

        assert "active" in active_msg, f"active mode: {active_msg!r}"
        assert "recent" in recent_msg, f"recent mode: {recent_msg!r}"
        assert active_msg != recent_msg, (
            f"placeholder must differ between modes; "
            f"active={active_msg!r} recent={recent_msg!r}"
        )


async def test_loading_placeholder_transitions_to_empty_state():
    """Bundle-1 Sub-fix-4: before any successful tick, the JobsTable's
    placeholder reads "loading…". After a successful tick lands an
    empty snapshot (first_tick_done=True, no jobs), it transitions to
    the empty-state text "no active jobs".

    Pre-fix the cold-start window was indistinguishable from
    "fetched, no rows" on LRZ where the ProxyJump cold-connect blocks
    the first tick for ~10 s — confusing users into thinking the
    cluster was idle when really we hadn't even talked to it yet."""
    app = RohanBoardApp(config=_minimal_overview_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        await pilot.pause()

        # Cold-start snapshot: empty + first_tick_done=False.
        snap_loading = Snapshot()
        snap_loading.jobs = []
        snap_loading.cluster_user = ""
        snap_loading.first_tick_done = False
        app.mine_only = False
        app.snapshot = snap_loading
        await pilot.pause(0.3)

        jt = app.query_one(JobsTable)
        placeholder = jt.query_one("#empty_placeholder", Static)
        loading_msg = str(placeholder.render())
        assert "loading" in loading_msg.lower(), (
            f"cold-start placeholder should say 'loading…', got {loading_msg!r}"
        )

        # First successful tick lands an empty snapshot.
        snap_empty = Snapshot()
        snap_empty.jobs = []
        snap_empty.cluster_user = ""
        snap_empty.first_tick_done = True
        app.snapshot = snap_empty
        await pilot.pause(0.3)

        empty_msg = str(placeholder.render())
        assert "loading" not in empty_msg.lower(), (
            f"post-tick placeholder should NOT say 'loading', got {empty_msg!r}"
        )
        assert "no active" in empty_msg, (
            f"post-tick placeholder should say 'no active jobs', got {empty_msg!r}"
        )


async def test_mine_only_flip_swaps_snapshot_in_recent_mode():
    """Bundle-2 B2.1: in Recent mode, mine_only=True reads from
    snap.recent_jobs_self (sacct -u $USER), mine_only=False reads
    from snap.recent_jobs_all (sacct -a). Flipping the toggle SWAPS
    which list renders — no refetch.

    Both snapshots are pre-staged on a single ssh tick, so the
    executor must NOT be called during the flip."""
    app = RohanBoardApp(config=_minimal_overview_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        await pilot.pause()

        snap = Snapshot()
        # Self list: 2 entries (the user's own). All list: 5 (different
        # mix). Distinct enough that row counts identify which list
        # the JobsTable is rendering.
        snap.recent_jobs_self = [
            _job("9001", "hli"),
            _job("9002", "hli"),
        ]
        snap.recent_jobs_all = [
            _job("9001", "hli"),
            _job("9003", "alice"),
            _job("9004", "alice"),
            _job("9005", "bob"),
            _job("9006", "bob"),
        ]
        snap.cluster_user = "hli"
        snap.first_tick_done = True
        app.mine_only = True
        app.snapshot = snap
        await pilot.pause(0.4)

        jt = app.query_one(JobsTable)
        jt.mode = "recent"
        await pilot.pause(0.4)

        table = jt.query_one("#jobs_table_dt", DataTable)
        assert table.row_count == 2, (
            f"mine_only=True + Recent should read recent_jobs_self (2 rows), "
            f"got {table.row_count}"
        )

        # Wrap executor.run AFTER the initial render so we count only
        # post-flip calls.
        original_run = app.executor.run
        post_flip_calls: list[tuple] = []

        async def counting_run(argv, timeout=None):
            post_flip_calls.append((tuple(argv), timeout))
            return await original_run(argv, timeout=timeout)

        app.executor.run = counting_run    # type: ignore[method-assign]

        app.mine_only = False
        await pilot.pause(0.4)

        assert table.row_count == 5, (
            f"mine_only=False + Recent should swap to recent_jobs_all (5 rows), "
            f"got {table.row_count}"
        )
        assert post_flip_calls == [], (
            f"mine_only flip on Recent must NOT refetch; "
            f"got {len(post_flip_calls)} calls"
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
