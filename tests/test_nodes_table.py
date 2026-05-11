"""Tests for widgets/nodes_table.py — GPU cell formatting + cluster totals."""
from __future__ import annotations

from rohanboard.collectors.models import GpuSpec, Node
from rohanboard.widgets.nodes_table import _gpu_cell, _norm_kind


def _node_cpu() -> Node:
    return Node(
        name="cpu1",
        partitions=["cpu"],
        state="IDLE",
        cpu_total=64,
        cpu_alloc=0,
        cpu_load=0.0,
        mem_total_mb=128_000,
        mem_alloc_mb=0,
        mem_free_mb=128_000,
        gpus=[],
    )


def _node_with(gpus: list[GpuSpec]) -> Node:
    return Node(
        name="n1",
        partitions=["x"],
        state="MIXED",
        cpu_total=64,
        cpu_alloc=0,
        cpu_load=0.0,
        mem_total_mb=128_000,
        mem_alloc_mb=0,
        mem_free_mb=128_000,
        gpus=gpus,
    )


def test_gpu_cell_cpu_only_dash():
    text = _gpu_cell(_node_cpu())
    assert text.plain == "—"


def test_gpu_cell_rohan_kind_only_triple_first():
    # rohan classic: kind without vram. Render-layer canonicalization
    # uppercases non-MIG kinds, so "rtx_a6000" → "RTX_A6000".
    # Order must be: triple, then kind.
    n = _node_with([GpuSpec(kind="rtx_a6000", total=8, alloc=3)])
    plain = _gpu_cell(n).plain
    # The first non-space tokens must be the numbers, then the kind.
    assert "RTX_A6000" in plain
    triple_idx = plain.find("5")  # free = 8-3 = 5
    kind_idx = plain.find("RTX_A6000")
    assert triple_idx >= 0 and kind_idx >= 0
    assert triple_idx < kind_idx, f"expected triple before kind, got {plain!r}"


def test_gpu_cell_lrz_kind_and_vram_triple_first():
    # LRZ: kind + vram, e.g. "H100 92GB". Order: triple, then "H100 92GB".
    n = _node_with([GpuSpec(kind="H100", total=4, alloc=1, vram="92GB")])
    plain = _gpu_cell(n).plain
    assert "H100 92GB" in plain
    triple_idx = plain.find("3")  # free = 4-1
    label_idx = plain.find("H100")
    assert triple_idx < label_idx, f"expected triple before label, got {plain!r}"


def test_gpu_cell_no_kind_no_vram_no_label_suffix():
    # Empty kind + no vram → display is "—"; we omit the suffix entirely.
    n = _node_with([GpuSpec(kind="", total=4, alloc=2)])
    plain = _gpu_cell(n).plain
    # No "—" suffix, no double-space label artifact.
    assert "—" not in plain
    assert "2" in plain  # 4-2 free


def test_gpu_cell_mig_multi_specs_each_triple_first():
    # MIG node with multiple profiles, each rendered as <triple>  <label>,
    # comma-joined.  MIG profile labels (e.g. "3g.40gb") are preserved
    # lowercase by `_norm_kind`.
    n = _node_with([
        GpuSpec(kind="3g.40gb", total=4, alloc=2),
        GpuSpec(kind="2g.20gb", total=4, alloc=0),
        GpuSpec(kind="1g.10gb", total=8, alloc=1),
    ])
    plain = _gpu_cell(n).plain
    # All three labels appear and each appears AFTER its triple (not before).
    for kind in ("3g.40gb", "2g.20gb", "1g.10gb"):
        idx = plain.find(kind)
        assert idx > 0, f"{kind} missing from {plain!r}"
    # The first character should be a digit (the first triple's free count),
    # not a letter from a kind label.
    assert plain[0].isdigit(), f"expected leading digit, got {plain!r}"


# ──────────────────────────────────────────────────────────────────────
# _norm_kind canonical formatting
# ──────────────────────────────────────────────────────────────────────


def test_norm_kind_lowercase_kind_uppercased():
    # "a100" → "A100"
    assert _norm_kind("a100", "80GB") == "A100 80GB"


def test_norm_kind_already_uppercase_idempotent():
    assert _norm_kind("A100", "80GB") == "A100 80GB"


def test_norm_kind_underscore_preserved_uppercased():
    # "rtx_3090" → "RTX_3090" (underscore preserved, both halves uppercased)
    assert _norm_kind("rtx_3090", "24GB") == "RTX_3090 24GB"


def test_norm_kind_mig_profile_lowercase_preserved():
    # MIG profile syntax "Ng.NNgb" stays lowercase as-is
    assert _norm_kind("3g.40gb", "40GB") == "3g.40gb 40GB"
    assert _norm_kind("2g.20gb", "20GB") == "2g.20gb 20GB"


def test_norm_kind_no_vram_returns_kind_only():
    # rohan-style: no VRAM info → just the (canonicalized) kind
    assert _norm_kind("a100", None) == "A100"
    assert _norm_kind("rtx_a6000", None) == "RTX_A6000"
    # MIG profile with no vram still preserves lowercase
    assert _norm_kind("3g.40gb", None) == "3g.40gb"


def test_norm_kind_empty_kind_returns_vram_or_empty():
    # Empty kind, vram present → vram alone (the "kind-less" Gres path)
    assert _norm_kind("", "80GB") == "80GB"
    # Both empty → empty string (caller decides "—")
    assert _norm_kind("", None) == ""


# ──────────────────────────────────────────────────────────────────────
# Bundle-2 B2.4: NodesTable sort flip routes through App.nodes_sort.
# Pre-fix the sort handler called the async `update_snapshot` from a
# sync context (same Phase-H shape Bundle 1's lint surfaced). Post-fix
# it flips the App reactive, and App.watch_nodes_sort drives the
# broadcast.
# ──────────────────────────────────────────────────────────────────────


import pytest
from textual.widgets import TabbedContent

from rohanboard.app import RohanBoardApp
from rohanboard.collectors.models import Snapshot
from rohanboard.config import Config, LayoutConfig, TabConfig
from rohanboard.widgets.nodes_table import NodesTable
from rohanboard.widgets.sortable_header import SortableHeader


def _nodes_config() -> Config:
    cfg = Config()
    cfg.exec_spec = "local"
    cfg.layout = LayoutConfig(tabs=[
        TabConfig(id="nodes", title="Nodes", widgets=["nodes_table"]),
    ])
    cfg.storage_entries = []
    cfg.presets = {"jobs": [], "nodes": []}
    return cfg


@pytest.mark.asyncio
async def test_nodes_sort_flip_broadcasts_through_app_reactive():
    """Bundle-2 B2.4: a NodesTable sort click flips `app.nodes_sort`
    rather than calling async `update_snapshot` from a sync context.
    Asserting the App reactive sees the new tuple proves the routing
    landed (and proves the broadcast worker has a path to schedule
    the rebuild)."""
    app = RohanBoardApp(config=_nodes_config())
    async with app.run_test() as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "nodes"
        await pilot.pause()

        snap = Snapshot()
        snap.nodes = [
            Node(name="z_last", partitions=["x"], state="IDLE",
                 cpu_total=64, cpu_alloc=0, cpu_load=0.0,
                 mem_total_mb=128_000, mem_alloc_mb=0, mem_free_mb=128_000,
                 gpus=[]),
            Node(name="a_first", partitions=["x"], state="IDLE",
                 cpu_total=64, cpu_alloc=0, cpu_load=0.0,
                 mem_total_mb=128_000, mem_alloc_mb=0, mem_free_mb=128_000,
                 gpus=[]),
        ]
        snap.first_tick_done = True
        app.snapshot = snap
        await pilot.pause(0.4)

        nt = app.query_one(NodesTable)
        # Default sort is ("name", "free", False) — ascending by name.
        assert app.nodes_sort == ("name", "free", False)
        # Wrap executor to count any post-flip ssh calls (must be 0;
        # sort is a re-render, not a refetch).
        original_run = app.executor.run
        post_calls: list[tuple] = []

        async def counting_run(argv, timeout=None):
            post_calls.append((tuple(argv), timeout))
            return await original_run(argv, timeout=timeout)

        app.executor.run = counting_run    # type: ignore[method-assign]

        # Trigger a sort flip on the "name" column → reverse=True
        # (descending). Post the SortChanged message through Textual
        # so the widget's own handler routes it normally.
        nt.post_message(SortableHeader.SortChanged("name", None))
        await pilot.pause(0.4)

        # App reactive flipped — broadcast wired up.
        assert app.nodes_sort == ("name", "free", True), (
            f"app.nodes_sort should flip to descending after click; "
            f"got {app.nodes_sort}"
        )
        # No refetch.
        assert post_calls == [], (
            f"sort flip must not refetch; got {len(post_calls)} ssh calls"
        )
