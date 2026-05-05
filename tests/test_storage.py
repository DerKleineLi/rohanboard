from pathlib import Path

from rohanboard.collectors.storage import parse_df, parse_dssusrinfo, parse_quota


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
# Step 4 — dssusrinfo parser (LRZ)
# ──────────────────────────────────────────────────────────────────────────

def test_parse_dssusrinfo():
    text = (FIXTURES / "dssusrinfo_all.txt").read_text()
    info = parse_dssusrinfo(text)
    # Home directory recovered from "DSS Homedir info" section.
    assert info.home is not None
    assert str(info.home) == "/dss/dsshome1/03/di35dob"
    # Home quota from same section: "89 of 100 GB used".
    assert info.home_used_gb == 89.0
    assert info.home_quota_gb == 100.0
    # Containers from "DSS Containers" section.
    assert len(info.containers) == 1
    c = info.containers[0]
    assert c.name == "pn25pi-dss-0000"
    assert str(c.path) == "/dss/dssmcmlfs01/pn25pi/pn25pi-dss-0000"
    # Container usage and limits: "9733 of 10000 GB used".
    assert c.used_gb == 9733.0
    assert c.quota_gb == 10000.0
    # Files quota: "14838667 of 20000000 Files used".
    assert c.used_files == 14838667
    assert c.quota_files == 20000000


def test_parse_dssusrinfo_empty_input():
    """Defensive: empty / garbage input → empty DssInfo, no exceptions."""
    info = parse_dssusrinfo("")
    assert info.home is None
    assert info.containers == []
    info2 = parse_dssusrinfo("nothing useful here\n***\n")
    assert info2.home is None
    assert info2.containers == []
