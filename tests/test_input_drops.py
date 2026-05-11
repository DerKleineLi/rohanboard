"""Filter-bar input-drop regression test (Phase 4d).

Repro recipe from `~/.claude/projects/-home-hli/memory/rohanboard.md`
§"Filter-bar character-drop variant": focus the filter Input, then send
characters one at a time with a slow ~600 ms gap between them. Any drop
at this rhythm is a synchronous-blocker bug in the filter-rebuild path
(per-stroke event-loop block, not queue saturation).

The Phase 4d fix (cherry-pick of e7c17b4) makes `_apply_filter_async`
chunked + dispatched via `run_worker`, with a 150 ms debounce on
`on_input_changed`. With both, every keystroke must land in the
filter Input regardless of background refresh pressure.
"""
from __future__ import annotations

import os

import pytest
from textual.widgets import Input, TabbedContent

from rohanboard.app import RohanBoardApp
from rohanboard.config import (
    Config,
    LayoutConfig,
    StorageEntryConfig,
    TabConfig,
)
from rohanboard.exec import LocalExecutor
from rohanboard.widgets.jobs_table import JobsTable
from rohanboard.widgets.nodes_table import NodesTable


def _minimal_config() -> Config:
    """Local-only Config with Jobs + Nodes tabs only (no Overview, no
    storage probe). Keeps the test snappy — no SSH, no real cluster.
    Local `squeue/scontrol` calls will fail and produce an error
    snapshot, but the filter Input is still mounted and reactive."""
    cfg = Config()
    cfg.exec_spec = "local"
    cfg.layout = LayoutConfig(tabs=[
        TabConfig(id="jobs", title="Jobs", widgets=["jobs_table"]),
        TabConfig(id="nodes", title="Nodes", widgets=["nodes_table"]),
    ])
    cfg.storage_entries = []
    cfg.presets = {"jobs": [], "nodes": []}
    return cfg


async def _type_and_assert(app, pilot, table_cls, text: str, gap_s: float):
    """Switch to the tab containing `table_cls`, focus its filter Input,
    type each char with `gap_s` between, then assert the Input contains
    exactly `text`."""
    tabbed = app.query_one(TabbedContent)
    if table_cls is JobsTable:
        tabbed.active = "jobs"
    else:
        tabbed.active = "nodes"
    await pilot.pause()

    table = app.query_one(table_cls)
    inp = table.query_one("#filter", Input)
    inp.focus()
    await pilot.pause()
    assert app.focused is inp, "filter Input did not take focus"

    for c in text:
        await pilot.press(c)
        await pilot.pause(gap_s)

    # The 150 ms debounce defers the actual rebuild — the Input value
    # itself updates on every keystroke, regardless. The bug we're
    # guarding against is keystrokes that never land in the Input
    # because a synchronous rebuild starved the input-dispatch loop.
    await pilot.pause(0.3)
    assert inp.value == text, (
        f"filter Input dropped chars: expected {text!r}, got {inp.value!r}"
    )


async def test_jobs_filter_no_drops_at_600ms_rhythm():
    """JobsTable filter: typing 'xenoview' one char at a time with
    600 ms gaps must produce 'xenoview' verbatim. ANY drop here is a
    per-stroke synchronous-blocker bug in the rebuild path.

    Bundle-3 B3.4: switched from the historical 'testhello' string.
    `testhello` contained `l` (App-bound to action_jobs_tail_log on
    the Jobs tab); Pilot kept focus on the Input so the `l` was
    harmless here, but the parallel tmux real-terminal recipe could
    in principle fire tail-log on focus drift. `xenoview` has zero
    collisions with the App's q/r/a/t/l bindings (the `e` binding
    on LogTailScreen is modal-only and not reachable during this
    test).
    """
    app = RohanBoardApp(config=_minimal_config())
    async with app.run_test() as pilot:
        await _type_and_assert(app, pilot, JobsTable, "xenoview", gap_s=0.6)


async def test_nodes_filter_no_drops_at_600ms_rhythm():
    """NodesTable filter: same drop-recipe on the Nodes tab. The 2026-05-06
    v3 debounce fix patched only JobsTable; NodesTable retained the
    synchronous rebuild and continued to drop chars (~22-44%). The Phase
    4d cherry-pick brings the chunked rebuild + debounce to both."""
    app = RohanBoardApp(config=_minimal_config())
    async with app.run_test() as pilot:
        await _type_and_assert(app, pilot, NodesTable, "xenoview", gap_s=0.6)


async def test_jobs_filter_no_drops_at_50ms_rhythm():
    """Fast-typing variant — exercises the 150 ms debounce coalesce
    path. Each press at 50 ms should still land in the Input value
    (the Input itself is synchronous on `Changed`); the debounce only
    delays the actual table rebuild, not the keystroke capture."""
    app = RohanBoardApp(config=_minimal_config())
    async with app.run_test() as pilot:
        await _type_and_assert(app, pilot, JobsTable, "xenoview", gap_s=0.05)


async def test_input_during_active_refresh_tick_no_drops():
    """Phase 4d.1.X: with the @work-decorated tick + chunked
    update_snapshot in every heavy widget, typing into the filter
    while a fat refresh broadcast is running must not drop chars.

    Synthesizes a large snapshot (200 jobs + 100 nodes + 50 storage
    entries), writes `app.snapshot = snap` directly to fan out, then
    immediately types into the filter at 600ms rhythm. The broadcast
    worker is still running when the first keystrokes arrive — the
    test pins that they all land in the Input regardless.
    """
    from datetime import datetime
    from rohanboard.collectors.models import (
        Job, Node, GpuSpec, Snapshot, StorageEntry,
    )

    app = RohanBoardApp(config=_minimal_config())
    async with app.run_test() as pilot:
        # Switch to Jobs tab first so the filter Input is mounted.
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "jobs"
        await pilot.pause()

        # Build a fat synthetic snapshot — 200 jobs is well past the
        # 50-row chunk threshold so we KNOW the rebuild has to yield.
        jobs = [
            Job(
                job_id=str(1000 + i),
                partition="a100_submit",
                name=f"synthetic_job_{i}",
                user="hli",
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
            for i in range(200)
        ]
        nodes = [
            Node(
                name=f"synth-{i:03d}",
                partitions=["a100_submit"],
                state="MIXED",
                cpu_total=128, cpu_alloc=64,
                cpu_load=1.0,
                mem_total_mb=512_000, mem_alloc_mb=256_000,
                mem_free_mb=256_000,
                gpus=[GpuSpec(kind="A100", total=8, alloc=4, vram="80GB")],
            )
            for i in range(100)
        ]
        storage = [
            StorageEntry(
                label=f"/cluster/synth-{i:03d}",
                used_bytes=10**12 * i, total_bytes=10**13,
                source="df", path=f"/cluster/synth-{i:03d}",
                avail_bytes=10**13 - 10**12 * i,
            )
            for i in range(50)
        ]
        snap = Snapshot(jobs=jobs, nodes=nodes, storage=storage)

        # Focus filter and start typing IMMEDIATELY after assigning
        # snapshot — the broadcast worker is now spinning through 200
        # rebuild rows.
        table = app.query_one(JobsTable)
        inp = table.query_one("#filter", Input)
        inp.focus()
        await pilot.pause()
        app.snapshot = snap   # kicks the broadcast worker

        for c in "xenoview":
            await pilot.press(c)
            await pilot.pause(0.6)

        # Settle the debounce + any tail of the broadcast.
        await pilot.pause(0.5)
        assert inp.value == "xenoview", (
            f"input dropped chars during heavy broadcast: {inp.value!r}"
        )
