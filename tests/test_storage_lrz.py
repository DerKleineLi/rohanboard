"""Step-4 — LRZ storage auto-discovery (discover_lrz_storage).

Covers:
- `parse_dssusrinfo` extracts home + at least one container with quota
  (also checked in test_storage.py; kept here for the file-as-spec view).
- `discover_lrz_storage` end-to-end: stub the executor so it returns
  ONE coalesced shell-script response containing env vars + dssusrinfo
  body + df output for each known + discovered path. Confirm the
  resulting StorageEntry list contains home, scratch, and each
  container, with quota-overridden totals where dssusrinfo reported one.

  The 2026-05-05 single-RTT coalesce embeds the df output under a
  `---DFBLOCK_BEGIN---` sentinel in the same shell script as
  dssusrinfo. The FakeExecutor below builds that response shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rohanboard.collectors.storage import discover_lrz_storage, parse_dssusrinfo
from rohanboard.exec import Executor


FIXTURES = Path(__file__).parent / "fixtures"

SEP_DSS = "---DSSUSRINFO_BEGIN---"
SEP_DSS_END = "---DSSUSRINFO_END---"
SEP_DF = "---DFBLOCK_BEGIN---"


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


def _build_bash_payload(
    home: str | None,
    mcml: str | None,
    dssusrinfo_text: str,
    df_paths: dict[str, tuple[int, int, int]],
) -> str:
    """Build the canned single-call response shape that the new coalesced
    `discover_lrz_storage` expects: prefix (env) + DSS body + DF block.

    `df_paths` maps {path → (total, used, avail)}. Paths missing from
    that dict are absent from the df dump (simulates df failing on that
    path or the path not existing).
    """
    lines: list[str] = []
    lines.append(f"HOME={home or ''}")
    lines.append(f"MCML={mcml or ''}")
    lines.append(SEP_DSS)
    lines.append(dssusrinfo_text)
    lines.append(SEP_DSS_END)
    lines.append(SEP_DF)
    if df_paths:
        lines.append("Filesystem     1B-blocks         Used   Available Use% Mounted on")
        for path, (total, used, avail) in df_paths.items():
            lines.append(f"fakefs {total} {used} {avail} 90% {path}")
    return "\n".join(lines) + "\n"


class FakeExecutor(Executor):
    """Toy executor that hands back ONE canned response for the bash call."""
    def __init__(self, bash_payload: str) -> None:
        self.bash_payload = bash_payload
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], timeout: float = 30.0) -> str:  # noqa: ARG002
        self.calls.append(list(argv))
        if argv and argv[0] == "bash":
            return self.bash_payload
        return ""

    async def whoami(self) -> str:
        return "di35dob"


@pytest.mark.asyncio
async def test_discover_lrz_storage_assembles_home_scratch_containers():
    """End-to-end: feed dssusrinfo + canned df responses (all coalesced
    into ONE bash call), confirm we get home + scratch + container
    entries with correct used / total."""
    dssusrinfo_text = (FIXTURES / "dssusrinfo_all.txt").read_text()
    bash_payload = _build_bash_payload(
        home="/dss/dsshome1/03/di35dob",
        mcml="/dss/mcmlscratch/03/di35dob",
        dssusrinfo_text=dssusrinfo_text,
        df_paths={
            "/dss/dsshome1/03/di35dob":
                (200_000_000_000, 85_000_000_000, 110_000_000_000),
            "/dss/mcmlscratch/03/di35dob":
                (50_000_000_000_000, 30_000_000_000_000, 20_000_000_000_000),
            "/dss/dssmcmlfs01/pn25pi/pn25pi-dss-0000":
                (10_000_000_000_000, 9_500_000_000_000, 500_000_000_000),
        },
    )
    ex = FakeExecutor(bash_payload)

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
async def test_discover_lrz_storage_issues_one_ssh_call_per_tick():
    """The COALESCE FIX (issue #1+#3) — discover_lrz_storage MUST hit
    the executor exactly once per call, not twice.

    Was: bash (env+dssusrinfo) + df-multi = 2 round-trips × Semaphore(1)
    fully serial, ~6 s LRZ tick. Now: 1 round-trip via embedded df.
    Per saved memory feedback_asyncio_subprocess_cancel_leaks.md, the
    coalesce is at the COLLECTOR layer; the Sem(1) cap is unchanged.
    """
    dssusrinfo_text = (FIXTURES / "dssusrinfo_all.txt").read_text()
    bash_payload = _build_bash_payload(
        home="/dss/dsshome1/03/di35dob",
        mcml="/dss/mcmlscratch/03/di35dob",
        dssusrinfo_text=dssusrinfo_text,
        df_paths={
            "/dss/dsshome1/03/di35dob":
                (200_000_000_000, 85_000_000_000, 110_000_000_000),
            "/dss/mcmlscratch/03/di35dob":
                (50_000_000_000_000, 30_000_000_000_000, 20_000_000_000_000),
            "/dss/dssmcmlfs01/pn25pi/pn25pi-dss-0000":
                (10_000_000_000_000, 9_500_000_000_000, 500_000_000_000),
        },
    )
    ex = FakeExecutor(bash_payload)
    await discover_lrz_storage(ex)
    assert len(ex.calls) == 1, (
        f"discover_lrz_storage must issue exactly ONE ssh call; got "
        f"{len(ex.calls)} ({[c[0] if c else None for c in ex.calls]})"
    )
    # And the one call is the bash script.
    assert ex.calls[0][0] == "bash"


@pytest.mark.asyncio
async def test_discover_lrz_storage_handles_missing_dssusrinfo():
    """Defensive: when dssusrinfo body is empty (failed / no containers),
    the discoverer should still emit home + scratch from env vars + df."""
    bash_payload = _build_bash_payload(
        home="/dss/dsshome1/03/u123",
        mcml="/dss/mcmlscratch/03/u123",
        dssusrinfo_text="",
        df_paths={
            "/dss/dsshome1/03/u123":
                (200_000_000_000, 50_000_000_000, 150_000_000_000),
            "/dss/mcmlscratch/03/u123":
                (50_000_000_000_000, 10_000_000_000_000, 40_000_000_000_000),
        },
    )
    ex = FakeExecutor(bash_payload)

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
    huge_df_avail = 317 * 1024**4  # 317 TiB
    bash_payload = _build_bash_payload(
        home="/dss/dsshome1/03/di35dob",
        mcml="/dss/mcmlscratch/03/di35dob",
        dssusrinfo_text=dssusrinfo_text,
        df_paths={
            "/dss/dsshome1/03/di35dob":
                (500_000_000_000_000, 200_000_000_000_000, huge_df_avail),
            "/dss/mcmlscratch/03/di35dob":
                (50_000_000_000_000, 10_000_000_000_000, 40_000_000_000_000),
            "/dss/dssmcmlfs01/pn25pi/pn25pi-dss-0000":
                (100 * 1024**4, 50 * 1024**4, 50 * 1024**4),
        },
    )
    ex = FakeExecutor(bash_payload)
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
    # df_paths empty — simulates df failing on every path.
    bash_payload = _build_bash_payload(
        home="/dss/dsshome1/03/di35dob",
        mcml="/dss/mcmlscratch/03/di35dob",
        dssusrinfo_text=dssusrinfo_text,
        df_paths={},
    )
    ex = FakeExecutor(bash_payload)
    entries = await discover_lrz_storage(ex)
    by_label = {e.label: e for e in entries}
    home = by_label["home"]
    assert home.avail_bytes == 100_000_000_000 - 89_000_000_000
