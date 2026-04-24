"""Custom clickable table header with per-metric sub-word click targets.

Sits above a DataTable configured with `show_header=False` and mirrors column
widths. Each "triple" column exposes three individually clickable sub-words
(free / alloc / total); "simple" columns are one clickable target that toggles
direction on repeat clicks.

The widget synchronises its horizontal scroll with a companion DataTable so
the two always line up even when the data needs to scroll.
"""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import HorizontalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Static


# ────────────────────────────────────────────────────────────────────────
# Per-column header cell
# ────────────────────────────────────────────────────────────────────────

class HeaderCell(Widget):
    """One column of the SortableHeader. Height 1, fixed width."""

    DEFAULT_CSS = """
    HeaderCell {
        height: 1;
        layout: horizontal;
        padding: 0 1;
        background: $surface;
    }
    HeaderCell > Static {
        height: 1;
        width: auto;
    }
    HeaderCell .plain {
        color: $text-muted;
    }
    HeaderCell .base {
        color: $text;
        text-style: bold;
    }
    HeaderCell .word {
        color: $text-muted;
    }
    HeaderCell .word:hover,
    HeaderCell .base:hover {
        color: $primary;
        text-style: bold underline;
    }
    HeaderCell .word.-active,
    HeaderCell .base.-active {
        color: $accent;
        text-style: bold;
    }
    HeaderCell .arrow {
        color: $accent;
        text-style: bold;
    }
    HeaderCell .spacer {
        color: $text-muted;
    }
    """

    def __init__(self, col_key: str, base_label: str, kind: str) -> None:
        super().__init__()
        self.col_key = col_key
        self.base_label = base_label
        self.kind = kind  # "simple" or "triple"

    def compose(self) -> ComposeResult:
        if self.kind == "triple":
            # base_label is like "CPU (free/alloc/total)".
            # Split at first "(" so we can render sub-words individually.
            paren = self.base_label.find(" (")
            if paren < 0:
                base = self.base_label
                suffix = ""
            else:
                base = self.base_label[:paren]
                suffix = self.base_label[paren:]  # e.g. " (free/alloc/total)"
            yield Static(base, classes="base", id="base")
            yield Static(" (", classes="plain")
            yield Static("free",  classes="word", id="free")
            yield Static("/", classes="plain")
            yield Static("alloc", classes="word", id="alloc")
            yield Static("/", classes="plain")
            yield Static("total", classes="word", id="total")
            yield Static(")", classes="plain")
        else:
            yield Static(self.base_label, classes="base", id="base")
        yield Static(" ", classes="spacer", id="spacer")
        yield Static("", classes="arrow", id="arrow")

    def apply_state(self, is_active_col: bool, active_metric: str, reverse: bool) -> None:
        try:
            arrow = self.query_one("#arrow", Static)
        except Exception:
            return
        arrow.update("↓" if is_active_col and reverse else ("↑" if is_active_col else ""))
        base = self.query_one("#base", Static)
        if self.kind == "simple":
            if is_active_col:
                base.add_class("-active")
            else:
                base.remove_class("-active")
            return
        # Triple: mark the active metric
        for m in ("free", "alloc", "total"):
            try:
                w = self.query_one(f"#{m}", Static)
            except Exception:
                continue
            if is_active_col and active_metric == m:
                w.add_class("-active")
            else:
                w.remove_class("-active")
        # Triple columns: also highlight the base name when any of its metrics active
        if is_active_col:
            base.add_class("-active")
        else:
            base.remove_class("-active")


# ────────────────────────────────────────────────────────────────────────
# SortableHeader — the row-wide container
# ────────────────────────────────────────────────────────────────────────

class SortableHeader(HorizontalScroll):
    """A row of HeaderCells. Wraps HorizontalScroll so it can mirror the
    scroll position of a companion DataTable."""

    DEFAULT_CSS = """
    SortableHeader {
        height: 1;
        background: $surface;
        scrollbar-size: 0 0;   /* no bar — we mirror the DataTable's */
        overflow-y: hidden;
    }
    """

    class SortChanged(Message):
        def __init__(self, col: str, metric: str | None) -> None:
            super().__init__()
            self.col = col
            self.metric = metric   # None → clicked label/arrow area, not a metric word

    def __init__(self, columns: list[tuple[str, str, int, str]]) -> None:
        """`columns` = list of (col_key, label, width, kind)."""
        super().__init__()
        self._columns = columns
        self._sort_col = ""
        self._sort_metric = "free"
        self._sort_reverse = False

    def compose(self) -> ComposeResult:
        for key, label, width, kind in self._columns:
            cell = HeaderCell(key, label, kind)
            # Each column: +2 for DataTable's cell padding (1 on each side).
            cell.styles.width = width + 2
            yield cell

    # ── state ──

    def set_sort(self, col: str, metric: str, reverse: bool) -> None:
        self._sort_col = col
        self._sort_metric = metric
        self._sort_reverse = reverse
        self._refresh_cells()

    def _refresh_cells(self) -> None:
        for cell in self.query(HeaderCell):
            cell.apply_state(
                is_active_col=(cell.col_key == self._sort_col),
                active_metric=self._sort_metric,
                reverse=self._sort_reverse,
            )

    def on_mount(self) -> None:
        self._refresh_cells()

    # ── input ──

    def on_click(self, event: events.Click) -> None:
        w = getattr(event, "widget", None) or getattr(event, "control", None)
        wid = getattr(w, "id", None) or ""
        # Walk up to find the enclosing HeaderCell.
        cell: HeaderCell | None = None
        node = w
        while node is not None and cell is None:
            if isinstance(node, HeaderCell):
                cell = node
                break
            node = getattr(node, "parent", None)
        if cell is None:
            return
        metric = wid if wid in ("free", "alloc", "total") else None
        self.post_message(self.SortChanged(cell.col_key, metric))

    # ── scroll mirroring ──

    def bind_scroll_source(self, source: DataTable) -> None:
        """Mirror `source.scroll_x` — call once after both are mounted."""
        self._scroll_source = source
        self.watch(source, "scroll_x", self._on_source_scroll)

    def _on_source_scroll(self, _old: float, new: float) -> None:
        try:
            self.scroll_to(x=new, animate=False)
        except Exception:
            pass
