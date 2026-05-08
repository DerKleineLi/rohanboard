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
    """JobsTable filter: typing 'testhello' one char at a time with
    600 ms gaps must produce 'testhello' verbatim. ANY drop here is a
    per-stroke synchronous-blocker bug in the rebuild path."""
    app = RohanBoardApp(config=_minimal_config())
    async with app.run_test() as pilot:
        await _type_and_assert(app, pilot, JobsTable, "testhello", gap_s=0.6)


async def test_nodes_filter_no_drops_at_600ms_rhythm():
    """NodesTable filter: same drop-recipe on the Nodes tab. The 2026-05-06
    v3 debounce fix patched only JobsTable; NodesTable retained the
    synchronous rebuild and continued to drop chars (~22-44%). The Phase
    4d cherry-pick brings the chunked rebuild + debounce to both."""
    app = RohanBoardApp(config=_minimal_config())
    async with app.run_test() as pilot:
        await _type_and_assert(app, pilot, NodesTable, "testhello", gap_s=0.6)


async def test_jobs_filter_no_drops_at_50ms_rhythm():
    """Fast-typing variant — exercises the 150 ms debounce coalesce
    path. Each press at 50 ms should still land in the Input value
    (the Input itself is synchronous on `Changed`); the debounce only
    delays the actual table rebuild, not the keystroke capture."""
    app = RohanBoardApp(config=_minimal_config())
    async with app.run_test() as pilot:
        await _type_and_assert(app, pilot, JobsTable, "testhello", gap_s=0.05)
