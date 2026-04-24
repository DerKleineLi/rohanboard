from pathlib import Path

from rohanboard.collectors.storage import parse_df, parse_quota


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
    assert entry.fraction > 0.94
