"""Bundle-4 (2026-05-11): new jobs in tick-driven snapshot must land in
their sort-order position, not at the bottom of the DataTable.

Symptom (pre-fix): a freshly-submitted job appeared in
`snapshot.jobs` but `JobsTable.update_snapshot`'s in-place-diff branch
appended it via `table.add_row` — which always appends at the BOTTOM of
the DataTable regardless of iteration order. With default sort = Job↓,
the user looked at the top of the table and never saw the new high-ID
job. Toggling `mine_only` (which clears `_row_keys` and forces a full
rebuild) was the workaround they used in the wild.

Fix: when `update_snapshot` (or `_apply_filter_async`) sees ANY job ID
not already in `_row_keys`, it forces the clear+rebuild branch the same
way a sort-change does. The Pilot test below drives two ticks (N then
N+1 jobs, with the new ID being the highest) and asserts the new row
lands at row index 0.
"""
from __future__ import annotations

from textual.widgets import DataTable, TabbedContent

from rohanboard.app import RohanBoardApp
from rohanboard.collectors.models import Job, Node, Snapshot
from rohanboard.config import Config, LayoutConfig, TabConfig
from rohanboard.widgets.jobs_table import JobsTable
from rohanboard.widgets.nodes_table import NodesTable


def _minimal_jobs_config() -> Config:
    cfg = Config()
    cfg.exec_spec = "local"
    cfg.layout = LayoutConfig(tabs=[
        TabConfig(id="jobs", title="Jobs", widgets=["jobs_table"]),
    ])
    cfg.storage_entries = []
    cfg.presets = {"jobs": [], "nodes": []}
    return cfg


def _minimal_nodes_config() -> Config:
    cfg = Config()
    cfg.exec_spec = "local"
    cfg.layout = LayoutConfig(tabs=[
        TabConfig(id="nodes", title="Nodes", widgets=["nodes_table"]),
    ])
    cfg.storage_entries = []
    cfg.presets = {"jobs": [], "nodes": []}
    return cfg


def _job(job_id: str, user: str = "alice") -> Job:
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


def _node(name: str) -> Node:
    return Node(
        name=name,
        partitions=["a100_submit"],
        state="IDLE",
        cpu_total=8,
        cpu_alloc=0,
        cpu_load=None,
        mem_total_mb=64 * 1024,
        mem_alloc_mb=0,
        mem_free_mb=64 * 1024,
        gpus=[],
        features=[],
    )


def _two_user_snapshot(ids: list[str]) -> Snapshot:
    """All jobs by `alice` plus a sshd row by `bob` so mine_only is
    meaningful. `cluster_user='alice'` ⇒ alice's jobs match
    mine_only=True."""
    snap = Snapshot()
    snap.jobs = [_job(jid, user="alice") for jid in ids]
    snap.cluster_user = "alice"
    snap.jobs_loaded = True
    return snap


async def test_new_job_id_lands_at_top_on_tick():
    """Two ticks: first with two jobs ['1001','1000'], second with
    three jobs ['1002','1001','1000'] (new highest ID = 1002). Under
    default sort=Job↓ the new row must be at row index 0 after the
    second tick — i.e. it lands in sort-order position, not at the
    bottom of the DataTable.

    This is the regression for the user-reported 'new jobs don't show
    up unless I toggle tabs' bug (2026-05-11)."""
    app = RohanBoardApp(config=_minimal_jobs_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        # mine_only=False so the row count matches len(snap.jobs) exactly.
        app.mine_only = False
        await pilot.pause(0.2)

        # Tick 1: N=2 jobs.
        app.snapshot = _two_user_snapshot(["1001", "1000"])
        await pilot.pause(0.4)
        table = app.query_one(JobsTable).query_one("#jobs_table_dt", DataTable)
        assert table.row_count == 2, (
            f"after first tick expected 2 rows, got {table.row_count}"
        )
        # Sort=Job↓ ⇒ row 0 should be the highest JobID (1001).
        first_after_tick1 = str(table.get_row_at(0)[0])
        assert first_after_tick1 == "1001", (
            f"first row after tick 1 should be 1001 (highest), got "
            f"{first_after_tick1!r}"
        )

        # Tick 2: N+1 jobs. New job 1002 is HIGHER than the existing
        # 1001 — must land at row index 0 in the rendered table.
        app.snapshot = _two_user_snapshot(["1002", "1001", "1000"])
        await pilot.pause(0.4)
        table = app.query_one(JobsTable).query_one("#jobs_table_dt", DataTable)
        assert table.row_count == 3, (
            f"after second tick expected 3 rows, got {table.row_count}"
        )
        first_after_tick2 = str(table.get_row_at(0)[0])
        assert first_after_tick2 == "1002", (
            f"new high-ID job 1002 must appear at row index 0; got "
            f"{first_after_tick2!r}. Pre-fix it landed at the bottom "
            f"(at index {table.row_count - 1}) because add_row appends "
            f"at the end of the DataTable."
        )
        last_after_tick2 = str(table.get_row_at(table.row_count - 1)[0])
        assert last_after_tick2 == "1000", (
            f"lowest-ID row should remain at the bottom; got "
            f"{last_after_tick2!r}"
        )


async def test_new_job_id_lands_in_sort_order_mine_only_true():
    """Repeat the regression in mine_only=True mode (default) — the
    client-side `j.user == cluster_user` filter shouldn't interact
    with the new-row sort fix. cluster_user='alice', all jobs by alice
    → mine_only=True keeps all rows."""
    app = RohanBoardApp(config=_minimal_jobs_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        app.mine_only = True
        await pilot.pause(0.2)

        app.snapshot = _two_user_snapshot(["1001", "1000"])
        await pilot.pause(0.4)
        table = app.query_one(JobsTable).query_one("#jobs_table_dt", DataTable)
        assert table.row_count == 2

        app.snapshot = _two_user_snapshot(["1002", "1001", "1000"])
        await pilot.pause(0.4)
        table = app.query_one(JobsTable).query_one("#jobs_table_dt", DataTable)
        assert table.row_count == 3
        assert str(table.get_row_at(0)[0]) == "1002"


async def test_new_node_lands_in_sort_order_on_tick():
    """Sister-path: NodesTable.update_snapshot has the same in-place
    diff. Default sort is by `name` ascending, so a new node with a
    LOWER alphabetical name must land at row 0 — not at the bottom."""
    app = RohanBoardApp(config=_minimal_nodes_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "nodes"
        await pilot.pause(0.2)

        # Tick 1: 2 nodes.
        snap = Snapshot()
        snap.nodes = [_node("falas"), _node("gondor")]
        snap.nodes_loaded = True
        app.snapshot = snap
        await pilot.pause(0.4)

        table = app.query_one(NodesTable).query_one("#nodes_dt", DataTable)
        assert table.row_count == 2
        # Sort by name ascending — falas < gondor.
        assert str(table.get_row_at(0)[0]) == "falas"

        # Tick 2: a node with name 'andram' joins the cluster — comes
        # FIRST alphabetically. Must land at row 0.
        snap = Snapshot()
        snap.nodes = [_node("andram"), _node("falas"), _node("gondor")]
        snap.nodes_loaded = True
        app.snapshot = snap
        await pilot.pause(0.4)

        table = app.query_one(NodesTable).query_one("#nodes_dt", DataTable)
        assert table.row_count == 3
        first = str(table.get_row_at(0)[0])
        assert first == "andram", (
            f"new node 'andram' (alphabetically first) must land at row "
            f"0 under sort=name↑; got {first!r}"
        )
