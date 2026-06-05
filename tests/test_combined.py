"""Tests for the single-channel combined collector.

Covers the pure-function pieces (`build_script`, `parse_combined`) plus
a fake-Executor end-to-end path that asserts exactly ONE channel per
fetch_combined call (the entire point of this module — see
collectors/combined.py docstring on rohan's MaxSessions=10 cap).
"""
from __future__ import annotations

from rohanboard.collectors.combined import (
    PER_CMD_TIMEOUT_SECS,
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
        f"{SECTION_DELIM}sacct_self\n"
        f"{SECTION_END}\n"
        f"{SECTION_DELIM}sacct_all\n"
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
    assert raw.sacct_self == ""
    assert raw.sacct_all == ""
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
    """Each section becomes a backgrounded `( timeout … bash -c <body> >
    tempfile … ) &` invocation, followed by `wait`, then the dump loop."""
    script = build_script({
        "mounts": "cat /proc/mounts",
        "quota": "quota -u hli",
    })
    # Both sections background, each redirecting into its own tempfile.
    assert '> "$T/mounts" 2>/dev/null || true ) &' in script
    assert '> "$T/quota" 2>/dev/null || true ) &' in script
    # The original command body survives verbatim inside the wrap.
    assert "cat /proc/mounts" in script
    assert "quota -u hli" in script
    assert script.count(" &") >= 2
    # Single `wait` between parallel and dump loop.
    assert "\nwait\n" in script
    # Dump loop emits sentinels per section.
    assert f'echo "{SECTION_DELIM}mounts"' in script
    assert f'echo "{SECTION_DELIM}quota"' in script
    assert f'echo "{SECTION_END}"' in script


def test_build_script_wraps_each_section_in_timeout():
    """Defense-in-depth: every section body runs under `timeout -k 1 N
    bash -c <body>` so one hanging command degrades to an empty section
    instead of blocking the shared `wait` and wedging the whole tick
    (2026-06-05 quota-hang freeze). The inner `bash -c` re-parses the body
    so globs / quoted args / the dssusrinfo subshell still expand."""
    script = build_script({
        "mounts": "cat /proc/mounts",
        "df": "df -B1 /cluster/* /cluster_HDD/*",
        "squeue": "squeue -h -O 'JobID:|,State:|'",
    })
    # Each body is wrapped — N is the module default, killed 1 s after TERM.
    assert f"timeout -k 1 {PER_CMD_TIMEOUT_SECS} bash -c " in script
    # One wrap per section.
    assert script.count("timeout -k 1 ") == 3
    # The wrapped body is single-quoted; the glob survives for the inner
    # shell to expand, and an already-quoted arg round-trips intact.
    assert "timeout -k 1 4 bash -c 'cat /proc/mounts'" in script
    assert "df -B1 /cluster/* /cluster_HDD/*" in script
    assert "JobID:|,State:|" in script


def test_build_script_timeout_value_is_configurable():
    """cmd_timeout overrides the per-command cap (tunable per cluster if a
    future config wants a tighter/looser budget)."""
    script = build_script({"mounts": "cat /proc/mounts"}, cmd_timeout=9)
    assert "timeout -k 1 9 bash -c 'cat /proc/mounts'" in script
    assert "timeout -k 1 4 " not in script


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
        f"{SECTION_DELIM}sacct_self\n{SECTION_END}\n"
        f"{SECTION_DELIM}sacct_all\n{SECTION_END}\n",
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
        sacct_starttime_self="now-7days",
        sacct_starttime_all="now-1day",
        sacct_user="hli",
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


async def test_fetch_combined_dssusrinfo_section_uses_inner_subshell():
    """When dssusrinfo_subcommands has 2+ entries, the section value
    must wrap them in an inner `( ... )` subshell so the outer `> FILE`
    redirect captures BOTH outputs. Without the wrap, only the LAST
    subcommand's stdout reaches the dump and the home block gets
    silently dropped — observed live on LRZ 2026-05-10."""
    fake = _FakeExecutor(response=(0, "", ""))
    await fetch_combined(
        fake,
        quota_user=None,
        df_explicit_paths=[],
        df_prefix_globs=[],
        squeue_format="x",
        squeue_users=None,
        sacct_format="y",
        sacct_starttime_self="now",
        sacct_starttime_all="now",
        sacct_user=None,
        dssusrinfo_subcommands=["dsshome", "container_usage"],
    )
    script = fake.calls[0][0][2]
    # Inner subshell wraps the chain; the outer template applies the
    # redirect to that whole subshell.
    assert "( dssusrinfo dsshome; dssusrinfo container_usage )" in script
    # Plain `dssusrinfo dsshome; dssusrinfo container_usage > $T/...`
    # without the inner parens MUST NOT be present — that's the bug shape.
    assert "dssusrinfo dsshome; dssusrinfo container_usage > " not in script


async def test_fetch_combined_dssusrinfo_section_skipped_when_empty():
    """No dssusrinfo entries → no dssusrinfo section in the script
    (rohan and other non-LRZ clusters lack the binary)."""
    fake = _FakeExecutor(response=(0, "", ""))
    await fetch_combined(
        fake,
        quota_user="hli",
        df_explicit_paths=[],
        df_prefix_globs=["/cluster/*"],
        squeue_format="x",
        squeue_users=None,
        sacct_format="y",
        sacct_starttime_self="now",
        sacct_starttime_all="now",
        sacct_user=None,
        dssusrinfo_subcommands=None,
    )
    script = fake.calls[0][0][2]
    assert "dssusrinfo" not in script


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
        sacct_starttime_self="now-3days",
        sacct_starttime_all="now-3days",
        sacct_user=None,
    )
    script = fake.calls[0][0][2]
    assert "quota -u" not in script


async def test_fetch_combined_quota_uses_local_only_flag():
    """The quota section MUST pass `-l` (--local-only). Plain
    `quota -u <user>` probes every NFS mount via rquotad RPC; on rohan's
    login node an unresponsive NFS server hangs the call >125 s, which —
    because quota runs inside the combined script's `wait` — blocks the
    whole tick and freezes the board (home never appears in Storage).
    rohan home (/rhome) is local, so `-l` returns its quota fast.
    Regression guard for the 2026-06-05 home-missing incident."""
    fake = _FakeExecutor(response=(0, "", ""))
    await fetch_combined(
        fake,
        quota_user="hli",
        df_explicit_paths=[],
        df_prefix_globs=[],
        squeue_format="x",
        squeue_users=None,
        sacct_format="y",
        sacct_starttime_self="now",
        sacct_starttime_all="now",
        sacct_user=None,
    )
    script = fake.calls[0][0][2]
    assert "quota -u hli -l" in script


async def test_fetch_combined_includes_whoami_section():
    """Bundle-1 Sub-fix-3: the combined script unconditionally includes
    a `whoami` section so REMOTE username arrives WITH the first tick
    instead of needing a serial `resolve_whoami` round-trip first."""
    fake = _FakeExecutor(response=(
        0,
        f"{SECTION_DELIM}mounts\n{SECTION_END}\n"
        f"{SECTION_DELIM}nodes\n{SECTION_END}\n"
        f"{SECTION_DELIM}whoami\n"
        "di35dob\n"
        f"{SECTION_END}\n"
        f"{SECTION_DELIM}squeue\n{SECTION_END}\n"
        f"{SECTION_DELIM}sacct_all\n{SECTION_END}\n",
        "",
    ))
    raw = await fetch_combined(
        fake,
        quota_user=None,
        df_explicit_paths=[],
        df_prefix_globs=[],
        squeue_format="x",
        squeue_users=None,
        sacct_format="y",
        sacct_starttime_self="now",
        sacct_starttime_all="now",
        sacct_user=None,
    )
    script = fake.calls[0][0][2]
    # Whoami runs in the parallel block (under the timeout wrap), redirecting
    # to its own tempfile.
    assert 'bash -c whoami > "$T/whoami"' in script, (
        f"whoami section missing from script:\n{script}"
    )
    # Parser extracts it.
    assert raw.whoami.strip() == "di35dob", f"got raw.whoami={raw.whoami!r}"


async def test_two_snapshot_sacct_self_query_filters_to_user():
    """Bundle-2 B2.1: when `sacct_user` is provided, the self section
    runs `sacct -u <user>` with `starttime_self` — it's the
    long-history per-user query."""
    fake = _FakeExecutor(response=(0, "", ""))
    await fetch_combined(
        fake,
        quota_user=None,
        df_explicit_paths=[],
        df_prefix_globs=[],
        squeue_format="x",
        squeue_users=None,
        sacct_format="y",
        sacct_starttime_self="now-7days",
        sacct_starttime_all="now-1day",
        sacct_user="hli",
    )
    script = fake.calls[0][0][2]
    # Self query: -u hli + starttime self.
    assert "sacct -X -P -n --starttime=now-7days -u hli" in script, (
        f"self section missing -u filter / wrong starttime:\n{script}"
    )


async def test_two_snapshot_sacct_all_query_omits_user_filter():
    """Bundle-2 B2.1: the all section runs `sacct -a --starttime=<all>`
    with NO -u filter — it's the short-window cross-user query."""
    fake = _FakeExecutor(response=(0, "", ""))
    await fetch_combined(
        fake,
        quota_user=None,
        df_explicit_paths=[],
        df_prefix_globs=[],
        squeue_format="x",
        squeue_users=None,
        sacct_format="y",
        sacct_starttime_self="now-7days",
        sacct_starttime_all="now-1day",
        sacct_user="hli",
    )
    script = fake.calls[0][0][2]
    # The all section uses -a and the all-starttime.
    assert "sacct -X -P -n --starttime=now-1day -a" in script, (
        f"all section missing -a / wrong starttime:\n{script}"
    )


async def test_two_snapshot_sacct_self_skipped_when_no_user():
    """Bundle-2 B2.1: cold start (sacct_user=None until whoami lands)
    → the self section is skipped (would be invalid as `sacct -u ''`).
    The all section still runs."""
    fake = _FakeExecutor(response=(0, "", ""))
    await fetch_combined(
        fake,
        quota_user=None,
        df_explicit_paths=[],
        df_prefix_globs=[],
        squeue_format="x",
        squeue_users=None,
        sacct_format="y",
        sacct_starttime_self="now-7days",
        sacct_starttime_all="now-1day",
        sacct_user=None,
    )
    script = fake.calls[0][0][2]
    # Self section is absent — no sacct -u line, no sacct_self section
    # name in the dump loop.
    assert "sacct -X -P -n --starttime=now-7days -u" not in script, (
        f"self section should be skipped when sacct_user=None:\n{script}"
    )
    # All section still runs.
    assert "sacct -X -P -n --starttime=now-1day -a" in script, (
        f"all section should run on cold start:\n{script}"
    )


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
            sacct_starttime_self="now",
            sacct_starttime_all="now",
            sacct_user=None,
        )

    try:
        asyncio.run(go())
    except RuntimeError as e:
        assert "rc=2" in str(e)
        assert "mktemp" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
