"""Tests for rohanboard.exec — Executor Protocol + LocalExecutor +
AsyncSSHExecutor stub + FakeLocalExecutor (collector-test fixture)."""
from __future__ import annotations

import asyncio
import os
from typing import Sequence

import pytest

from rohanboard.exec import (
    AsyncSSHExecutor,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RUN_TIMEOUT,
    Executor,
    LocalExecutor,
)


# ──────────────────────────────────────────────────────────────────────────
# FakeLocalExecutor — in-memory canned-response Executor for collector tests
# ──────────────────────────────────────────────────────────────────────────

class FakeLocalExecutor:
    """Map argv tuples to canned `(rc, stdout, stderr)` responses.  Used in
    collector tests (Phase 4c+) to drive `slurm.fetch_*` / `storage.fetch_*`
    without spawning subprocesses or hitting a cluster.

    Example:
        fake = FakeLocalExecutor({
            ("squeue", "-h", "-O", ...): (0, squeue_text, ""),
            ("scontrol", "show", "node", "--all"): (0, scontrol_text, ""),
        })
        jobs = await slurm.fetch_jobs(fake, ["self"])
    """

    def __init__(
        self,
        canned: dict[tuple[str, ...], tuple[int, str, str]] | None = None,
        default: tuple[int, str, str] = (0, "", ""),
    ) -> None:
        self._canned = canned or {}
        self._default = default
        self.calls: list[tuple[str, ...]] = []

    async def run(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        key = tuple(argv)
        self.calls.append(key)
        return self._canned.get(key, self._default)

    async def aclose(self) -> None:
        return None


# ──────────────────────────────────────────────────────────────────────────
# Protocol conformance
# ──────────────────────────────────────────────────────────────────────────

def test_local_executor_satisfies_protocol():
    assert isinstance(LocalExecutor(), Executor)


def test_async_ssh_executor_satisfies_protocol():
    assert isinstance(AsyncSSHExecutor(host="example.invalid"), Executor)


def test_fake_local_executor_satisfies_protocol():
    assert isinstance(FakeLocalExecutor(), Executor)


def test_module_defaults_present():
    # Sanity: constants we want callers to be able to import.
    assert DEFAULT_RUN_TIMEOUT > 0
    assert DEFAULT_CONNECT_TIMEOUT > 0


# ──────────────────────────────────────────────────────────────────────────
# LocalExecutor — happy path
# ──────────────────────────────────────────────────────────────────────────

async def test_local_run_echo():
    ex = LocalExecutor()
    rc, out, err = await ex.run(["echo", "hello"])
    assert rc == 0
    assert out.strip() == "hello"
    assert err == ""


async def test_local_run_nonzero_rc():
    """Non-zero rc is reported, NOT raised — matches storage.py's prior _run
    semantics (the more-general shape collectors expect).  slurm.py callers
    check rc themselves via _run_checked."""
    ex = LocalExecutor()
    # `false` always exits 1.
    rc, out, err = await ex.run(["false"])
    assert rc == 1


async def test_local_run_captures_stderr():
    ex = LocalExecutor()
    rc, out, err = await ex.run(["sh", "-c", "echo to-stderr 1>&2; exit 0"])
    assert rc == 0
    assert "to-stderr" in err


async def test_local_aclose_is_noop_and_idempotent():
    ex = LocalExecutor()
    await ex.aclose()
    await ex.aclose()  # idempotent


# ──────────────────────────────────────────────────────────────────────────
# LocalExecutor — timeout reaps the child cleanly
# ──────────────────────────────────────────────────────────────────────────

async def test_local_run_timeout_raises_and_reaps():
    ex = LocalExecutor()
    # `sleep 30` would normally block well past our 0.2 s timeout.
    with pytest.raises(asyncio.TimeoutError):
        await ex.run(["sleep", "30"], timeout=0.2)
    # Give the OS a tick to actually reap; pytest-asyncio's loop is fast,
    # but in CI a sleep(0) is sometimes not enough.
    await asyncio.sleep(0.05)
    # `pgrep -f 'sleep 30'` should find none of OUR children.  We can't
    # easily reach into the proc table without false positives, but we CAN
    # check that the `proc.wait()` reap completed by spawning another and
    # confirming the loop is healthy.
    rc, out, _ = await ex.run(["echo", "post-timeout"])
    assert rc == 0
    assert "post-timeout" in out


# ──────────────────────────────────────────────────────────────────────────
# LocalExecutor — cancellation kills the child
# ──────────────────────────────────────────────────────────────────────────

async def test_local_run_cancellation_kills_child():
    """If the calling task is cancelled mid-run, the child must be killed.
    This is the cancel-leak pattern from
    feedback_asyncio_subprocess_cancel_leaks.md — without catch-BaseException
    the OS subprocess outlives the python parent."""
    ex = LocalExecutor()

    # Run a sleep in a background task.  We'll cancel it from outside.
    # Use a `python -c` so we get a unique cmdline pattern that won't
    # collide with the user's other shells.
    sentinel_marker = f"rohanboard_test_marker_{os.getpid()}"
    payload = (
        f"import time, sys; sys.stdout.write('{sentinel_marker} ready\\n'); "
        f"sys.stdout.flush(); time.sleep(60)"
    )
    task = asyncio.create_task(
        ex.run(["python", "-c", payload], timeout=120)
    )
    # Give the child time to actually spawn before we cancel.
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # After cancellation, the child must be reaped.  Re-spawn an `echo` to
    # confirm the loop and the executor are still healthy.
    rc, _, _ = await ex.run(["echo", "ok"])
    assert rc == 0
    # And confirm the marker process didn't survive: pgrep returns rc=1
    # when nothing matches.
    rc_pgrep, out_pgrep, _ = await ex.run(["pgrep", "-fa", sentinel_marker])
    # rc=1 = no match (good); rc=0 + match line = bad (leaked).
    assert rc_pgrep == 1, f"orphan child survived cancellation: {out_pgrep!r}"


# ──────────────────────────────────────────────────────────────────────────
# FakeLocalExecutor — used by collector tests in 4c+
# ──────────────────────────────────────────────────────────────────────────

async def test_fake_executor_returns_canned():
    fake = FakeLocalExecutor(
        canned={
            ("echo", "hi"): (0, "hi\n", ""),
            ("false",):     (1, "", "err"),
        },
    )
    rc, out, err = await fake.run(["echo", "hi"])
    assert (rc, out, err) == (0, "hi\n", "")
    rc, out, err = await fake.run(["false"])
    assert (rc, out, err) == (1, "", "err")


async def test_fake_executor_unmapped_returns_default():
    fake = FakeLocalExecutor(default=(127, "", "command not found"))
    rc, out, err = await fake.run(["definitely-not-a-real-command"])
    assert (rc, out, err) == (127, "", "command not found")


async def test_fake_executor_records_calls():
    fake = FakeLocalExecutor()
    await fake.run(["a", "b"])
    await fake.run(["c"])
    assert fake.calls == [("a", "b"), ("c",)]


# ──────────────────────────────────────────────────────────────────────────
# AsyncSSHExecutor — stub tests (full wiring lands in 4e)
# ──────────────────────────────────────────────────────────────────────────

def test_async_ssh_executor_instantiation():
    ex = AsyncSSHExecutor(
        host="example.invalid",
        port=2222,
        username="me",
        connect_timeout=5.0,
    )
    assert ex.host == "example.invalid"
    assert ex.port == 2222
    assert ex.username == "me"
    assert ex.connect_timeout == 5.0
    assert ex._conn is None


async def test_async_ssh_executor_aclose_no_connect_is_noop():
    """`aclose()` on an instance that never opened a connection is a no-op
    and must not raise — matters because App.on_unmount calls aclose on
    every shutdown path including those where connect() never succeeded."""
    ex = AsyncSSHExecutor(host="example.invalid")
    await ex.aclose()
    await ex.aclose()  # idempotent
    assert ex._conn is None
