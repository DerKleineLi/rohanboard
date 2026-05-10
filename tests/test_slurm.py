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


# ──────────────────────────────────────────────────────────────────────
# Issue #5 — MIG node aggregation at NODE level
# ──────────────────────────────────────────────────────────────────


def _one_node(fixture: str):
    text = (FIXTURES / fixture).read_text()
    nodes = parse_scontrol_show_node(text)
    assert len(nodes) == 1, f"{fixture}: expected 1 node, got {len(nodes)}"
    return nodes[0]


def test_parse_node_tolerates_na_in_numeric_fields():
    """LRZ scontrol emits `FreeMem=N/A` on DOWN / NOT_RESPONDING nodes.
    The parser must absorb that as 0, not raise ValueError that kills
    the whole snapshot. Real-world repro: rohanboard pointed at LRZ
    showed "Cluster totals: ⚠ invalid literal for int() with base 10:
    'N/A'" until the safe-int wrapper landed.
    """
    block = (
        "NodeName=lrz-down-001 Arch=x86_64 CoresPerSocket=24\n"
        "   CPUAlloc=0 CPUEfctv=92 CPUTot=96 CPULoad=N/A\n"
        "   AvailableFeatures=A100-80GB\n"
        "   ActiveFeatures=A100-80GB\n"
        "   Gres=gpu:4(S:0-1)\n"
        "   RealMemory=1031700 AllocMem=0 FreeMem=N/A Sockets=2 Boards=1\n"
        "   State=DOWN+NOT_RESPONDING ThreadsPerCore=2 TmpDisk=0 Weight=1\n"
        "   Partitions=lrz-hgx-a100-80x4\n"
        "   AllocTRES=\n"
    )
    nodes = parse_scontrol_show_node(block)
    assert len(nodes) == 1
    n = nodes[0]
    assert n.name == "lrz-down-001"
    assert n.cpu_load is None
    assert n.mem_free_mb == 0   # FreeMem=N/A → 0


def test_parse_node_unknown_kind_no_fallback():
    """A kind that's NOT in `_KIND_VRAM_FALLBACK` and lacks hyphenated
    AvailableFeatures must keep `vram=None` — fallback is opt-in per kind.
    """
    block = (
        "NodeName=fake-future-gpu Arch=x86_64 CPUTot=64 CPUAlloc=0 "
        "CPULoad=0.0 RealMemory=512000 AllocMem=0 FreeMem=512000\n"
        "   AvailableFeatures=newgpu_xyz\n"
        "   Gres=gpu:newgpu_xyz:4\n"
        "   Partitions=test\n"
        "   AllocTRES=cpu=0\n"
        "   State=IDLE\n"
    )
    nodes = parse_scontrol_show_node(block)
    assert len(nodes) == 1
    g = nodes[0].gpus[0]
    assert g.kind == "newgpu_xyz"
    assert g.vram is None


def test_parse_node_rohan_kind_gets_static_vram_fallback():
    """rohan classic: AvailableFeatures lacks hyphen+VRAM, so VRAM comes
    from the static `_KIND_VRAM_FALLBACK` table. Kind in the table → vram
    filled in. The original 3ee48b0 issue: LRZ's hyphenated A100-80GB
    must NOT be overridden by the fallback (regex match wins first), and
    rohan's bare `a100` AvailableFeatures must get fallback "80GB".
    """
    block = (
        "NodeName=fake-rohan-a100 Arch=x86_64 CPUTot=128 CPUAlloc=0 "
        "CPULoad=0.0 RealMemory=512000 AllocMem=0 FreeMem=512000\n"
        "   AvailableFeatures=a100\n"
        "   Gres=gpu:a100:4\n"
        "   Partitions=a100_submit\n"
        "   AllocTRES=cpu=0\n"
        "   State=IDLE\n"
    )
    nodes = parse_scontrol_show_node(block)
    g = nodes[0].gpus[0]
    assert g.kind == "a100"
    assert g.vram == "80GB"   # static fallback


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
