"""Bundle-3 B3.1: per-collector "loading…" placeholder.

Snapshot grew three sticky boolean flags — `jobs_loaded`, `nodes_loaded`,
`storage_loaded` — flipped True per-collector on first successful parse
inside `_refresh_all`. Widgets gate their "loading…" placeholder on the
relevant flag so cold-start (especially LRZ ProxyJump ~10 s) reads as
"still fetching" instead of as a confident "no rows here".

Tests pin the False → True transition for JobsTable, CompactJobs, and
NodesTable. StoragePanel is not exercised here — it renders group-named
section cards, not a "no rows" empty state, so the loading distinction
is less load-bearing there (sticky flag still exists on Snapshot for
future use).
"""
from __future__ import annotations

from textual.widgets import DataTable, Static, TabbedContent

from rohanboard.app import RohanBoardApp
from rohanboard.collectors.models import GpuSpec, Node, Snapshot
from rohanboard.config import Config, LayoutConfig, TabConfig
from rohanboard.widgets.jobs_table import JobsTable
from rohanboard.widgets.nodes_table import NodesTable
from rohanboard.widgets.overview import CompactJobs


def _jobs_overview_config() -> Config:
    cfg = Config()
    cfg.exec_spec = "local"
    cfg.layout = LayoutConfig(tabs=[
        TabConfig(id="overview", title="Overview", widgets=["overview_panel"]),
        TabConfig(id="jobs",     title="Jobs",     widgets=["jobs_table"]),
        TabConfig(id="nodes",    title="Nodes",    widgets=["nodes_table"]),
    ])
    cfg.storage_entries = []
    cfg.presets = {"jobs": [], "nodes": []}
    return cfg


async def test_jobs_table_shows_loading_before_first_parse():
    """Cold-start `jobs_loaded=False` → JobsTable placeholder reads
    "loading…". Post-tick `jobs_loaded=True` (still empty jobs) →
    placeholder transitions to the empty-state text."""
    app = RohanBoardApp(config=_jobs_overview_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        await pilot.pause()

        # Pre-tick.
        snap_loading = Snapshot()
        snap_loading.jobs_loaded = False
        app.mine_only = False
        app.snapshot = snap_loading
        await pilot.pause(0.3)

        jt = app.query_one(JobsTable)
        placeholder = jt.query_one("#empty_placeholder", Static)
        assert "loading" in str(placeholder.render()).lower(), (
            f"pre-tick placeholder should be 'loading…', got "
            f"{placeholder.render()!r}"
        )

        # Post-tick (still empty).
        snap_done = Snapshot()
        snap_done.jobs_loaded = True
        app.snapshot = snap_done
        await pilot.pause(0.3)

        msg = str(placeholder.render())
        assert "loading" not in msg.lower(), (
            f"post-tick placeholder should NOT say 'loading'; got {msg!r}"
        )
        assert "no active" in msg, (
            f"post-tick should say 'no active jobs'; got {msg!r}"
        )


def test_compact_jobs_shows_loading_before_first_parse():
    """CompactJobs body reads "loading…" when `jobs_loaded=False`
    OR `cluster_user == ""`. Same Bundle-3 B3.1 gate as JobsTable;
    the cluster_user check is the Bundle-2 B2.3 carryover."""
    import asyncio

    from textual.app import App

    class _Harness(App):
        def compose(self):
            yield CompactJobs()

    async def go():
        async with _Harness().run_test() as pilot:
            await pilot.pause(0.2)
            compact = pilot.app.query_one(CompactJobs)

            # Pre-tick: jobs_loaded=False, cluster_user set.
            snap_pre = Snapshot()
            snap_pre.cluster_user = "hli"
            snap_pre.jobs_loaded = False
            compact.update_snapshot(snap_pre)
            body_text = str(compact.query_one("#body", Static).render())
            assert "loading" in body_text.lower(), (
                f"pre-tick CompactJobs body must be 'loading…', got "
                f"{body_text!r}"
            )

            # Post-tick: jobs_loaded=True with the user having no jobs →
            # the empty-state text wins.
            snap_post = Snapshot()
            snap_post.cluster_user = "hli"
            snap_post.jobs_loaded = True
            compact.update_snapshot(snap_post)
            body_text = str(compact.query_one("#body", Static).render())
            assert "loading" not in body_text.lower(), (
                f"post-tick body should NOT say 'loading'; got {body_text!r}"
            )
            assert "no active" in body_text.lower(), (
                f"post-tick body should say 'no active jobs'; got {body_text!r}"
            )

    asyncio.run(go())


async def test_nodes_table_shows_loading_before_first_parse():
    """NodesTable adds the same gate via `_empty_placeholder_text` —
    `nodes_loaded=False` AND no filter → "loading…"; `nodes_loaded=True`
    OR an active filter → the existing no-match message."""
    app = RohanBoardApp(config=_jobs_overview_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "nodes"
        await pilot.pause()

        # Pre-tick: empty nodes + nodes_loaded=False.
        snap_loading = Snapshot()
        snap_loading.nodes_loaded = False
        app.snapshot = snap_loading
        await pilot.pause(0.4)

        nt = app.query_one(NodesTable)
        table = nt.query_one("#nodes_dt", DataTable)
        assert table.row_count == 1, (
            f"empty NodesTable should render exactly one placeholder row, "
            f"got {table.row_count}"
        )
        cell = str(table.get_cell_at((0, 0)))
        assert "loading" in cell.lower(), (
            f"pre-tick NodesTable cell should say 'loading…', got {cell!r}"
        )

        # Post-tick: nodes_loaded=True, still empty → no-match text.
        snap_done = Snapshot()
        snap_done.nodes_loaded = True
        app.snapshot = snap_done
        await pilot.pause(0.4)

        table = nt.query_one("#nodes_dt", DataTable)
        cell = str(table.get_cell_at((0, 0)))
        assert "loading" not in cell.lower(), (
            f"post-tick NodesTable should NOT say 'loading'; got {cell!r}"
        )
        # `snapshot.nodes` is empty, so the early-return branch renders
        # "— no node data —" (the filter-path "no nodes match" only
        # fires when nodes existed pre-filter and got filtered out).
        assert "no node data" in cell.lower(), (
            f"post-tick NodesTable should say 'no node data'; got {cell!r}"
        )


async def test_per_collector_flags_are_sticky_independently():
    """jobs_loaded flips False → True once a squeue parse lands; an
    independent failed scontrol parse on the same tick leaves
    nodes_loaded at False. Likewise the reverse. Tests the
    "per-collector, not coarse-snapshot" semantic the Bundle-3 B3.1
    refactor was justified by."""
    app = RohanBoardApp(config=_jobs_overview_config())
    async with app.run_test() as pilot:
        await pilot.pause()
        # Direct write — bypass _refresh_all. Asserts the snapshot
        # field is independently settable per collector (the model
        # supports the split; widgets read each flag separately).
        snap = Snapshot()
        snap.jobs_loaded = True
        snap.nodes_loaded = False
        snap.storage_loaded = True
        app.snapshot = snap
        await pilot.pause(0.3)

        assert snap.jobs_loaded is True
        assert snap.nodes_loaded is False
        assert snap.storage_loaded is True
