"""Tests for rohanboard.config — focus on the Phase 4c `exec` field +
build_executor picker. Multi-cluster TOML lands in 4e and gets its own file."""
from __future__ import annotations

from pathlib import Path

import pytest

from rohanboard.config import Config, load
from rohanboard.exec import AsyncSSHExecutor, LocalExecutor


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


# ──────────────────────────────────────────────────────────────────────────
# Default exec_spec
# ──────────────────────────────────────────────────────────────────────────

def test_default_exec_is_local(tmp_path):
    """A config with no `exec` line defaults to LocalExecutor — preserves
    the 0612f58 baseline behavior for existing single-cluster configs."""
    cfg = load(_write(tmp_path, ""))
    assert cfg.exec_spec == "local"
    assert isinstance(cfg.build_executor(), LocalExecutor)


def test_explicit_exec_local(tmp_path):
    cfg = load(_write(tmp_path, 'exec = "local"\n'))
    assert cfg.exec_spec == "local"
    assert isinstance(cfg.build_executor(), LocalExecutor)


# ──────────────────────────────────────────────────────────────────────────
# ssh:<host> form
# ──────────────────────────────────────────────────────────────────────────

def test_exec_ssh_rohan(tmp_path):
    cfg = load(_write(tmp_path, 'exec = "ssh:rohan"\n'))
    assert cfg.exec_spec == "ssh:rohan"
    ex = cfg.build_executor()
    assert isinstance(ex, AsyncSSHExecutor)
    assert ex.host == "rohan"
    # No connection opened by build_executor — that's lazy on first run().
    assert ex._conn is None


def test_exec_ssh_arbitrary_host(tmp_path):
    """Hostname can be anything resolvable via ~/.ssh/config; the executor
    just stashes the string."""
    cfg = load(_write(tmp_path, 'exec = "ssh:my-laptop.local"\n'))
    ex = cfg.build_executor()
    assert isinstance(ex, AsyncSSHExecutor)
    assert ex.host == "my-laptop.local"


# ──────────────────────────────────────────────────────────────────────────
# Validation errors
# ──────────────────────────────────────────────────────────────────────────

def test_exec_garbage_raises_at_load(tmp_path):
    """Malformed `exec` fails at load time — not at first run() — so the
    user sees the error immediately on startup, not on the first refresh."""
    with pytest.raises(ValueError, match="unknown exec"):
        load(_write(tmp_path, 'exec = "garbage"\n'))


def test_exec_ssh_empty_host_raises_at_load(tmp_path):
    with pytest.raises(ValueError, match="must include a host"):
        load(_write(tmp_path, 'exec = "ssh:"\n'))


# ──────────────────────────────────────────────────────────────────────────
# build_executor() also validates (defense in depth: someone could
# instantiate Config() in code with a bad exec_spec)
# ──────────────────────────────────────────────────────────────────────────

def test_build_executor_rejects_bad_spec_in_code():
    cfg = Config(exec_spec="not-a-valid-spec")
    with pytest.raises(ValueError, match="unknown exec"):
        cfg.build_executor()


def test_build_executor_rejects_empty_ssh_host_in_code():
    cfg = Config(exec_spec="ssh:")
    with pytest.raises(ValueError, match="must include a host"):
        cfg.build_executor()


# ──────────────────────────────────────────────────────────────────────────
# Default layout — rohan-style is the app default; [layout] is optional
# ──────────────────────────────────────────────────────────────────────────

def test_config_with_no_layout_section_uses_rohan_style_default(tmp_path):
    """A config file that omits [layout] should land the rohan-style
    default — Overview uses overview_panel, Nodes shows the totals
    summary above the per-node table.

    Lets cluster-specific TOML files focus on cluster-specific knobs
    (ssh, storage paths, GPU naming) without re-declaring the layout.
    """
    cfg = load(_write(tmp_path, 'exec = "ssh:rohan"\n'))
    tabs = {t.id: t for t in cfg.layout.tabs}
    assert "overview" in tabs
    assert tabs["overview"].widgets == ["overview_panel"]
    assert "nodes" in tabs
    assert tabs["nodes"].widgets == ["nodes_summary", "nodes_table"], (
        "Nodes tab default must surface cluster totals above the table"
    )
    assert "jobs" in tabs and "jobs_table" in tabs["jobs"].widgets
    assert "storage" in tabs and "storage_panel" in tabs["storage"].widgets


def test_config_with_explicit_layout_overrides_default(tmp_path):
    """An explicit [layout] section still overrides the rohan default."""
    body = """
[[layout.tabs]]
id = "jobs"
title = "Jobs"
widgets = ["jobs_table"]
"""
    cfg = load(_write(tmp_path, body))
    assert len(cfg.layout.tabs) == 1
    assert cfg.layout.tabs[0].id == "jobs"


# ──────────────────────────────────────────────────────────────────────────
# Project-shipped configs — `configs/lrz.toml` (Phase 4d.2-prereq)
# ──────────────────────────────────────────────────────────────────────────


def test_rohan_config_loads():
    """`configs/rohan.toml` parses cleanly: ssh:rohan, quota+auto storage,
    and OPT-IN [[nodes_table.columns]] declaring ssd + hdd via the
    `storage_prefix` source."""
    project_root = Path(__file__).resolve().parent.parent
    cfg = load(project_root / "configs" / "rohan.toml")
    assert cfg.exec_spec == "ssh:rohan"
    assert cfg.slurm.users == ["self"]
    # storage: home (quota) + auto-discovery
    by_label = {e.label: e for e in cfg.storage_entries}
    assert by_label["home"].kind == "quota"
    assert by_label["auto"].kind == "auto"
    assert by_label["auto"].prefixes == ["/cluster", "/cluster_HDD"]
    # opt-in extra columns
    cols = cfg.nodes_table.columns
    assert len(cols) == 2
    assert cols[0].id == "ssd" and cols[0].source == "storage_prefix"
    assert cols[0].prefix == "/cluster"
    assert cols[1].id == "hdd" and cols[1].prefix == "/cluster_HDD"


def test_config_with_no_nodes_table_section_has_zero_extra_columns(tmp_path):
    """Default + minimal config = NO opt-in columns. The Nodes tab gets
    just name/state/cpu/gpu/mem/partitions."""
    cfg = load(_write(tmp_path, 'exec = "local"\n'))
    assert cfg.nodes_table.columns == []


def test_nodes_table_columns_unknown_source_raises(tmp_path):
    """Typo in `source` fails at load time (not at first paint)."""
    body = """
[[nodes_table.columns]]
id = "foo"
header = "Foo"
source = "storage-prefix"   # NOTE the dash typo
prefix = "/cluster"
"""
    with pytest.raises(ValueError, match="unknown source"):
        load(_write(tmp_path, body))


def test_nodes_table_columns_storage_prefix_requires_prefix(tmp_path):
    body = """
[[nodes_table.columns]]
id = "foo"
header = "Foo"
source = "storage_prefix"
"""
    with pytest.raises(ValueError, match="requires `prefix`"):
        load(_write(tmp_path, body))


# ──────────────────────────────────────────────────────────────────────────
# Phase 4d.2-C — `group` field + kind="dssusrinfo" config validation
# ──────────────────────────────────────────────────────────────────────────


def test_storage_entry_group_field_loads(tmp_path):
    """`group` on [[storage.entries]] propagates through to the
    StorageEntryConfig — used by the StoragePanel classifier to bucket
    the entry into a render group (home / scratch / persistent / …)."""
    body = """
[[storage.entries]]
label = "scratch"
kind = "df"
path = "/dss/mcmlscratch"
group = "scratch"
"""
    cfg = load(_write(tmp_path, body))
    assert len(cfg.storage_entries) == 1
    assert cfg.storage_entries[0].group == "scratch"


def test_dssusrinfo_requires_mode(tmp_path):
    body = """
[[storage.entries]]
label = "home"
kind = "dssusrinfo"
path = "/dss/dsshome1"
"""
    with pytest.raises(ValueError, match="requires mode in"):
        load(_write(tmp_path, body))


def test_dssusrinfo_container_mode_requires_container(tmp_path):
    body = """
[[storage.entries]]
label = "MCML DSS"
kind = "dssusrinfo"
mode = "container"
path = "/dss/dssmcmlfs01"
"""
    with pytest.raises(ValueError, match="requires `container`"):
        load(_write(tmp_path, body))


def test_dssusrinfo_home_mode_loads_without_container(tmp_path):
    """mode='home' doesn't need a `container` — `dssusrinfo dsshome`
    is implicitly user-scoped."""
    body = """
[[storage.entries]]
label = "home"
kind = "dssusrinfo"
mode = "home"
path = "/dss/dsshome1"
group = "home"
"""
    cfg = load(_write(tmp_path, body))
    assert cfg.storage_entries[0].kind == "dssusrinfo"
    assert cfg.storage_entries[0].mode == "home"
    assert cfg.storage_entries[0].container is None
    assert cfg.storage_entries[0].group == "home"


def test_lrz_config_loads():
    """`configs/lrz.toml` parses cleanly and carries the LRZ-specific
    knobs from `cluster_lrz.md`: ssh:lrz exec, dssusrinfo + df storage,
    bumped refresh interval to absorb ProxyJump cold-connect, explicit
    `group` keys for the panel render order.
    """
    project_root = Path(__file__).resolve().parent.parent
    cfg = load(project_root / "configs" / "lrz.toml")
    assert cfg.exec_spec == "ssh:lrz"
    # users = ["self"] resolves to the REMOTE whoami at runtime (not the
    # WSL $USER). Keeps configs PII-free.
    assert cfg.slurm.users == ["self"]
    # ProxyJump cold ≈ 10.5 s; default 5 s would cancel mid-handshake.
    assert cfg.refresh.slurm_jobs >= 15
    # Storage entries: home (dssusrinfo dsshome) + scratch (df) + MCML DSS
    # (dssusrinfo container_usage). LRZ login lacks `quota`; per-user
    # numbers come from dssusrinfo where it applies.
    labels = {e.label: e for e in cfg.storage_entries}
    assert "home" in labels
    assert labels["home"].kind == "dssusrinfo"
    assert labels["home"].mode == "home"
    assert labels["home"].group == "home"
    assert labels["home"].path == "/dss/dsshome1"
    scratch_entries = [e for lbl, e in labels.items() if "scratch" in lbl.lower()]
    assert scratch_entries, f"expected a scratch entry, got labels {list(labels)}"
    assert scratch_entries[0].kind == "df"
    assert scratch_entries[0].group == "scratch"
    mcml_dss = [
        e for lbl, e in labels.items()
        if "mcml" in lbl.lower() and "scratch" not in lbl.lower()
    ]
    assert mcml_dss, f"expected an MCML DSS entry, got labels {list(labels)}"
    assert mcml_dss[0].kind == "dssusrinfo"
    assert mcml_dss[0].mode == "container"
    assert mcml_dss[0].container == "pn25pi-dss-0000"
    assert mcml_dss[0].group == "persistent"
    # No `quota` kind (LRZ login lacks the binary).
    assert not any(e.kind == "quota" for e in cfg.storage_entries)
    # No [layout] / [overview] in the file → rohan-style defaults inherit.
    tabs = {t.id: t for t in cfg.layout.tabs}
    assert tabs["overview"].widgets == ["overview_panel"]
    assert tabs["nodes"].widgets == ["nodes_summary", "nodes_table"]
    # LRZ does NOT declare [[nodes_table.columns]] — Nodes tab shows
    # only the base columns (no per-node SSD/HDD; LRZ has no
    # /cluster/<node> autofs structure).
    assert cfg.nodes_table.columns == []


# ──────────────────────────────────────────────────────────────────────────
# Bundle-1 Sub-fix-2: sacct_max_rows config knob.
# ──────────────────────────────────────────────────────────────────────────


def test_sacct_max_rows_defaults_to_none(tmp_path):
    """Empty config → sacct_max_rows is None (= no cap, keep all)."""
    cfg = load(_write(tmp_path, ""))
    assert cfg.refresh.sacct_max_rows is None


def test_sacct_max_rows_parses_positive_int(tmp_path):
    """`sacct_max_rows = 25` lands as int 25 on cfg.refresh."""
    cfg = load(_write(tmp_path, "[refresh]\nsacct_max_rows = 25\n"))
    assert cfg.refresh.sacct_max_rows == 25
