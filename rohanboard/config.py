"""TOML config loader with sensible defaults."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:
    import tomli as tomllib  # type: ignore[no-redef]

if TYPE_CHECKING:
    from .exec import Executor


DEFAULT_CONFIG_PATH = Path(
    os.environ.get("ROHANBOARD_CONFIG")
    or (Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "rohanboard" / "config.toml")
)


@dataclass
class StorageEntryConfig:
    label: str
    kind: str                      # "quota", "df", "auto", "dssusrinfo"
    path: str | None = None
    filesystem: str | None = None  # for quota
    prefixes: list[str] | None = None  # for kind="auto"
    # kind="dssusrinfo" (LRZ DSS): which sub-output to read.
    #   "home"      → `dssusrinfo dsshome`        (per-user home dir)
    #   "container" → `dssusrinfo container_usage`(project-shared container)
    mode: str | None = None
    container: str | None = None   # for mode="container" — DSS container name
    # Explicit StoragePanel group override; when set, _classify uses it
    # instead of the path-based fallback. Valid values are whatever
    # StoragePanel.GROUP_ORDER carries (today: "home", "ssd", "hdd",
    # "scratch", "persistent", "other").
    group: str | None = None


@dataclass
class RefreshConfig:
    slurm_jobs: int = 5
    slurm_nodes: int = 30
    storage: int = 60
    # Optional cap on the recent-jobs (sacct) list kept in each snapshot.
    # `None` = keep all rows the parser returned. Set to a positive int
    # to trim — useful when sacct returns thousands of entries and the
    # DataTable re-render dominates click-path latency.
    sacct_max_rows: int | None = None


@dataclass
class SlurmConfig:
    include_partitions: list[str] = field(default_factory=list)
    exclude_partitions: list[str] = field(default_factory=list)
    users: list[str] = field(default_factory=lambda: ["self"])


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
    # One of: "matrix" (cmatrix rain — rohan-canonical), "art" (static
    #         rohan logo), "fire" (DOOM PSX fire), or "none" (hide the
    #         decoration).
    animation: str = "matrix"


@dataclass
class NodesTableColumnConfig:
    """One opt-in column on the Nodes-tab DataTable.

    `source` is a registered handler key (today: "storage_prefix"). Future
    columns add a new source value + a handler in nodes_table.py without
    touching the config schema.

    For source = "storage_prefix": `prefix` is the path-prefix to match
    against snapshot.storage entries (e.g. "/cluster" → match per-node
    NFS mounts under /cluster/<node>). The cell renders as the matching
    StorageEntry's free / used / total triple via fat_bytes.
    """
    id: str            # column key in the DataTable (e.g. "ssd", "hdd")
    header: str        # SortableHeader label (e.g. "SSD", "HDD")
    source: str        # registered handler — today: "storage_prefix"
    prefix: str | None = None     # for storage_prefix
    width: int | None = None      # optional override; default sized for triples


@dataclass
class NodesTableConfig:
    columns: list[NodesTableColumnConfig] = field(default_factory=list)


@dataclass
class Config:
    refresh: RefreshConfig = field(default_factory=RefreshConfig)
    slurm: SlurmConfig = field(default_factory=SlurmConfig)
    storage_entries: list[StorageEntryConfig] = field(default_factory=list)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    overview: OverviewConfig = field(default_factory=OverviewConfig)
    nodes_table: NodesTableConfig = field(default_factory=NodesTableConfig)
    presets: dict = field(default_factory=lambda: {"nodes": [], "jobs": []})
    # Phase 4c: which Executor backs collectors.
    #   "local"        → LocalExecutor (default; what 0612f58 baseline did)
    #   "ssh:<host>"   → AsyncSSHExecutor(host=<host>) using ~/.ssh/config
    # Anything else raises ValueError at parse time.
    exec_spec: str = "local"

    def build_executor(self) -> "Executor":
        """Construct an Executor matching `exec_spec`. Lazy-imports
        `rohanboard.exec` so config consumers that never need an Executor
        (e.g. a config-validation script) don't pay the asyncssh import cost."""
        from .exec import LocalExecutor, AsyncSSHExecutor
        spec = self.exec_spec
        if spec == "local":
            return LocalExecutor()
        if spec.startswith("ssh:"):
            host = spec[len("ssh:"):].strip()
            if not host:
                raise ValueError(f"exec = 'ssh:' must include a host (e.g. 'ssh:rohan')")
            return AsyncSSHExecutor(host=host)
        raise ValueError(
            f"unknown exec: {spec!r} (expected 'local' or 'ssh:<host>')"
        )


def _default_layout() -> LayoutConfig:
    """Rohan-style default layout — used when a config file omits [layout].

    Overview tab uses the responsive `overview_panel` widget (which itself
    composes home_storage / nodes_summary / utilization / ascii_art into a
    1- or 2-col layout depending on terminal width). Nodes tab puts the
    cluster totals card above the per-node table so the GPU pool is
    visible while scanning rows.

    Override only when the cluster has unusual UI needs (e.g. an LRZ-only
    config might want a custom widget set on Storage). Most clusters
    should not need a [layout] section at all.
    """
    return LayoutConfig(tabs=[
        TabConfig(id="overview", title="Overview", widgets=["overview_panel"]),
        TabConfig(id="jobs",     title="Jobs",     widgets=["jobs_table"]),
        TabConfig(id="nodes",    title="Nodes",    widgets=["nodes_summary", "nodes_table"]),
        TabConfig(id="storage",  title="Storage",  widgets=["storage_panel"]),
    ])


def _default_storage() -> list[StorageEntryConfig]:
    return [
        StorageEntryConfig(label="home", kind="quota", filesystem=None),
    ]


def load(path: Path | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG_PATH).expanduser()
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as f:
            raw = tomllib.load(f)

    cfg = Config()

    if r := raw.get("refresh"):
        refresh_kwargs: dict[str, Any] = {}
        for k, v in r.items():
            if k not in RefreshConfig.__dataclass_fields__:
                continue
            if k == "sacct_max_rows":
                # Accept `None` / missing → no cap. Otherwise positive int.
                refresh_kwargs[k] = None if v is None else int(v)
            else:
                refresh_kwargs[k] = int(v)
        cfg.refresh = RefreshConfig(**refresh_kwargs)

    if s := raw.get("slurm"):
        cfg.slurm = SlurmConfig(
            include_partitions=list(s.get("include_partitions", [])),
            exclude_partitions=list(s.get("exclude_partitions", [])),
            users=list(s.get("users", ["self"])),
        )

    storage_block = raw.get("storage", {})
    entries = storage_block.get("entries", []) if isinstance(storage_block, dict) else []
    if entries:
        cfg.storage_entries = []
        for e in entries:
            kind = e["kind"]
            mode = e.get("mode")
            container = e.get("container")
            if kind == "dssusrinfo":
                if mode not in ("home", "container"):
                    raise ValueError(
                        f"[[storage.entries]] label={e.get('label')!r}: "
                        f"kind='dssusrinfo' requires mode in ('home', 'container'), got {mode!r}"
                    )
                if mode == "container" and not container:
                    raise ValueError(
                        f"[[storage.entries]] label={e.get('label')!r}: "
                        f"kind='dssusrinfo' mode='container' requires `container` (DSS container name)"
                    )
            cfg.storage_entries.append(StorageEntryConfig(
                label=e.get("label", e.get("path") or kind),
                kind=kind,
                path=e.get("path"),
                filesystem=e.get("filesystem"),
                prefixes=list(e["prefixes"]) if e.get("prefixes") else None,
                mode=mode,
                container=container,
                group=e.get("group"),
            ))
    else:
        cfg.storage_entries = _default_storage()

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
        cfg.overview = OverviewConfig(animation=str(o.get("animation", "matrix")))

    nodes_table_block = raw.get("nodes_table", {})
    nt_columns_raw = (
        nodes_table_block.get("columns", []) if isinstance(nodes_table_block, dict) else []
    )
    if nt_columns_raw:
        cols: list[NodesTableColumnConfig] = []
        for col in nt_columns_raw:
            source = str(col.get("source", ""))
            if source != "storage_prefix":
                # Today only "storage_prefix" is implemented. Fail fast on
                # an unknown source so a typo doesn't silently render an
                # empty column.
                raise ValueError(
                    f"[[nodes_table.columns]] id={col.get('id')!r}: "
                    f"unknown source {source!r} (expected 'storage_prefix')"
                )
            if not col.get("prefix"):
                raise ValueError(
                    f"[[nodes_table.columns]] id={col.get('id')!r}: "
                    f"source='storage_prefix' requires `prefix`"
                )
            cols.append(NodesTableColumnConfig(
                id=str(col["id"]),
                header=str(col.get("header", col["id"].upper())),
                source=source,
                prefix=str(col["prefix"]),
                width=int(col["width"]) if col.get("width") is not None else None,
            ))
        cfg.nodes_table = NodesTableConfig(columns=cols)

    # Phase 4c: top-level `exec` picks the Executor backend. We validate the
    # shape here at load time so a malformed `exec = "garbage"` fails fast
    # rather than at first run() call.  See `Config.build_executor`.
    if "exec" in raw:
        spec = str(raw["exec"])
        if spec != "local" and not spec.startswith("ssh:"):
            raise ValueError(
                f"unknown exec: {spec!r} (expected 'local' or 'ssh:<host>')"
            )
        if spec.startswith("ssh:") and not spec[len("ssh:"):].strip():
            raise ValueError(
                f"exec = 'ssh:' must include a host (e.g. 'ssh:rohan')"
            )
        cfg.exec_spec = spec

    # Filter presets live in a separate JSON file for easy editing.
    from . import filter as _filter
    cfg.presets = _filter.load_presets()

    return cfg
