"""Step-4 — LRZ storage auto-discovery (discover_lrz_storage).

Covers:
- `parse_dssusrinfo` extracts home + at least one container with quota
  (also checked in test_storage.py; kept here for the file-as-spec view).
- `discover_lrz_storage` end-to-end: stub the executor so it returns
  (a) the env-var prefix + dssusrinfo body and (b) df -B1 output for
  each path. Confirm the resulting StorageEntry list contains home,
  scratch, and each container, with quota-overridden totals where
  dssusrinfo reported one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rohanboard.collectors.storage import discover_lrz_storage, parse_dssusrinfo
from rohanboard.exec import Executor


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_dssusrinfo_home_and_container():
    """The fixture's dssusrinfo all output yields a home + 1 container."""
    text = (FIXTURES / "dssusrinfo_all.txt").read_text()
    info = parse_dssusrinfo(text)
    assert info.home is not None
    assert info.home_quota_gb == 100.0
    assert info.home_used_gb == 89.0
    assert len(info.containers) >= 1
    c = info.containers[0]
    assert c.name == "pn25pi-dss-0000"
    assert c.quota_gb == 10000.0
    assert c.used_gb == 9733.0


# ──────────────────────────────────────────────────────────────────────
# discover_lrz_storage — fake executor to avoid touching SSH.
# ──────────────────────────────────────────────────────────────────────


class FakeExecutor(Executor):
    """Toy executor that hands back canned responses keyed by argv shape."""
    def __init__(self, responses: dict[tuple, str]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], timeout: float = 30.0) -> str:  # noqa: ARG002
        self.calls.append(list(argv))
        # Match by tuple-of-argv (or first/last token for a fuzzy fallback).
        key = tuple(argv)
        if key in self.responses:
            return self.responses[key]
        # Fallback: dispatch by argv[0] and the last positional arg.
        if argv[0] == "df":
            # df -B1 path1 path2 ... — concatenate per-path canned rows
            # under a single header so parse_df_multi can split them.
            paths = [a for a in argv[1:] if not a.startswith("-")]
            if not paths:
                return ""
            rows: list[str] = []
            header_used = False
            for p in paths:
                resp = self.responses.get(("df", p))
                if resp is None:
                    continue
                # Each canned response has a header on the first non-empty
                # line; emit it once, then strip it from subsequent rows.
                lines = [ln for ln in resp.splitlines() if ln.strip()]
                if not lines:
                    continue
                if not header_used:
                    rows.append(lines[0])
                    header_used = True
                for ln in lines[1:]:
                    rows.append(ln)
            return "\n".join(rows) + ("\n" if rows else "")
        if argv[0] == "bash":
            return self.responses.get(("bash",), "")
        return ""


def _df_response(path: str, total: int, used: int, avail: int) -> str:
    """Mimic `df -B1 <path>` single-row output."""
    return (
        "Filesystem     1B-blocks         Used   Available Use% Mounted on\n"
        f"fakefs {total} {used} {avail} 90% {path}\n"
    )


@pytest.mark.asyncio
async def test_discover_lrz_storage_assembles_home_scratch_containers():
    """End-to-end: feed dssusrinfo + canned df responses, confirm we get
    home + scratch + container entries with correct used / total."""
    dssusrinfo_text = (FIXTURES / "dssusrinfo_all.txt").read_text()
    sep = "---DSSUSRINFO_BEGIN---"
    bash_payload = (
        "HOME=/dss/dsshome1/03/di35dob\n"
        "MCML=/dss/mcmlscratch/03/di35dob\n"
        f"{sep}\n"
        f"{dssusrinfo_text}"
    )

    responses = {
        ("bash",): bash_payload,
        # df returns are indexed by (cmd, path) — see FakeExecutor.run.
        ("df", "/dss/dsshome1/03/di35dob"):
            _df_response("/dss/dsshome1/03/di35dob",
                         total=200_000_000_000, used=85_000_000_000, avail=110_000_000_000),
        ("df", "/dss/mcmlscratch/03/di35dob"):
            _df_response("/dss/mcmlscratch/03/di35dob",
                         total=50_000_000_000_000, used=30_000_000_000_000, avail=20_000_000_000_000),
        ("df", "/dss/dssmcmlfs01/pn25pi/pn25pi-dss-0000"):
            _df_response("/dss/dssmcmlfs01/pn25pi/pn25pi-dss-0000",
                         total=10_000_000_000_000, used=9_500_000_000_000, avail=500_000_000_000),
    }
    ex = FakeExecutor(responses)

    entries = await discover_lrz_storage(ex)
    by_label = {e.label: e for e in entries}

    # Home + scratch + 1 container.
    assert "home" in by_label
    assert "scratch" in by_label
    assert "pn25pi-dss-0000" in by_label

    home = by_label["home"]
    # Quota override: dssusrinfo says 89/100 GB → bytes via GB=1e9.
    assert home.total_bytes == 100_000_000_000
    assert home.used_bytes == 89_000_000_000
    assert home.source == "quota"

    scratch = by_label["scratch"]
    # No quota for scratch — falls back to df.
    assert scratch.source == "df"
    assert scratch.total_bytes == 50_000_000_000_000

    container = by_label["pn25pi-dss-0000"]
    # Quota override on container: 9733/10000 GB.
    assert container.total_bytes == 10_000_000_000_000
    assert container.used_bytes == 9_733_000_000_000
    assert container.source == "quota"


@pytest.mark.asyncio
async def test_discover_lrz_storage_handles_missing_dssusrinfo():
    """Defensive: when dssusrinfo body is empty (failed / no containers),
    the discoverer should still emit home + scratch from env vars + df."""
    sep = "---DSSUSRINFO_BEGIN---"
    bash_payload = (
        "HOME=/dss/dsshome1/03/u123\n"
        "MCML=/dss/mcmlscratch/03/u123\n"
        f"{sep}\n"
    )
    responses = {
        ("bash",): bash_payload,
        ("df", "/dss/dsshome1/03/u123"):
            _df_response("/dss/dsshome1/03/u123",
                         total=200_000_000_000, used=50_000_000_000, avail=150_000_000_000),
        ("df", "/dss/mcmlscratch/03/u123"):
            _df_response("/dss/mcmlscratch/03/u123",
                         total=50_000_000_000_000, used=10_000_000_000_000, avail=40_000_000_000_000),
    }
    ex = FakeExecutor(responses)

    entries = await discover_lrz_storage(ex)
    labels = {e.label for e in entries}
    assert "home" in labels
    assert "scratch" in labels
    # No containers when dssusrinfo body is empty.
    assert all(lbl in {"home", "scratch"} for lbl in labels)


@pytest.mark.asyncio
async def test_discover_lrz_storage_caps_avail_at_quota_headroom():
    """Issue #6 — when dssusrinfo overrides total with a quota, the df-
    reported avail (filesystem-wide) MUST be capped at quota - used.

    Without the cap, /dss/dsshome1's NFS-wide free space (hundreds of TiB)
    paired with a 100 GB user quota produced a "free 317 TiB / total 100 GB"
    nonsense reading. After the cap, free_bytes ≤ total_bytes always.
    """
    dssusrinfo_text = (FIXTURES / "dssusrinfo_all.txt").read_text()
    sep = "---DSSUSRINFO_BEGIN---"
    bash_payload = (
        "HOME=/dss/dsshome1/03/di35dob\n"
        "MCML=/dss/mcmlscratch/03/di35dob\n"
        f"{sep}\n"
        f"{dssusrinfo_text}"
    )
    # df reports MASSIVE free for /dss/dsshome1 (hundreds of TiB); the
    # user's quota is only 100 GB. The quota - used headroom = 100-89 = 11 GB.
    huge_df_avail = 317 * 1024**4  # 317 TiB
    responses = {
        ("bash",): bash_payload,
        ("df", "/dss/dsshome1/03/di35dob"):
            _df_response("/dss/dsshome1/03/di35dob",
                         total=500_000_000_000_000, used=200_000_000_000_000,
                         avail=huge_df_avail),
        ("df", "/dss/mcmlscratch/03/di35dob"):
            _df_response("/dss/mcmlscratch/03/di35dob",
                         total=50_000_000_000_000, used=10_000_000_000_000,
                         avail=40_000_000_000_000),
        # Container with df overstating avail too.
        ("df", "/dss/dssmcmlfs01/pn25pi/pn25pi-dss-0000"):
            _df_response("/dss/dssmcmlfs01/pn25pi/pn25pi-dss-0000",
                         total=100 * 1024**4, used=50 * 1024**4,
                         avail=50 * 1024**4),
    }
    ex = FakeExecutor(responses)
    entries = await discover_lrz_storage(ex)
    by_label = {e.label: e for e in entries}

    home = by_label["home"]
    expected_home_headroom = 100_000_000_000 - 89_000_000_000  # 11 GB
    assert home.avail_bytes == expected_home_headroom, (
        f"home avail must cap at quota headroom = {expected_home_headroom}, "
        f"got {home.avail_bytes}"
    )
    assert home.avail_bytes <= home.total_bytes, "free must be ≤ total"

    container = by_label["pn25pi-dss-0000"]
    # Quota: 9733 / 10000 GB used. Headroom = 267 GB. df overstates at 50 TiB.
    expected_c_headroom = 10_000_000_000_000 - 9_733_000_000_000
    assert container.avail_bytes == expected_c_headroom
    assert container.avail_bytes <= container.total_bytes


@pytest.mark.asyncio
async def test_discover_lrz_storage_uses_quota_headroom_when_df_missing():
    """When df doesn't return for a quota'd path, avail still gets the
    quota - used headroom (was None previously)."""
    dssusrinfo_text = (FIXTURES / "dssusrinfo_all.txt").read_text()
    sep = "---DSSUSRINFO_BEGIN---"
    bash_payload = (
        "HOME=/dss/dsshome1/03/di35dob\n"
        "MCML=/dss/mcmlscratch/03/di35dob\n"
        f"{sep}\n"
        f"{dssusrinfo_text}"
    )
    # No df response for home — simulates df failing on that path.
    responses = {("bash",): bash_payload}
    ex = FakeExecutor(responses)
    entries = await discover_lrz_storage(ex)
    by_label = {e.label: e for e in entries}
    home = by_label["home"]
    assert home.avail_bytes == 100_000_000_000 - 89_000_000_000
