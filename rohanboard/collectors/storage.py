"""Storage collectors: per-user `quota` and per-mount `df`."""
from __future__ import annotations

import os
import re

from ..exec import Executor
from .models import StorageEntry


_QUOTA_NUM_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)")


# ──────────────────────────────────────────────────────────────────────────
# quota
# ──────────────────────────────────────────────────────────────────────────

def parse_quota(text: str, label: str, filesystem: str | None = None) -> StorageEntry | None:
    """Parse `quota -u <user>` output. Block units are 1 KiB.

    Matches the data row that follows the filesystem line (or matches the
    filesystem in the same row if `quota` printed it inline).
    """
    lines = text.splitlines()
    selected_fs: str | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Filesystem name on its own line (when too long for quota's column width).
        if stripped.startswith("/"):
            if filesystem is None or stripped == filesystem:
                selected_fs = stripped
                # The numbers are on the next line.
                if i + 1 < len(lines):
                    m = _QUOTA_NUM_RE.match(lines[i + 1])
                    if m:
                        used_kb, soft_kb, hard_kb = (int(g) for g in m.groups())
                        return StorageEntry(
                            label=label,
                            used_bytes=used_kb * 1024,
                            total_bytes=soft_kb * 1024,
                            hard_limit_bytes=hard_kb * 1024,
                            source="quota",
                            path=selected_fs,
                        )
            continue
        # Inline form: filesystem and numbers on the same line.
        m = re.match(r"^\s*(\S+)\s+(\d+)\s+(\d+)\s+(\d+)", line)
        if m and m.group(1).startswith("/"):
            fs, used_kb, soft_kb, hard_kb = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
            if filesystem is None or fs == filesystem:
                return StorageEntry(
                    label=label,
                    used_bytes=used_kb * 1024,
                    total_bytes=soft_kb * 1024,
                    hard_limit_bytes=hard_kb * 1024,
                    source="quota",
                    path=fs,
                )
    return None


async def fetch_quota(
    executor: Executor,
    label: str,
    filesystem: str | None,
    user: str | None = None,
) -> StorageEntry | None:
    user = user or os.environ.get("USER", "")
    if not user:
        return None
    # `-l` / --local-only: skip NFS filesystems. Plain `quota -u <user>`
    # issues an rquotad RPC per NFS mount; on a login node with an
    # unresponsive NFS server (rohan: /canis/*, /hamsa, /sirion, …) that
    # hangs for minutes. Home filesystems backing a `kind="quota"` entry
    # are local (rohan /rhome), so `-l` returns the quota in ~30 ms instead
    # of wedging. See collectors/combined.py for the live-path rationale.
    rc, out, err = await executor.run(["quota", "-u", user, "-l"], timeout=10.0)
    # `quota` returns non-zero when the user is over quota — output is still valid.
    text = out or err
    return parse_quota(text, label=label, filesystem=filesystem)


# ──────────────────────────────────────────────────────────────────────────
# df
# ──────────────────────────────────────────────────────────────────────────

def parse_df(text: str, label: str, path: str) -> StorageEntry | None:
    """Parse `df -B1 <path>` output. First non-header line is what we want."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        # parts: Filesystem, 1B-blocks, Used, Available, Use%, Mounted on
        try:
            total = int(parts[-5])
            used = int(parts[-4])
            avail = int(parts[-3])
        except ValueError:
            continue
        return StorageEntry(
            label=label,
            used_bytes=used,
            total_bytes=total,
            avail_bytes=avail,
            source="df",
            path=path,
        )
    return None


async def fetch_df(executor: Executor, label: str, path: str) -> StorageEntry | None:
    rc, out, err = await executor.run(["df", "-B1", path], timeout=10.0)
    if rc != 0:
        return None
    return parse_df(out, label=label, path=path)


def parse_df_multi(text: str) -> list[StorageEntry]:
    """Parse multi-path `df -B1 <p1> <p2> ...` output. One row per path.

    The "Mounted on" column (last field on each data line) is used as both
    the entry's `path` and a default `label` — callers that want a custom
    label must do their own (path → label) mapping after parsing.

    Dedupe semantic matches `discover_mounts` (source-keyed): two paths
    that resolve to the same backing filesystem (autofs case-aliases like
    `/cluster/daidalos` vs `/cluster/daidaloS`) collapse to one entry —
    otherwise SSD totals double-count the same physical storage.

    Skips header line and any non-numeric rows (df may emit
    "df: cannot access ...: No such file or directory" on stderr but
    the caller is expected to redirect stderr away).
    """
    out: list[StorageEntry] = []
    seen_sources: set[str] = set()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for line in lines[1:]:  # skip header
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            total = int(parts[-5])
            used = int(parts[-4])
            avail = int(parts[-3])
        except ValueError:
            continue
        source = parts[0]   # Filesystem column — host:/local etc.
        path = parts[-1]
        if source in seen_sources:
            continue
        seen_sources.add(source)
        out.append(StorageEntry(
            label=path,
            used_bytes=used,
            total_bytes=total,
            avail_bytes=avail,
            source="df",
            path=path,
        ))
    return out


# ──────────────────────────────────────────────────────────────────────────
# Mount discovery (for kind="auto" storage entries)
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_SKIP_FSTYPES: frozenset[str] = frozenset({
    "fuse.mergerfs", "fuse.sshfs", "tmpfs", "devtmpfs", "proc", "sysfs",
    "cgroup", "cgroup2", "autofs", "overlay",
})


def discover_mounts(
    proc_mounts_text: str,
    prefixes: list[str],
    skip_fstypes: frozenset[str] = DEFAULT_SKIP_FSTYPES,
) -> list[tuple[str, str]]:
    """Parse `/proc/mounts` text and return (label, path) pairs for any
    mount under one of `prefixes`.

    Pure parser — no I/O. Caller fetches the text (locally for
    LocalExecutor or via `fetch_proc_mounts(executor)` for SSH).

    Dedupe rules:
      - skip uninteresting fstypes (overlays, fuse user-binds, kernel pseudo-fs)
      - one entry per (source, fstype) pair — handles case-aliased paths like
        /cluster/daidalos vs /cluster/DAIDALOS that share the same NFS source
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    norm_prefixes = [p.rstrip("/") for p in prefixes]
    for line in proc_mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        source, mp, fstype = parts[0], parts[1], parts[2]
        if fstype in skip_fstypes:
            continue
        if not any(mp == p or mp.startswith(p + "/") for p in norm_prefixes):
            continue
        key = (source, fstype)
        if key in seen:
            continue
        seen.add(key)
        out.append((mp, mp))
    out.sort(key=lambda x: x[0].lower())
    return out


# ──────────────────────────────────────────────────────────────────────────
# dssusrinfo (LRZ DSS — per-user / per-container quota)
# ──────────────────────────────────────────────────────────────────────────
#
# dssusrinfo is a Python wrapper at /usr/local/bin/dssusrinfo on LRZ login
# nodes. Subcommands of interest (probed live 2026-05-10):
#
#   dssusrinfo dsshome           → user's home-dir usage on /dss/dsshome1
#   dssusrinfo container_usage   → project-shared usage of every container
#                                  the user has access to (NOT per-user)
#   dssusrinfo my_usage          → user's footprint inside each container
#                                  (totals are "Container MAX" — no per-user
#                                  cap, so unusable for a usage-bar)
#
# Both `dsshome` and `container_usage` print star-bordered blocks. The data
# rows we care about look like:
#
#   *               89 of 100              GB    used                    *
#   * pn25pi-dss-0000          9810 of 10000      GB    used             *
#
# Files-counted rows have the same shape but say "Files used" instead of
# "GB used"; those are skipped here.

# Match a "<used> of <total> GB used" row — total may be a numeric string
# (real quota), or "Unlimited" / "Container MAX" (unbounded; row is
# unusable for a usage bar). Group 1 = optional leading container/label,
# Group 2 = used (digits), Group 3 = total token.
_DSSUSRINFO_GB_RE = re.compile(
    r"^\*\s*(?P<lead>\S*)\s+(?P<used>\d+)\s+of\s+"
    r"(?P<total>\d+|Unlimited|Container\s+MAX)\s+GB\s+used\b",
    re.IGNORECASE,
)

_GB = 1024 ** 3


def parse_dssusrinfo_dsshome(text: str, label: str, path: str | None) -> StorageEntry | None:
    """Parse `dssusrinfo dsshome` stdout. The home block has a leading
    "DSS Homedir info for <user>:" header followed by ONE GB-used row
    (no container prefix; `lead` group is empty)."""
    in_block = False
    for line in text.splitlines():
        if "DSS Homedir info" in line:
            in_block = True
            continue
        if not in_block:
            continue
        m = _DSSUSRINFO_GB_RE.match(line)
        if not m:
            continue
        # Home: lead must be empty (no container token).
        if m.group("lead"):
            continue
        total_token = m.group("total")
        if not total_token.isdigit():
            return None  # Unlimited home is unusual but not a usage bar.
        return StorageEntry(
            label=label,
            used_bytes=int(m.group("used")) * _GB,
            total_bytes=int(total_token) * _GB,
            source="dssusrinfo",
            path=path,
        )
    return None


def parse_dssusrinfo_container(
    text: str, container: str, label: str, path: str | None
) -> StorageEntry | None:
    """Parse `dssusrinfo container_usage` stdout for the named container.

    The block has one GB-used row per accessible container. We match the
    `lead` token against `container` exactly (containers are namespaced
    like `pn25pi-dss-0000`)."""
    in_block = False
    for line in text.splitlines():
        if "DSS Container usage" in line:
            in_block = True
            continue
        if not in_block:
            continue
        m = _DSSUSRINFO_GB_RE.match(line)
        if not m:
            continue
        if m.group("lead") != container:
            continue
        total_token = m.group("total")
        if not total_token.isdigit():
            return None
        return StorageEntry(
            label=label,
            used_bytes=int(m.group("used")) * _GB,
            total_bytes=int(total_token) * _GB,
            source="dssusrinfo",
            path=path,
        )
    return None


async def fetch_proc_mounts(executor: Executor) -> str:
    """Read `/proc/mounts` via the executor. Returns "" on failure so a
    transient SSH glitch doesn't crash the refresh tick — callers
    interpret empty text as "no mounts visible" and skip the fanout.
    """
    try:
        rc, out, _err = await executor.run(["cat", "/proc/mounts"], timeout=10.0)
    except Exception:
        return ""
    if rc != 0:
        return ""
    return out
