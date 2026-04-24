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
