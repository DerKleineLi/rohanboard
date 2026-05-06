"""Storage collectors: per-user `quota` and per-mount `df`.

Also includes LRZ-specific helpers (`parse_dssusrinfo`, `discover_lrz_storage`)
since LRZ has no `quota`/`mmlsquota` CLI — `dssusrinfo all` is the only
user-facing quota source. The output is undocumented and banner-delimited;
the parser is intentionally defensive (missing sections → empty fields).
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from ..exec import Executor
from .models import DssContainer, DssInfo, StorageEntry


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
    # Use the executor's REMOTE username via whoami (cached) — falls back to
    # local $USER only when the executor doesn't expose `whoami` (e.g. an
    # old test stub). Without this fix, ssh:lrz called `quota -u hli`
    # against a host whose user is `di35dob` → "Invalid user id".
    if user is None:
        if hasattr(executor, "whoami"):
            user = await executor.whoami()
        else:
            user = os.environ.get("USER", "")
    if not user:
        return None
    # `quota` returns non-zero when the user is over quota — the stdout payload
    # is still valid, so we tolerate RuntimeError and parse whatever we got.
    try:
        text = await executor.run(["quota", "-u", user], timeout=10.0)
    except RuntimeError as e:
        # The executor wraps stderr into the message; use it as the fallback
        # text since `quota` writes its over-quota report to stderr in some
        # configurations.
        text = str(e)
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


def parse_df_multi(text: str, paths: list[str]) -> dict[str, tuple[int, int, int]]:
    """Parse `df -B1 path1 path2 ... pathN` output into {path → (total, used, avail)}.

    df groups output by mount-point; the "Mounted on" column at the end of
    each row identifies which path it covers. We index the result by the
    REQUESTED path (df may return the actual mount-point if the requested
    path was a sub-directory). Strategy:
      1. Build a mount-point → row-tuple dict from df output (skip header).
      2. For each requested path, walk up the directory tree until a
         mount-point matches. That handles `df /cluster/foo/bar` when the
         actual mount is `/cluster/foo`.

    Long path lines may continue across two text rows when the Filesystem
    column overflows — df backslash-wraps them. We detect a row that ONLY
    contains the filesystem field (one column) and merge it with the
    NEXT line.
    """
    raw_lines = text.splitlines()
    # Skip the header row(s). df with multiple paths emits ONE header.
    lines: list[str] = []
    seen_header = False
    for ln in raw_lines:
        if not ln.strip():
            continue
        if not seen_header and ln.lstrip().startswith("Filesystem"):
            seen_header = True
            continue
        lines.append(ln)

    # Merge wrapped rows: if a row has exactly 1 token (the filesystem),
    # join it with the next row's data.
    merged: list[str] = []
    i = 0
    while i < len(lines):
        parts = lines[i].split()
        if len(parts) == 1 and i + 1 < len(lines):
            merged.append(lines[i].rstrip() + " " + lines[i + 1].lstrip())
            i += 2
        else:
            merged.append(lines[i])
            i += 1

    by_mount: dict[str, tuple[int, int, int]] = {}
    for ln in merged:
        parts = ln.split()
        if len(parts) < 6:
            continue
        try:
            total = int(parts[-5])
            used = int(parts[-4])
            avail = int(parts[-3])
        except ValueError:
            continue
        # The last token is the mount point (with no spaces in any
        # filesystem we care about).
        mount = parts[-1]
        by_mount[mount] = (total, used, avail)

    out: dict[str, tuple[int, int, int]] = {}
    for p in paths:
        if p in by_mount:
            out[p] = by_mount[p]
            continue
        # Walk up the path tree to find the mount that contains it.
        # Bounded by path-component count to avoid any pathological loop.
        cur = p.rstrip("/") or "/"
        for _ in range(64):
            if cur in by_mount:
                out[p] = by_mount[cur]
                break
            if cur == "/" or "/" not in cur:
                break
            cur = cur.rsplit("/", 1)[0] or "/"
    return out


async def fetch_df(executor: Executor, label: str, path: str) -> StorageEntry | None:
    try:
        out = await executor.run(["df", "-B1", path], timeout=10.0)
    except RuntimeError:
        return None
    return parse_df(out, label=label, path=path)


async def fetch_df_multi(
    executor: Executor,
    labelled_paths: list[tuple[str, str]],
) -> list[StorageEntry | None]:
    """Issue ONE `df -B1 path1 path2 ... pathN` call, return per-path entries.

    This is the COLLECTOR-LAYER coalesce that fixes interaction lag:
    rather than N round-trips through the per-host Semaphore(1), we send
    one argv. The order of returned entries matches `labelled_paths`.

    Empty input returns []. On non-zero rc OR empty output, every entry
    in the returned list is None (callers handle).
    """
    if not labelled_paths:
        return []
    paths = [p for _, p in labelled_paths]
    try:
        out = await executor.run(["df", "-B1", *paths], timeout=15.0)
    except RuntimeError:
        return [None] * len(labelled_paths)
    by_path = parse_df_multi(out, paths)
    results: list[StorageEntry | None] = []
    for label, path in labelled_paths:
        triple = by_path.get(path)
        if triple is None:
            results.append(None)
            continue
        total, used, avail = triple
        results.append(StorageEntry(
            label=label,
            used_bytes=used,
            total_bytes=total,
            avail_bytes=avail,
            source="df",
            path=path,
        ))
    return results


# ──────────────────────────────────────────────────────────────────────────
# Mount discovery (for kind="auto" storage entries)
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_SKIP_FSTYPES: frozenset[str] = frozenset({
    "fuse.mergerfs", "fuse.sshfs", "tmpfs", "devtmpfs", "proc", "sysfs",
    "cgroup", "cgroup2", "autofs", "overlay",
})


async def discover_mounts(
    executor: Executor,
    prefixes: list[str],
    skip_fstypes: frozenset[str] = DEFAULT_SKIP_FSTYPES,
) -> list[tuple[str, str]]:
    """Enumerate active mounts under any of `prefixes`. Returns (label, path) pairs.

    Reads `/proc/mounts` via the executor so this works for both local and
    (future) SSH executors.

    Dedupe rules:
      - skip uninteresting fstypes (overlays, fuse user-binds, kernel pseudo-fs)
      - one entry per (source, fstype) pair — handles case-aliased paths like
        /cluster/daidalos vs /cluster/DAIDALOS that share the same NFS source
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    norm_prefixes = [p.rstrip("/") for p in prefixes]
    try:
        text = await executor.run(["cat", "/proc/mounts"], timeout=10.0)
    except RuntimeError:
        return out
    for line in text.splitlines():
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
# LRZ — dssusrinfo parser + auto-discovery
# ──────────────────────────────────────────────────────────────────────────

# Section headers are framed by "*****" banner lines and a star-padded
# header row. The actual layout is:
#   *********************
#   *                   *      <- padding "* (spaces) *"
#   *    HEADER TEXT    *      <- header
#   *                   *      <- padding
#   * body lines        *
#   *********************
# We anchor on the opening banner, then look at subsequent "* ... *" lines
# until we find one that has non-empty text — that's the title.
_BANNER_RE = re.compile(r"^\*{10,}$", re.MULTILINE)
_HEADER_LINE_RE = re.compile(r"^\*\s+(?P<title>\S.+?)\s*\*\s*$")
_DSS_CONTAINER_LINE_RE = re.compile(
    r"^\*\s+(?P<name>\S+)\s+at\s+(?P<path>\S+)\s+\*\s*$",
    re.MULTILINE,
)
# Quota row shapes (used in BOTH "Container usage and limits" and "DSS
# usage and limits for <user>" — both use the same layout). Examples:
#   * pn25pi-dss-0000                  9733 of 10000            GB    used    *
#   * pn25pi-dss-0000              14838667 of 20000000         Files used    *
#   * pn25pi-dss-0000                   220 of Container MAX    GB    used    *
_DSS_QUOTA_LINE_RE = re.compile(
    r"^\*\s+(?P<name>\S+)\s+(?P<used>\d+)\s+of\s+(?P<limit>\S+(?:\s+\S+)*?)\s+(?P<unit>GB|Files)\s+used\s*\*\s*$",
    re.MULTILINE,
)
# Home parsing: separate regexes for path and quota line — they live in
# the "DSS Homedir info for <user>:" section.
_DSS_HOME_PATH_RE = re.compile(r"home directory is at (?P<path>\S+)")
_DSS_HOME_QUOTA_RE = re.compile(
    r"^\*\s+(?P<used>\d+)\s+of\s+(?P<limit>\S+(?:\s+\S+)*?)\s+GB\s+used",
    re.MULTILINE,
)


def _split_dss_sections(text: str) -> list[tuple[str, str]]:
    """Walk through the banner-bracketed sections.

    A section is bracketed by a pair of `*****` lines. The header text
    sits inside that bracket on a `* TITLE *` line (after some empty
    padding lines). We slice between consecutive *opening* banners and
    extract the first non-empty `* ... *` line as the title.

    Returns a list of (title, body) where body is everything from after
    the title through the closing banner (exclusive of next opener).
    """
    banner_lines = [
        i for i, line in enumerate(text.splitlines())
        if _BANNER_RE.match(line)
    ]
    lines = text.splitlines()
    sections: list[tuple[str, str]] = []
    # Each "section" is bracketed by a pair of banner lines; a "header"
    # sits inside the bracket. We pair up banners 2-by-2.
    i = 0
    while i + 1 < len(banner_lines):
        opener = banner_lines[i]
        closer = banner_lines[i + 1]
        body_lines = lines[opener + 1:closer]
        title: str | None = None
        body_start_idx = 0
        for j, ln in enumerate(body_lines):
            m = _HEADER_LINE_RE.match(ln)
            if m and m.group("title").strip():
                title = m.group("title").strip()
                body_start_idx = j + 1
                break
        if title is not None:
            body = "\n".join(body_lines[body_start_idx:])
            sections.append((title, body))
        i += 2
    return sections


def _parse_int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _parse_limit(s: str) -> float | None:
    """Returns a numeric limit, or None for 'Unlimited' / 'Container MAX'."""
    s = s.strip()
    if not s or s.lower() in ("unlimited", "container max"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_dssusrinfo(text: str) -> DssInfo:
    """Parse `dssusrinfo all` output.

    The output is banner-delimited ASCII art. Sections of interest:
      * "...DSS Containers..."           → list of containers (name + path)
      * "DSS Container usage and limits" → quotas per container (GB + Files)
      * "DSS Homedir info for <user>"    → home path + quota

    Defensive: missing sections leave fields empty rather than raising.
    Returns a `DssInfo` with `home`, `home_used_gb`, `home_quota_gb`,
    and a list of `DssContainer`s.
    """
    info = DssInfo()
    sections = _split_dss_sections(text)
    by_name: dict[str, DssContainer] = {}

    for title, body in sections:
        # Strip trailing punctuation/colons, normalize.
        title_l = title.strip().rstrip(":").lower()
        if "following dss containers" in title_l:
            # Container enumeration: "You have access to the following DSS Containers"
            for m in _DSS_CONTAINER_LINE_RE.finditer(body):
                name = m.group("name")
                path = m.group("path")
                by_name.setdefault(name, DssContainer(name=name, path=Path(path)))
        elif title_l.startswith("dss container usage"):
            # Container usage and limits.
            for m in _DSS_QUOTA_LINE_RE.finditer(body):
                name = m.group("name")
                used = _parse_int(m.group("used"))
                lim = _parse_limit(m.group("limit"))
                unit = m.group("unit")
                c = by_name.get(name)
                if c is None:
                    # Container quota for a name not in the list section
                    # (rare, but parser is defensive). Synthesize an entry
                    # with no path; caller can drop it if path is needed.
                    c = DssContainer(name=name, path=Path(""))
                    by_name[name] = c
                if unit == "GB":
                    c.used_gb = float(used) if used is not None else None
                    c.quota_gb = lim
                elif unit == "Files":
                    c.used_files = used
                    c.quota_files = int(lim) if lim is not None else None
        elif title_l.startswith("dss homedir info"):
            m_path = _DSS_HOME_PATH_RE.search(body)
            if m_path:
                info.home = Path(m_path.group("path"))
            m_q = _DSS_HOME_QUOTA_RE.search(body)
            if m_q:
                used = _parse_int(m_q.group("used"))
                lim = _parse_limit(m_q.group("limit"))
                info.home_used_gb = float(used) if used is not None else None
                info.home_quota_gb = lim

    info.containers = list(by_name.values())
    return info


# Sentinels for the LRZ discover script — exposed at module scope so the
# orchestrator's bulk_run path can splice the script into a multi-command
# round-trip and demux without re-defining them.
_LRZ_SEP_DSS = "---DSSUSRINFO_BEGIN---"
_LRZ_SEP_DSS_END = "---DSSUSRINFO_END---"
_LRZ_SEP_DF = "---DFBLOCK_BEGIN---"


def lrz_discover_script() -> str:
    """Return the shell script for one-round-trip LRZ storage discovery.

    Exposed so `app._refresh_cluster_bulk` can include it as one entry in
    a multi-command `executor.bulk_run` call. The script is shell-only
    (no Python interpolation needed beyond the sentinels) — pure idempotent
    text.
    """
    return f"""
set -u
echo HOME=$HOME
echo MCML=${{MCMLSCRATCH-}}
echo {_LRZ_SEP_DSS}
TMPF=$(mktemp)
trap 'rm -f "$TMPF"' EXIT
dssusrinfo all > "$TMPF" 2>&1 || true
cat "$TMPF"
echo {_LRZ_SEP_DSS_END}
echo {_LRZ_SEP_DF}
# Build df argv: $HOME, $MCMLSCRATCH (if set), and any "at <path>" tokens
# from the DSS containers section of dssusrinfo.
PATHS=""
[ -n "${{HOME:-}}" ] && PATHS="$PATHS $HOME"
[ -n "${{MCMLSCRATCH:-}}" ] && PATHS="$PATHS $MCMLSCRATCH"
# Container lines look like:  *  <name>  at  <path>  *
CPATHS=$(awk '/^\\* +[^ ]+ +at +\\// {{ print $4 }}' "$TMPF")
PATHS="$PATHS $CPATHS"
if [ -n "$PATHS" ]; then
  # df may emit warnings for missing paths; suppress to keep stdout clean.
  df -B1 $PATHS 2>/dev/null || true
fi
"""


def parse_lrz_discover(text: str) -> list[StorageEntry]:
    """Parse the output of `lrz_discover_script()` into StorageEntry list.

    Split out from `discover_lrz_storage` so the orchestrator can call
    `executor.bulk_run` (which combines this with other collectors into
    ONE ssh round-trip) and feed the resulting text back through the
    same parser.
    """
    SEP_DSS = _LRZ_SEP_DSS
    SEP_DSS_END = _LRZ_SEP_DSS_END
    SEP_DF = _LRZ_SEP_DF

    # Parse: prefix env, dssusrinfo body, df body — sliced by sentinels.
    home_path: Path | None = None
    scratch_path: Path | None = None
    body = ""
    df_text = ""

    if SEP_DSS in text:
        prefix, rest = text.split(SEP_DSS, 1)
    else:
        prefix, rest = text, ""
    if SEP_DSS_END in rest:
        body, rest = rest.split(SEP_DSS_END, 1)
    if SEP_DF in rest:
        _, df_text = rest.split(SEP_DF, 1)

    for line in prefix.splitlines():
        if line.startswith("HOME="):
            v = line[len("HOME="):].strip()
            if v:
                home_path = Path(v)
        elif line.startswith("MCML="):
            v = line[len("MCML="):].strip()
            if v:
                scratch_path = Path(v)

    info = parse_dssusrinfo(body)

    # Prefer dssusrinfo's home over $HOME (they should agree, but dssusrinfo
    # is canonical for the DSS-tier home).
    if info.home is not None:
        home_path = info.home

    # Build the labelled-path list (df was already issued in the script).
    paths: list[tuple[str, Path]] = []
    if home_path is not None:
        paths.append(("home", home_path))
    if scratch_path is not None:
        paths.append(("scratch", scratch_path))
    for c in info.containers:
        if c.path and str(c.path):
            paths.append((c.name, c.path))

    # Demux the embedded df output. df_text already contains the multi-row
    # df dump (with header on first line); reuse parse_df_multi to map
    # mount-points back to requested paths.
    df_results: dict[str, StorageEntry | None] = {}
    if paths and df_text.strip():
        path_strs = [str(p) for _, p in paths]
        by_path = parse_df_multi(df_text, path_strs)
        for lbl, p in paths:
            triple = by_path.get(str(p))
            if triple is None:
                df_results[lbl] = None
            else:
                total, used, avail = triple
                df_results[lbl] = StorageEntry(
                    label=lbl,
                    used_bytes=used,
                    total_bytes=total,
                    avail_bytes=avail,
                    source="df",
                    path=str(p),
                )

    out: list[StorageEntry] = []

    # Home: prefer dssusrinfo's quota (in GB) over df's total when available.
    if home_path is not None:
        df_e = df_results.get("home")
        if info.home_used_gb is not None and info.home_quota_gb is not None:
            # Override total with the soft quota and CAP avail at the quota
            # headroom. Without the cap, df's avail (which reports the
            # filesystem-wide free space — hundreds of TiB on /dss/dsshome1)
            # gets paired with a 100 GB quota total, producing a nonsense
            # "free > total" reading. The user's actual writable amount is
            # min(filesystem_avail, quota - used).
            quota_total = int(info.home_quota_gb * 1_000_000_000)
            quota_used = int(info.home_used_gb * 1_000_000_000)
            quota_headroom = max(quota_total - quota_used, 0)
            if df_e is not None and df_e.avail_bytes is not None:
                avail = min(df_e.avail_bytes, quota_headroom)
            else:
                avail = quota_headroom
            out.append(StorageEntry(
                label="home",
                used_bytes=quota_used,
                total_bytes=quota_total,
                avail_bytes=avail,
                source="quota",
                path=str(home_path),
            ))
        elif df_e is not None:
            # No dssusrinfo quota — fall back to raw df.
            df_e.label = "home"
            out.append(df_e)

    # Scratch: no quota on LRZ scratch. df only.
    if scratch_path is not None:
        df_e = df_results.get("scratch")
        if df_e is not None:
            df_e.label = "scratch"
            out.append(df_e)

    # Containers: prefer dssusrinfo quota over df. Cap avail at quota
    # headroom — same reasoning as home above.
    for c in sorted(info.containers, key=lambda x: x.name):
        df_e = df_results.get(c.name)
        if c.used_gb is not None and c.quota_gb is not None:
            quota_total = int(c.quota_gb * 1_000_000_000)
            quota_used = int(c.used_gb * 1_000_000_000)
            quota_headroom = max(quota_total - quota_used, 0)
            if df_e is not None and df_e.avail_bytes is not None:
                avail = min(df_e.avail_bytes, quota_headroom)
            else:
                avail = quota_headroom
            out.append(StorageEntry(
                label=c.name,
                used_bytes=quota_used,
                total_bytes=quota_total,
                avail_bytes=avail,
                source="quota",
                path=str(c.path),
            ))
        elif df_e is not None:
            df_e.label = c.name
            out.append(df_e)

    return out


async def discover_lrz_storage(executor: Executor) -> list[StorageEntry]:
    """Auto-discover LRZ storage: home + scratch + DSS containers.

    ONE SSH round-trip — coalesced from the previous two-call shape. The
    sequence is:

      1. (single ssh) emit env vars (HOME, MCMLSCRATCH) + dssusrinfo body
         + (after parse on the wire) df -B1 of all known fixed paths in
         the SAME shell command, separated by sentinels.

    Per the saved memory `feedback_asyncio_subprocess_cancel_leaks.md`:
    `Semaphore(1)` is correct for wedge protection but is PESSIMAL for
    fan-out — coalesce at the COLLECTOR layer instead of widening the
    cap. Two round-trips × Semaphore(1) is fully serial; one round-trip
    halves the LRZ tick.

    Quotas (where reported by dssusrinfo) override the df-derived `total`
    so the bar reflects user-visible quota rather than physical disk.

    Returns an ordered list: home, scratch, then each container alphabetically.

    Implementation: thin wrapper around `lrz_discover_script()` +
    `parse_lrz_discover()` so the orchestrator's bulk_run path can reuse
    the script and parser without re-issuing the ssh round-trip.
    """
    cmd = ["bash", "-lc", lrz_discover_script()]
    text = await executor.run(cmd, timeout=60.0)
    return parse_lrz_discover(text)
