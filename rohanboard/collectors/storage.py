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
    rc, out, err = await executor.run(["quota", "-u", user], timeout=10.0)
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


# ──────────────────────────────────────────────────────────────────────────
# Mount discovery (for kind="auto" storage entries)
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_SKIP_FSTYPES: frozenset[str] = frozenset({
    "fuse.mergerfs", "fuse.sshfs", "tmpfs", "devtmpfs", "proc", "sysfs",
    "cgroup", "cgroup2", "autofs", "overlay",
})


def discover_mounts(
    prefixes: list[str],
    skip_fstypes: frozenset[str] = DEFAULT_SKIP_FSTYPES,
) -> list[tuple[str, str]]:
    """Enumerate active mounts under any of `prefixes`. Returns (label, path) pairs.

    Dedupe rules:
      - skip uninteresting fstypes (overlays, fuse user-binds, kernel pseudo-fs)
      - one entry per (source, fstype) pair — handles case-aliased paths like
        /cluster/daidalos vs /cluster/DAIDALOS that share the same NFS source
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    norm_prefixes = [p.rstrip("/") for p in prefixes]
    try:
        with open("/proc/mounts") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
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
