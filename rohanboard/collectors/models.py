from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class GpuSpec:
    """A single GPU entry from a node's Gres list, e.g. rtx_a6000 x 8.

    `kind` may be empty when Gres reports only `gpu:N(S:...)` (LRZ shape).
    `vram` is filled in when `AvailableFeatures` carries a `KIND-NNGB` shape
    (LRZ); rohan's classic Gres has the kind but no VRAM, so `vram` is None
    there. The combined display is "<kind> <vram>" with graceful "—"
    fallback when both are missing.
    """
    kind: str       # e.g. "rtx_a6000", "a100", "H100", or "" for kind-less Gres
    total: int
    alloc: int = 0
    vram: str | None = None    # e.g. "92GB", "80GB"; None when not parseable

    @property
    def free(self) -> int:
        return max(self.total - self.alloc, 0)

    @property
    def display(self) -> str:
        """Human-readable label for the GPU column: '<kind> <vram>' or '—'."""
        if self.kind and self.vram:
            return f"{self.kind} {self.vram}"
        if self.kind:
            return self.kind
        if self.vram:
            return self.vram
        return "—"


@dataclass
class Node:
    name: str
    partitions: list[str]
    state: str                  # e.g. "ALLOCATED", "MIXED", "IDLE", "DOWN", ...
    cpu_total: int
    cpu_alloc: int
    cpu_load: float | None
    mem_total_mb: int
    mem_alloc_mb: int
    mem_free_mb: int
    gpus: list[GpuSpec]         # per-GPU-kind totals + alloc (usually one entry)
    features: list[str] = field(default_factory=list)

    @property
    def cpu_free(self) -> int:
        return max(self.cpu_total - self.cpu_alloc, 0)

    @property
    def mem_used_mb(self) -> int:
        # AllocMem is what slurm has handed out; FreeMem is OS-level free.
        # Use AllocMem for the dashboard so it lines up with squeue accounting.
        return self.mem_alloc_mb


@dataclass
class Job:
    job_id: str
    partition: str
    name: str
    user: str
    state: str                  # RUNNING, PENDING, COMPLETING, ...
    node_or_reason: str         # NODELIST(REASON) field
    time_used: str              # e.g. "1-00:16:06"
    time_left: str              # e.g. "2-23:43:54" or "N/A"
    num_nodes: int
    num_cpus: int
    tres: str = ""              # raw alloc/req TRES e.g. "cpu=24,mem=100G,gres/gpu=1"
    alloc_mem: str = "—"        # parsed e.g. "100G"
    alloc_gpu: str = "—"        # parsed e.g. "1"


@dataclass
class StorageEntry:
    """One bar in the Storage tab. Bytes throughout for uniformity."""
    label: str
    used_bytes: int
    total_bytes: int            # for quota: soft limit; for df: filesystem size
    hard_limit_bytes: int | None = None   # quota only
    source: str = "df"          # "df", "quota", or "dssusrinfo"
    path: str | None = None     # filesystem path (df) or device (quota)
    # df's "Available" column — what the *current user* can actually allocate.
    # Differs from `total_bytes - used_bytes` on filesystems with reserved-for-root
    # blocks (typically ~5% of total). None means "unknown, fall back to subtraction".
    avail_bytes: int | None = None
    # Explicit StoragePanel group; when set, the panel's _classify uses
    # this instead of its path-based fallback. Carries the value from
    # StorageEntryConfig.group through the collector.
    group: str | None = None

    @property
    def fraction(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(self.used_bytes / self.total_bytes, 1.0)

    @property
    def free_bytes(self) -> int:
        """Writable free space — uses df Avail when known, else total - used."""
        if self.avail_bytes is not None:
            return max(self.avail_bytes, 0)
        return max(self.total_bytes - self.used_bytes, 0)


@dataclass
class UtilizationSample:
    """One historical sample of cluster utilization (fractions in [0, 1])."""
    cpu: float
    gpu: float
    mem: float
    at: datetime = field(default_factory=datetime.now)


@dataclass
class Snapshot:
    """Whatever the collectors managed to fetch on the last refresh."""
    jobs: list[Job] = field(default_factory=list)
    recent_jobs: list[Job] = field(default_factory=list)   # from sacct
    nodes: list[Node] = field(default_factory=list)
    storage: list[StorageEntry] = field(default_factory=list)
    history: list[UtilizationSample] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)
    errors: dict[str, str] = field(default_factory=dict)   # collector_name -> error text
    # Phase 4d.2-D: REMOTE whoami (e.g. "di35dob" on ssh:lrz, "hli" on
    # ssh:rohan) — stamped each tick so widgets can do client-side
    # `mine_only` filtering without poking the executor. Empty string
    # if whoami hasn't resolved yet (cold start); widgets treat that
    # as "show everything" since we can't tell which row is yours.
    cluster_user: str = ""
    # Bundle-1 Sub-fix-4: True once a tick's parsers populated jobs +
    # recent_jobs successfully. Distinguishes "still fetching" (False
    # → render "loading…" placeholder) from "fetched, no rows" (True →
    # render "no active/recent jobs"). Set ONLY on the success path of
    # `_refresh_all`; failed fetches leave it False so a transient
    # error doesn't pretend to be a known-empty cluster.
    first_tick_done: bool = False
