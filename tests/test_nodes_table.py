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
