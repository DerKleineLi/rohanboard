"""TOML config loader with sensible defaults.

Step 2 (2026-05-04) introduces a multi-cluster schema:

    [[clusters]]
    id = "rohan"
    title = "rohan"
    exec = "local"            # or "ssh:<host>"
    [clusters.slurm]          # same shape as legacy top-level [slurm]
    users = ["self"]
    [[clusters.storage.entries]]   # same shape as legacy [[storage.entries]]
    label = "..."
    kind = "df"
    path = "..."

Legacy single-cluster TOML (top-level [slurm], [[storage.entries]],
[refresh]) is still accepted: it is silently rewritten to a single
cluster with id="rohan", title="rohan", exec="local". Visible UI is
unchanged (the App still renders the first cluster). Outer cluster
tabs land in Step 3.

Per-cluster fields live on `Cluster` (rohanboard/cluster.py). Top-level
config keeps things that span clusters: `layout`, `theme`, `overview`,
`presets`.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:
    import tomli as tomllib  # type: ignore[no-redef]

from .cluster import Cluster, RefreshConfig, SlurmFilterConfig, StorageEntryConfig
from .exec import Executor, LocalExecutor, SSHExecutor

# Re-export so any out-of-tree consumer that imports
# `rohanboard.config.SlurmConfig` keeps working.
SlurmConfig = SlurmFilterConfig


DEFAULT_CONFIG_PATH = Path(
    os.environ.get("ROHANBOARD_CONFIG")
    or (Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "rohanboard" / "config.toml")
)


@dataclass
class TabConfig:
    id: str
    title: str
    widgets: list[str]


@dataclass
class LayoutConfig:
    tabs: list[TabConfig] = field(default_factory=list)


@dataclass
class ThemeConfig:
    file: str = "default.tcss"


@dataclass
class OverviewConfig:
    # One of: "art" (static rohan logo), "matrix" (cmatrix rain),
    #         "fire" (DOOM PSX fire), or "none" (hide the decoration).
    animation: str = "art"


@dataclass
class Config:
    """Top-level rohanboard config.

    Per-cluster fields (slurm filter, storage entries, refresh intervals,
    executor) live on each `Cluster` in `clusters`. Top-level fields
    (`layout`, `theme`, `overview`, `presets`) are shared across clusters.

    Back-compat: `Config.slurm`, `Config.storage_entries`, `Config.refresh`
    are properties proxying to `clusters[0]`. This keeps step-1 callers
    working while step 3 migrates them to per-cluster access.
    """
    clusters: list[Cluster] = field(default_factory=list)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    overview: OverviewConfig = field(default_factory=OverviewConfig)
    presets: dict = field(default_factory=lambda: {"nodes": [], "jobs": []})

    # ── back-compat shims ───────────────────────────────────────────
    @property
    def slurm(self) -> SlurmFilterConfig:
        return self.clusters[0].slurm

    @property
    def storage_entries(self) -> list[StorageEntryConfig]:
        return self.clusters[0].storage_entries

    @property
    def refresh(self) -> RefreshConfig:
        return self.clusters[0].refresh


def _default_layout() -> LayoutConfig:
    return LayoutConfig(tabs=[
        TabConfig(id="overview", title="Overview", widgets=["overview_panel"]),
        TabConfig(id="jobs",     title="Jobs",     widgets=["jobs_table"]),
        TabConfig(id="nodes",    title="Nodes",    widgets=["nodes_summary", "nodes_table"]),
        TabConfig(id="storage",  title="Storage",  widgets=["storage_panel"]),
    ])


def _default_storage() -> list[StorageEntryConfig]:
    return [
        StorageEntryConfig(label="home", kind="quota", filesystem=None),
        StorageEntryConfig(label="auto", kind="auto", prefixes=["/cluster", "/cluster_HDD"]),
    ]


def _build_executor(spec: str) -> Executor:
    """Map an `exec = "..."` string to an Executor instance.

    - "local" → LocalExecutor.
    - "ssh:<host>" → SSHExecutor stub (real impl in step 3).
    """
    if spec == "local":
        return LocalExecutor()
    if spec.startswith("ssh:"):
        host = spec[len("ssh:"):]
        if not host:
            raise ValueError(f"exec={spec!r}: ssh host is empty")
        return SSHExecutor(host=host)
    raise ValueError(f"unknown exec={spec!r}; expected 'local' or 'ssh:<host>'")


def _parse_storage_entries(entries_raw: list[dict[str, Any]]) -> list[StorageEntryConfig]:
    return [
        StorageEntryConfig(
            label=e.get("label", e.get("path") or e.get("kind", "")),
            kind=e["kind"],
            path=e.get("path"),
            filesystem=e.get("filesystem"),
            prefixes=list(e["prefixes"]) if e.get("prefixes") else [],
            container=e.get("container"),
        )
        for e in entries_raw
    ]


def _parse_slurm(s: dict[str, Any]) -> SlurmFilterConfig:
    return SlurmFilterConfig(
        include_partitions=list(s.get("include_partitions", [])),
        exclude_partitions=list(s.get("exclude_partitions", [])),
        users=list(s.get("users", ["self"])),
    )


def _parse_refresh(r: dict[str, Any]) -> RefreshConfig:
    fields = RefreshConfig.__dataclass_fields__
    kwargs = {k: float(v) for k, v in r.items() if k in fields}
    return RefreshConfig(**kwargs)


def _parse_log_path_map(raw: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(str(m["remote"]), str(m["local"])) for m in raw if "remote" in m and "local" in m]


def _build_legacy_cluster(raw: dict[str, Any]) -> Cluster:
    """Synthesize a single Cluster from the legacy top-level [slurm] /
    [storage] / [refresh] keys. id="rohan", title="rohan", exec="local".
    """
    slurm = _parse_slurm(raw["slurm"]) if isinstance(raw.get("slurm"), dict) else SlurmFilterConfig()
    refresh = _parse_refresh(raw["refresh"]) if isinstance(raw.get("refresh"), dict) else RefreshConfig()
    storage_block = raw.get("storage", {})
    entries_raw = storage_block.get("entries", []) if isinstance(storage_block, dict) else []
    storage_entries = _parse_storage_entries(entries_raw) if entries_raw else _default_storage()
    storage_mode = "pinned"
    if isinstance(storage_block, dict) and isinstance(storage_block.get("mode"), str):
        storage_mode = storage_block["mode"]
    return Cluster(
        id="rohan",
        title="rohan",
        executor=LocalExecutor(),
        slurm=slurm,
        storage_entries=storage_entries,
        refresh=refresh,
        log_path_map=[],
        storage_mode=storage_mode,
    )


def _build_cluster_from_block(block: dict[str, Any]) -> Cluster:
    """Build one Cluster from a single `[[clusters]]` TOML block."""
    if "id" not in block:
        raise ValueError(f"[[clusters]] block missing required `id`: {block!r}")
    cid = str(block["id"])
    title = str(block.get("title", cid))
    exec_spec = str(block.get("exec", "local"))
    executor = _build_executor(exec_spec)

    slurm = _parse_slurm(block["slurm"]) if isinstance(block.get("slurm"), dict) else SlurmFilterConfig()
    refresh = _parse_refresh(block["refresh"]) if isinstance(block.get("refresh"), dict) else RefreshConfig()

    storage_block = block.get("storage", {})
    if isinstance(storage_block, dict):
        entries_raw = storage_block.get("entries", []) or []
        storage_mode = str(storage_block.get("mode", "pinned"))
    else:
        entries_raw = []
        storage_mode = "pinned"
    storage_entries = _parse_storage_entries(entries_raw)

    log_path_map_raw = block.get("log_path_map", [])
    log_path_map = _parse_log_path_map(log_path_map_raw) if isinstance(log_path_map_raw, list) else []

    return Cluster(
        id=cid,
        title=title,
        executor=executor,
        slurm=slurm,
        storage_entries=storage_entries,
        refresh=refresh,
        log_path_map=log_path_map,
        storage_mode=storage_mode,
    )


def load(path: Path | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG_PATH).expanduser()
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as f:
            raw = tomllib.load(f)

    cfg = Config()

    # ── clusters ─────────────────────────────────────────────────────
    clusters_raw = raw.get("clusters")
    if isinstance(clusters_raw, list) and clusters_raw:
        cfg.clusters = [_build_cluster_from_block(b) for b in clusters_raw]
        # Sanity: ids unique.
        seen: set[str] = set()
        for c in cfg.clusters:
            if c.id in seen:
                raise ValueError(f"duplicate cluster id={c.id!r}")
            seen.add(c.id)
    else:
        # Legacy single-cluster shim.
        cfg.clusters = [_build_legacy_cluster(raw)]

    # ── shared (non-per-cluster) ─────────────────────────────────────
    layout_block = raw.get("layout", {})
    tabs_raw = layout_block.get("tabs", []) if isinstance(layout_block, dict) else []
    if tabs_raw:
        cfg.layout = LayoutConfig(tabs=[
            TabConfig(id=t["id"], title=t["title"], widgets=list(t.get("widgets", [])))
            for t in tabs_raw
        ])
    else:
        cfg.layout = _default_layout()

    if t := raw.get("theme"):
        cfg.theme = ThemeConfig(file=str(t.get("file", "default.tcss")))

    if o := raw.get("overview"):
        cfg.overview = OverviewConfig(animation=str(o.get("animation", "art")))

    # Filter presets live in a separate JSON file for easy editing.
    from . import filter as _filter
    cfg.presets = _filter.load_presets()

    return cfg
