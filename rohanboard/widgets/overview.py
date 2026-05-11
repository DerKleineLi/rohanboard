"""Overview tab — simple responsive layout.

Two modes, chosen by width:
  narrow (< WIDE_MIN_WIDTH):  single column — Totals, Home, Jobs stacked;
                              jobs pane scrolls internally if overflow;
                              ASCII appears below if there is slack.
  wide:                       left col = Totals + Home,
                              right col = Jobs (max height = window; scrolls).
                              If the height slack can fit a Utilization panel,
                              it is added to the left col; otherwise the Home
                              card stretches to match the right col.
                              ASCII appears below if there is still slack.

No imperative balancer, no grow_to, no finalize pass — the single `.flex`
card per column uses `height: 1fr` inside a grid row of `1fr`, so Textual's
layout engine does the balancing for free.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from ..collectors.models import Job, Snapshot
from ..perf import perf_block
from .animations import DoomFire, MatrixRain
from .format import fat_bytes
from .nodes_table import NodesSummary
from .storage_panel import UsageBar
from .utilization_panel import UtilizationPanel


# ────────────────────────────────────────────────────────────────────────
# Cards
# ────────────────────────────────────────────────────────────────────────

class HomeStorage(Widget):
    DEFAULT_CSS = """
    HomeStorage {
        height: auto;
        min-height: 4;
        padding: 1 2;
        border: round $primary;
    }
    HomeStorage.flex { height: 1fr; }
    HomeStorage > .title { height: 1; text-style: bold; }
    HomeStorage > .usage { height: 1; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        yield Static("Home", classes="title")
        yield UsageBar(0.0)
        yield Static("[dim italic]loading…[/dim italic]", classes="usage", id="usage")

    def update_snapshot(self, snap: Snapshot) -> None:
        # Resolve the "home" entry. Preference order:
        #   1. label "home" (matches what the user wrote in TOML, regardless
        #      of source — quota OR df)
        #   2. any source=="quota" entry (legacy rohan default config)
        #   3. first storage entry, if any
        # The widget previously matched only `source == "quota"` and showed
        # "no quota info yet" forever when the config used `kind = "df"`
        # (the v2-Phase-4c default for ssh:rohan, which avoids depending on
        # the `quota` binary on the remote).
        home = next((e for e in snap.storage if e.label == "home"), None)
        if home is None:
            home = next((e for e in snap.storage if e.source == "quota"), None)
        if home is None and snap.storage:
            home = snap.storage[0]
        usage_line = self.query_one("#usage", Static)
        bar = self.query_one(UsageBar)
        if home is None:
            usage_line.update("[dim italic]no storage info yet[/dim italic]")
            return
        bar.update_fraction(home.fraction)
        text = fat_bytes(home.free_bytes, home.used_bytes, home.total_bytes)
        # Suffix only on quota entries — they have a soft/hard split that's
        # worth surfacing. df entries just show free/used/total bytes.
        if home.source == "quota":
            text.append("  (soft quota)", style="dim")
        usage_line.update(text)


class CompactJobs(Widget):
    DEFAULT_CSS = """
    CompactJobs {
        height: auto;
        min-height: 5;
        padding: 1 2;
        border: round $primary;
    }
    CompactJobs.flex { height: 1fr; }
    CompactJobs > .title { height: 1; text-style: bold; }
    CompactJobs > #body_scroll {
        height: auto;
        scrollbar-size-vertical: 1;
    }
    /* only stretch/scroll the body when the whole card is flex. */
    CompactJobs.flex > #body_scroll { height: 1fr; min-height: 1; }
    CompactJobs > #body_scroll > .body { height: auto; }
    """

    def compose(self) -> ComposeResult:
        yield Static("Active jobs", classes="title", id="title")
        with VerticalScroll(id="body_scroll"):
            yield Static("", classes="body", id="body")

    def update_snapshot(self, snap: Snapshot) -> None:
        body = self.query_one("#body", Static)
        # Bundle-2 B2.3: Overview is "your stuff at a glance." CompactJobs
        # UNCONDITIONALLY filters to `cluster_user` regardless of the
        # global `app.mine_only` toggle. JobsTable's Active+Recent
        # behavior is unchanged — the toggle still drives those — but
        # the Overview card is always the user's own jobs.
        # Cold-start (cluster_user == "") → no jobs match the filter
        # AND `first_tick_done` is False → render "loading…" below.
        jobs = snap.jobs
        if snap.cluster_user:
            jobs = [j for j in jobs if j.user == snap.cluster_user]
        else:
            jobs = []
        title = "Active jobs"
        if snap.cluster_user:
            title += f"  [dim]({len(jobs)} · {snap.cluster_user})[/dim]"
        else:
            title += f"  [dim](…)[/dim]"
        self.query_one("#title", Static).update(title)
        if not jobs:
            # Bundle-1 Sub-fix-4: discriminate "still fetching" from
            # "fetched, no rows". `first_tick_done` is False until the
            # first successful refresh tick lands on `_refresh_all`; on
            # cold start (especially LRZ ProxyJump ~10 s) the user
            # should see "loading…" rather than a confident "no active
            # jobs" that turns into rows seconds later. Cold-start also
            # implies cluster_user == "" → "loading…" covers both
            # "haven't fetched yet" and "haven't resolved whoami yet".
            if not getattr(snap, "first_tick_done", True) or not snap.cluster_user:
                body.update("[dim italic]loading…[/dim italic]")
            else:
                body.update("[dim italic]no active jobs[/dim italic]")
            return
        rows = [_compact_row(j) for j in jobs]
        body.update(Text("\n").join(rows))


def _compact_row(j: Job) -> Text:
    name = j.name[:22]
    row = Text()
    if j.state == "RUNNING":
        row.append("● ", style="bold green")
        row.append(f"{name:<22}  ")
        row.append(f"{j.time_used:>10}")
        row.append(f"  @ {j.node_or_reason}", style="dim")
    elif j.state == "PENDING":
        row.append("○ ", style="bold yellow")
        row.append(f"{name:<22}  ")
        row.append("pending  ", style="yellow")
        row.append(j.node_or_reason, style="dim")
    else:
        row.append("· ", style="dim")
        row.append(f"{name:<22}  ")
        row.append(j.state, style="red")
    return row


# ────────────────────────────────────────────────────────────────────────
# ASCII art
# ────────────────────────────────────────────────────────────────────────

_ART_XL = (
    "      ╔══════════════════════════════════════════════╗      \n"
    "      ║  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐    ║      \n"
    "      ║  │●●│  │●●│  │●●│  │●●│  │●●│  │●●│  │●●│    ║      \n"
    "      ║  └──┘  └──┘  └──┘  └──┘  └──┘  └──┘  └──┘    ║      \n"
    "      ║  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐    ║      \n"
    "      ║  │●●│  │●●│  │●●│  │●●│  │●●│  │●●│  │●●│    ║      \n"
    "      ║  └──┘  └──┘  └──┘  └──┘  └──┘  └──┘  └──┘    ║      \n"
    "      ║  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐    ║      \n"
    "      ║  │●●│  │●●│  │●●│  │●●│  │●●│  │●●│  │●●│    ║      \n"
    "      ║  └──┘  └──┘  └──┘  └──┘  └──┘  └──┘  └──┘    ║      \n"
    "      ║      r  o  h  a  n     c  l  u  s  t  e  r    ║      \n"
    "      ╚══════════════════════════════════════════════╝      "
)

_ART_L = (
    "   ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐     \n"
    "   │●●│  │●●│  │●●│  │●●│  │●●│  │●●│     \n"
    "   └──┘  └──┘  └──┘  └──┘  └──┘  └──┘     \n"
    "   ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐  ┌──┐     \n"
    "   │●●│  │●●│  │●●│  │●●│  │●●│  │●●│     \n"
    "   └──┘  └──┘  └──┘  └──┘  └──┘  └──┘     \n"
    "      r  o  h  a  n     c l u s t e r     "
)

_ART_M = (
    "  ┌──┐ ┌──┐ ┌──┐ ┌──┐ \n"
    "  │●●│ │●●│ │●●│ │●●│ \n"
    "  └──┘ └──┘ └──┘ └──┘ \n"
    "     rohan cluster    "
)

_ART_S = (
    " ┌── rohan ──┐ \n"
    " │  online ● │ \n"
    " └───────────┘ "
)

# (min_width, height_rows, art)  — height_rows is art lines, excl. border/caption
_ARTS = [
    (60, 12, _ART_XL),
    (50, 7,  _ART_L),
    (26, 4,  _ART_M),
    (14, 3,  _ART_S),
]

# Smallest art's total card footprint (art + caption + border).
ASCII_MIN_TOTAL = _ARTS[-1][1] + 1 + 2   # 3 + 1 + 2 = 6


class AsciiArt(Widget):
    """Decorative, size-adaptive. Picks the largest art that fits both the
    current width and an optional max-height budget set by the caller."""

    DEFAULT_CSS = """
    AsciiArt {
        height: 100%;
        min-height: 3;
        padding: 0 2;
        border: round $primary-darken-2;
        color: $accent;
        content-align: center middle;
    }
    AsciiArt > #art    { height: auto; width: 100%; text-align: center; }
    AsciiArt > #caption { height: 1;    width: 100%; text-align: center; color: $text-muted; }
    """

    def __init__(self, max_height: int | None = None) -> None:
        super().__init__()
        # Total card footprint budget (art lines + 1 caption + 2 border).
        self._max_height = max_height

    def compose(self) -> ComposeResult:
        yield Static("", id="art")
        yield Static("", id="caption")

    def on_mount(self) -> None:
        self._pick_art()
        self._update_caption()
        self.set_interval(10, self._update_caption)

    def on_resize(self, event=None) -> None:
        self._pick_art()

    def _pick_art(self) -> None:
        w = self.size.width
        budget = self._max_height if self._max_height is not None else 999
        chosen = _ART_S
        for min_w, art_lines, art in _ARTS:
            footprint = art_lines + 1 + 2   # caption + top/bot border
            if w >= min_w and footprint <= budget:
                chosen = art
                break
        try:
            self.query_one("#art", Static).update(chosen)
        except Exception:
            pass

    def _update_caption(self) -> None:
        try:
            caption = self.query_one("#caption", Static)
        except Exception:
            return
        caption.update(f"{_dt.datetime.now():%a %d %b · %H:%M}")


# ────────────────────────────────────────────────────────────────────────
# OverviewPanel — pure layout decision
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LayoutDecision:
    """Pure-function output of `decide_layout`. The OverviewPanel reads
    this and applies it imperatively (mounting cards, setting heights);
    keeping the math out of the widget makes it table-testable.

    Phase 4d.2-E spec (verbatim from user, message_id 1502963258083115120):

        right_natural = min(height, compactjobs)         # cap by WINDOW HEIGHT
        target        = max(left_natural, right_natural)
        gap_right     = target - right_natural
        gap_left      = target - left_natural

        if gap_right >= UTIL_MIN: util on right (h = gap_right)
        elif gap_left >= UTIL_MIN: util on left  (h = gap_left)
        else:
            util hidden
            if left_nat > right_nat:  compact_jobs.h = target
            elif right_nat > left_nat:
                add_n = (right_nat - left_nat) // 2
                add_h =  right_nat - left_nat - add_n
                nodesummary.h += add_n
                home.h        += add_h
    """
    mode: str                        # "narrow" or "wide"
    util_side: str | None            # "left", "right", or None
    target: int                      # main grid height in wide mode (rows)
    fit: bool
    ascii_budget: int
    show_ascii: bool
    nodesummary_extra: int = 0       # rows added beyond _TOTALS_NATURAL (right>left no-util)
    home_extra: int = 0              # rows added beyond _HOME_NATURAL (right>left no-util)
    compact_jobs_flex: bool = False  # CompactJobs flex-fills the right column (left>right no-util)


def decide_layout(
    *,
    w: int,
    h: int,
    jobs_nat: int,
    util_min: int,
    wide_min_width: int,
    totals_natural: int,
    home_natural: int,
    ascii_min_total: int,
) -> LayoutDecision:
    """Pure decision function — no Textual access. See `LayoutDecision`
    docstring for the spec."""
    if w < wide_min_width:
        # Narrow: single column, all natural; parent scroll handles overflow.
        # NodesSummary collapses to a 6-row compact form below ~60 cols.
        totals_h = 6 if w < 60 else totals_natural
        target = totals_h + 1 + home_natural + 1 + jobs_nat
        fit = target <= h
        ascii_budget = max(h - target, 0) if fit else 0
        show_ascii = fit and ascii_budget >= ascii_min_total
        return LayoutDecision(
            mode="narrow", util_side=None, target=target, fit=fit,
            ascii_budget=ascii_budget, show_ascii=show_ascii,
        )

    # Wide.
    left_nat = totals_natural + 1 + home_natural
    right_nat = min(h, jobs_nat)        # CAP BY WINDOW HEIGHT, NOT left_nat
    target = max(left_nat, right_nat)
    gap_right = target - right_nat
    gap_left = target - left_nat

    util_side: str | None = None
    nodes_extra = 0
    home_extra = 0
    compact_flex = False

    if gap_right >= util_min:
        util_side = "right"
    elif gap_left >= util_min:
        util_side = "left"
    else:
        # Util doesn't fit. Distribute the slack so the columns balance.
        if left_nat > right_nat:
            # Right col is shorter by gap_right < util_min — stretch
            # CompactJobs (it has a VerticalScroll body so the cell fills
            # `target` and any overflow scrolls internally).
            compact_flex = True
        elif right_nat > left_nat:
            # Left col is shorter by gap_left < util_min — grow Totals
            # and Home to fill. Half/half-ish; remainder goes to home.
            diff = right_nat - left_nat
            nodes_extra = diff // 2
            home_extra = diff - nodes_extra
        # else: equal, no balance needed.

    fit = target <= h
    ascii_budget = max(h - target, 0) if fit else 0
    show_ascii = fit and ascii_budget >= ascii_min_total
    return LayoutDecision(
        mode="wide", util_side=util_side, target=target,
        fit=fit, ascii_budget=ascii_budget, show_ascii=show_ascii,
        nodesummary_extra=nodes_extra, home_extra=home_extra,
        compact_jobs_flex=compact_flex,
    )


# ────────────────────────────────────────────────────────────────────────
# OverviewPanel — simple 1/2-column layout
# ────────────────────────────────────────────────────────────────────────

class OverviewPanel(Widget):
    """Responsive Overview: 1 column when narrow, 2 columns when wide."""

    DEFAULT_CSS = """
    OverviewPanel {
        layout: vertical;
        height: 100%;
        padding: 0;
        margin: 0;
    }
    /* root scroll wrapper — provides a page scrollbar when content
       doesn't fit, otherwise invisible. */
    OverviewPanel > #root {
        height: 100%;
        scrollbar-size-vertical: 1;
    }
    /* common card styling */
    OverviewPanel .card { margin-bottom: 1; height: auto; }
    OverviewPanel .col > .card:last-of-type { margin-bottom: 0; }
    /* In single-column modes, cards already have border+margin from the
       widget itself (NodesSummary uses `margin: 1 2`); the per-card
       margin-bottom would double the gap. */
    OverviewPanel.narrow .card,
    OverviewPanel.scroll .card { margin-bottom: 0; }
    OverviewPanel #ascii_row.hidden { display: none; }

    /* ── WIDE + fit: 2-col grid, equal-height cols, one flex card each ── */
    OverviewPanel.wide.fit #main {
        layout: grid;
        grid-size: 2;
        grid-gutter: 0 1;
        grid-rows: 1fr;
        height: auto;          /* set imperatively to target rows */
        min-height: 0;
    }
    OverviewPanel.wide.fit #main > .col { layout: vertical; height: 100%; }
    OverviewPanel.wide.fit #main > .col > .card.flex { height: 1fr; }

    /* ── NARROW or scroll: stack at natural heights ─────────────────── */
    OverviewPanel.narrow #main, OverviewPanel.scroll #main {
        layout: vertical;
        height: auto;
    }

    /* ── fit mode ascii: fills vertical slack below main ────────────── */
    OverviewPanel.fit #ascii_row {
        height: 1fr;
        min-height: 0;
    }
    OverviewPanel.fit #ascii_row > AsciiArt { height: 100%; }

    /* ── scroll mode: cards just stack, no ascii (already hidden) ──── */
    OverviewPanel.scroll #ascii_row { display: none; }
    """

    # width threshold for switching between 1-col and 2-col
    WIDE_MIN_WIDTH = 130
    # rough natural heights used only for deciding whether util fits
    _TOTALS_NATURAL = 15
    _HOME_NATURAL = 7

    def __init__(self) -> None:
        super().__init__()
        self._job_count = 0
        # Phase 4d.2-E step A: split the prior monolithic `_last_layout`
        # cache key into two. `_last_layout_key` triggers `_populate`
        # (tear down + re-mount cards); `_last_height_key` triggers
        # `_apply_heights` (in-place `widget.styles.height = …`). Critical
        # for the user's `a` click latency — mine_only flips change
        # `_job_count` → jobs_natural → target/ascii_budget but NOT
        # mode/util_side, so they should NOT remount.
        self._last_layout_key: tuple | None = None
        self._last_height_key: tuple | None = None
        self._last_decision: LayoutDecision | None = None
        self._ascii_budget = 0

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="root"):
            yield Vertical(id="main")
            yield Vertical(id="ascii_row")

    def on_mount(self) -> None:
        self._relayout()
        self.call_after_refresh(self._push_snapshot)

    def on_resize(self, event=None) -> None:
        self._relayout()

    def update_snapshot(self, snap: Snapshot) -> None:
        # Bundle-2 B2.5 (sister-path to B2.3): CompactJobs's BODY is
        # locked to `cluster_user`-only jobs on Overview regardless of
        # `app.mine_only` (see CompactJobs.update_snapshot). The HEIGHT
        # path here must mirror that filter — pre-fix it still read
        # `app.mine_only` and on `mine_only=False` set `_job_count =
        # len(snap.jobs)` (all-users), so `_jobs_natural()` reserved
        # space for ~50 rows while only ~3 actually rendered → large
        # blank below the visible rows.
        #
        # Cold-start (cluster_user == "") → `_job_count = 0` → the
        # natural height is `4 + max(0, 1) = 5` lines, matching the
        # single-line "loading…" body CompactJobs draws while whoami
        # is still resolving.
        if snap.cluster_user:
            self._job_count = sum(
                1 for j in snap.jobs if j.user == snap.cluster_user
            )
        else:
            self._job_count = 0
        # re-layout may be needed if util fit-check flipped
        self._relayout()
        self._push_snapshot()

    # ── layout decision ──────────────────────────────────────

    def _available_size(self) -> tuple[int, int]:
        w = int(self.size.width or 0)
        h = int(self.size.height or 0)
        if w <= 0 or h <= 0:
            try:
                w = w or int(self.app.size.width)
                h = h or int(self.app.size.height)
            except Exception:
                pass
        return max(w, 1), max(h, 1)

    def _relayout(self) -> None:
        w, h = self._available_size()
        decision = decide_layout(
            w=w, h=h,
            jobs_nat=self._jobs_natural(),
            util_min=UtilizationPanel.MIN_HEIGHT,
            wide_min_width=self.WIDE_MIN_WIDTH,
            totals_natural=self._TOTALS_NATURAL,
            home_natural=self._HOME_NATURAL,
            ascii_min_total=ASCII_MIN_TOTAL,
        )
        # Phase 4d.2-E step A: split into two cache keys.
        # `layout_key` covers everything that determines which widgets
        # are mounted, in which slot, with which class. A change here
        # requires `_populate` (tear down + re-mount). `height_key`
        # covers the geometry numbers we can apply via in-place
        # `widget.styles.height = …` — no remount needed.
        layout_key = (
            decision.mode,
            decision.util_side,
            decision.show_ascii,
            decision.fit,
            decision.compact_jobs_flex,
        )
        height_key = (
            decision.target,
            min(decision.ascii_budget, 15),
            decision.nodesummary_extra,
            decision.home_extra,
        )
        self._last_decision = decision
        if layout_key != self._last_layout_key:
            self._last_layout_key = layout_key
            self._last_height_key = height_key
            self._populate()
        elif height_key != self._last_height_key:
            self._last_height_key = height_key
            self._apply_heights(decision)
        # else: no-op — both keys unchanged.

    def _jobs_natural(self) -> int:
        """Natural height of the CompactJobs card.

        border(2) + padding_top(1) + title(1) + body = 4 + max(1, job_count)."""
        return 4 + max(self._job_count, 1)

    # ── populate ─────────────────────────────────────────────

    def _apply_heights(self, d: LayoutDecision) -> None:
        """Phase 4d.2-E step A: in-place height update for an already-
        populated tree. Triggered by `_relayout` when the layout-key
        matches but the height-key changed (e.g. mine_only flip
        changed `_job_count` → jobs_natural → target). Falls back to
        `_populate` if any expected widget is missing — NEVER silently
        swallows, logs the fallback so we know if it fires."""
        with perf_block("layout", "overview_apply_heights"):
            try:
                main = self.query_one("#main", Vertical)
            except Exception:
                self.log("OverviewPanel._apply_heights: #main missing; "
                         "falling back to _populate")
                self._populate()
                return
            if d.fit and d.mode == "wide":
                main.styles.height = d.target
            else:
                main.styles.height = None
            # Wide-mode no-util balance branch may have given Totals /
            # Home explicit heights. Recompute in place; clear when
            # the extras are 0 so a transition out of balance mode
            # doesn't leave stale style on the widget.
            if d.mode == "wide":
                try:
                    nodes = self.query_one(NodesSummary)
                    if d.nodesummary_extra:
                        nodes.styles.height = (
                            self._TOTALS_NATURAL + d.nodesummary_extra
                        )
                    else:
                        nodes.styles.height = None
                except Exception:
                    self.log("OverviewPanel._apply_heights: NodesSummary "
                             "missing; falling back to _populate")
                    self._populate()
                    return
                try:
                    home = self.query_one(HomeStorage)
                    if d.home_extra:
                        home.styles.height = self._HOME_NATURAL + d.home_extra
                    else:
                        home.styles.height = None
                except Exception:
                    self.log("OverviewPanel._apply_heights: HomeStorage "
                             "missing; falling back to _populate")
                    self._populate()
                    return
            # AsciiArt's `_max_height` gates which art variant it picks.
            # If the decor isn't an AsciiArt (Matrix / Fire fill via
            # `#ascii_row { height: 1fr }`), this lookup raises and we
            # silently skip — those animations don't need the update.
            if d.show_ascii:
                try:
                    art = self.query_one(AsciiArt)
                    art._max_height = d.ascii_budget
                    art._pick_art()
                except Exception:
                    pass    # decor is matrix/fire/not yet mounted

    def _populate(self) -> None:
        # Phase 4d.2-E step 2.5: time the populate path. This mounts
        # NodesSummary + HomeStorage + CompactJobs + UtilizationPanel +
        # ascii fresh each call, so it's the closest analogue we have
        # to a "lazy mount" cost. Triggered on every layout-changing
        # event (resize, util-fit flip).
        with perf_block("layout", "overview_populate"):
            self._populate_impl()

    def _populate_impl(self) -> None:
        main = self.query_one("#main", Vertical)
        main.remove_children()
        ascii_row = self.query_one("#ascii_row", Vertical)
        ascii_row.remove_children()

        d = self._last_decision
        if d is None:    # _populate called before _relayout — defensive.
            return

        # Toggle classes for the right CSS branch.
        for cls in ("narrow", "wide", "fit", "scroll"):
            self.remove_class(cls)
        self.add_class(d.mode)
        self.add_class("fit" if d.fit else "scroll")
        if d.fit and d.mode == "wide":
            main.styles.height = d.target
        else:
            main.styles.height = None

        if d.mode == "narrow":
            # Single col, all natural — overflow handled by parent scroll.
            main.mount(_card(NodesSummary()))
            main.mount(_card(HomeStorage()))
            main.mount(_card(CompactJobs()))
            return

        # Wide mode.
        main.styles.grid_size_columns = 2
        col_left = Vertical(classes="col")
        col_right = Vertical(classes="col")
        main.mount(col_left)
        main.mount(col_right)

        # Phase 4d.2-E: per the user's pseudocode, util can mount on the
        # LEFT column (when right is taller and gap_left fits util_min)
        # or on the RIGHT (gap_right fits util_min); when neither, the
        # decision tells us how to balance the columns instead.
        nodes = NodesSummary()
        home = HomeStorage()
        # Apply explicit per-widget heights from the no-util right>left
        # balance branch. nodesummary_extra / home_extra are 0 in every
        # other case; setting `height = natural + 0` is a no-op.
        if d.nodesummary_extra:
            nodes.styles.height = self._TOTALS_NATURAL + d.nodesummary_extra
        if d.home_extra:
            home.styles.height = self._HOME_NATURAL + d.home_extra
        col_left.mount(_card(nodes))

        if d.util_side == "left":
            # Util on the left → HomeStorage natural, UtilizationPanel
            # flex-fills the gap_left slack. Right column is just
            # CompactJobs (which is naturally taller than left_nat).
            col_left.mount(_card(home))
            col_left.mount(_card(UtilizationPanel(), flex=True))
            col_right.mount(_card(CompactJobs(), flex=True))
        elif d.util_side == "right":
            # Util on the right → HomeStorage flex-fills gap_right
            # slack on the left, UtilizationPanel sits below
            # CompactJobs on the right.
            col_left.mount(_card(home, flex=True))
            col_right.mount(_card(CompactJobs()))
            col_right.mount(_card(UtilizationPanel(), flex=True))
        else:
            # No util. Balance per `compact_jobs_flex` / nodes_extra /
            # home_extra. When left_nat > right_nat, stretch CompactJobs
            # (its body scrolls internally). Otherwise the explicit
            # heights set above on Totals/Home fill the slack.
            col_left.mount(_card(home))
            col_right.mount(_card(CompactJobs(), flex=d.compact_jobs_flex))

        if d.show_ascii:
            ascii_row.remove_class("hidden")
            ascii_row.mount(self._make_decor(d.ascii_budget))
        else:
            ascii_row.add_class("hidden")

    def _make_decor(self, ascii_budget: int) -> Widget:
        """Pick the Overview decoration based on the user's config."""
        kind = "art"
        try:
            kind = (self.app.cfg.overview.animation or "art").lower()
        except Exception:
            pass
        if kind == "matrix":
            return MatrixRain()
        if kind == "fire":
            return DoomFire()
        return AsciiArt(max_height=ascii_budget)

    def _push_snapshot(self) -> None:
        snap = getattr(self.app, "snapshot", None)
        if snap is None:
            return
        for w in self.query("*"):
            handler = getattr(w, "update_snapshot", None)
            if callable(handler):
                try:
                    handler(snap)
                except Exception:
                    pass


def _card(widget: Widget, flex: bool = False) -> Widget:
    widget.add_class("card")
    if flex:
        widget.add_class("flex")
    return widget
