"""Overview-panel _relayout decisions (Bug 2 fix).

When the user has many active jobs (or, before the Bug 1 fix, all users
together had many), the Active Jobs card's natural height could exceed
the window height.  That used to flip the panel into "scroll" mode
(`OverviewPanel.scroll #main { layout: vertical; height: auto; }`),
collapsing the wide two-column grid into a single column and squeezing
the Home-usage card.

The fix caps the right column's "natural" demand at the window height —
CompactJobs has an internal VerticalScroll, so when the right column is
flex(1fr) the body just scrolls.  fit=True is preserved for wide mode
as long as the LEFT column (Totals + Home, both bounded heights) fits.

These tests drive `_relayout` directly without spinning up a Textual
App: we monkey-patch `_available_size` and `_populate` to capture the
decided layout tuple.
"""
from __future__ import annotations

from rohanboard.widgets.overview import OverviewPanel


def _decide_layout(w: int, h: int, job_count: int):
    """Run OverviewPanel._relayout with a stubbed window size and return
    the resulting (mode, util_side, show_ascii, target, ascii_budget, fit)
    tuple."""
    p = OverviewPanel.__new__(OverviewPanel)
    p._job_count = job_count
    p._last_layout = None
    p._available_size = lambda: (w, h)  # type: ignore[method-assign]
    captured: list = []
    p._populate = lambda: captured.append(p._last_layout)  # type: ignore[method-assign]
    p._relayout()
    return captured[-1] if captured else None


def test_wide_with_many_jobs_stays_two_column():
    """130 jobs at 170x50 — used to flip to scroll mode and collapse to
    single column.  Now should stay wide+fit so the 2-col grid renders."""
    layout = _decide_layout(w=170, h=50, job_count=130)
    mode, util_side, show_ascii, target, ascii_budget, fit = layout
    assert mode == "wide"
    assert fit is True, "wide mode must report fit=True so the 2-col grid CSS applies"


def test_wide_with_few_jobs_two_column_fit():
    """5 jobs at 170x50 — easy fit, should be wide+fit."""
    layout = _decide_layout(w=170, h=50, job_count=5)
    mode, _, _, _, _, fit = layout
    assert mode == "wide" and fit is True


def test_narrow_uses_single_column():
    """80-col window — narrow mode, single column intentionally."""
    layout = _decide_layout(w=80, h=50, job_count=130)
    mode, *_ = layout
    assert mode == "narrow"


def test_wide_but_too_short_falls_to_scroll():
    """170x15 — left col (Totals + Home) exceeds h.  Wide layout can't
    actually fit; we fall through to scroll mode, single column.  This is
    the rare edge case (terminal too short for the left stack)."""
    layout = _decide_layout(w=170, h=15, job_count=130)
    mode, _, _, _, _, fit = layout
    assert mode == "wide"
    assert fit is False, "left col can't fit window → must report fit=False"


def test_wide_util_panel_placement_with_many_jobs():
    """When right_nat is capped to h, the Util panel (which fills slack)
    should land on whichever side is shorter — left in this case, since
    capped right_nat ≈ h dwarfs left_nat ≈ 23."""
    layout = _decide_layout(w=170, h=50, job_count=130)
    _, util_side, *_ = layout
    assert util_side == "left", f"util should land on the shorter (left) side, got {util_side!r}"
