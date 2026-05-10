"""Phase 4d.2-E: pure-function decision tests for OverviewPanel layout.

The math used to live entangled in `_relayout`; pulling it out into
`decide_layout` makes the decision flow table-testable. These tests
exercise every branch in the user's verbatim pseudocode (Discord
message_id 1502963258083115120) without booting Textual.
"""
from __future__ import annotations

from rohanboard.widgets.overview import LayoutDecision, decide_layout


# ──────────────────────────────────────────────────────────────────────
# Geometry constants matching what OverviewPanel uses at runtime
# ──────────────────────────────────────────────────────────────────────

WIDE_MIN_WIDTH = 130
TOTALS_NATURAL = 15
HOME_NATURAL = 7
LEFT_NAT = TOTALS_NATURAL + 1 + HOME_NATURAL    # = 23
UTIL_MIN = 8
ASCII_MIN_TOTAL = 6


def _decide(*, w: int, h: int, jobs_nat: int) -> LayoutDecision:
    return decide_layout(
        w=w, h=h, jobs_nat=jobs_nat,
        util_min=UTIL_MIN, wide_min_width=WIDE_MIN_WIDTH,
        totals_natural=TOTALS_NATURAL, home_natural=HOME_NATURAL,
        ascii_min_total=ASCII_MIN_TOTAL,
    )


# ──────────────────────────────────────────────────────────────────────
# Wide-mode branches (the heart of Phase 4d.2-E)
# ──────────────────────────────────────────────────────────────────────


def test_wide_huge_h_few_jobs_util_on_right():
    """h=60, jobs_nat=5 → right_nat=5, target=23 (= left_nat),
    gap_right=18 ≥ 8 → util on RIGHT. ASCII fills the slack below."""
    d = _decide(w=200, h=60, jobs_nat=5)
    assert d.mode == "wide"
    assert d.util_side == "right"
    assert d.target == LEFT_NAT
    assert d.fit is True
    assert d.ascii_budget == 60 - LEFT_NAT
    assert d.show_ascii is True
    assert d.nodesummary_extra == 0
    assert d.home_extra == 0
    assert d.compact_jobs_flex is False


def test_wide_many_jobs_util_on_left():
    """h=60, jobs_nat=35 → right_nat=35, target=35 (right > left),
    gap_left=12 ≥ 8 → util on LEFT. The branch that the WRONG
    Phase 4d.2-C cap removed; restoring it is the headline of 4d.2-E."""
    d = _decide(w=200, h=60, jobs_nat=35)
    assert d.mode == "wide"
    assert d.util_side == "left"
    assert d.target == 35
    assert d.fit is True


def test_wide_no_util_left_taller_compact_flex():
    """h=23, jobs_nat=18 → right_nat=18, target=23, gap_right=5,
    gap_left=0. Both fail UTIL_MIN=8 → util hidden. left > right
    → compact_jobs flex-fills the right column (its body scrolls)."""
    d = _decide(w=200, h=23, jobs_nat=18)
    assert d.util_side is None
    assert d.compact_jobs_flex is True
    assert d.nodesummary_extra == 0
    assert d.home_extra == 0
    assert d.target == LEFT_NAT


def test_wide_no_util_right_taller_balance_grows_left():
    """h=30, jobs_nat=29 → right_nat=29, target=29, gap_right=0,
    gap_left=6. Both < UTIL_MIN=8 → util hidden. right > left → grow
    NodesSummary + HomeStorage to fill the 6-row gap (split half/half,
    home gets the odd one)."""
    d = _decide(w=200, h=30, jobs_nat=29)
    assert d.util_side is None
    assert d.compact_jobs_flex is False
    assert d.nodesummary_extra == 3       # 6 // 2
    assert d.home_extra == 3              # 6 - 3
    assert d.target == 29


def test_wide_h_smaller_than_target_scroll_mode():
    """h=10, jobs_nat=15 → right_nat=10 (capped by h), target=23.
    23 > 10 → fit=False, scroll mode. ASCII hidden."""
    d = _decide(w=200, h=10, jobs_nat=15)
    assert d.fit is False
    assert d.show_ascii is False


def test_wide_jobs_nat_capped_by_window_height():
    """h=20, jobs_nat=100 → right_nat=min(20,100)=20 (CAPPED by h, not
    by left_nat — that's the Phase 4d.2-E correction). target=23,
    gap_right=3 < 8, gap_left=0 < 8 → util hidden, left > right →
    compact_jobs flex. fit=23<=20=False → scroll."""
    d = _decide(w=200, h=20, jobs_nat=100)
    assert d.util_side is None
    assert d.compact_jobs_flex is True
    assert d.fit is False


def test_wide_jobs_nat_very_small_no_cap():
    """h=60, jobs_nat=3 → right_nat=min(60,3)=3 (NOT capped, jobs_nat
    is the binding floor). Same shape as the few-jobs case → util on
    right with a generous slack."""
    d = _decide(w=200, h=60, jobs_nat=3)
    assert d.util_side == "right"
    assert d.target == LEFT_NAT


def test_wide_right_equals_left_no_util_no_balance():
    """h=60, jobs_nat=23 (== left_nat) → right_nat=23, target=23,
    both gaps 0. Util hidden, no balance, no compact-jobs flex —
    the columns are already perfectly balanced."""
    d = _decide(w=200, h=60, jobs_nat=23)
    assert d.util_side is None
    assert d.nodesummary_extra == 0
    assert d.home_extra == 0
    assert d.compact_jobs_flex is False
    assert d.target == LEFT_NAT


# ──────────────────────────────────────────────────────────────────────
# Narrow-mode (single column)
# ──────────────────────────────────────────────────────────────────────


def test_narrow_mode_when_below_wide_min_width():
    """w<130 → narrow. target = totals(15) + 1 + home(7) + 1 + jobs(5) = 29."""
    d = _decide(w=80, h=60, jobs_nat=5)
    assert d.mode == "narrow"
    assert d.util_side is None
    assert d.target == 15 + 1 + 7 + 1 + 5
    assert d.fit is True
    # Plenty of slack → ASCII shows.
    assert d.show_ascii is True


def test_narrow_mode_below_60_cols_uses_compact_totals():
    """w<60 → totals collapses to 6 rows. target = 6+1+7+1+5 = 20."""
    d = _decide(w=50, h=60, jobs_nat=5)
    assert d.mode == "narrow"
    assert d.target == 6 + 1 + 7 + 1 + 5


# ──────────────────────────────────────────────────────────────────────
# ASCII budget gating
# ──────────────────────────────────────────────────────────────────────


def test_ascii_hides_when_budget_below_min():
    """h just barely larger than target → leftover budget < ASCII_MIN_TOTAL.
    No ASCII even though fit=True."""
    # target = 23 (left_nat). h = 25 → ascii_budget = 2 < 6.
    d = _decide(w=200, h=25, jobs_nat=5)
    assert d.fit is True
    assert d.ascii_budget == 2
    assert d.show_ascii is False


def test_ascii_shows_when_budget_meets_min():
    """h - target >= ASCII_MIN_TOTAL → ASCII gets to render."""
    d = _decide(w=200, h=29, jobs_nat=5)        # target=23, budget=6 == ASCII_MIN_TOTAL
    assert d.show_ascii is True
    assert d.ascii_budget == 6


# ──────────────────────────────────────────────────────────────────────
# Phase 4d.2-D regression: the WRONG cap that 4d.2-E undoes
# ──────────────────────────────────────────────────────────────────────


def test_wide_right_taller_than_left_does_NOT_cap_to_left_nat():
    """The Phase 4d.2-C cap was `right_nat = min(left_nat, jobs_nat)`,
    which clamped right_nat to ≤ 23 even when the user had a long jobs
    list. Phase 4d.2-E caps by WINDOW height instead, so right_nat is
    free to grow up to h. With jobs_nat=40 and h=60, right_nat must be
    40 (not 23) — otherwise we silently lose the "util on left" branch.
    """
    d = _decide(w=200, h=60, jobs_nat=40)
    assert d.target == 40, (
        "Phase 4d.2-E correction: right_nat = min(h, jobs_nat); h=60 jobs=40 → 40"
    )
    assert d.util_side == "left"        # gap_left = 40 - 23 = 17 ≥ 8
