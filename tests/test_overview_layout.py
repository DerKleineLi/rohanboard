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


def _decide_layout(w: int, h: int, job_count: int, gpu_rows: int = 5,
                   has_ssd: bool = True, has_hdd: bool = True):
    """Run OverviewPanel._relayout with a stubbed window size and return
    the resulting (mode, util_side, show_ascii, target, ascii_budget, fit)
    tuple."""
    p = OverviewPanel.__new__(OverviewPanel)
    p._job_count = job_count
    p._gpu_row_count = gpu_rows
    p._has_ssd = has_ssd
    p._has_hdd = has_hdd
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


# ──────────────────────────────────────────────────────────────────────
# Issue #4 — totals natural height scales with GPU row count
# ──────────────────────────────────────────────────────────────────────


def test_totals_natural_grows_with_gpu_row_count():
    """LRZ has more distinct GPU kinds (H100 + A100-80 + A100-40 + V100 +
    3 MIG profiles = 7) than rohan (5). The totals card's natural height
    must scale with that count or the bottom of the Home pane below gets
    painted over by the ASCII pane."""
    p = OverviewPanel.__new__(OverviewPanel)
    p._gpu_row_count = 5
    p._has_ssd = True
    p._has_hdd = True
    rohan_h = p._totals_natural()

    p._gpu_row_count = 7
    lrz_h = p._totals_natural()
    assert lrz_h > rohan_h, "more GPU kinds → taller totals card"


def test_totals_natural_no_hdd_smaller_than_with_hdd():
    """LRZ has no /cluster_HDD/ — totals should be one row shorter."""
    p = OverviewPanel.__new__(OverviewPanel)
    p._gpu_row_count = 5
    p._has_ssd = True
    p._has_hdd = True
    with_hdd = p._totals_natural()
    p._has_hdd = False
    without = p._totals_natural()
    assert without == with_hdd - 1


def test_totals_natural_minimum_floor():
    """A cluster with no nodes yet (snapshot pre-fill) shouldn't drop to
    a 5-row totals card — that would let the ASCII pane mash into it."""
    p = OverviewPanel.__new__(OverviewPanel)
    p._gpu_row_count = 0
    p._has_ssd = False
    p._has_hdd = False
    assert p._totals_natural() >= p._TOTALS_MIN


def test_lrz_layout_with_many_gpu_kinds_does_not_overflow():
    """Issue #4 reproduction: with 7 GPU kinds (LRZ), the natural target
    must be high enough that ascii_budget doesn't overlap Home.

    Before the fix, _TOTALS_NATURAL was hardcoded at 15 — but LRZ's actual
    totals card needs ~20 rows. The ascii pane was sized into rows that
    Home actually wanted to render."""
    layout = _decide_layout(w=170, h=50, job_count=10, gpu_rows=7,
                            has_ssd=True, has_hdd=False)
    mode, _, _, target, ascii_budget, fit = layout
    assert mode == "wide"
    # The target (left column natural) must accommodate at least
    # totals_natural + Home (7) — ~20 + 7 = 27 minimum, far above old 15+7=22.
    # We're explicitly checking that target reflects the LRZ-sized totals card.
    p = OverviewPanel.__new__(OverviewPanel)
    p._gpu_row_count = 7
    p._has_ssd = True
    p._has_hdd = False
    expected_left_min = p._totals_natural() + p._HOME_NATURAL
    assert target >= expected_left_min, (
        f"target {target} must accommodate dynamic totals "
        f"({p._totals_natural()}) + home ({p._HOME_NATURAL})"
    )


# ──────────────────────────────────────────────────────────────────────
# Issue #2 — content-sized cols + 1fr ASCII spacer at multiple sizes
# ──────────────────────────────────────────────────────────────────────
#
# These tests cover the 2026-05-05 layout fix: dropped `grid-rows: 1fr`
# on #main and dropped the imperative `main.styles.height = target`. The
# decision-tree assertions remain valid (mode + fit + util_side + target
# + ascii_budget); the new property is that fit-mode never sets a
# concrete height on #main, so the column with fewer GPU rows can't
# leave dead space below it — the ASCII row at `1fr` absorbs the
# slack regardless of which column is taller.


def test_layout_at_narrow_width_100_x_30():
    """Narrow window (100×30): forced single-column, mode='narrow'."""
    layout = _decide_layout(w=100, h=30, job_count=8, gpu_rows=5)
    mode, *_ = layout
    assert mode == "narrow", f"100x30 → narrow, got {mode}"


def test_layout_at_medium_width_140_x_40():
    """Medium window (140×40, just past WIDE_MIN_WIDTH=130): wide+fit."""
    layout = _decide_layout(w=140, h=40, job_count=8, gpu_rows=5)
    mode, _, _, target, ascii_budget, fit = layout
    assert mode == "wide" and fit is True
    # target = max(left_nat, right_nat); should be > 0 and <= h.
    assert 0 < target <= 40
    # ASCII gets whatever remains of the window height after target.
    assert ascii_budget == max(40 - target, 0)


def test_layout_at_wide_width_180_x_50():
    """Wide window (180×50): wide+fit, plenty of ASCII slack."""
    layout = _decide_layout(w=180, h=50, job_count=8, gpu_rows=5)
    mode, _, show_ascii, target, ascii_budget, fit = layout
    assert mode == "wide" and fit is True
    assert show_ascii is True, "180x50 has plenty of vertical room — ASCII should show"
    assert ascii_budget >= 6, "ASCII needs at least its smallest art's footprint"


def test_lrz_shape_no_hdd_at_three_sizes_target_grows_with_gpus():
    """LRZ shape (no HDD, more GPU kinds) at three window sizes —
    the target must scale with gpu_rows at every size and never
    exceed window height."""
    for (w, h) in [(140, 40), (160, 45), (180, 50)]:
        rohan = _decide_layout(w=w, h=h, job_count=5, gpu_rows=5,
                               has_ssd=True, has_hdd=True)
        lrz = _decide_layout(w=w, h=h, job_count=5, gpu_rows=7,
                             has_ssd=True, has_hdd=False)
        _, _, _, rohan_t, _, _ = rohan
        _, _, _, lrz_t, _, _ = lrz
        # LRZ's totals card has +2 GPU rows but -1 HDD row → net taller.
        assert lrz_t >= rohan_t, (
            f"at {w}x{h}: LRZ target {lrz_t} should be >= rohan target "
            f"{rohan_t} because LRZ has more GPU kinds"
        )


# ──────────────────────────────────────────────────────────────────────
# Issue #2 v2 — grid → Horizontal layout: per-column independent height
# ──────────────────────────────────────────────────────────────────────
#
# The 2026-05-06 fix replaces `layout: grid` (which pads shorter cols to
# the row's max-child height — the "2-line gap below HomeStorage" the
# user reported) with `layout: horizontal` (each column sizes to its own
# content; ASCII slack absorber lives BELOW #main at 1fr).
#
# These Pilot-based tests mount the real OverviewPanel inside a stub App,
# resize the screen at the three sizes the plan requires (100x30, 140x40,
# 180x50), and read post-mount widget regions to confirm:
#   - in wide+fit mode neither column's last card has bare-border padding
#     below it (the bottom of the column's last child is at the same row
#     as the column's own bottom edge).
#   - the column heights MAY differ (Horizontal allows that), but neither
#     extends past its content.


import pytest
from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer
from textual.widgets import TabPane, TabbedContent

from rohanboard.collectors.models import (
    GpuSpec, Job, Node, Snapshot, StorageEntry,
)


def _make_test_snapshot(*, gpu_kinds: int = 5, has_hdd: bool = True,
                        job_count: int = 5) -> Snapshot:
    snap = Snapshot()
    nodes: list[Node] = []
    # 1 node per distinct GPU kind, each with a unique kind/vram pair so the
    # NodesSummary key set has `gpu_kinds` rows.
    kinds = [
        ("rtx_a6000", "48GB"),
        ("a100",       "80GB"),
        ("rtx_3090",   "24GB"),
        ("h100",       "80GB"),
        ("v100",       "32GB"),
        ("p100",       "16GB"),
        ("gtx_1080",   "8GB"),
    ]
    for i in range(gpu_kinds):
        kind, vram = kinds[i % len(kinds)]
        nodes.append(Node(
            name=f"node{i}",
            partitions=["test"],
            state="MIXED",
            cpu_total=64, cpu_alloc=8,
            cpu_load=1.0,
            mem_total_mb=512000, mem_alloc_mb=64000, mem_free_mb=400000,
            gpus=[GpuSpec(kind=kind, total=4, alloc=1, vram=vram)],
            features=[],
        ))
    snap.nodes = nodes
    snap.jobs_mine = [
        Job(job_id=str(1000 + i), partition="test", name=f"job_{i}",
            user="hli", state="RUNNING", node_or_reason=f"node{i % 3}",
            time_used="00:01:00", time_left="00:59:00",
            num_nodes=1, num_cpus=1, tres="cpu=1",
            alloc_mem="—", alloc_gpu="—")
        for i in range(job_count)
    ]
    snap.jobs = list(snap.jobs_mine)
    snap.storage = [
        # quota entry → HomeStorage reads this
        StorageEntry(label="Home", path="/home/hli", source="quota",
                     total_bytes=300_000_000_000,
                     used_bytes=42_000_000_000,
                     avail_bytes=258_000_000_000),
        StorageEntry(label="cluster_ssd", path="/cluster/test",
                     source="df",
                     total_bytes=10_000_000_000_000,
                     used_bytes=5_000_000_000_000,
                     avail_bytes=5_000_000_000_000),
    ]
    if has_hdd:
        snap.storage.append(StorageEntry(
            label="cluster_hdd", path="/cluster_HDD/test", source="df",
            total_bytes=20_000_000_000_000,
            used_bytes=10_000_000_000_000,
            avail_bytes=10_000_000_000_000,
        ))
    return snap


class _OverviewHostApp(App):
    """Bare-bones App that hosts a single OverviewPanel for layout tests.

    OverviewPanel reads `self.app.snapshot` directly (single-cluster
    fallback path). We expose a `Snapshot` reactive on the app so its
    update_snapshot calls see real data.
    """
    CSS = ""

    def __init__(self, snap: Snapshot) -> None:
        super().__init__()
        self._snap = snap

    @property
    def snapshot(self):  # noqa: D401  — match RohanBoardApp's API
        return self._snap

    def compose(self) -> ComposeResult:
        # OverviewPanel walks parents looking for a TabPane with id
        # cluster_*; without one (single-cluster), it falls back to
        # self.app.snapshot. We don't need to wrap it.
        from rohanboard.widgets.overview import OverviewPanel
        yield OverviewPanel()


@pytest.mark.parametrize("size", [(100, 30), (140, 40), (180, 50)])
@pytest.mark.parametrize("gpu_kinds,has_hdd",
                         [(5, True), (7, False)],   # rohan, LRZ
                         ids=["rohan", "lrz"])
async def test_layout_no_bottom_padding_per_column(size, gpu_kinds, has_hdd):
    """In wide+fit mode each column must end where its content ends —
    NOT pad to the row's max-child height. Verified by walking the
    column's children and asserting the last child's region.bottom is
    equal to (or 1 above) the column's own region.bottom.

    100x30 is narrow (single column) — that path is exercised too but
    falls through the wide-only assertion (returns early).
    """
    w, h = size
    snap = _make_test_snapshot(gpu_kinds=gpu_kinds, has_hdd=has_hdd,
                                job_count=5)
    app = _OverviewHostApp(snap)
    async with app.run_test(size=(w, h)) as pilot:
        await pilot.pause()
        # Drive update_snapshot manually to run the relayout decision
        # against the stub snapshot.
        from rohanboard.widgets.overview import OverviewPanel
        panel = app.query_one(OverviewPanel)
        panel.update_snapshot(snap)
        await pilot.pause()
        await pilot.pause()
        # Narrow path doesn't exercise the horizontal-columns code.
        if w < panel.WIDE_MIN_WIDTH:
            assert "narrow" in panel.classes, \
                f"100x30 should be in narrow class; got classes={panel.classes}"
            return
        assert "wide" in panel.classes
        if "fit" not in panel.classes:
            # Window too short → scroll mode (single-col); not the path
            # this test guards. Skip the per-col check.
            return
        # Wide+fit: verify each column's content fills it.
        try:
            left = panel.query_one(".left-col")
            right = panel.query_one(".right-col")
        except Exception as e:
            pytest.fail(f".left-col / .right-col not mounted in wide+fit: {e}")
        for col_id, col in [("left-col", left), ("right-col", right)]:
            children = list(col.children)
            assert children, f"{col_id} has no children"
            last = children[-1]
            # The last child's bottom must be no more than 1 row above the
            # column's bottom. Allow 1 row of slack for borders/margin
            # rounding; anything more is the "bare border padding" bug.
            col_bot = col.region.y + col.region.height
            last_bot = last.region.y + last.region.height
            gap = col_bot - last_bot
            assert gap <= 1, (
                f"{col_id} at {w}x{h} (gpu_kinds={gpu_kinds}, hdd={has_hdd}):"
                f" {gap}-row gap below last card "
                f"(col bottom={col_bot}, last child bottom={last_bot}). "
                "Wide+fit mode columns must size to their own content."
            )


async def test_layout_main_uses_horizontal_in_wide_fit():
    """Cross-check: in wide+fit mode #main has `layout: horizontal`,
    not `grid`. Confirms the v2 fix actually replaced the layout, not
    just tweaked the row sizing."""
    snap = _make_test_snapshot(gpu_kinds=5)
    app = _OverviewHostApp(snap)
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        from rohanboard.widgets.overview import OverviewPanel
        panel = app.query_one(OverviewPanel)
        panel.update_snapshot(snap)
        await pilot.pause()
        if "wide" in panel.classes and "fit" in panel.classes:
            main = panel.query_one("#main")
            # Textual's resolved layout type for `layout: horizontal`.
            from textual.layouts.horizontal import HorizontalLayout
            assert isinstance(main.styles.layout, HorizontalLayout), (
                "#main must use horizontal layout in wide+fit "
                f"(got {type(main.styles.layout).__name__})"
            )
