"""Tests for the single-channel combined collector.

Covers the pure-function pieces (`build_script`, `parse_combined`) plus
a fake-Executor end-to-end path that asserts exactly ONE channel per
fetch_combined call (the entire point of this module — see
collectors/combined.py docstring on rohan's MaxSessions=10 cap).
"""
from __future__ import annotations

from rohanboard.collectors.combined import (
    SECTION_DELIM,
    SECTION_END,
    build_script,
    fetch_combined,
    parse_combined,
)


def test_parse_combined_roundtrip_well_formed():
    """build_script's emitted format → parse_combined recovers it."""
    text = (
        f"{SECTION_DELIM}mounts\n"
        "/dev/sda1 / ext4 rw 0 0\n"
        "tmpfs /run tmpfs rw 0 0\n"
        f"{SECTION_END}\n"
        f"{SECTION_DELIM}quota\n"
        "Disk quotas for user hli (uid 10069):\n"
        f"{SECTION_END}\n"
        f"{SECTION_DELIM}df\n"
        "Filesystem 1B-blocks Used Available Use% Mounted on\n"
        "host:/local 100 50 50 50% /cluster/host\n"
        f"{SECTION_END}\n"
        f"{SECTION_DELIM}squeue\n"
        f"{SECTION_END}\n"
        f"{SECTION_DELIM}sacct\n"
        f"{SECTION_END}\n"
        f"{SECTION_DELIM}nodes\n"
        "NodeName=foo CPUTot=8\n"
        f"{SECTION_END}\n"
    )
    raw = parse_combined(text)
    assert "/dev/sda1" in raw.mounts
    assert "tmpfs" in raw.mounts
    assert "Disk quotas for user hli" in raw.quota
    assert "/cluster/host" in raw.df
    assert raw.squeue == ""
    assert raw.sacct == ""
    assert "NodeName=foo" in raw.nodes


def test_parse_combined_missing_section_returns_empty():
    """Sections not emitted (e.g. quota_user=None → no quota probe) come
    back as empty strings, NOT KeyError."""
    text = (
        f"{SECTION_DELIM}mounts\n"
        "x / proc rw 0 0\n"
        f"{SECTION_END}\n"
    )
    raw = parse_combined(text)
    assert raw.mounts == "x / proc rw 0 0"
    assert raw.quota == ""
    assert raw.df == ""
    assert raw.nodes == ""


def test_parse_combined_unterminated_section_still_captured():
    """If the script is truncated mid-section (timeout / SIGPIPE), the
    parser shouldn't lose the partial output."""
    text = (
        f"{SECTION_DELIM}mounts\n"
        "x / proc rw 0 0\n"
        # No SECTION_END before EOF.
    )
    raw = parse_combined(text)
    assert raw.mounts == "x / proc rw 0 0"


def test_build_script_runs_each_section_in_parallel():
    """Each section becomes a `( cmd > tempfile ... ) &` invocation,
    followed by `wait`, then the dump loop."""
    script = build_script({
        "mounts": "cat /proc/mounts",
        "quota": "quota -u hli",
    })
    # Both sections background.
    assert "( cat /proc/mounts > " in script
    assert "( quota -u hli > " in script
    assert script.count(" &") >= 2
    # Single `wait` between parallel and dump loop.
    assert "\nwait\n" in script
    # Dump loop emits sentinels per section.
    assert f'echo "{SECTION_DELIM}mounts"' in script
    assert f'echo "{SECTION_DELIM}quota"' in script
    assert f'echo "{SECTION_END}"' in script


def test_build_script_traps_exit_for_tempdir_cleanup():
    """The script must clean up its tempdir on exit (success OR failure)
    so a SIGINT'd refresh tick doesn't leak /tmp/rohanboard.* dirs on
    every failure."""
    script = build_script({"mounts": "cat /proc/mounts"})
    assert "T=$(mktemp -d -t rohanboard.XXXXXX)" in script
    assert "trap 'rm -rf \"$T\"' EXIT" in script


# ──────────────────────────────────────────────────────────────────────
# Fake Executor — records each .run() call. Asserts ONE channel per fetch.
# ──────────────────────────────────────────────────────────────────────


class _FakeExecutor:
    def __init__(self, response: tuple[int, str, str] = (0, "", "")):
        self.calls: list[tuple[tuple[str, ...], float | None]] = []
        self._response = response

    async def run(self, argv, timeout=None):
        self.calls.append((tuple(argv), timeout))
        return self._response

    async def aclose(self):
        return None


async def test_fetch_combined_makes_exactly_one_channel_per_tick():
    """The whole point of this module — one ssh channel per refresh tick,
    not one per sub-collector. Asserts the fake executor's .run() count
    is exactly 1, not 3-30+."""
    fake = _FakeExecutor(response=(
        0,
        # Minimal-but-well-formed sectioned output.
        f"{SECTION_DELIM}mounts\n{SECTION_END}\n"
        f"{SECTION_DELIM}nodes\n{SECTION_END}\n"
        f"{SECTION_DELIM}quota\n{SECTION_END}\n"
        f"{SECTION_DELIM}df\n{SECTION_END}\n"
        f"{SECTION_DELIM}squeue\n{SECTION_END}\n"
        f"{SECTION_DELIM}sacct\n{SECTION_END}\n",
        "",
    ))
    raw = await fetch_combined(
        fake,
        quota_user="hli",
        df_explicit_paths=[],
        df_prefix_globs=["/cluster/*", "/cluster_HDD/*"],
        squeue_format="JobID:|,State:|",
        squeue_users=["hli"],
        sacct_format="JobID,State",
        sacct_starttime="now-3days",
        sacct_users=["hli"],
    )
    assert len(fake.calls) == 1, (
        f"expected exactly 1 ssh channel per tick, got {len(fake.calls)}: "
        f"{fake.calls}"
    )
    argv, _timeout = fake.calls[0]
    # Wrapped in `bash -c <script>`.
    assert argv[0] == "bash"
    assert argv[1] == "-c"
    script = argv[2]
    # All five+ sub-commands appear inside the same script.
    assert "cat /proc/mounts" in script
    assert "quota -u hli" in script
    assert "df -B1" in script
    assert "/cluster/*" in script
    assert "/cluster_HDD/*" in script
    assert "squeue -h -O" in script
    assert "sacct -X -P -n" in script
    assert "scontrol show node --all" in script
    # Section dump produced empty raw fields — they're well-formed empty,
    # not None.
    assert raw.mounts == ""
    assert raw.df == ""


async def test_fetch_combined_quota_skipped_when_no_user():
    """quota_user=None → the quota section is dropped from the script
    entirely (no spurious `quota -u ` invocation)."""
    fake = _FakeExecutor(response=(0, "", ""))
    await fetch_combined(
        fake,
        quota_user=None,
        df_explicit_paths=[],
        df_prefix_globs=[],
        squeue_format="JobID:|",
        squeue_users=None,
        sacct_format="JobID",
        sacct_starttime="now-3days",
        sacct_users=None,
    )
    script = fake.calls[0][0][2]
    assert "quota -u" not in script


def test_fetch_combined_raises_on_wrapper_failure():
    """rc != 0 from the bash wrapper itself (e.g. mktemp failure) should
    propagate as RuntimeError — caller's tick records it in snap.errors
    rather than burying the diagnostic."""
    import asyncio

    fake = _FakeExecutor(response=(2, "", "mktemp: failed to create directory"))

    async def go():
        await fetch_combined(
            fake,
            quota_user="hli",
            df_explicit_paths=[],
            df_prefix_globs=[],
            squeue_format="x",
            squeue_users=None,
            sacct_format="y",
            sacct_starttime="now",
            sacct_users=None,
        )

    try:
        asyncio.run(go())
    except RuntimeError as e:
        assert "rc=2" in str(e)
        assert "mktemp" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
