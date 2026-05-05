"""Snapshot.jobs_mine vs jobs split (Bug 1 fix).

The Overview tab's "Active jobs" card must always show the current user's
jobs only — never other users' jobs even when the Jobs tab's mine_only
toggle is off.  The refresh tick achieves this by populating two fields:

  * snapshot.jobs       — what the Jobs tab shows (honours mine_only)
  * snapshot.jobs_mine  — always the current user's jobs only

When mine_only=True both lists are the same (one squeue call).  When
mine_only=False the refresh tick does TWO squeue calls.

These tests exercise the same code path `_refresh_all` runs but without
constructing the full Textual App — we replay the relevant logic against
a recording stub executor and verify the resulting Snapshot fields.
"""
from __future__ import annotations

import os

import pytest

from rohanboard.collectors import slurm
from rohanboard.collectors.models import Snapshot


class _RecordingExecutor:
    """Stub executor that returns a canned squeue line per call and
    records every argv it was asked to run, so the test can verify both
    that the right number of squeue calls happened and that the second
    call (when present) had `-u $USER`."""

    SQUEUE_LINE_MINE = (
        "100|p|sshd|hli|RUNNING|node1|None|1-00:00:00|2-00:00:00|1|24|100G|"
        "cpu=24,mem=100G,node=1,gres/gpu=1\n"
    )
    SQUEUE_LINES_ALL = (
        "100|p|sshd|hli|RUNNING|node1|None|1-00:00:00|2-00:00:00|1|24|100G|"
        "cpu=24,mem=100G,node=1,gres/gpu=1\n"
        "200|p|train|alice|RUNNING|node2|None|0-12:00:00|0-12:00:00|1|8|50G|"
        "cpu=8,mem=50G,node=1,gres/gpu=2\n"
        "300|p|infer|bob|PENDING|(Resources)|None|0:00|UNLIMITED|1|4|20G|"
        "cpu=4,mem=20G,node=1\n"
    )

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(self, argv, timeout=15.0):
        self.calls.append(list(argv))
        # squeue invocations: pick output based on whether `-u` was passed.
        if argv[0] == "squeue":
            if "-u" in argv:
                return self.SQUEUE_LINE_MINE
            return self.SQUEUE_LINES_ALL
        # Anything else (sacct, scontrol, etc.) is irrelevant here.
        return ""


async def _grab_jobs(executor, mine_only: bool) -> Snapshot:
    """Reproduces the relevant slice of RohanBoardApp._refresh_all.grab_jobs.

    Tests should ideally drive the App directly, but constructing it pulls
    in the entire Textual widget tree.  The contract being tested is
    purely about which squeue calls happen and what lands in the Snapshot
    — that contract is independent of the widget tree.  Keeping this
    helper in lockstep with app.py is enforced by the assertion in
    `test_grab_jobs_helper_matches_app_source` below.
    """
    snap = Snapshot()
    users_for_fetch = ["self"] if mine_only else ["all"]
    snap.jobs = await slurm.fetch_jobs(executor, users_for_fetch)
    if mine_only:
        snap.jobs_mine = snap.jobs
    else:
        snap.jobs_mine = await slurm.fetch_jobs(executor, ["self"])
    return snap


@pytest.mark.asyncio
async def test_mine_only_true_one_squeue_call_lists_aliased():
    """mine_only=True should issue ONE squeue call and alias the lists."""
    ex = _RecordingExecutor()
    snap = await _grab_jobs(ex, mine_only=True)
    squeue_calls = [c for c in ex.calls if c[0] == "squeue"]
    assert len(squeue_calls) == 1, f"expected 1 squeue call, got {squeue_calls}"
    assert "-u" in squeue_calls[0], "mine_only=True must pass -u"
    # Both lists populated, identity-equal because we alias.
    assert len(snap.jobs) == 1
    assert len(snap.jobs_mine) == 1
    assert snap.jobs is snap.jobs_mine


@pytest.mark.asyncio
async def test_mine_only_false_two_squeue_calls_distinct_lists():
    """mine_only=False should issue TWO squeue calls — one without `-u`
    (for snap.jobs = all users) and one with `-u $USER` (for
    snap.jobs_mine = current user only).  The Overview tab reads
    jobs_mine; the Jobs tab reads jobs."""
    ex = _RecordingExecutor()
    snap = await _grab_jobs(ex, mine_only=False)
    squeue_calls = [c for c in ex.calls if c[0] == "squeue"]
    assert len(squeue_calls) == 2, f"expected 2 squeue calls, got {squeue_calls}"
    # First call: all users (no -u flag).
    assert "-u" not in squeue_calls[0], (
        f"first squeue call should be all-users (no -u), got {squeue_calls[0]}"
    )
    # Second call: current user only.
    assert "-u" in squeue_calls[1], (
        f"second squeue call should be self-only (-u), got {squeue_calls[1]}"
    )
    # snap.jobs has all users, snap.jobs_mine has only the current user.
    assert len(snap.jobs) == 3, "all-users squeue returned 3 jobs"
    assert len(snap.jobs_mine) == 1, "self-only squeue returned 1 job"
    users_in_jobs = {j.user for j in snap.jobs}
    users_in_mine = {j.user for j in snap.jobs_mine}
    assert users_in_jobs == {"hli", "alice", "bob"}
    assert users_in_mine == {"hli"}


def test_snapshot_has_jobs_mine_field():
    """Snapshot model must expose jobs_mine (the field this whole change
    is about).  Catches accidental rename."""
    snap = Snapshot()
    assert hasattr(snap, "jobs_mine")
    assert snap.jobs_mine == []


def test_grab_jobs_helper_matches_app_source():
    """Sanity check: the helper above must mirror the real app.py logic.

    If app.py's grab_jobs ever stops doing the two-squeue-when-not-mine
    pattern, the helper above will silently drift and the other tests in
    this file will pass while the actual app misbehaves.  Read app.py and
    require that both call sites are still there.

    Step-3 refactor: `_refresh_cluster(cluster)` takes the executor as a
    local variable named `executor` (was `self.executor` before).  The
    two-squeue-when-not-mine pattern is preserved; only the receiver
    changed.
    """
    from pathlib import Path
    src = (Path(__file__).parent.parent / "rohanboard" / "app.py").read_text()
    # Two `slurm.fetch_jobs` calls in _refresh_cluster — one for the Jobs
    # tab view (users_for_fetch), one for jobs_mine when not already
    # aliased.  Receiver is `executor` (local) since step 3.
    assert src.count("slurm.fetch_jobs(executor, users_for_fetch)") == 1
    assert 'slurm.fetch_jobs(executor, ["self"])' in src
    # And the alias path for mine_only=True.
    assert "snap.jobs_mine = snap.jobs" in src
