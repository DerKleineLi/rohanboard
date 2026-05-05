"""Collect SLURM job + node state via squeue / scontrol.

JSON/YAML output is unavailable on rohan (no serializer plugin) so we go
through stable text formats: `squeue -h -o ...` and `scontrol show node --all`.
"""
from __future__ import annotations

import os
import re
from typing import Iterable

from ..exec import Executor
from .models import GpuSpec, Job, Node


SQUEUE_FORMAT = "%i|%P|%j|%u|%T|%R|%M|%L|%D|%C|%m|%b|%X"
# %X is a placeholder; tres-alloc isn't available via -o, so we override with -O below.
SQUEUE_FIELDS = (
    "job_id", "partition", "name", "user", "state",
    "node_or_reason", "time_used", "time_left", "num_nodes", "num_cpus",
    "min_memory", "tres_per_node", "_unused",
)

# -O (long format) supports tres-alloc which has cpu/mem/gpu in one cleanly parseable string.
SQUEUE_O_FORMAT = (
    "JobID:|,Partition:|,Name:|,UserName:|,State:|,NodeList:|,Reason:|,TimeUsed:|,TimeLeft:|,"
    "NumNodes:|,NumCPUs:|,MinMemory:|,tres-alloc:"
)
SQUEUE_O_FIELDS = (
    "job_id", "partition", "name", "user", "state",
    "nodelist", "reason", "time_used", "time_left", "num_nodes", "num_cpus",
    "min_memory", "tres",
)

# `\b\w+=\S+` captures KEY=VALUE pairs whose values contain no whitespace,
# which covers every field we care about. The OS=... line has spaces and is
# correctly skipped by this pattern.
_KV_RE = re.compile(r"\b([A-Za-z_]+)=(\S+)")
# Gres takes two shapes in the wild:
#   rohan classic:  `gpu:rtx_a6000:8`        → kind=rtx_a6000, count=8
#   LRZ classic:    `gpu:8(S:0-1)`           → kind="",        count=8
#   LRZ MIG:        `gpu:3g.20gb:16(S:0-1)`  → kind=3g.20gb,   count=16
# The kind group is OPTIONAL (LRZ vanilla shape lacks it); the trailing
# `(S:...)` socket-locality suffix is also optional. Allow `.` in kind so
# MIG profiles like `3g.20gb` parse cleanly.
_GRES_GPU_RE = re.compile(r"gpu:(?:([A-Za-z0-9_.]+):)?(\d+)(?:\(S:[^)]+\))?")
_ALLOC_GPU_RE = re.compile(r"gres/gpu=(\d+)")
# MIG profile shape: digit + 'g.' + digit(s) + 'gb' (case-insensitive).
# Example: 3g.40gb, 2g.20gb, 1g.10gb, 7g.80gb, 4g.20gb.
_MIG_PROFILE_RE = re.compile(r"^\d+g\.\d+gb$", re.IGNORECASE)
# AvailableFeatures encodes <kind>-<vram> on LRZ for GPU nodes
# (e.g. H100-92GB, A100-80GB, A100-40GB, V100-16GB, P100-16GB).
# CPU nodes use CPU-350GB / CPU-AMD / CPU-1.7TB — these must NOT be
# treated as GPU specs. Caller filters on the leading group != "CPU".
_AVAIL_FEAT_GPU_RE = re.compile(r"^([A-Za-z0-9]+)-(\d+(?:\.\d+)?[A-Za-z]+)$")


# ──────────────────────────────────────────────────────────────────────────
# squeue
# ──────────────────────────────────────────────────────────────────────────

def _parse_tres(tres: str) -> tuple[str, str]:
    """Pull (mem, gpu_count) out of a TRES string like 'cpu=24,mem=100G,gres/gpu=1'."""
    mem = "—"
    gpu = "—"
    for tok in tres.split(","):
        tok = tok.strip()
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        if k == "mem":
            mem = v or "—"
        elif k.startswith("gres/gpu"):
            gpu = v or "—"
    return mem, gpu


def parse_squeue(text: str) -> list[Job]:
    """Parse `squeue -h -O ...` (pipe-separated long format)."""
    jobs: list[Job] = []
    for raw in text.splitlines():
        # Each line ends with a trailing '|' from our format; strip and split.
        line = raw.rstrip()
        if not line:
            continue
        parts = line.split("|")
        # Long-format fields are space-padded; strip each.
        parts = [p.strip() for p in parts]
        if len(parts) < len(SQUEUE_O_FIELDS):
            continue
        d = dict(zip(SQUEUE_O_FIELDS, parts[: len(SQUEUE_O_FIELDS)]))
        try:
            mem, gpu = _parse_tres(d["tres"])
            nodelist = d.get("nodelist", "") or ""
            reason = d.get("reason", "") or ""
            # NodeList for running jobs (e.g. "angmar"); Reason for pending
            # ("Resources", "Priority"); a literal "None" comes back when the
            # other side is empty — treat as missing.
            if nodelist and nodelist != "None":
                node_or_reason = nodelist
            elif reason and reason != "None":
                node_or_reason = f"({reason})"
            else:
                node_or_reason = ""
            jobs.append(Job(
                job_id=d["job_id"],
                partition=d["partition"],
                name=d["name"],
                user=d["user"],
                state=d["state"],
                node_or_reason=node_or_reason,
                time_used=d["time_used"],
                time_left=d["time_left"],
                num_nodes=int(d["num_nodes"]),
                num_cpus=int(d["num_cpus"]),
                tres=d["tres"],
                alloc_mem=mem if mem != "—" else (d.get("min_memory") or "—"),
                alloc_gpu=gpu,
            ))
        except (ValueError, KeyError):
            continue
    return jobs


# ──────────────────────────────────────────────────────────────────────────
# scontrol show node
# ──────────────────────────────────────────────────────────────────────────

def _split_node_blocks(text: str) -> Iterable[str]:
    """scontrol show node separates entries with blank lines."""
    block: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if block:
                yield "\n".join(block)
                block = []
        else:
            block.append(line)
    if block:
        yield "\n".join(block)


def _parse_node_block(block: str) -> Node | None:
    fields = dict(_KV_RE.findall(block))
    if "NodeName" not in fields:
        return None

    gres_str = fields.get("Gres", "")
    alloc_tres = fields.get("AllocTRES", "")
    alloc_gpu_total = 0
    m = _ALLOC_GPU_RE.search(alloc_tres)
    if m:
        alloc_gpu_total = int(m.group(1))

    partitions = [p for p in fields.get("Partitions", "").split(",") if p]
    features = [f for f in fields.get("AvailableFeatures", "").split(",") if f and f != "(null)"]

    # Resolve a single (kind, vram) pair from AvailableFeatures (LRZ shape).
    # Used to fill in missing kind/vram for nodes whose Gres is only
    # `gpu:N(S:...)`. Skipped for `CPU-*` features (cpu-only nodes).
    feat_kind: str | None = None
    feat_vram: str | None = None
    for f in features:
        m_feat = _AVAIL_FEAT_GPU_RE.match(f)
        if m_feat and not f.startswith("CPU-"):
            feat_kind = m_feat.group(1)
            feat_vram = m_feat.group(2)
            break

    raw_gres = _GRES_GPU_RE.findall(gres_str)
    # Detect a MIG node: 2+ comma-separated gpu entries OR any kind that
    # matches the MIG profile shape `<N>g.<M>gb`. Per
    # feedback_slurm_mig_alloctres_flat.md, slurm's AllocTRES exposes only
    # ONE flat `gres/gpu=N` for the whole MIG node — there is NO per-
    # profile alloc breakdown. Naively attributing all alloc to the first
    # profile produces fictitious -N free counts.
    is_mig = (
        len(raw_gres) >= 2
        and any(_MIG_PROFILE_RE.match(k) for k, _ in raw_gres if k)
    )

    gpus: list[GpuSpec] = []
    if is_mig:
        # Aggregate at node level: ONE GpuSpec representing all MIG slices,
        # with kind = "MIG (<profile1>+<profile2>+...)" and total = sum of
        # profile counts. alloc is the flat `gres/gpu=N` from AllocTRES —
        # accurate at node level even though slurm doesn't break it down
        # per profile.
        profiles = [k for k, _ in raw_gres if k]
        total_slices = sum(int(t) for _, t in raw_gres)
        kind_label = "MIG (" + "+".join(profiles) + ")" if profiles else "MIG"
        gpus.append(GpuSpec(
            kind=kind_label,
            total=total_slices,
            alloc=alloc_gpu_total,
            vram=None,
        ))
    else:
        for kind, total in raw_gres:
            # All allocated GPUs on a non-MIG node are of the node's single
            # GPU kind, so attribute the alloc count to the first (and only)
            # entry.
            alloc = alloc_gpu_total if not gpus else 0
            # Resolution order for kind/vram:
            #  1. Gres `gpu:KIND:N` (rohan classic): kind from Gres,
            #     vram=None unless AvailableFeatures gives a sibling vram.
            #  2. Gres `gpu:N(S:...)` (LRZ classic, no kind): kind+vram
            #     from AvailableFeatures match.
            if kind:
                resolved_kind = kind
                resolved_vram: str | None = None
                if feat_kind and feat_kind.lower() == kind.lower() and feat_vram:
                    resolved_vram = feat_vram
            else:
                # Gres lacks kind — fall back to AvailableFeatures.
                resolved_kind = feat_kind or ""
                resolved_vram = feat_vram
            gpus.append(GpuSpec(
                kind=resolved_kind,
                total=int(total),
                alloc=alloc,
                vram=resolved_vram,
            ))

    try:
        cpu_load = float(fields["CPULoad"]) if fields.get("CPULoad", "N/A") != "N/A" else None
    except ValueError:
        cpu_load = None

    # LRZ reports `FreeMem=N/A` on nodes that are slow to respond to slurmd
    # health probes — and similar fields can be N/A on drained or just-booting
    # nodes elsewhere. Treat any unparseable numeric field as 0 rather than
    # crashing the whole parse.
    def _int(name: str, default: int = 0) -> int:
        v = fields.get(name)
        if v is None:
            return default
        try:
            return int(v)
        except ValueError:
            return default

    return Node(
        name=fields["NodeName"],
        partitions=partitions,
        state=fields.get("State", "UNKNOWN"),
        cpu_total=_int("CPUTot"),
        cpu_alloc=_int("CPUAlloc"),
        cpu_load=cpu_load,
        mem_total_mb=_int("RealMemory"),
        mem_alloc_mb=_int("AllocMem"),
        mem_free_mb=_int("FreeMem"),
        gpus=gpus,
        features=features,
    )


def parse_scontrol_show_node(text: str) -> list[Node]:
    nodes = []
    for block in _split_node_blocks(text):
        node = _parse_node_block(block)
        if node is not None:
            nodes.append(node)
    return nodes


# ──────────────────────────────────────────────────────────────────────────
# subprocess wrappers (async) — now delegated to an Executor.
# ──────────────────────────────────────────────────────────────────────────


async def fetch_jobs(executor: Executor, users: list[str] | None = None) -> list[Job]:
    """`users=['self']` → current user only; ['all'] or None → all users."""
    args = ["squeue", "-h", "-O", SQUEUE_O_FORMAT]
    if users and "all" not in users:
        resolved = [os.environ.get("USER", "") if u == "self" else u for u in users]
        resolved = [u for u in resolved if u]
        if resolved:
            args += ["-u", ",".join(resolved)]
    text = await executor.run(args)
    return parse_squeue(text)


async def fetch_nodes(executor: Executor) -> list[Node]:
    text = await executor.run(["scontrol", "show", "node", "--all"])
    return parse_scontrol_show_node(text)


# ──────────────────────────────────────────────────────────────────────────
# sacct (recent history)
# ──────────────────────────────────────────────────────────────────────────

SACCT_FORMAT = "JobID,Partition,JobName,User,State,NodeList,Elapsed,NCPUS,ReqTRES"


def parse_sacct(text: str) -> list[Job]:
    jobs: list[Job] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 9:
            continue
        job_id, partition, name, user, state, nodelist, elapsed, ncpus, req_tres = parts[:9]
        state_compact = state.split(" ")[0]
        try:
            num_cpus = int(ncpus)
        except ValueError:
            num_cpus = 0
        mem, gpu = _parse_tres(req_tres)
        jobs.append(Job(
            job_id=job_id,
            partition=partition,
            name=name,
            user=user,
            state=state_compact,
            node_or_reason=nodelist,
            time_used=elapsed,
            time_left="—",
            num_nodes=1,
            num_cpus=num_cpus,
            tres=req_tres,
            alloc_mem=mem,
            alloc_gpu=gpu,
        ))
    return jobs


def parse_scontrol_show_job(text: str) -> dict[str, str]:
    """Single-job key=value extractor (same shape as the node parser)."""
    return dict(_KV_RE.findall(text))


_SACCT_FALLBACK_FIELDS = (
    "JobID", "JobName", "User", "State", "Partition", "WorkDir",
    "StdOut", "StdErr", "NodeList", "Submit", "Start", "End",
)


def parse_sacct_row(text: str) -> dict[str, str]:
    """Parse a single -P (pipe-separated) sacct row into the dict shape that
    parse_scontrol_show_job returns.  sacct's column names mostly differ from
    scontrol's; we re-map a few so callers don't care which source fed us."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {}
    parts = lines[0].split("|")
    if len(parts) < len(_SACCT_FALLBACK_FIELDS):
        return {}
    raw = dict(zip(_SACCT_FALLBACK_FIELDS, parts))
    # Translate sacct → scontrol naming so callers (e.g. log_tail._path_for_stream)
    # work without changes.  sacct exposes "JobID" / "State"; scontrol uses
    # "JobId" / "JobState".  StdOut/StdErr/WorkDir/JobName/Partition are identical.
    return {
        "JobId":     raw.get("JobID", ""),
        "JobName":   raw.get("JobName", ""),
        "UserId":    raw.get("User", ""),
        "JobState":  raw.get("State", ""),
        "Partition": raw.get("Partition", ""),
        "WorkDir":   raw.get("WorkDir", ""),
        "StdOut":    raw.get("StdOut", ""),
        "StdErr":    raw.get("StdErr", ""),
        "NodeList":  raw.get("NodeList", ""),
        "SubmitTime": raw.get("Submit", ""),
        "StartTime":  raw.get("Start", ""),
        "EndTime":    raw.get("End", ""),
    }


async def fetch_job_info(executor: Executor, job_id: str) -> dict[str, str]:
    """Job info dict.  Prefers `scontrol show job` (richest source) and falls
    back to `sacct` for completed jobs that slurmctld no longer holds in memory.
    Returns an empty dict if both sources fail (caller should handle that).

    The returned dict includes a synthetic `_source` key whose value is the
    string `scontrol show job <id>` or the sacct command line used, so the
    caller can show users where the data came from."""
    scontrol_cmd = ["scontrol", "show", "job", str(job_id)]
    try:
        text = await executor.run(scontrol_cmd)
    except RuntimeError:
        # scontrol fails (typically: "Invalid job id specified") for jobs that
        # have left the controller's memory.  Fall back to the accounting db.
        sacct_cmd = [
            "sacct", "-j", str(job_id), "-X", "-P", "-n",
            "-o", ",".join(_SACCT_FALLBACK_FIELDS),
        ]
        text = await executor.run(sacct_cmd)
        d = parse_sacct_row(text)
        if d:
            d["_source"] = " ".join(sacct_cmd)
        return d
    d = parse_scontrol_show_job(text)
    if d:
        d["_source"] = " ".join(scontrol_cmd)
    return d


async def fetch_job_script(executor: Executor, job_id: str) -> str:
    """Original batch script body.  Live jobs: `scontrol write batch_script
    <id> -` (last arg `-` writes to stdout).  Completed jobs: falls back to
    `sacct -j <id> --batch-script` (Slurm 20.11+)."""
    try:
        return await executor.run(["scontrol", "write", "batch_script", str(job_id), "-"])
    except RuntimeError:
        # sacct emits the script straight to stdout (no pipe-format header).
        return await executor.run(["sacct", "-j", str(job_id), "--batch-script"])


async def fetch_job_env(executor: Executor, job_id: str) -> str:
    """Environment variables snapshot at submission time.  Only available
    for jobs slurmctld still has in memory (live or very recently completed).
    Raises RuntimeError when Slurm doesn't have it — caller should treat that
    as 'unavailable' rather than an error."""
    return await executor.run(["scontrol", "getenvironment", str(job_id)])


async def fetch_recent_jobs(
    executor: Executor,
    users: list[str] | None = None,
    starttime: str = "now-3days",
    limit: int = 50,
) -> list[Job]:
    args = ["sacct", "-X", "-P", "-n", f"--starttime={starttime}", "-o", SACCT_FORMAT]
    if users and "all" not in users:
        resolved = [os.environ.get("USER", "") if u == "self" else u for u in users]
        resolved = [u for u in resolved if u]
        if resolved:
            args += ["-u", ",".join(resolved)]
    else:
        # sacct defaults to the invoking user when no -u is given; -a /
        # --allusers is required to actually see everyone's history.
        args += ["-a"]
    text = await executor.run(args, timeout=20.0)
    jobs = parse_sacct(text)
    # Most-recent first; sacct returns ascending JobID, reverse.
    return list(reversed(jobs))[:limit]
