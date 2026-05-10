from pathlib import Path

from rohanboard.collectors.storage import (
    discover_mounts,
    parse_df,
    parse_dssusrinfo_container,
    parse_dssusrinfo_dsshome,
    parse_quota,
)


_GiB = 1024 ** 3


# Real captured stdout from `ssh lrz 'dssusrinfo dsshome; dssusrinfo
# container_usage'` on 2026-05-10 (user di35dob). Contains both blocks
# back-to-back as the combined collector emits.
_DSSUSRINFO_FIXTURE = """
********************************************************************************
*                                                                              *
*                           LRZ DSS User Info Script                           *
*                                                                              *
*           ATTENTION: This script may block if node is not healthy.           *
*                                                                              *
********************************************************************************




********************************************************************************
*                                                                              *
*                        DSS Homedir info for di35dob:                         *
*                                                                              *
* Your DSS based home directory is at /dss/dsshome1/03/di35dob                 *
*                                                                              *
* Quota and usage information of your DSS based home directory:                *
*                                                                              *
*               89 of 100              GB    used                              *
*                                                                              *
*           242415 of Unlimited        Files used                              *
*                                                                              *
********************************************************************************

********************************************************************************
*                                                                              *
*                       DSS Container usage and limits:                        *
*                                                                              *
* pn25pi-dss-0000                  9810 of 10000            GB    used         *
*                                                                              *
* pn25pi-dss-0000              15121730 of 20000000         Files used         *
*                                                                              *
********************************************************************************
"""


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_quota_two_line_form():
    text = (FIXTURES / "quota.txt").read_text()
    entry = parse_quota(text, label="home")
    assert entry is not None
    assert entry.label == "home"
    assert entry.source == "quota"
    assert entry.path == "/dev/mapper/i28storage-data"
    # 281361708 KiB used, 314572800 KiB soft, 419430400 KiB hard.
    assert entry.used_bytes == 281361708 * 1024
    assert entry.total_bytes == 314572800 * 1024
    assert entry.hard_limit_bytes == 419430400 * 1024
    # ~268 GiB used, ~300 GiB soft.
    assert 0.85 < entry.fraction < 0.95


def test_parse_df_first_mount():
    text = (FIXTURES / "df.txt").read_text()
    entry = parse_df(text, label="balar", path="/cluster/balar")
    assert entry is not None
    assert entry.label == "balar"
    assert entry.source == "df"
    # First data row in fixture is /cluster/balar with 45 TB total.
    assert entry.total_bytes == 45847312072704
    assert entry.used_bytes == 43514124566528
    # df Available is far smaller than total - used on this fs (reserved blocks).
    assert entry.avail_bytes == 31591497728
    assert entry.free_bytes == 31591497728
    assert entry.fraction > 0.94


# ──────────────────────────────────────────────────────────────────────────
# discover_mounts — pure parser; caller fetches /proc/mounts text
# ──────────────────────────────────────────────────────────────────────────


def test_discover_mounts_with_rohan_proc_mounts_fixture():
    """Real /proc/mounts text from rohan, captured 2026-05-08. Has the
    autofs root for /cluster + /cluster_HDD plus 24 unique per-node SSD
    NFS mounts (one case-aliased dupe `/cluster/daidaloS` shares its
    source with `/cluster/daidalos` and is deduped) and 8 per-node HDD
    NFS mounts."""
    text = (FIXTURES / "rohan_proc_mounts.txt").read_text()
    pairs = discover_mounts(text, prefixes=["/cluster", "/cluster_HDD"])
    paths = [p for _label, p in pairs]
    # autofs roots filtered (DEFAULT_SKIP_FSTYPES). Per-node NFS mounts kept.
    assert "/cluster" not in paths
    assert "/cluster_HDD" not in paths
    # 24 unique SSD nodes (daidalos / daidaloS dedupe to one).
    ssd = [p for p in paths if p.startswith("/cluster/")]
    assert len(ssd) == 24, f"expected 24 SSD mounts, got {len(ssd)}: {ssd}"
    # Sample expected nodes — comprehensive enumeration not needed once
    # the count + dedupe semantics are pinned.
    assert "/cluster/balar" in ssd
    assert "/cluster/angmar" in ssd
    assert "/cluster/valinor" in ssd
    # 8 HDD mounts.
    hdd = [p for p in paths if p.startswith("/cluster_HDD/")]
    assert len(hdd) == 8, f"expected 8 HDD mounts, got {len(hdd)}: {hdd}"
    assert "/cluster_HDD/gondor" in hdd
    assert "/cluster_HDD/lothlann" in hdd
    # Sorted alphabetically.
    assert paths == sorted(paths, key=str.lower)
    # daidalos / daidaloS share a source — only one survives dedupe.
    daidalos_paths = [p for p in ssd if p.lower() == "/cluster/daidalos"]
    assert len(daidalos_paths) == 1, (
        f"case-aliased daidalos/daidaloS must dedupe by source, got {daidalos_paths}"
    )


def test_discover_mounts_empty_text_returns_empty():
    """Defensive: empty `/proc/mounts` (e.g. transient SSH glitch where
    `fetch_proc_mounts` returns "") yields zero pairs, not an exception."""
    assert discover_mounts("", prefixes=["/cluster"]) == []


def test_parse_df_multi_dedupes_case_aliased_autofs_mounts():
    """rohan's autofs exposes some nodes via case-aliased paths
    (`/cluster/daidalos` and `/cluster/daidaloS` both NFS-mounted from
    `daidalos.vc.in.tum.de:/local`). df reports BOTH paths as separate
    rows with the same Filesystem column. parse_df_multi must dedupe
    by source — otherwise the SSD total double-counts the underlying
    storage.
    """
    from rohanboard.collectors.storage import parse_df_multi
    text = (
        "Filesystem                       1B-blocks            Used      Available Use% Mounted on\n"
        "balar.vc.in.tum.de:/local     46100612972544  39907700609024   4623127347200  90% /cluster/balar\n"
        "daidalos.vc.in.tum.de:/local  38682537619456  35986325577728    721922187264  99% /cluster/daidalos\n"
        "daidalos.vc.in.tum.de:/local  38682537619456  35986325577728    721922187264  99% /cluster/daidaloS\n"
    )
    entries = parse_df_multi(text)
    assert len(entries) == 2, (
        f"daidalos/daidaloS must dedupe to one entry, got {len(entries)}: "
        f"{[e.path for e in entries]}"
    )
    paths = [e.path for e in entries]
    assert "/cluster/balar" in paths
    # First-seen wins; daidalos appears before daidaloS in df output.
    assert "/cluster/daidalos" in paths
    assert "/cluster/daidaloS" not in paths


def test_discover_mounts_skips_uninteresting_fstypes():
    text = (
        "tmpfs /run tmpfs rw 0 0\n"
        "overlay /var/lib/docker/overlay2 overlay rw 0 0\n"
        "/etc/auto.cluster /cluster autofs rw 0 0\n"
        "host.example:/local /cluster/host nfs rw 0 0\n"
    )
    pairs = discover_mounts(text, prefixes=["/cluster"])
    assert pairs == [("/cluster/host", "/cluster/host")]


# ──────────────────────────────────────────────────────────────────────────
# dssusrinfo parser — LRZ DSS per-user / per-container quota
# ──────────────────────────────────────────────────────────────────────────


def test_parse_dssusrinfo_dsshome_extracts_used_and_total_gb():
    """`dssusrinfo dsshome` reports `89 of 100 GB used` for the user's
    home dir. The parser converts to bytes and skips the Files row."""
    entry = parse_dssusrinfo_dsshome(
        _DSSUSRINFO_FIXTURE, label="home", path="/dss/dsshome1"
    )
    assert entry is not None
    assert entry.label == "home"
    assert entry.source == "dssusrinfo"
    assert entry.path == "/dss/dsshome1"
    assert entry.used_bytes == 89 * _GiB
    assert entry.total_bytes == 100 * _GiB
    # 89/100 = 0.89 — fraction sanity check.
    assert 0.85 < entry.fraction < 0.92


def test_parse_dssusrinfo_container_filters_by_container_name():
    """`dssusrinfo container_usage` lists multiple containers; the
    parser must pick the one matching the configured container name."""
    entry = parse_dssusrinfo_container(
        _DSSUSRINFO_FIXTURE,
        container="pn25pi-dss-0000",
        label="MCML DSS",
        path="/dss/dssmcmlfs01",
    )
    assert entry is not None
    assert entry.used_bytes == 9810 * _GiB
    assert entry.total_bytes == 10000 * _GiB
    assert entry.source == "dssusrinfo"


def test_parse_dssusrinfo_container_unknown_returns_none():
    """If the named container isn't in the output (typo, lost access),
    the parser returns None — caller treats this same as a transient
    fetch failure (no entry added that tick)."""
    entry = parse_dssusrinfo_container(
        _DSSUSRINFO_FIXTURE,
        container="not-a-real-container",
        label="MCML DSS",
        path="/dss/dssmcmlfs01",
    )
    assert entry is None


def test_parse_dssusrinfo_dsshome_skips_my_usage_block():
    """`dssusrinfo my_usage` has rows like
    `pn25pi-dss-0000  220 of Container MAX  GB used` — those must NOT
    be picked up by the dsshome parser (they're NOT home-dir data, and
    `Container MAX` isn't a usable total)."""
    text = """
********************************************************************************
*                                                                              *
*                      DSS usage and limits for di35dob:                       *
*                                                                              *
* pn25pi-dss-0000                   220 of Container MAX    GB    used         *
*                                                                              *
********************************************************************************
"""
    assert parse_dssusrinfo_dsshome(text, label="home", path="/dss/dsshome1") is None


def test_parse_dssusrinfo_dsshome_handles_empty_input():
    """Defensive: combined collector returns "" when dssusrinfo isn't
    on PATH (e.g. running on rohan instead of LRZ). Parser returns None
    rather than crashing the refresh tick."""
    assert parse_dssusrinfo_dsshome("", label="home", path=None) is None
    assert parse_dssusrinfo_container(
        "", container="pn25pi-dss-0000", label="MCML DSS", path=None
    ) is None


# ──────────────────────────────────────────────────────────────────────────
# StoragePanel._classify — explicit `group` field overrides path heuristic
# ──────────────────────────────────────────────────────────────────────────


def test_classify_explicit_group_wins_over_path_heuristic():
    """An entry with `group="scratch"` lands in the scratch bucket even
    when its path doesn't start with /cluster or /cluster_HDD — that's
    the whole point of the explicit override."""
    from rohanboard.collectors.models import StorageEntry
    from rohanboard.widgets.storage_panel import _classify
    e = StorageEntry(
        label="MCMLSCRATCH",
        used_bytes=1, total_bytes=10,
        source="df", path="/dss/mcmlscratch",
        group="scratch",
    )
    assert _classify(e) == "scratch"


def test_classify_falls_back_to_path_when_no_group():
    """Back-compat: rohan's `kind="auto"` entries don't carry a group;
    the path-prefix fallback continues to work for /cluster + /cluster_HDD."""
    from rohanboard.collectors.models import StorageEntry
    from rohanboard.widgets.storage_panel import _classify
    ssd = StorageEntry(label="balar", used_bytes=1, total_bytes=10,
                       source="df", path="/cluster/balar")
    hdd = StorageEntry(label="gondor", used_bytes=1, total_bytes=10,
                       source="df", path="/cluster_HDD/gondor")
    other = StorageEntry(label="x", used_bytes=1, total_bytes=10,
                         source="df", path="/tmp/x")
    assert _classify(ssd) == "ssd"
    assert _classify(hdd) == "hdd"
    assert _classify(other) == "other"


def test_classify_unknown_group_still_returns_the_group_string():
    """The classifier itself is dumb — it returns whatever `group` says.
    StoragePanel.update_snapshot is the one that buckets unknown groups
    into "other" (the panel doesn't render groups absent from
    GROUP_ORDER). Test that contract here."""
    from rohanboard.collectors.models import StorageEntry
    from rohanboard.widgets.storage_panel import _classify
    e = StorageEntry(label="x", used_bytes=1, total_bytes=10,
                     source="df", path="/x", group="future_tier")
    assert _classify(e) == "future_tier"


def test_storage_panel_group_order_includes_new_groups():
    """Phase 4d.2-C adds scratch + persistent to the panel's render
    order so LRZ-style configs slot in cleanly. Lock the order so a
    future refactor can't silently drop them."""
    from rohanboard.widgets.storage_panel import StoragePanel
    assert "scratch" in StoragePanel.GROUP_ORDER
    assert "persistent" in StoragePanel.GROUP_ORDER
    # Home first, other last.
    assert StoragePanel.GROUP_ORDER[0] == "home"
    assert StoragePanel.GROUP_ORDER[-1] == "other"
