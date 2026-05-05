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

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from ..collectors.models import Job, Snapshot
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
        home = next((e for e in snap.storage if e.source == "quota"), None)
        usage_line = self.query_one("#usage", Static)
        bar = self.query_one(UsageBar)
        if home is None:
            usage_line.update("[dim italic]no quota info yet[/dim italic]")
            return
        bar.update_fraction(home.fraction)
        text = fat_bytes(home.free_bytes, home.used_bytes, home.total_bytes)
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
        # Overview always shows the current user's jobs only — never other
        # users', even when the Jobs tab's mine_only toggle is off.
        jobs = snap.jobs_mine
        self.query_one("#title", Static).update(
            f"Active jobs  [dim]({len(jobs)})[/dim]"
        )
        if not jobs:
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
    # util needs: border(2) + title(1) + title_margin(2) + padding_bot(1)
    #           + body(3 labels=3 + 3 sparks ≥1 each=3) = 12.  A bit more
    # for readable sparks:
    UTIL_MIN_HEIGHT = 13
    # rough natural heights used only for deciding whether util fits
    _TOTALS_NATURAL = 15
    _HOME_NATURAL = 7

    def __init__(self) -> None:
        super().__init__()
        self._job_count = 0
        self._last_layout: tuple | None = None
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
        # Overview's job pane shows mine-only — drive layout decisions off
        # that count, not the (possibly all-users) Jobs-tab list.
        self._job_count = len(snap.jobs_mine)
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
        jobs_nat = self._jobs_natural()
        if w < self.WIDE_MIN_WIDTH:
            mode = "narrow"
            # NodesSummary uses its full multiline format unless really
            # cramped (<60 cols), so size for the full form here.
            totals_h = 6 if w < 60 else self._TOTALS_NATURAL
            natural_need = totals_h + 1 + self._HOME_NATURAL + 1 + jobs_nat
            util_side = None
            # Narrow mode flips to vertical scroll-the-page when the stack
            # can't fit; the page-level ScrollableContainer wraps everything.
            fit = natural_need <= h
        else:
            mode = "wide"
            left_nat = self._TOTALS_NATURAL + 1 + self._HOME_NATURAL
            # Cap the right column's "natural" demand to the window height —
            # CompactJobs has an internal VerticalScroll, so when it's given
            # the right column at flex(1fr) its body just scrolls.  Without
            # this cap, a 100+-job count would push natural_need beyond h,
            # send us into scroll mode, and collapse the 2-col grid to a
            # single column (because OverviewPanel.scroll #main forces
            # layout: vertical).  Bounded right_nat → wide layout is always
            # achievable; the CompactJobs card scrolls internally.
            right_nat = min(jobs_nat, max(h, left_nat))
            natural_need = max(left_nat, right_nat)
            shorter_nat = min(left_nat, right_nat)
            gap = natural_need - shorter_nat
            util_side = None
            if natural_need <= h and gap >= self.UTIL_MIN_HEIGHT + 1:
                util_side = "right" if right_nat <= left_nat else "left"
            # In wide mode we always fit — the right card uses internal
            # scroll for any job-count overflow.  Only the left column's
            # stack (Totals + Home, both with bounded height) needs to fit
            # for the fit-mode CSS to give us a real two-column grid.
            fit = left_nat <= h
        if fit:
            target = natural_need
            ascii_budget = max(h - target, 0)
            show_ascii = ascii_budget >= ASCII_MIN_TOTAL
        else:
            target = natural_need    # not actually applied (scroll mode uses auto)
            ascii_budget = 0
            show_ascii = False
        layout = (mode, util_side, show_ascii, target, min(ascii_budget, 15), fit)
        if layout == self._last_layout:
            return
        self._last_layout = layout
        self._populate()

    def _jobs_natural(self) -> int:
        """Natural height of the CompactJobs card.

        border(2) + padding_top(1) + title(1) + body = 4 + max(1, job_count)."""
        return 4 + max(self._job_count, 1)

    # ── populate ─────────────────────────────────────────────

    def _populate(self) -> None:
        main = self.query_one("#main", Vertical)
        main.remove_children()
        ascii_row = self.query_one("#ascii_row", Vertical)
        ascii_row.remove_children()

        mode, util_side, show_ascii, target, ascii_budget, fit = self._last_layout  # type: ignore

        # Toggle classes for the right CSS branch.
        for cls in ("narrow", "wide", "fit", "scroll"):
            self.remove_class(cls)
        self.add_class(mode)
        self.add_class("fit" if fit else "scroll")
        if fit and mode == "wide":
            main.styles.height = target
        else:
            main.styles.height = None

        if mode == "narrow":
            # Single col, all natural — overflow handled by parent scroll.
            main.mount(_card(NodesSummary()))
            main.mount(_card(HomeStorage()))
            main.mount(_card(CompactJobs()))
        else:
            main.styles.grid_size_columns = 2
            col_left = Vertical(classes="col")
            col_right = Vertical(classes="col")
            main.mount(col_left)
            main.mount(col_right)

            col_left.mount(_card(NodesSummary()))
            if util_side == "left":
                col_left.mount(_card(HomeStorage()))
                col_left.mount(_card(UtilizationPanel(), flex=True))
            else:
                # no util on left: stretch Home only when the columns need
                # balancing (i.e. util is absent entirely).  When util is
                # on the right, left is already the taller col — leave Home
                # natural.
                col_left.mount(_card(HomeStorage(), flex=(util_side is None)))

            if util_side == "right":
                col_right.mount(_card(CompactJobs()))
                col_right.mount(_card(UtilizationPanel(), flex=True))
            else:
                # right col has just jobs — stretch it so the columns match.
                col_right.mount(_card(CompactJobs(), flex=True))

        if show_ascii:
            ascii_row.remove_class("hidden")
            ascii_row.mount(self._make_decor(ascii_budget))
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
        snap = self._owning_snapshot()
        if snap is None:
            return
        for w in self.query("*"):
            handler = getattr(w, "update_snapshot", None)
            if callable(handler):
                try:
                    handler(snap)
                except Exception:
                    pass

    def _owning_snapshot(self) -> Snapshot | None:
        """Find the Snapshot for the cluster this OverviewPanel lives in.

        In multi-cluster mode each cluster has its own Snapshot in
        `self.app.snapshots[<id>]`; we walk up to the outer TabPane to
        figure out which cluster id we belong to.  Falls back to
        `self.app.snapshot` (the active cluster's) when we can't tell —
        which is the right answer in single-cluster mode.
        """
        # Avoid circular import.
        from textual.widgets import TabPane as _TabPane
        node = self.parent
        while node is not None:
            if isinstance(node, _TabPane) and (node.id or "").startswith("cluster_"):
                cid = (node.id or "")[len("cluster_"):]
                snaps = getattr(self.app, "snapshots", None)
                if isinstance(snaps, dict) and cid in snaps:
                    return snaps[cid]
                break
            node = node.parent
        return getattr(self.app, "snapshot", None)


def _card(widget: Widget, flex: bool = False) -> Widget:
    widget.add_class("card")
    if flex:
        widget.add_class("flex")
    return widget
