"""Cluster dataclass and per-cluster Config bundle.

A `Cluster` bundles everything needed to run rohanboard against ONE
slurm cluster: its executor (local subprocess or SSH), slurm filter
config, storage entries, refresh intervals, and log_path_map for the
log_tail screen.

Step 2 ships parsing only — the App still constructs a single Cluster
from the legacy [slurm]/[storage]/[refresh] top-level config when no
[[clusters]] block is present. The visible UI is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rohanboard.exec import Executor


@dataclass
class StorageEntryConfig:
    label: str
    kind: str            # "quota" | "df" | "auto" | "dssusrinfo" (lrz, step 4)
    filesystem: str | None = None     # for "quota"
    path: str | None = None           # for "df"
    prefixes: list[str] = field(default_factory=list)  # for "auto"
    container: str | None = None      # for "dssusrinfo" (step 4)


@dataclass
class SlurmFilterConfig:
    users: list[str] = field(default_factory=lambda: ["self"])
    include_partitions: list[str] = field(default_factory=list)
    exclude_partitions: list[str] = field(default_factory=list)


@dataclass
class RefreshConfig:
    slurm_jobs: float = 5.0
    slurm_nodes: float = 30.0
    storage: float = 60.0


@dataclass
class Cluster:
    """One slurm cluster's full config + executor."""
    id: str
    title: str
    executor: Executor
    slurm: SlurmFilterConfig
    storage_entries: list[StorageEntryConfig]
    refresh: RefreshConfig
    log_path_map: list[tuple[str, str]] = field(default_factory=list)
    storage_mode: str = "pinned"        # "pinned" or "auto" (step 4)
