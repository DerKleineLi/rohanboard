"""Step-2 multi-cluster config parsing tests.

Verify that:
1. Legacy single-cluster TOML (top-level [slurm], [[storage.entries]],
   [refresh]) is silently rewritten into a single Cluster with id="rohan"
   exec="local" — preserving every existing user config verbatim.
2. New [[clusters]] block parses into one Cluster per entry with the
   right executor (Local for `exec="local"`, SSHExecutor stub for
   `exec="ssh:<host>"`).
"""
from __future__ import annotations

from rohanboard.config import Config, load as load_config


def test_legacy_config_synthesizes_single_cluster(tmp_path):
    """Old single-cluster TOML must produce exactly one cluster named 'rohan'."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[slurm]\n'
        'users = ["self"]\n'
        '[[storage.entries]]\n'
        'label = "home (quota)"\n'
        'kind = "quota"\n'
        'filesystem = "/dev/mapper/something"\n'
    )
    cfg = load_config(cfg_path)
    assert len(cfg.clusters) == 1
    assert cfg.clusters[0].id == "rohan"
    assert cfg.clusters[0].title == "rohan"
    assert cfg.clusters[0].executor.__class__.__name__ == "LocalExecutor"
    assert any(e.label == "home (quota)" for e in cfg.clusters[0].storage_entries)
    # Back-compat shims still work.
    assert cfg.slurm.users == ["self"]
    assert any(e.label == "home (quota)" for e in cfg.storage_entries)


def test_legacy_empty_config_synthesizes_default_cluster(tmp_path):
    """A TOML with NO [slurm]/[storage] blocks still synthesizes one rohan
    cluster with default storage (home/quota)."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    cfg = load_config(cfg_path)
    assert len(cfg.clusters) == 1
    assert cfg.clusters[0].id == "rohan"
    assert cfg.clusters[0].executor.__class__.__name__ == "LocalExecutor"
    # Default storage entry is "home" / quota.
    assert cfg.clusters[0].storage_entries[0].label == "home"
    assert cfg.clusters[0].storage_entries[0].kind == "quota"


def test_multi_cluster_config_parses(tmp_path):
    """Two [[clusters]] blocks → cfg.clusters has both, with the right
    executor classes (Local for "local", SSHExecutor stub for
    "ssh:<host>")."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[[clusters]]\n'
        'id = "rohan"\n'
        'title = "rohan"\n'
        'exec = "local"\n'
        '[clusters.slurm]\n'
        'users = ["self"]\n'
        '\n'
        '[[clusters]]\n'
        'id = "lrz"\n'
        'title = "LRZ"\n'
        'exec = "ssh:lrz"\n'
        '[clusters.slurm]\n'
        'users = ["self"]\n'
    )
    cfg = load_config(cfg_path)
    assert [c.id for c in cfg.clusters] == ["rohan", "lrz"]
    assert cfg.clusters[0].title == "rohan"
    assert cfg.clusters[1].title == "LRZ"
    assert cfg.clusters[0].executor.__class__.__name__ == "LocalExecutor"
    # SSHExecutor stub may raise on use but should construct.
    assert cfg.clusters[1].executor.__class__.__name__ == "SSHExecutor"
    assert cfg.clusters[1].executor.host == "lrz"


def test_multi_cluster_storage_and_log_path_map(tmp_path):
    """[[clusters.storage.entries]] and [[clusters.log_path_map]] parse."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[[clusters]]\n'
        'id = "lrz"\n'
        'exec = "ssh:lrz"\n'
        '[clusters.slurm]\n'
        'users = ["self"]\n'
        '[clusters.storage]\n'
        'mode = "auto"\n'
        '[[clusters.storage.entries]]\n'
        'label = "DSS project"\n'
        'kind = "df"\n'
        'path = "/dss/dssmcmlfs01/pn25pi/pn25pi-dss-0000"\n'
        '[[clusters.log_path_map]]\n'
        'remote = "/dss/dsshome1"\n'
        'local = "/home/hli/mounts/lrz/dsshome1"\n'
    )
    cfg = load_config(cfg_path)
    assert len(cfg.clusters) == 1
    c = cfg.clusters[0]
    assert c.id == "lrz"
    assert c.storage_mode == "auto"
    assert c.storage_entries[0].label == "DSS project"
    assert c.storage_entries[0].kind == "df"
    assert c.log_path_map == [("/dss/dsshome1", "/home/hli/mounts/lrz/dsshome1")]


def test_multi_cluster_duplicate_id_raises(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[[clusters]]\n'
        'id = "rohan"\n'
        'exec = "local"\n'
        '\n'
        '[[clusters]]\n'
        'id = "rohan"\n'
        'exec = "local"\n'
    )
    import pytest
    with pytest.raises(ValueError, match="duplicate cluster id"):
        load_config(cfg_path)


def test_default_layout_includes_nodes_summary_in_both_overview_and_nodes():
    """Cluster totals card must appear above the nodes table AND in overview.

    Regression guard for issue #8: the default layout used to mount
    `nodes_summary` only inside the Overview panel, leaving the Nodes tab
    without the totals header. Both the Overview tab's `overview_panel`
    (which embeds NodesSummary internally) and the Nodes tab must surface
    the totals so users see the cluster-wide GPU pool when scanning nodes.
    """
    from rohanboard.config import _default_layout

    layout = _default_layout()
    tabs = {t.id: t for t in layout.tabs}
    # Overview embeds NodesSummary inside the OverviewPanel widget — the
    # tab itself just lists overview_panel. Nodes tab must list nodes_summary
    # explicitly (mounted above nodes_table).
    assert "overview" in tabs
    assert "overview_panel" in tabs["overview"].widgets
    assert "nodes" in tabs
    assert "nodes_summary" in tabs["nodes"].widgets, (
        "nodes_summary must be mounted in the Nodes tab default layout "
        "so the cluster totals card sits above the per-node table"
    )
    assert "nodes_table" in tabs["nodes"].widgets
    # Order: summary above table.
    widgets = tabs["nodes"].widgets
    assert widgets.index("nodes_summary") < widgets.index("nodes_table")
