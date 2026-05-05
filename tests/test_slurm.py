from pathlib import Path

from rohanboard.collectors.slurm import parse_scontrol_show_node, parse_squeue


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_squeue_one_running_job():
    text = (FIXTURES / "squeue.txt").read_text()
    jobs = parse_squeue(text)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.job_id == "2721325"
    assert j.partition == "rtx_a6000_submit"
    assert j.user == "hli"
    assert j.state == "RUNNING"
    assert j.num_nodes == 1
    assert j.num_cpus == 24
    assert j.alloc_mem == "100G"
    assert j.alloc_gpu == "1"
    assert j.node_or_reason == "angmar"
    assert "gres/gpu=1" in j.tres


def test_parse_scontrol_known_nodes():
    text = (FIXTURES / "scontrol_all.txt").read_text()
    nodes = {n.name: n for n in parse_scontrol_show_node(text)}

    assert "angmar" in nodes
    angmar = nodes["angmar"]
    assert angmar.cpu_total == 80
    assert angmar.cpu_alloc == 80
    assert angmar.cpu_free == 0
    assert angmar.mem_total_mb == 742683
    assert angmar.mem_alloc_mb == 634880
    assert angmar.state.startswith("ALLOCATED")
    assert angmar.partitions == ["rtx_a6000_submit", "rtx_a6000_interactive"]
    assert len(angmar.gpus) == 1
    assert angmar.gpus[0].kind == "rtx_a6000"
    assert angmar.gpus[0].total == 8
    assert angmar.gpus[0].alloc == 7
    assert angmar.gpus[0].free == 1

    char = nodes["char"]
    assert char.cpu_alloc == 0
    assert char.gpus[0].kind == "gtx_1080"
    assert char.gpus[0].total == 4
    assert char.gpus[0].alloc == 0  # AllocTRES has no gres/gpu when idle


def test_parse_scontrol_handles_blank_separators():
    text = (FIXTURES / "scontrol_all.txt").read_text()
    nodes = parse_scontrol_show_node(text)
    # No empty/null nodes should slip through.
    assert all(n.name and n.cpu_total > 0 for n in nodes)
    assert len(nodes) >= 5


# ──────────────────────────────────────────────────────────────────────────
# Step 4 — LRZ + rohan node parsing fixtures (GPU column)
# ──────────────────────────────────────────────────────────────────────────


def _one_node(fixture: str):
    text = (FIXTURES / fixture).read_text()
    nodes = parse_scontrol_show_node(text)
    assert len(nodes) == 1, f"{fixture}: expected 1 node, got {len(nodes)}"
    return nodes[0]


def test_parse_node_lrz_h100():
    n = _one_node("lrz_node_h100.txt")
    assert n.name == "lrz-hgx-h100-001"
    # LRZ Gres has no kind: `gpu:4(S:0-1)`. Kind+VRAM come from
    # AvailableFeatures=H100-92GB.
    assert len(n.gpus) == 1
    g = n.gpus[0]
    assert g.kind == "H100"
    assert g.vram == "92GB"
    assert g.total == 4
    assert g.alloc == 4   # AllocTRES gres/gpu=4
    assert g.display == "H100 92GB"


def test_parse_node_lrz_a100_80():
    n = _one_node("lrz_node_a100_80.txt")
    g = n.gpus[0]
    assert g.kind == "A100"
    assert g.vram == "80GB"
    assert g.total == 8
    assert g.display == "A100 80GB"


def test_parse_node_lrz_a100_40():
    n = _one_node("lrz_node_a100_40.txt")
    g = n.gpus[0]
    assert g.kind == "A100"
    assert g.vram == "40GB"
    assert g.total == 8
    # Same kind as 80GB but different vram — must distinguish.
    assert g.display == "A100 40GB"


def test_parse_node_lrz_v100():
    n = _one_node("lrz_node_v100.txt")
    g = n.gpus[0]
    assert g.kind == "V100"
    assert g.vram == "16GB"
    assert g.total == 2


def test_parse_node_lrz_cpu():
    n = _one_node("lrz_node_cpu.txt")
    # CPU node: Gres=(null), AvailableFeatures=CPU-350GB.
    # No GpuSpec entries (CPU- features must NOT be treated as GPU).
    assert n.gpus == []


def test_parse_node_rohan_a100():
    """Regression: rohan classic Gres carries kind, no vram."""
    n = _one_node("rohan_node_a100.txt")
    assert n.name == "andram"
    g = n.gpus[0]
    assert g.kind == "a100"
    assert g.vram is None
    assert g.total == 4
    assert g.alloc == 4
    assert g.display == "a100"


def test_parse_node_rohan_a6000():
    n = _one_node("rohan_node_a6000.txt")
    assert n.name == "angmar"
    g = n.gpus[0]
    assert g.kind == "rtx_a6000"
    assert g.vram is None
    assert g.total == 8
    assert g.alloc == 8
    assert g.display == "rtx_a6000"


# ──────────────────────────────────────────────────────────────────────
# Issue #5 — MIG node aggregation at NODE level
# ──────────────────────────────────────────────────────────────────────


def test_parse_node_lrz_mig_aggregates_at_node_level():
    """MIG node with mixed profiles: AllocTRES gives ONE flat gres/gpu=N
    for the whole node — slurm doesn't break out per profile. We must
    emit ONE GpuSpec at node level, not one per profile.

    Before the fix, the parser attributed all alloc to the FIRST profile,
    producing fictitious -2 free per node and -6 free at cluster level
    when 3 such nodes were summed.
    """
    n = _one_node("lrz_node_mig.txt")
    assert n.name == "mcml-hgx-a100-019"
    # Single synthetic GpuSpec covering all MIG slices on the node.
    assert len(n.gpus) == 1, (
        f"MIG node must produce exactly one GpuSpec at node level, "
        f"got {len(n.gpus)}: {[g.kind for g in n.gpus]}"
    )
    g = n.gpus[0]
    # Total = sum of profile counts: 4 (3g.40gb) + 4 (2g.20gb) + 8 (1g.10gb) = 16.
    assert g.total == 16
    # Alloc = AllocTRES gres/gpu=6 — the flat sum slurm reports.
    assert g.alloc == 6
    # Free is non-negative (the bug repro: -6 free at cluster level).
    assert g.free == 10, f"free must equal total-alloc=10, got {g.free}"
    # Kind label preserves which profiles were on the node so the user
    # can see "MIG (3g.40gb+2g.20gb+1g.10gb)".
    assert "3g.40gb" in g.kind
    assert "2g.20gb" in g.kind
    assert "1g.10gb" in g.kind
    assert g.kind.startswith("MIG")


def test_parse_node_non_mig_h100_unchanged():
    """Issue #5 regression: the non-MIG path must keep its old behaviour."""
    n = _one_node("lrz_node_h100.txt")
    # H100: single Gres `gpu:4(S:0-1)` — NOT MIG. Per-profile attribution
    # is valid because there's only one profile. This test guards against
    # a regression that would treat any GPU node as MIG.
    assert len(n.gpus) == 1
    g = n.gpus[0]
    assert g.kind == "H100"
    assert g.vram == "92GB"
    assert g.total == 4


def test_cluster_totals_across_mixed_mig_and_non_mig_nodes():
    """Aggregating across a mixed cluster (MIG node + non-MIG H100) must
    NOT produce negative free counts on any row. Prior to fix, the MIG
    profile rows summed alloc=18 vs total=12 → free=-6 cluster-wide."""
    from rohanboard.widgets.nodes_table import _cluster_totals

    mig = _one_node("lrz_node_mig.txt")
    h100 = _one_node("lrz_node_h100.txt")
    a100_80 = _one_node("lrz_node_a100_80.txt")
    # 3 identical MIG nodes (the actual LRZ cluster has 3) plus one each
    # of H100 and A100-80.
    nodes = [mig, mig, mig, h100, a100_80]
    totals = _cluster_totals(nodes)
    for label, (alloc, total) in totals["gpus"].items():
        free = total - alloc
        assert free >= 0, (
            f"row {label!r} has negative free: {free} (alloc={alloc} total={total})"
        )
    # MIG row should aggregate: 3 nodes × 16 slices total = 48, 3 × 6 alloc = 18.
    mig_rows = [k for k in totals["gpus"] if k.startswith("MIG")]
    assert len(mig_rows) == 1, f"all 3 MIG nodes should aggregate to one row, got {mig_rows}"
    mig_alloc, mig_total = totals["gpus"][mig_rows[0]]
    assert mig_total == 48
    assert mig_alloc == 18
