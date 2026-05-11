"""Bundle-3 B3.3: per-cluster Nodes filter help — `build_nodes_filter_spec`
builds the numeric_fields + examples list from `cfg.nodes_table.columns`
so a cluster without `[[nodes_table.columns]]` (e.g. LRZ) doesn't see
rohan-specific `ssd_free` / `hdd_used` tokens that match nothing.
"""
from __future__ import annotations

from rohanboard.config import NodesTableColumnConfig
from rohanboard.screens.filter_help import build_nodes_filter_spec


def _rohan_cols() -> list[NodesTableColumnConfig]:
    """Mirrors `configs/rohan.toml`'s two declared columns."""
    return [
        NodesTableColumnConfig(
            id="ssd", header="SSD", source="storage_prefix", prefix="/cluster",
        ),
        NodesTableColumnConfig(
            id="hdd", header="HDD", source="storage_prefix", prefix="/cluster_HDD",
        ),
    ]


def test_filter_spec_with_rohan_columns_carries_ssd_and_hdd_tokens():
    """Rohan-style config (two columns) → numeric_fields include
    ssd_free/used/total AND hdd_free/used/total; the first column's
    name appears in the per-cluster example."""
    spec = build_nodes_filter_spec(_rohan_cols())
    for token in ("ssd_free", "ssd_used", "ssd_total",
                  "hdd_free", "hdd_used", "hdd_total"):
        assert token in spec.numeric_fields, (
            f"expected {token!r} in numeric_fields; got {spec.numeric_fields}"
        )
    # Base numeric still present.
    assert "cpu_free" in spec.numeric_fields
    assert "mem_free" in spec.numeric_fields
    # First column drives the per-cluster example.
    example_blob = "\n".join(spec.examples)
    assert "ssd_free>=1T" in example_blob, (
        f"first-column example missing from:\n{example_blob}"
    )
    assert "SSD free" in example_blob


def test_filter_spec_with_no_columns_falls_back_to_generic_example():
    """LRZ-style config (no `[[nodes_table.columns]]`) → numeric_fields
    omit ssd/hdd tokens; the per-cluster storage example is replaced
    by a generic mem_free example."""
    spec = build_nodes_filter_spec([])
    for token in ("ssd_free", "ssd_used", "hdd_free", "hdd_used"):
        assert token not in spec.numeric_fields, (
            f"{token!r} should NOT appear when no columns are declared; "
            f"got {spec.numeric_fields}"
        )
    # Base numeric still present.
    assert "cpu_free" in spec.numeric_fields
    assert "mem_free" in spec.numeric_fields
    # Generic example is in place of the per-cluster one.
    example_blob = "\n".join(spec.examples)
    assert "ssd_free" not in example_blob
    assert "hdd_used" not in example_blob
    assert "mem_free>=500G" in example_blob, (
        f"generic mem-headroom example missing:\n{example_blob}"
    )


def test_filter_spec_passes_through_none_as_empty():
    """`build_nodes_filter_spec(None)` is equivalent to passing `[]`
    (defensive default — callers can read a missing attribute as None)."""
    spec = build_nodes_filter_spec(None)
    assert "ssd_free" not in spec.numeric_fields
    assert "mem_free" in spec.numeric_fields


def test_filter_spec_uses_first_column_for_example_when_multiple():
    """When multiple columns are declared, the first one drives the
    per-cluster example (deterministic ordering; user's config order
    survives). All declared columns still appear in numeric_fields."""
    cols = _rohan_cols()
    spec = build_nodes_filter_spec(cols)
    example_blob = "\n".join(spec.examples)
    # First column (ssd) wins the example slot.
    assert "ssd_free>=1T" in example_blob
    # Second column (hdd) does NOT drive the example but its tokens
    # are still listed as numeric fields.
    assert "hdd_free" in spec.numeric_fields
    assert "hdd_free>=1T" not in example_blob
