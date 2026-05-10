"""Lightweight perf instrumentation.

Set the env var ``ROHANBOARD_PERF_LOG=/path/to/file`` (or just `1` for the
default `/tmp/rohanboard_perf.log`) to enable.  When unset, every helper
in this module is a near-zero-cost no-op.

CSV columns: ``t_unix, kind, name, ms, extra``
  * ``t_unix``    — seconds-since-epoch with ms precision
  * ``kind``      — short tag, e.g. "refresh", "fanout", "widget", "frame"
  * ``name``      — sub-label, e.g. "jobs", "NodesSummary"
  * ``ms``        — duration in milliseconds (float)
  * ``extra``     — free-form string (count, size, …); commas escaped

Usage:

    from .perf import perf_block, perf_log

    with perf_block("refresh", "all"):
        ...

    perf_log("fanout", widget_name, duration_ms, extra=str(n_rows))

The animation widget already has its own frame-time logger
(`ROHANBOARD_ANIM_LOG`) — they're independent so you can enable either.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

_ENV = os.environ.get("ROHANBOARD_PERF_LOG")
_PATH: Path | None = None
if _ENV:
    _PATH = Path(_ENV if _ENV not in ("1", "true", "yes") else "/tmp/rohanboard_perf.log")
    try:
        _PATH.write_text("t_unix,kind,name,ms,extra\n")
    except Exception:
        _PATH = None


def enable(path: "Path | str") -> Path:
    """Phase 4d.2-E: programmatically turn on perf logging from the
    `--debug` CLI flag, writing to `path`. Returns the resolved path so
    the caller can echo it. Existing `ROHANBOARD_PERF_LOG`-driven init
    above still works; this is the lighter path the CLI uses when the
    user just wants `rohanboard --debug`.
    """
    global _PATH
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Header on each fresh enable so a tail-following user can
        # spot the column shape immediately.
        p.write_text("t_unix,kind,name,ms,extra\n")
    except Exception:
        pass
    _PATH = p
    return p


def perf_log(kind: str, name: str, ms: float, extra: str = "") -> None:
    if _PATH is None:
        return
    extra = extra.replace(",", ";").replace("\n", " ")
    try:
        with _PATH.open("a") as f:
            f.write(f"{time.time():.3f},{kind},{name},{ms:.2f},{extra}\n")
    except Exception:
        pass


@contextmanager
def perf_block(kind: str, name: str, extra: str = ""):
    if _PATH is None:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        perf_log(kind, name, (time.perf_counter() - t0) * 1000, extra)


def perf_enabled() -> bool:
    return _PATH is not None
