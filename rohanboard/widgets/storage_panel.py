"""Storage view: home / SSD / HDD groups, green bars showing free space, responsive."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Grid, Vertical
from textual.widget import Widget
from textual.widgets import Static

from ..collectors.models import Snapshot, StorageEntry
from .format import fat_bytes


def _classify(entry: StorageEntry) -> str:
    if entry.source == "quota":
        return "home"
    p = entry.path or ""
    if p.startswith("/cluster_HDD/"):
        return "hdd"
    if p.startswith("/cluster/"):
        return "ssd"
    return "other"


def _used_class(fraction: float) -> str:
    if fraction >= 0.95:
        return "level-critical"
    if fraction >= 0.80:
        return "level-warning"
    return "level-ok"


# ──────────────────────────────────────────────────────────────────────────
# UsageBar: 1-row two-color horizontal bar. Yellow = used, green = free.
# ──────────────────────────────────────────────────────────────────────────

class UsageBar(Widget):
    DEFAULT_CSS = """
    UsageBar {
        height: 1;
        width: 100%;
    }
    """

    BLOCK = "━"

    def __init__(self, fraction: float = 0.0) -> None:
        super().__init__()
        self._fraction = max(0.0, min(1.0, fraction))

    def update_fraction(self, fraction: float) -> None:
        f = max(0.0, min(1.0, fraction))
        if f == self._fraction:
            return
        self._fraction = f
        self.refresh()

    def on_resize(self) -> None:
        self.refresh()

    def render(self) -> Text:
        width = max(1, self.size.width - 6)  # leave room for trailing " 94%".
        used_chars = int(round(width * self._fraction))
        used_chars = max(0, min(width, used_chars))
        free_chars = width - used_chars
        text = Text()
        text.append(self.BLOCK * used_chars, style="yellow")
        text.append(self.BLOCK * free_chars, style="green")
        text.append(f" {int(round(self._fraction * 100)):>3}%", style="dim")
        return text


# ──────────────────────────────────────────────────────────────────────────
# StorageBar: one card per mount
# ──────────────────────────────────────────────────────────────────────────

class StorageBar(Widget):
    """Three-row card: label · bar (yellow=used, green=free) · usage text."""

    DEFAULT_CSS = """
    StorageBar {
        height: 4;
        min-width: 30;
        padding: 0 1;
    }
    StorageBar > .bar_label {
        height: 1;
        text-style: bold;
        color: $text;
    }
    StorageBar > .bar_usage {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(self, entry: StorageEntry) -> None:
        super().__init__()
        self.entry = entry
        self.add_class(_used_class(entry.fraction))

    def compose(self) -> ComposeResult:
        yield Static(self.entry.label, classes="bar_label", id="bar_label")
        yield UsageBar(self.entry.fraction)
        yield Static(self._usage_text(), classes="bar_usage", id="bar_usage")

    def update_entry(self, entry: StorageEntry) -> None:
        old_class = _used_class(self.entry.fraction)
        new_class = _used_class(entry.fraction)
        self.entry = entry
        if old_class != new_class:
            self.remove_class(old_class)
            self.add_class(new_class)
        self.query_one("#bar_label", Static).update(entry.label)
        self.query_one("#bar_usage", Static).update(self._usage_text())
        self.query_one(UsageBar).update_fraction(entry.fraction)

    def _usage_text(self) -> Text:
        free_bytes = self.entry.free_bytes
        text = fat_bytes(free_bytes, self.entry.used_bytes, self.entry.total_bytes)
        if self.entry.source == "quota":
            text.append("  (soft quota)", style="dim")
        return text


# ──────────────────────────────────────────────────────────────────────────
# StorageGroup: a labelled grid of bars
# ──────────────────────────────────────────────────────────────────────────

_GROUP_TITLES = {
    "home": "Home",
    "ssd":  "SSD  /cluster",
    "hdd":  "HDD  /cluster_HDD",
    "other": "Other",
}


class StorageGroup(Widget):
    """Heading + grid of StorageBar cards."""

    DEFAULT_CSS = """
    StorageGroup {
        height: auto;
        margin-top: 1;
        border-top: hkey $primary-darken-2;
        padding-top: 1;
    }
    StorageGroup.-first {
        margin-top: 0;
        border-top: none;
        padding-top: 0;
    }
    StorageGroup > .group_title {
        height: 1;
        text-style: bold;
        color: $accent;
        padding: 0 1;
    }
    StorageGroup > .grid {
        layout: grid;
        grid-size: 1;
        grid-gutter: 0 2;
        grid-rows: auto;
        height: auto;
    }
    """

    def __init__(self, group_id: str) -> None:
        super().__init__(id=f"group_{group_id}")
        self.group_id = group_id
        # label-keyed cache so we can update in place across refreshes.
        self._bars: dict[str, StorageBar] = {}

    def compose(self) -> ComposeResult:
        yield Static(_GROUP_TITLES.get(self.group_id, self.group_id.title()), classes="group_title")
        yield Grid(classes="grid", id="grid")

    def update_entries(self, entries: list[StorageEntry]) -> None:
        grid = self.query_one("#grid", Grid)
        # Adapt grid columns to current viewport: ~50 cols per bar.
        try:
            width = self.app.size.width
        except Exception:
            width = 100
        cols = max(1, min(4, width // 50))
        if grid.styles.grid_size_columns != cols:
            grid.styles.grid_size_columns = cols

        new_labels = {e.label for e in entries}
        for entry in entries:
            if entry.label in self._bars:
                self._bars[entry.label].update_entry(entry)
            else:
                bar = StorageBar(entry)
                self._bars[entry.label] = bar
                grid.mount(bar)
        for stale in list(self._bars.keys() - new_labels):
            try:
                self._bars[stale].remove()
            except Exception:
                pass
            del self._bars[stale]
        self.display = bool(self._bars)


# ──────────────────────────────────────────────────────────────────────────
# StoragePanel: top-level orchestrator
# ──────────────────────────────────────────────────────────────────────────

class StoragePanel(Widget):
    DEFAULT_CSS = """
    StoragePanel {
        height: auto;
        min-height: 5;
        min-width: 40;
        padding: 1 2;
        border: round $primary;
        margin: 1 2;
    }
    StoragePanel > .panel_title {
        height: 1;
        text-style: bold;
        color: $text;
        padding-bottom: 1;
    }
    StoragePanel > .empty {
        color: $text-muted;
        padding: 1;
    }
    """

    GROUP_ORDER = ("home", "ssd", "hdd", "other")

    def compose(self) -> ComposeResult:
        yield Static("Storage  [dim](green = free)[/dim]", classes="panel_title")
        for i, gid in enumerate(self.GROUP_ORDER):
            group = StorageGroup(gid)
            if i == 0:
                group.add_class("-first")
            yield group
        yield Static("[dim italic]no storage data yet[/dim italic]", classes="empty", id="empty_msg")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        empty_msg = self.query_one("#empty_msg", Static)
        if not snapshot.storage:
            empty_msg.display = True
            for gid in self.GROUP_ORDER:
                self.query_one(f"#group_{gid}", StorageGroup).update_entries([])
            return
        empty_msg.display = False
        grouped: dict[str, list[StorageEntry]] = {gid: [] for gid in self.GROUP_ORDER}
        for entry in snapshot.storage:
            grouped[_classify(entry)].append(entry)
        for gid, entries in grouped.items():
            entries.sort(key=lambda e: e.label.lower())
            self.query_one(f"#group_{gid}", StorageGroup).update_entries(entries)
