"""Tests for the coalesced df parser (issue #1).

`parse_df_multi` ingests `df -B1 path1 path2 ... pathN` output and returns
a dict keyed by the requested path. The motivation: one ssh round-trip
per refresh tick instead of N (24+ on the user's rohan setup) when the
ssh executor has Semaphore(1) per-host.
"""
from __future__ import annotations

import pytest

from rohanboard.collectors.storage import (
    fetch_df_multi,
    parse_df_multi,
)
from rohanboard.exec import Executor


def _df_row(filesystem: str, total: int, used: int, avail: int, mount: str) -> str:
    return f"{filesystem} {total} {used} {avail} 50% {mount}"


def test_parse_df_multi_three_paths_three_rows():
    text = "\n".join([
        "Filesystem      1B-blocks            Used      Available Use% Mounted on",
        _df_row("nfs:/cluster/a", 1_000_000, 100_000, 900_000, "/cluster/a"),
        _df_row("nfs:/cluster/b", 2_000_000, 500_000, 1_500_000, "/cluster/b"),
        _df_row("nfs:/cluster/c", 4_000_000, 1_000_000, 3_000_000, "/cluster/c"),
    ])
    out = parse_df_multi(text, ["/cluster/a", "/cluster/b", "/cluster/c"])
    assert out["/cluster/a"] == (1_000_000, 100_000, 900_000)
    assert out["/cluster/b"] == (2_000_000, 500_000, 1_500_000)
    assert out["/cluster/c"] == (4_000_000, 1_000_000, 3_000_000)


def test_parse_df_multi_empty_input_returns_empty():
    """No infinite loop, no crash — just an empty dict."""
    assert parse_df_multi("", ["/some/path"]) == {}


def test_parse_df_multi_path_subdir_resolves_to_mount():
    """When the requested path is a subdir of the actual mount, walk up."""
    text = "\n".join([
        "Filesystem 1B-blocks Used Available Use% Mounted on",
        _df_row("nfs:/cluster", 1_000_000, 100_000, 900_000, "/cluster"),
    ])
    out = parse_df_multi(text, ["/cluster/sub/dir"])
    assert out["/cluster/sub/dir"] == (1_000_000, 100_000, 900_000)


def test_parse_df_multi_long_filesystem_wraps_to_two_rows():
    """df wraps very long Filesystem fields to a second row — merge them."""
    text = (
        "Filesystem 1B-blocks Used Available Use% Mounted on\n"
        "verylongfilesystemnamethatdfwraps\n"
        "                       1000 100 900 10% /cluster/x\n"
    )
    out = parse_df_multi(text, ["/cluster/x"])
    assert out["/cluster/x"] == (1000, 100, 900)


def test_parse_df_multi_unknown_path_omitted():
    text = (
        "Filesystem 1B-blocks Used Available Use% Mounted on\n"
        "nfs 1000 100 900 10% /cluster/known\n"
    )
    out = parse_df_multi(text, ["/cluster/known", "/totally/missing"])
    assert "/cluster/known" in out
    assert "/totally/missing" not in out


# ──────────────────────────────────────────────────────────────────────
# fetch_df_multi end-to-end via fake executor
# ──────────────────────────────────────────────────────────────────────


class _OneShotExecutor(Executor):
    """Records exact argv passed; returns a canned df-multi response."""
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], timeout: float = 30.0) -> str:  # noqa: ARG002
        self.calls.append(list(argv))
        return self.response


@pytest.mark.asyncio
async def test_fetch_df_multi_emits_one_call_for_n_paths():
    """The whole point of the coalesce: ONE executor.run for N paths."""
    response = "\n".join([
        "Filesystem 1B-blocks Used Available Use% Mounted on",
        "nfs1 100 10 90 10% /cluster/a",
        "nfs2 200 20 180 10% /cluster/b",
        "nfs3 300 30 270 10% /cluster/c",
    ])
    ex = _OneShotExecutor(response)
    paths = [("a", "/cluster/a"), ("b", "/cluster/b"), ("c", "/cluster/c")]
    out = await fetch_df_multi(ex, paths)

    # Exactly one round-trip — that's the lag fix.
    assert len(ex.calls) == 1
    assert ex.calls[0][0] == "df"
    assert ex.calls[0][1] == "-B1"
    # All three paths in the same argv.
    assert "/cluster/a" in ex.calls[0]
    assert "/cluster/b" in ex.calls[0]
    assert "/cluster/c" in ex.calls[0]

    # And the parsed entries line up with input order.
    assert out[0] is not None and out[0].path == "/cluster/a"
    assert out[1] is not None and out[1].path == "/cluster/b"
    assert out[2] is not None and out[2].path == "/cluster/c"
    assert out[0].total_bytes == 100
    assert out[1].avail_bytes == 180


@pytest.mark.asyncio
async def test_fetch_df_multi_empty_input_no_call():
    ex = _OneShotExecutor("")
    out = await fetch_df_multi(ex, [])
    assert out == []
    assert ex.calls == []


@pytest.mark.asyncio
async def test_fetch_df_multi_runtime_error_returns_all_none():
    class _Failer(Executor):
        async def run(self, argv: list[str], timeout: float = 30.0) -> str:  # noqa: ARG002
            raise RuntimeError("rc=1")
    ex = _Failer()
    out = await fetch_df_multi(ex, [("a", "/x"), ("b", "/y")])
    assert out == [None, None]
