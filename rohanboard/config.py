"""TOML config loader with sensible defaults."""
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


DEFAULT_CONFIG_PATH = Path(
    os.environ.get("ROHANBOARD_CONFIG")
    or (Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "rohanboard" / "config.toml")
)


@dataclass
class StorageEntryConfig:
    label: str
    kind: str                      # "quota", "df", or "auto"
    path: str | None = None
    filesystem: str | None = None  # for quota
    prefixes: list[str] | None = None  # for kind="auto"


@dataclass
class RefreshConfig:
    slurm_jobs: int = 5
    slurm_nodes: int = 30
    storage: int = 60


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
    # One of: "art" (static rohan logo), "matrix" (cmatrix rain),
    #         "fire" (DOOM PSX fire), or "none" (hide the decoration).
    animation: str = "art"


@dataclass
class Config:
    refresh: RefreshConfig = field(default_factory=RefreshConfig)
    slurm: SlurmConfig = field(default_factory=SlurmConfig)
    storage_entries: list[StorageEntryConfig] = field(default_factory=list)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    overview: OverviewConfig = field(default_factory=OverviewConfig)
    presets: dict = field(default_factory=lambda: {"nodes": [], "jobs": []})


def _default_layout() -> LayoutConfig:
    return LayoutConfig(tabs=[
        TabConfig(id="overview", title="Overview", widgets=["storage_panel", "jobs_table", "nodes_summary"]),
        TabConfig(id="jobs",     title="Jobs",     widgets=["jobs_table"]),
        TabConfig(id="nodes",    title="Nodes",    widgets=["nodes_table"]),
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
        cfg.refresh = RefreshConfig(**{k: int(v) for k, v in r.items() if k in RefreshConfig.__dataclass_fields__})

    if s := raw.get("slurm"):
        cfg.slurm = SlurmConfig(
            include_partitions=list(s.get("include_partitions", [])),
            exclude_partitions=list(s.get("exclude_partitions", [])),
            users=list(s.get("users", ["self"])),
        )

    storage_block = raw.get("storage", {})
    entries = storage_block.get("entries", []) if isinstance(storage_block, dict) else []
    if entries:
        cfg.storage_entries = [
            StorageEntryConfig(
                label=e.get("label", e.get("path") or e.get("kind", "")),
                kind=e["kind"],
                path=e.get("path"),
                filesystem=e.get("filesystem"),
                prefixes=list(e["prefixes"]) if e.get("prefixes") else None,
            )
            for e in entries
        ]
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
        cfg.overview = OverviewConfig(animation=str(o.get("animation", "art")))

    # Filter presets live in a separate JSON file for easy editing.
    from . import filter as _filter
    cfg.presets = _filter.load_presets()

    return cfg
