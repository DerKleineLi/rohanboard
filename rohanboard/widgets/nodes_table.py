"""Per-node table + cluster totals card."""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static
from textual.widgets.data_table import RowKey

from ..collectors.models import Node, Snapshot, StorageEntry
from ..filter import make_matcher
from .format import fat, fat_bytes, fat_float
from .sortable_header import SortableHeader


def _state_color(state: str) -> str:
    base = state.split("+")[0].split("*")[0]
    return {
        "IDLE": "green",
        "MIXED": "yellow",
        "ALLOCATED": "red",
        "DOWN": "bold red",
        "DRAIN": "magenta",
        "RESERVED": "cyan",
    }.get(base, "white")


def _state_text(state: str) -> Text:
    return Text(state, style=_state_color(state))


# MIG-profile pattern: e.g. "3g.40gb", "2g.20gb", "1g.10gb". Lower-cased
# by convention (matches the AvailableFeatures string slurm reports).
_MIG_PROFILE_RE = re.compile(r"^\d+g\.\d+gb$", re.IGNORECASE)


def _norm_kind(kind: str, vram: str | None) -> str:
    """Canonical GPU label across clusters: "<KIND> <VRAM>".

    Non-MIG kinds get uppercased ("a100"→"A100", "rtx_3090"→"RTX_3090").
    MIG profile kinds preserve their lowercase form ("3g.40gb"). VRAM is
    appended unchanged when present (already a "NNGB" string from the
    AvailableFeatures regex, e.g. "80GB"). When `vram` is None, only the
    kind is returned. Empty `kind` and None `vram` return "" — caller
    decides whether to render that as "—".
    """
    if not kind:
        return vram or ""
    is_mig = bool(_MIG_PROFILE_RE.match(kind))
    canon = kind if is_mig else kind.upper()
    if vram:
        return f"{canon} {vram}"
    return canon


def _gpu_label(g) -> str:
    """Canonical render-time label for a GpuSpec — wraps `_norm_kind`."""
    label = _norm_kind(g.kind, g.vram)
    return label if label else "—"


def _gpu_cell(node: Node) -> Text:
    """Single GPU column — free/alloc/total triple, then canonical label.

    CPU-only node → "—". Single GPU spec → "free / alloc / total  <KIND> <VRAM>"
    (label collapses gracefully when kind or vram is missing — see
    `_norm_kind`). Multiple GPU specs (MIG profiles) → distinct
    "<triple> <label>" segments comma-joined.

    Order is intentional: the numbers are the load-bearing data the user
    scans for; the kind/vram is the qualifier. Putting numbers first keeps
    the column visually aligned across rows and makes free-count scanning
    fast. Same format applies to all clusters (rohan, slurm, lrz, balar).
    """
    if not node.gpus:
        return Text("—", style="dim")
    parts: list[Text] = []
    for g in node.gpus:
        cell = Text()
        cell.append_text(fat(g.free, g.alloc, g.total))
        label = _gpu_label(g)  # "A100 80GB", "RTX_A6000", "3g.40gb 40GB", or "—"
        if label and label != "—":
            cell.append(f"  {label}")
        parts.append(cell)
    out = Text()
    for i, p in enumerate(parts):
        if i:
            out.append(", ")
        out.append_text(p)
    return out


def _mem_gib(mb: int) -> float:
    return mb / 1024


def _storage_by_node(storage: list[StorageEntry]) -> dict[str, dict[str, StorageEntry]]:
    out: dict[str, dict[str, StorageEntry]] = {}
    for e in storage:
        p = (e.path or "").rstrip("/")
        if not p:
            continue
        if p.startswith("/cluster_HDD/"):
            name = p[len("/cluster_HDD/"):]
            kind = "hdd"
        elif p.startswith("/cluster/"):
            name = p[len("/cluster/"):]
            kind = "ssd"
        else:
            continue
        if "/" in name:
            continue
        out.setdefault(name.lower(), {})[kind] = e
    return out


def _storage_cell(entry: StorageEntry | None) -> Text:
    if entry is None:
        return Text("—", style="dim")
    return fat_bytes(entry.free_bytes, entry.used_bytes, entry.total_bytes)


def _node_filter_record(n: Node, storage_map: dict[str, dict[str, StorageEntry]]) -> dict:
    """Structured view of a node for use with the filter expressions.

    Sizes are exposed in **bytes** so filter values can use unit suffixes
    (e.g. `mem_free>=100G`, `ssd_free>=1T`).
    """
    per = storage_map.get(n.name.lower(), {})
    ssd = per.get("ssd")
    hdd = per.get("hdd")
    MB = 1024 * 1024
    return {
        "name":        n.name,
        "state":       n.state,
        "partitions":  ",".join(n.partitions),
        "gpu_kind":    ",".join(g.kind for g in n.gpus),
        "cpu_free":    n.cpu_free,
        "cpu_alloc":   n.cpu_alloc,
        "cpu_total":   n.cpu_total,
        "gpu_free":    sum(g.free for g in n.gpus),
        "gpu_alloc":   sum(g.alloc for g in n.gpus),
        "gpu_total":   sum(g.total for g in n.gpus),
        "mem_free":    (n.mem_total_mb - n.mem_alloc_mb) * MB,
        "mem_alloc":   n.mem_alloc_mb * MB,
        "mem_total":   n.mem_total_mb * MB,
        "ssd_free":    ssd.free_bytes if ssd else 0,
        "ssd_used":    ssd.used_bytes if ssd else 0,
        "ssd_total":   ssd.total_bytes if ssd else 0,
        "hdd_free":    hdd.free_bytes if hdd else 0,
        "hdd_used":    hdd.used_bytes if hdd else 0,
        "hdd_total":   hdd.total_bytes if hdd else 0,
    }


_NODE_FILTER_DEFAULT_FIELDS = ("name", "state", "partitions", "gpu_kind")


_FILTER_HELP = """Filter syntax — AND across tokens, `|` = OR inside a value:

  word                 substring in any default field
  field:substring      substring in a specific field
  field==value         exact (case-insensitive) match
  a|b|c                OR alternatives (works in any value)
  field>=N / <= / > / < / =    numeric comparison

Number values accept byte-unit suffixes: 100G, 1.5T, 500M, 2TB.

Text fields:   name, state, partitions, gpu_kind
Numeric:       cpu_free, cpu_alloc, cpu_total,
               gpu_free, gpu_alloc, gpu_total,
               mem_free, mem_alloc, mem_total (bytes),
               ssd_free, ssd_used, ssd_total (bytes),
               hdd_free, hdd_used, hdd_total (bytes)

Examples:
  a6000 gpu_free>=1           rtx_a6000 nodes with a free GPU
  state==IDLE                 exactly idle (not IDLE+DRAIN)
  gpu_kind:a6000|a100         either GPU family
  ssd_free>=1T                ≥ 1 TiB of SSD free
  a100 mem_free>=500G         a100 with big mem headroom"""



# ──────────────────────────────────────────────────────────────────────────
# Columns + sort configuration
# ──────────────────────────────────────────────────────────────────────────

# (col_key, base label, expected max data width, kind).
# The effective column width is max(label_len + 2 for arrow, data_width + 1).
_NODE_COLUMNS_RAW: tuple[tuple[str, str, int, str], ...] = (
    ("name",       "Node",                        9, "simple"),   # "daidalos" = 8
    ("state",      "State",                      13, "simple"),   # "MIXED+PLANNED" = 13
    ("cpu",        "CPU (free/alloc/total)",     17, "triple"),   # "1376 / 432 / 1808"
    ("gpu",        "GPU (free/alloc/total)",     22, "triple"),   # "16 / 56 / 64 rtx_a6000"
    ("mem",        "Mem GiB (free/alloc/total)", 22, "triple"),   # "9593 / 6823 / 16415"
    ("ssd",        "SSD (free/alloc/total)",     32, "triple"),   # per-value units: "289 GiB / 39.3 TiB / 41.7 TiB"
    ("hdd",        "HDD (free/alloc/total)",     32, "triple"),
    ("partitions", "Partitions",                 15, "simple"),
)


def _sized_columns() -> list[tuple[str, str, int, str]]:
    out = []
    for key, label, data_w, kind in _NODE_COLUMNS_RAW:
        # +2 for " ↑", +1 for a breathing column gap.
        width = max(len(label) + 2, data_w + 1)
        out.append((key, label, width, kind))
    return out


_NODE_COLUMNS = tuple(_sized_columns())

_METRIC_CYCLE = ("free", "alloc", "total")


def _node_sort_key(
    node: Node,
    col: str,
    metric: str,
    storage_map: dict[str, dict[str, StorageEntry]],
) -> tuple:
    name_lower = node.name.lower()
    if col == "name":
        return (name_lower,)
    if col == "state":
        return (node.state, name_lower)
    if col == "partitions":
        return (",".join(sorted(node.partitions)).lower(), name_lower)
    if col == "cpu":
        value = {"free": node.cpu_free, "alloc": node.cpu_alloc, "total": node.cpu_total}[metric]
        return (value, name_lower)
    if col == "gpu":
        pool = {
            "free":  sum(g.free for g in node.gpus),
            "alloc": sum(g.alloc for g in node.gpus),
            "total": sum(g.total for g in node.gpus),
        }
        return (pool[metric], name_lower)
    if col == "mem":
        alloc = node.mem_alloc_mb
        total = node.mem_total_mb
        value = {"free": total - alloc, "alloc": alloc, "total": total}[metric]
        return (value, name_lower)
    if col in ("ssd", "hdd"):
        entry = storage_map.get(name_lower, {}).get(col)
        if entry is None:
            return (float("-inf"), name_lower)
        value = {
            "free":  entry.free_bytes,
            "alloc": entry.used_bytes,
            "total": entry.total_bytes,
        }[metric]
        return (value, name_lower)
    return (name_lower,)


# ──────────────────────────────────────────────────────────────────────────
# NodesTable
# ──────────────────────────────────────────────────────────────────────────

class NodesTable(Widget):
    DEFAULT_CSS = """
    NodesTable {
        height: 1fr;
        min-height: 10;
        min-width: 80;
        padding: 0 2;
        margin: 1 2;
        border: round $primary;
    }
    NodesTable > Vertical {
        height: 1fr;
    }
    NodesTable .header_row {
        height: 1;
        padding: 0 1;
        layout: horizontal;
    }
    NodesTable .header_row > .title {
        width: auto;
        text-style: bold;
    }
    NodesTable .controls_row {
        height: 1;
        padding: 0 1;
        layout: horizontal;
    }
    NodesTable .controls_row > .controls_label {
        width: auto;
        height: 1;
        padding-right: 1;
        color: $text-muted;
    }
    NodesTable .chip {
        width: auto;
        height: 1;
        padding: 0 1;
        margin-right: 1;
        background: $boost;
        color: $text-muted;
    }
    NodesTable .chip:hover {
        background: $primary 70%;
    }
    NodesTable .filter_row {
        height: 1;
        padding: 0 1;
        layout: horizontal;
    }
    NodesTable .filter_row > Input {
        width: 1fr;
        height: 1;
        border: none;
        background: $boost;
        color: $text;
    }
    NodesTable DataTable {
        height: 1fr;
    }
    NodesTable .help_btn {
        width: 3;
        height: 1;
        padding: 0 1;
        margin-left: 1;
        background: $primary;
        color: $background;
        text-style: bold;
    }
    NodesTable .help_btn:hover {
        background: $primary 70%;
    }
    NodesTable .clear_btn {
        width: auto;
        height: 1;
        padding: 0 1;
        margin-left: 1;
        background: $error 50%;
        color: $text;
    }
    NodesTable .clear_btn:hover {
        background: $error;
        text-style: bold;
    }
    """

    filter_text: reactive[str] = reactive("")

    _row_keys: dict[str, RowKey]
    _last_snapshot: Snapshot | None
    _sort_col: str
    _sort_metric: str
    _sort_reverse: bool
    _applied_sort: tuple | None

    def __init__(self, presets: list[dict] | None = None) -> None:
        super().__init__()
        self._row_keys = {}
        self._last_snapshot = None
        self._sort_col = "name"
        self._sort_metric = "free"
        self._sort_reverse = False
        self._applied_sort = None
        self._presets = presets or []
        # Phase 3.5 (filter input drop): ~150 ms debounce so a fast
        # typist's keystrokes coalesce into ONE DataTable rebuild.
        self._filter_debounce_timer = None

    def compose(self) -> ComposeResult:
        with Vertical():
            # line 1 — title
            with Horizontal(classes="header_row"):
                yield Static("Nodes:", classes="title")
            # line 2 — presets
            with Horizontal(classes="controls_row", id="presets_row"):
                yield Static("Presets:", classes="controls_label")
                for i, preset in enumerate(self._presets):
                    yield Static(preset["name"], classes="chip", id=f"preset_{i}")
                if not self._presets:
                    yield Static("[dim italic](none — add to ~/.config/rohanboard/filters.json)[/dim italic]",
                                 classes="controls_label")
            # line 3 — filter
            with Horizontal(classes="filter_row"):
                yield Input(placeholder="filter — e.g. a6000 gpu_free>=1 state==IDLE",
                            id="filter")
                yield Static("?", classes="help_btn", id="filter_help")
                yield Static("Clear", classes="clear_btn", id="filter_clear")
            # Custom clickable header — replaces DataTable's built-in one.
            yield SortableHeader(list(_NODE_COLUMNS))
            table = DataTable(zebra_stripes=True, cursor_type="row",
                              show_header=False, id="nodes_dt")
            for key, _base, width, _kind in _NODE_COLUMNS:
                table.add_column("", key=key, width=width)
            yield table

    def on_mount(self) -> None:
        hdr = self.query_one(SortableHeader)
        dt = self.query_one("#nodes_dt", DataTable)
        hdr.bind_scroll_source(dt)
        hdr.set_sort(self._sort_col, self._sort_metric, self._sort_reverse)

    # ── preset clicks ─────────────────────────────────────

    def on_click(self, event: events.Click) -> None:
        w = getattr(event, "widget", None) or getattr(event, "control", None)
        wid = getattr(w, "id", None) or ""
        if wid.startswith("preset_"):
            try:
                idx = int(wid[len("preset_"):])
                expr = self._presets[idx]["expr"]
                self.query_one("#filter", Input).value = expr
            except (ValueError, IndexError, KeyError):
                pass
        elif wid == "filter_help":
            from ..screens.filter_help import FilterHelpModal, NODES_FILTER_SPEC
            self.app.push_screen(FilterHelpModal(NODES_FILTER_SPEC,
                                                 on_insert=self._insert_filter_fragment))
        elif wid == "filter_clear":
            self.query_one("#filter", Input).value = ""

    def _sort_col_kind(self) -> str:
        return next((k for ck, _b, _w, k in _NODE_COLUMNS if ck == self._sort_col), "simple")

    # SortableHeader → sort state change.
    def on_sortable_header_sort_changed(self, event: SortableHeader.SortChanged) -> None:
        col = event.col
        metric = event.metric
        kind = next((k for ck, _b, _w, k in _NODE_COLUMNS if ck == col), "simple")

        if kind == "triple":
            if metric is None:
                # Clicked the base label or arrow → toggle direction on
                # whatever metric is already active for this column.
                if self._sort_col == col:
                    self._sort_reverse = not self._sort_reverse
                else:
                    self._sort_col = col
                    self._sort_metric = "free"
                    self._sort_reverse = True
            else:
                if self._sort_col == col and self._sort_metric == metric:
                    self._sort_reverse = not self._sort_reverse
                else:
                    self._sort_col = col
                    self._sort_metric = metric
                    self._sort_reverse = True    # desc default for triples
        else:
            if self._sort_col == col:
                self._sort_reverse = not self._sort_reverse
            else:
                self._sort_col = col
                self._sort_metric = "free"
                self._sort_reverse = False
        self.query_one(SortableHeader).set_sort(
            self._sort_col, self._sort_metric, self._sort_reverse
        )
        if self._last_snapshot is not None:
            self.update_snapshot(self._last_snapshot)

    def _insert_filter_fragment(self, fragment: str | None) -> None:
        if not fragment:
            return
        inp = self.query_one("#filter", Input)
        sep = " " if inp.value and not inp.value.endswith(" ") else ""
        new_value = f"{inp.value}{sep}{fragment}"
        # Append without stealing focus or selecting the whole field.
        inp.value = new_value
        try:
            inp.cursor_position = len(new_value)
        except Exception:
            pass

    # ── filter ─────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "filter":
            return
        # Set the reactive (keeps any external observer in sync) but
        # DON'T let watch_filter_text drive a synchronous full rebuild.
        # Debounce at ~150 ms so a fast typist's keystrokes coalesce
        # into ONE DataTable rebuild.
        self.filter_text = event.value
        if self._filter_debounce_timer is not None:
            try:
                self._filter_debounce_timer.stop()
            except Exception:
                pass
        try:
            self._filter_debounce_timer = self.set_timer(
                0.15, self._apply_filter_debounced
            )
        except Exception:
            # Fallback (no event loop in test path): apply synchronously.
            self._apply_filter_debounced()

    def _apply_filter_debounced(self) -> None:
        """Run the actual filter+rebuild after the debounce timer fires.

        Phase 3.5 (chunked rebuild): dispatch via `run_worker` so the
        rebuild runs on the event loop as a coroutine that yields every
        50 row mutations.
        """
        self._filter_debounce_timer = None
        if not (self.is_mounted and self._last_snapshot is not None):
            return
        self._applied_sort = None
        try:
            self.run_worker(
                self._apply_filter_async(self._last_snapshot),
                exclusive=True,
                group=f"apply_filter_nodes_{self.id or id(self)}",
            )
        except Exception:
            # Fallback (no event loop / test path): run synchronously.
            self.update_snapshot(self._last_snapshot)

    async def _apply_filter_async(self, snapshot: Snapshot) -> None:
        """Async chunked filter+rebuild — `await asyncio.sleep(0)` every
        50 row mutations to yield control back to Textual's input
        dispatch loop.
        """
        self._last_snapshot = snapshot
        table = self.query_one("#nodes_dt", DataTable)
        prev_scroll_x = table.scroll_x
        prev_scroll_y = table.scroll_y
        prev_cursor = table.cursor_coordinate

        if (err := snapshot.errors.get("nodes")) or not snapshot.nodes:
            self._row_keys.clear()
            table.clear()
            n_cols = len(_NODE_COLUMNS)
            if err:
                table.add_row(Text(f"⚠ {err}", style="bold red"), *([""] * (n_cols - 1)))
            else:
                table.add_row(Text("— no node data —", style="dim italic"), *([""] * (n_cols - 1)))
            return

        storage_map = _storage_by_node(snapshot.storage)
        matcher = make_matcher(self.filter_text, list(_NODE_FILTER_DEFAULT_FIELDS))
        candidate_nodes = [
            n for n in snapshot.nodes
            if matcher(_node_filter_record(n, storage_map))
        ]
        nodes = sorted(
            candidate_nodes,
            key=lambda n: _node_sort_key(n, self._sort_col, self._sort_metric, storage_map),
            reverse=self._sort_reverse,
        )

        current_sort = (self._sort_col, self._sort_metric, self._sort_reverse)
        if self._applied_sort != current_sort or (not self._row_keys and table.row_count > 0):
            table.clear()
            self._row_keys.clear()
            self._applied_sort = current_sort

        new_keys: set[str] = set()
        mut_count = 0
        for n in nodes:
            row = self._row_for_node(n, storage_map)
            new_keys.add(n.name)
            if n.name in self._row_keys:
                rk = self._row_keys[n.name]
                for col_key, val in row.items():
                    try:
                        table.update_cell(rk, col_key, val, update_width=False)
                    except Exception:
                        pass
            else:
                values = [row[k] for k, _l, _w, _s in _NODE_COLUMNS]
                rk = table.add_row(*values, key=n.name)
                self._row_keys[n.name] = rk
            mut_count += 1
            if mut_count % 50 == 0:
                await asyncio.sleep(0)

        for stale in list(self._row_keys.keys() - new_keys):
            try:
                table.remove_row(self._row_keys[stale])
            except Exception:
                pass
            del self._row_keys[stale]
            mut_count += 1
            if mut_count % 50 == 0:
                await asyncio.sleep(0)

        if not nodes:
            n_cols = len(_NODE_COLUMNS)
            table.add_row(Text(f"— no nodes match '{self.filter_text}' —", style="dim italic"),
                          *([""] * (n_cols - 1)))

        try:
            table.cursor_coordinate = prev_cursor
            table.scroll_to(x=prev_scroll_x, y=prev_scroll_y, animate=False)
        except Exception:
            pass

    def watch_filter_text(self, _old: str, _new: str) -> None:
        # No-op: debounce in on_input_changed handles the rebuild.
        return

    # ── data ──────────────────────────────────────────────

    def _row_for_node(
        self,
        n: Node,
        storage_map: dict[str, dict[str, StorageEntry]],
    ) -> dict[str, Text | str]:
        mem_total = _mem_gib(n.mem_total_mb)
        mem_alloc = _mem_gib(n.mem_alloc_mb)
        mem_free = max(mem_total - mem_alloc, 0.0)
        bases = sorted({p.rsplit("_", 1)[0] if "_" in p else p for p in n.partitions})
        per_node = storage_map.get(n.name.lower(), {})
        return {
            "name":       Text(n.name),
            "state":      _state_text(n.state),
            "cpu":        fat(n.cpu_free, n.cpu_alloc, n.cpu_total),
            "gpu":        _gpu_cell(n),
            "mem":        fat_float(mem_free, mem_alloc, mem_total),
            "ssd":        _storage_cell(per_node.get("ssd")),
            "hdd":        _storage_cell(per_node.get("hdd")),
            "partitions": Text(", ".join(bases) if bases else "—"),
        }

    def update_snapshot(self, snapshot: Snapshot) -> None:
        self._last_snapshot = snapshot
        table = self.query_one("#nodes_dt", DataTable)
        prev_scroll_x = table.scroll_x
        prev_scroll_y = table.scroll_y
        prev_cursor = table.cursor_coordinate

        if (err := snapshot.errors.get("nodes")) or not snapshot.nodes:
            self._row_keys.clear()
            table.clear()
            n_cols = len(_NODE_COLUMNS)
            if err:
                table.add_row(Text(f"⚠ {err}", style="bold red"), *([""] * (n_cols - 1)))
            else:
                table.add_row(Text("— no node data —", style="dim italic"), *([""] * (n_cols - 1)))
            return

        storage_map = _storage_by_node(snapshot.storage)
        matcher = make_matcher(self.filter_text, list(_NODE_FILTER_DEFAULT_FIELDS))
        candidate_nodes = [
            n for n in snapshot.nodes
            if matcher(_node_filter_record(n, storage_map))
        ]
        nodes = sorted(
            candidate_nodes,
            key=lambda n: _node_sort_key(n, self._sort_col, self._sort_metric, storage_map),
            reverse=self._sort_reverse,
        )

        current_sort = (self._sort_col, self._sort_metric, self._sort_reverse)
        if self._applied_sort != current_sort or (not self._row_keys and table.row_count > 0):
            table.clear()
            self._row_keys.clear()
            self._applied_sort = current_sort

        new_keys: set[str] = set()
        for n in nodes:
            row = self._row_for_node(n, storage_map)
            new_keys.add(n.name)
            if n.name in self._row_keys:
                rk = self._row_keys[n.name]
                for col_key, val in row.items():
                    try:
                        table.update_cell(rk, col_key, val, update_width=False)
                    except Exception:
                        pass
            else:
                values = [row[k] for k, _l, _w, _s in _NODE_COLUMNS]
                rk = table.add_row(*values, key=n.name)
                self._row_keys[n.name] = rk

        for stale in list(self._row_keys.keys() - new_keys):
            try:
                table.remove_row(self._row_keys[stale])
            except Exception:
                pass
            del self._row_keys[stale]

        if not nodes:
            n_cols = len(_NODE_COLUMNS)
            table.add_row(Text(f"— no nodes match '{self.filter_text}' —", style="dim italic"),
                          *([""] * (n_cols - 1)))

        try:
            table.cursor_coordinate = prev_cursor
            table.scroll_to(x=prev_scroll_x, y=prev_scroll_y, animate=False)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# NodesSummary
# ──────────────────────────────────────────────────────────────────────────

def _cluster_totals(nodes: list[Node]) -> dict:
    cpu_alloc = sum(n.cpu_alloc for n in nodes)
    cpu_total = sum(n.cpu_total for n in nodes)
    mem_alloc = sum(_mem_gib(n.mem_alloc_mb) for n in nodes)
    mem_total = sum(_mem_gib(n.mem_total_mb) for n in nodes)
    # Aggregate by canonical (KIND, vram) so A100 80GB and A100 40GB are
    # reported on separate rows on LRZ. On rohan, vram is None for
    # everything so rows are keyed by KIND alone. Using `_gpu_label`
    # (instead of raw `g.display`) ensures the same physical GPU kind
    # never splits into two rows just because its `kind` string was
    # parsed in a different case across nodes.
    gpus: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for n in nodes:
        for g in n.gpus:
            label = _gpu_label(g)
            if label == "—":
                continue
            gpus[label][0] += g.alloc
            gpus[label][1] += g.total
    return {
        "cpu_alloc": cpu_alloc, "cpu_total": cpu_total,
        "mem_alloc": mem_alloc, "mem_total": mem_total,
        "gpus": dict(gpus),
    }


class NodesSummary(Widget):
    DEFAULT_CSS = """
    NodesSummary {
        height: auto;
        min-height: 6;
        min-width: 40;
        padding: 0 2;
        margin: 1 2;
        border: round $primary;
    }
    NodesSummary > .title {
        text-style: bold;
        padding-top: 1;
        padding-bottom: 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Cluster totals", classes="title")
        yield Static("(loading…)", id="summary_body")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        body = self.query_one("#summary_body", Static)
        if err := snapshot.errors.get("nodes"):
            body.update(f"[bold red]⚠ {err}[/bold red]")
            return
        if not snapshot.nodes:
            body.update("[dim italic]no node data yet[/dim italic]")
            return
        t = _cluster_totals(snapshot.nodes)
        cpu_free = t["cpu_total"] - t["cpu_alloc"]
        mem_free = t["mem_total"] - t["mem_alloc"]

        ssd_total = ssd_used = ssd_free = 0
        hdd_total = hdd_used = hdd_free = 0
        for e in snapshot.storage:
            p = (e.path or "").rstrip("/")
            if p.startswith("/cluster_HDD/") and p.count("/") == 2:
                hdd_total += e.total_bytes
                hdd_used += e.used_bytes
                hdd_free += e.free_bytes
            elif p.startswith("/cluster/") and p.count("/") == 2:
                ssd_total += e.total_bytes
                ssd_used += e.used_bytes
                ssd_free += e.free_bytes

        # Only fall back to the compact format when the panel itself is
        # really narrow.  Threshold kept well below half of OverviewPanel's
        # 2-col cutoff (130 → ~65 col width in 2-col mode) so the full
        # multiline form renders inside a normal 2-col cell without
        # the overview's column-height calculation getting fooled.
        avail = int(self.size.width) or int(self.app.size.width or 0)
        if avail and avail < 50:
            # Build with Text.append so the style strings match the
            # canonical `fat()` helper exactly — BBCode `[yellow]` resolves
            # via the active Textual theme, while `style="yellow"` goes
            # straight through Rich's named-colour map (renders as orange
            # in this app's theme; matches the storage card).
            out = Text()
            out.append(f"{len(snapshot.nodes)}", style="bold")
            out.append(" nodes\n")
            out.append("CPU "); out.append_text(fat(cpu_free, t['cpu_alloc'], t['cpu_total']))
            out.append("  Mem "); out.append_text(fat_float(mem_free, t['mem_alloc'], t['mem_total'], suffix=" GiB"))
            out.append("\nGPU: ")
            first = True
            for k, (a, tot) in sorted(t["gpus"].items()):
                if not first:
                    out.append("  ")
                first = False
                out.append_text(fat(tot - a, a, tot))
                out.append(f" {k}")
            body.update(out)
            return

        cpu_w = max(len(str(t["cpu_total"])), 5)
        mem_w = max(len(f"{t['mem_total']:.0f}"), 5)
        gpu_w = max((len(str(tot)) for _, (_, tot) in t["gpus"].items()), default=3) + 1

        cpu_text = fat(cpu_free, t["cpu_alloc"], t["cpu_total"], width=cpu_w)
        mem_text = fat_float(mem_free, t["mem_alloc"], t["mem_total"], suffix=" GiB", places=0, width=mem_w)

        out = Text()
        out.append("Nodes: ")
        out.append(f"{len(snapshot.nodes)}\n", style="bold")
        out.append("CPU   ")
        out.append_text(cpu_text)
        out.append("\n")
        out.append("Mem   ")
        out.append_text(mem_text)
        out.append("\n")
        if ssd_total:
            out.append("SSD   ")
            out.append_text(fat_bytes(ssd_free, ssd_used, ssd_total))
            out.append("\n")
        if hdd_total:
            out.append("HDD   ")
            out.append_text(fat_bytes(hdd_free, hdd_used, hdd_total))
            out.append("\n")
        out.append("GPU\n")
        for kind, (a, tot) in sorted(t["gpus"].items()):
            # Order: triple first, then kind label — matches per-row _gpu_cell.
            out.append("  ")
            out.append_text(fat(tot - a, a, tot, width=gpu_w))
            out.append(f"  {kind}\n")
        body.update(out)
