"""Regression tests for `_sort_value` on non-numeric job IDs.

Phase 4d.2-E step F.2: LRZ Recent + mine_only=False contained sacct
rows with array-task and het-job suffixes ("1234_5", "1234+0",
"1234.batch"). `int(job.job_id)` raised ValueError; the swallow path
fell through to `return job.job_id` (a str). Mixing int and str
return values inside the same `jobs.sort(...)` call raised
`TypeError: '<' not supported between instances of 'str' and 'int'`
— which the broadcast loop swallowed, so the table silently stopped
updating.

These tests pin the new tuple-shape return so a future commit can't
re-introduce the mixed-type sort.
"""
from __future__ import annotations

from rohanboard.collectors.models import Job
from rohanboard.widgets.jobs_table import _sort_value


def _j(jid: str) -> Job:
    return Job(
        job_id=jid,
        partition="x",
        name="x",
        user="x",
        state="RUNNING",
        node_or_reason="x",
        time_used="0:01",
        time_left="N/A",
        num_nodes=1,
        num_cpus=1,
    )


def test_sort_value_job_id_numeric_returns_tuple():
    """Pure numeric IDs return (int_prefix, full_str) so they compare
    cleanly against array-task tuples."""
    v = _sort_value(_j("1234"), "job_id")
    assert isinstance(v, tuple)
    assert v[0] == 1234
    assert v[1] == "1234"


def test_sort_value_job_id_array_task_returns_tuple():
    """Array tasks like `1234_5` parse the leading prefix and keep the
    full string so the array index breaks ties within a job."""
    v = _sort_value(_j("1234_5"), "job_id")
    assert isinstance(v, tuple)
    assert v[0] == 1234
    assert v[1] == "1234_5"


def test_sort_value_job_id_step_id_returns_tuple():
    """Step IDs like `1234.batch` follow the same prefix rule."""
    v = _sort_value(_j("1234.batch"), "job_id")
    assert isinstance(v, tuple)
    assert v[0] == 1234
    assert v[1] == "1234.batch"


def test_sort_value_job_id_hetjob_returns_tuple():
    """Het-job components like `1234+0`."""
    v = _sort_value(_j("1234+0"), "job_id")
    assert isinstance(v, tuple)
    assert v[0] == 1234
    assert v[1] == "1234+0"


def test_sort_value_job_id_empty_string_returns_tuple_with_neg_one():
    """Empty / non-parseable IDs deprioritize via -1, NEVER raise.
    Critical: this must not fall back to returning the raw string
    (the original bug)."""
    v = _sort_value(_j(""), "job_id")
    assert isinstance(v, tuple)
    assert v[0] == -1
    assert v[1] == ""


def test_sort_value_job_id_mixed_collection_sorts_without_typeerror():
    """The regression: a list mixing numeric, array, step, and het IDs
    must `sort()` without raising TypeError. Pin this so the silent
    swallow at the broadcast layer can't hide it again."""
    jobs = [
        _j("2745001"),
        _j("2745000_5"),     # array task
        _j("2744999.batch"), # step id
        _j("2744998+1"),     # het component
        _j("2744997"),
    ]
    # Must not raise:
    jobs.sort(key=lambda j: _sort_value(j, "job_id"), reverse=True)
    assert [j.job_id for j in jobs] == [
        "2745001",
        "2745000_5",
        "2744999.batch",
        "2744998+1",
        "2744997",
    ]


def test_sort_value_outer_fallback_does_not_return_raw_string():
    """The OUTER except-fallback in _sort_value used to `return
    job.job_id` (raw string) when any branch raised. That's what
    caused the original mixed-type sort. The new shape: the only
    branch that USED to raise (job_id parsing) now never raises, so
    the outer fallback can't be the source of a string-vs-int mix.

    Defense in depth: even if an unknown col name slips through and
    triggers the outer fallback, we shouldn't break sort by returning
    a raw int (e.g. cpu's `job.num_cpus`) vs a raw str (job_id).
    Today the fallback returns `job.job_id` (a str) — fine for sort
    consistency WITHIN an unknown-col sort, but a latent landmine.
    This test documents the current shape; promote to a stricter
    contract when we tighten unknown-col handling.
    """
    # Unknown column → outer fallback → returns the raw string job_id.
    v = _sort_value(_j("1234"), "unknown_col")
    assert isinstance(v, str)   # documenting current shape, not a target.
