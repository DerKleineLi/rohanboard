from pathlib import Path

from rohanboard.collectors.storage import discover_mounts, parse_df, parse_quota


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
