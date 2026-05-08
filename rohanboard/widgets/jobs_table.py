"""Jobs DataTable with active/recent toggle, sort, and filter."""
from __future__ import annotations

import asyncio
import os
import time

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static
from textual.widgets.data_table import RowKey

from ..collectors.models import Job, Snapshot
from ..filter import make_matcher
from .sortable_header import SortableHeader


_STATE_STYLE = {
    "RUNNING":     "green",
    "PENDING":     "yellow",
    "COMPLETING":  "cyan",
    "COMPLETED":   "dim green",
    "CANCELLED":   "red",
    "FAILED":      "bold red",
    "TIMEOUT":     "bold red",
    "SUSPENDED":   "magenta",
    "PREEMPTED":   "magenta",
}


def _state_text(state: str) -> Text:
    style = _STATE_STYLE.get(state.split("+")[0], "white")
    return Text(state, style=style)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _mem_bytes(s: str) -> int:
    s = s.strip()
    if not s or not s[0].isdigit():
        return -1
    num = ""
    i = 0
    while i < len(s) and (s[i].isdigit() or s[i] == "."):
        num += s[i]
        i += 1
    try:
        n = float(num)
    except ValueError:
        return -1
    suffix = s[i:].strip().upper()
    mult = {
        "":   1,
        "K":  1024, "KB": 1024, "KIB": 1024,
        "M":  1024 ** 2, "MB": 1024 ** 2, "MIB": 1024 ** 2,
        "G":  1024 ** 3, "GB": 1024 ** 3, "GIB": 1024 ** 3,
        "T":  1024 ** 4, "TB": 1024 ** 4, "TIB": 1024 ** 4,
    }.get(suffix, 1)
    return int(n * mult)


def _job_filter_record(j: Job) -> dict:
    """Structured view for the filter expression. `mem` is in bytes so you
    can filter with natural units (e.g. `mem>=100G`)."""
    gpu = -1
    try:
        gpu = int(j.alloc_gpu) if j.alloc_gpu not in ("—", "") else -1
    except ValueError:
        gpu = -1
    return {
        "id":        j.job_id,
        "name":      j.name,
        "user":      j.user,
        "state":     j.state,
        "partition": j.partition,
        "node":      j.node_or_reason,
        "tres":      j.tres,
        "cpu":       j.num_cpus,
        "gpu":       gpu,
        "nodes":     j.num_nodes,
        "mem":       _mem_bytes(j.alloc_mem),
    }


_JOB_FILTER_DEFAULT_FIELDS = ("id", "name", "user", "state", "partition", "node", "tres")


_FILTER_HELP = """Filter syntax — AND across tokens, `|` = OR inside a value:

  word                 substring in any default field
  field:substring      substring in a specific field
  field==value         exact (case-insensitive) match
  a|b|c                OR alternatives (works in any value)
  field>=N / <= / > / < / =    numeric comparison

Number values accept byte-unit suffixes: 100G, 1.5T, 500M, 2TB.

Text fields:   id, name, user, state, partition, node, tres
Numeric:       cpu, gpu, nodes, mem (bytes)

Examples:
  state:RUNNING                running jobs only
  state==RUNNING|PENDING       exactly running or pending
  user:hli gpu>=1              my GPU jobs
  cpu>=8                       jobs allocating 8+ CPUs
  mem>=100G                    jobs asking ≥ 100 GiB memory"""



# ──────────────────────────────────────────────────────────────────────────
# Columns. Widths reserve 2 chars for the trailing " ↑".
# ──────────────────────────────────────────────────────────────────────────

# (col_key, label, expected_data_max_width)
# Effective width = max(label + 2 for arrow, data + 1 for gap).
_ACTIVE_RAW: tuple[tuple[str, str, int], ...] = (
    ("job_id",     "Job",         8),
    ("partition",  "Partition",  18),
    ("name",       "Name",       26),
    ("user",       "User",       10),
    ("state",      "State",      10),
    ("node",       "Node/Reason", 12),
    ("time",       "Time",       10),
    ("left",       "Left",       10),
    ("nodes",      "N",           2),
    ("cpu",        "CPU",         4),
    ("gpu",        "GPU",         4),
    ("mem",        "Mem",         6),
)

_RECENT_RAW: tuple[tuple[str, str, int], ...] = (
    ("job_id",     "Job",         8),
    ("partition",  "Partition",  22),   # sacct may include multiple partitions
    ("name",       "Name",       26),
    ("user",       "User",       10),
    ("state",      "State",      12),
    ("node",       "Node",       12),
    ("time",       "Elapsed",    10),
    ("cpu",        "CPU",         4),
    ("gpu",        "GPU",         4),
    ("mem",        "Mem",         6),
)


def _size_cols(raw):
    # (key, label, width, kind) — all simple for jobs.
    return tuple((k, l, max(len(l) + 2, w + 1), "simple") for k, l, w in raw)


_COLUMNS_ACTIVE = _size_cols(_ACTIVE_RAW)
_COLUMNS_RECENT = _size_cols(_RECENT_RAW)


def _sort_value(job: Job, col: str):
    try:
        if col == "job_id":
            return int(job.job_id)
        if col == "cpu":
            return job.num_cpus
        if col == "nodes":
            return job.num_nodes
        if col == "gpu":
            try:
                return int(job.alloc_gpu) if job.alloc_gpu not in ("—", "") else -1
            except ValueError:
                return -1
        if col == "mem":
            return _mem_bytes(job.alloc_mem)
        if col == "time":
            return job.time_used
        if col == "left":
            return job.time_left
        if col == "partition":
            return job.partition.lower()
        if col == "name":
            return job.name.lower()
        if col == "user":
            return job.user.lower()
        if col == "state":
            return job.state
        if col == "node":
            return job.node_or_reason.lower()
    except Exception:
        pass
    return job.job_id


class JobsTable(Widget):
    DEFAULT_CSS = """
    JobsTable {
        height: 1fr;
        min-height: 8;
        min-width: 60;
        padding: 0 2;
        margin: 1 2;
        border: round $primary;
    }
    JobsTable > Vertical {
        height: 1fr;
    }
    JobsTable .header_row {
        height: 1;
        padding: 0 1;
        layout: horizontal;
    }
    JobsTable .header_row > .title {
        width: 7;
        height: 1;
        text-style: bold;
    }
    JobsTable .toggle_pill {
        width: auto;
        height: 1;
        padding: 0 1;
        margin-left: 1;
        background: $boost;
        color: $text-muted;
    }
    JobsTable .toggle_pill.-active {
        background: $accent;
        color: $background;
        text-style: bold;
    }
    JobsTable .toggle_pill:hover {
        background: $primary 70%;
    }
    JobsTable .controls_row {
        height: 1;
        padding: 0 1;
        layout: horizontal;
    }
    JobsTable .controls_row > .controls_label {
        width: auto;
        height: 1;
        padding-right: 1;
        color: $text-muted;
    }
    JobsTable .chip {
        width: auto;
        height: 1;
        padding: 0 1;
        margin-right: 1;
        background: $boost;
        color: $text-muted;
    }
    JobsTable .chip:hover {
        background: $primary 70%;
    }
    JobsTable .filter_row {
        height: 1;
        padding: 0 1;
        layout: horizontal;
    }
    JobsTable .filter_row > Input {
        width: 1fr;
        height: 1;
        border: none;
        background: $boost;
        color: $text;
    }
    JobsTable DataTable {
        height: 1fr;
    }
    JobsTable .help_btn {
        width: 3;
        height: 1;
        padding: 0 1;
        margin-left: 1;
        background: $primary;
        color: $background;
        text-style: bold;
    }
    JobsTable .help_btn:hover {
        background: $primary 70%;
    }
    JobsTable .clear_btn {
        width: auto;
        height: 1;
        padding: 0 1;
        margin-left: 1;
        background: $error 50%;
        color: $text;
    }
    JobsTable .clear_btn:hover {
        background: $error;
        text-style: bold;
    }
    """

    BINDINGS = []

    mode: reactive[str] = reactive("active")
    filter_text: reactive[str] = reactive("")
    mine_only: reactive[bool] = reactive(True)

    _last_snapshot: Snapshot | None = None
    _row_keys: dict[str, RowKey]
    _current_columns: tuple[tuple[str, str, int, str], ...]
    _sort_col: str
    _sort_reverse: bool
    _applied_sort: tuple | None

    def __init__(self, presets: list[dict] | None = None) -> None:
        super().__init__()
        self._row_keys = {}
        self._current_columns = ()
        self._sort_col = "job_id"
        self._sort_reverse = True   # newest jobs (highest JOBID) at the top
        self._applied_sort = None
        self._presets = presets or []
        # Phase 3.5 (filter input drop): ~150 ms debounce so a fast
        # typist's keystrokes coalesce into ONE DataTable rebuild after
        # they pause. Without debounce, every keystroke triggered a
        # synchronous rebuild that — under refresh-tick pressure —
        # starved Textual's input dispatch and dropped keystrokes.
        self._filter_debounce_timer = None

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(classes="header_row"):
                yield Static("Jobs:", classes="title")
                yield Static(" Active (squeue) ", classes="toggle_pill -active", id="pill_active")
                yield Static(" Recent (sacct) ", classes="toggle_pill", id="pill_recent")
                yield Static(" Mine only ", classes="toggle_pill -active", id="pill_mine")
            with Horizontal(classes="controls_row", id="presets_row"):
                yield Static("Presets:", classes="controls_label")
                for i, p in enumerate(self._presets):
                    yield Static(p["name"], classes="chip", id=f"preset_{i}")
                if not self._presets:
                    yield Static("[dim italic](none — add to ~/.config/rohanboard/filters.json)[/dim italic]",
                                 classes="controls_label")
            with Horizontal(classes="filter_row"):
                yield Input(placeholder="filter — e.g. state:RUNNING gpu>=1 user:hli",
                            id="filter")
                yield Static("?", classes="help_btn", id="filter_help")
                yield Static("Clear", classes="clear_btn", id="filter_clear")
            yield SortableHeader(list(_COLUMNS_ACTIVE))
            table = DataTable(zebra_stripes=True, cursor_type="row",
                              show_header=False, id="jobs_table_dt")
            yield table

    def on_mount(self) -> None:
        self._rebuild_columns(_COLUMNS_ACTIVE)
        hdr = self.query_one(SortableHeader)
        dt = self.query_one("#jobs_table_dt", DataTable)
        hdr.bind_scroll_source(dt)
        hdr.set_sort(self._sort_col, "free", self._sort_reverse)

    # ── mode toggle + preset clicks ──────────────────────────

    # Double-click on a data row opens that job's log.  Architecture:
    # Textual's DataTable._on_click handler calls event.stop() on row
    # clicks, so a Click on a row never bubbles up to JobsTable.on_click.
    # MouseDown DOES bubble though (DataTable._on_mouse_down only brokers,
    # doesn't stop), and it carries the cell metadata (row/column) in
    # event.style.meta when the click was on a real cell — empty space
    # has no metadata, header has row=-1.  So we do double-click detection
    # in on_mouse_down and leave on_click for the non-row widgets.
    DOUBLE_CLICK_WINDOW_S: float = 0.4
    _last_row_click_at: float = 0.0
    _last_click_row: int = -1

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # Pull row index from rich-text style metadata.  Empty space click
        # → meta lacks 'row' (or has out_of_bounds) → ignore.  Header click
        # → row == -1 → ignore.
        meta = getattr(getattr(event, "style", None), "meta", None) or {}
        row = meta.get("row")
        if row is None or row < 0 or meta.get("out_of_bounds", False):
            return
        try:
            table = self.query_one("#jobs_table_dt", DataTable)
        except Exception:
            return
        if not (0 <= row < table.row_count):
            return

        now = time.monotonic()
        if (
            self._last_click_row == row
            and (now - self._last_row_click_at) < self.DOUBLE_CLICK_WINDOW_S
        ):
            # Genuine fast double-click on the same row → open the log.
            # Reset so a 3rd click within the window doesn't re-fire.
            self._last_row_click_at = 0.0
            self._last_click_row = -1
            try:
                table.cursor_coordinate = (row, table.cursor_column or 0)
            except Exception:
                pass
            self.action_tail_log()
            return
        # First click on this row (or window expired): record only.  Let
        # Textual's own _on_click handle cursor placement / row highlight.
        self._last_row_click_at = now
        self._last_click_row = row

    def on_click(self, event: events.Click) -> None:
        # Row-click handling lives in on_mouse_down (see comment above).
        # Here we only handle the non-table widgets whose Click events do
        # bubble up: mode pills, preset chips, filter help/clear.
        w = getattr(event, "widget", None) or getattr(event, "control", None)
        wid = getattr(w, "id", None) or ""
        if wid == "pill_active" and self.mode != "active":
            self.mode = "active"
        elif wid == "pill_recent" and self.mode != "recent":
            self.mode = "recent"
        elif wid == "pill_mine":
            self.mine_only = not self.mine_only
        elif wid.startswith("preset_"):
            try:
                idx = int(wid[len("preset_"):])
                expr = self._presets[idx]["expr"]
                self.query_one("#filter", Input).value = expr
            except (ValueError, IndexError, KeyError):
                pass
        elif wid == "filter_help":
            from ..screens.filter_help import FilterHelpModal, JOBS_FILTER_SPEC
            self.app.push_screen(FilterHelpModal(JOBS_FILTER_SPEC,
                                                 on_insert=self._insert_filter_fragment))
        elif wid == "filter_clear":
            self.query_one("#filter", Input).value = ""

    def _insert_filter_fragment(self, fragment: str | None) -> None:
        if not fragment:
            return
        inp = self.query_one("#filter", Input)
        sep = " " if inp.value and not inp.value.endswith(" ") else ""
        new_value = f"{inp.value}{sep}{fragment}"
        inp.value = new_value
        try:
            inp.cursor_position = len(new_value)
        except Exception:
            pass

    def watch_mode(self, _old: str, new: str) -> None:
        if not self.is_mounted:
            return
        active_pill = self.query_one("#pill_active", Static)
        recent_pill = self.query_one("#pill_recent", Static)
        if new == "active":
            active_pill.add_class("-active")
            recent_pill.remove_class("-active")
        else:
            recent_pill.add_class("-active")
            active_pill.remove_class("-active")
        cols = _COLUMNS_ACTIVE if new == "active" else _COLUMNS_RECENT
        self._rebuild_columns(cols)
        # Rebuild the custom header for the new column set.
        try:
            hdr = self.query_one(SortableHeader)
            hdr.remove()
        except Exception:
            pass
        new_hdr = SortableHeader(list(cols))
        self.query_one(Vertical).mount(new_hdr, before=self.query_one("#jobs_table_dt", DataTable))
        new_hdr.bind_scroll_source(self.query_one("#jobs_table_dt", DataTable))
        new_hdr.set_sort(self._sort_col, "free", self._sort_reverse)
        if self._last_snapshot is not None:
            self.update_snapshot(self._last_snapshot)

    def action_toggle_mode(self) -> None:
        self.mode = "recent" if self.mode == "active" else "active"

    def action_tail_log(self) -> None:
        table = self.query_one("#jobs_table_dt", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            self.app.notify("No job selected.", severity="warning")
            return
        try:
            row = table.get_row_at(table.cursor_row)
            job_id = str(row[0])
        except Exception:
            self.app.notify("Could not read selected row.", severity="error")
            return
        if not job_id or not job_id[0].isdigit():
            self.app.notify("Selected row has no job id.", severity="warning")
            return
        from ..screens.log_tail import LogTailScreen
        # The app instantiates its Executor in __init__; reach for it here so
        # the screen's slurm calls go through the same execution path as the
        # main refresh loop.
        executor = getattr(self.app, "executor", None)
        if executor is None:
            self.app.notify("App has no executor — cannot fetch job info.",
                            severity="error")
            return
        self.app.push_screen(LogTailScreen(job_id, executor))

    # ── filter ──────────────────────────────────────────────

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
        50 row mutations. The synchronous body would otherwise block
        input dispatch for 50–200 ms when the table has 100+ rows,
        dropping keystrokes from a fast typist who's STILL typing while
        the debounce-fired rebuild lands.
        """
        self._filter_debounce_timer = None
        if not (self.is_mounted and self._last_snapshot is not None):
            return
        self._applied_sort = None
        try:
            self.run_worker(
                self._apply_filter_async(self._last_snapshot),
                exclusive=True,
                group=f"apply_filter_jobs_{self.id or id(self)}",
            )
        except Exception:
            # Fallback (no event loop / test path): run synchronously.
            self.update_snapshot(self._last_snapshot)

    async def _apply_filter_async(self, snapshot: Snapshot) -> None:
        """Async chunked filter+rebuild — `await asyncio.sleep(0)` every
        50 row mutations to yield control back to Textual's input
        dispatch loop. Mirrors the synchronous `update_snapshot` body
        but pauses periodically so a fast typist's next keystroke can
        be processed before the rebuild finishes.
        """
        self._last_snapshot = snapshot
        table = self.query_one("#jobs_table_dt", DataTable)
        prev_scroll_x = table.scroll_x
        prev_scroll_y = table.scroll_y
        prev_cursor = table.cursor_coordinate

        jobs: list[Job] = snapshot.jobs if self.mode == "active" else getattr(snapshot, "recent_jobs", [])
        err_key = "jobs" if self.mode == "active" else "recent_jobs"

        if (err := snapshot.errors.get(err_key)) or not jobs:
            self._row_keys.clear()
            table.clear()
            n_cols = len(self._current_columns)
            if err:
                table.add_row(Text(f"⚠ {err}", style="bold red"), *([""] * (n_cols - 1)))
            else:
                kind = "active" if self.mode == "active" else "recent"
                table.add_row(Text(f"— no {kind} jobs —", style="dim italic"), *([""] * (n_cols - 1)))
            return

        matcher = make_matcher(self.filter_text, list(_JOB_FILTER_DEFAULT_FIELDS))
        jobs = [j for j in jobs if matcher(_job_filter_record(j))]
        jobs.sort(key=lambda j: _sort_value(j, self._sort_col), reverse=self._sort_reverse)

        current_sort = (self._sort_col, self._sort_reverse)
        if self._applied_sort != current_sort or (not self._row_keys and table.row_count > 0):
            table.clear()
            self._row_keys.clear()
            self._applied_sort = current_sort

        new_keys: set[str] = set()
        mut_count = 0
        for j in jobs:
            row = self._row_for_job(j, self.mode)
            new_keys.add(j.job_id)
            if j.job_id in self._row_keys:
                rk = self._row_keys[j.job_id]
                for col_key, val in row.items():
                    try:
                        table.update_cell(rk, col_key, val, update_width=False)
                    except Exception:
                        pass
            else:
                values = [row[k] for k, _l, _w, _s in self._current_columns]
                rk = table.add_row(*values, key=j.job_id)
                self._row_keys[j.job_id] = rk
            mut_count += 1
            if mut_count % 50 == 0:
                await asyncio.sleep(0)

        for stale_key in list(self._row_keys.keys() - new_keys):
            try:
                table.remove_row(self._row_keys[stale_key])
            except Exception:
                pass
            del self._row_keys[stale_key]
            mut_count += 1
            if mut_count % 50 == 0:
                await asyncio.sleep(0)

        if not jobs:
            n_cols = len(self._current_columns)
            table.add_row(Text(f"— no jobs match '{self.filter_text}' —", style="dim italic"),
                          *([""] * (n_cols - 1)))

        try:
            table.cursor_coordinate = prev_cursor
            table.scroll_to(x=prev_scroll_x, y=prev_scroll_y, animate=False)
        except Exception:
            pass

    def watch_filter_text(self, _old: str, _new: str) -> None:
        # No-op: debounce in on_input_changed handles the rebuild. The
        # reactive write still fires this watcher, but driving the
        # synchronous rebuild here was the root cause of filter-bar
        # input drops.
        return

    def watch_mine_only(self, _old: bool, new: bool) -> None:
        if not self.is_mounted:
            return
        pill = self.query_one("#pill_mine", Static)
        if new:
            pill.add_class("-active")
        else:
            pill.remove_class("-active")
        # Mine-only is applied server-side via `-u $USER` — flip the App
        # flag and kick a fresh fetch. _refresh_all is @work-decorated on
        # the App; calling it spawns the worker (and cancels any prior
        # in-flight tick via exclusive=True, group="collect").
        try:
            self.app.mine_only = new                 # type: ignore[attr-defined]
            self.app._refresh_all()                  # type: ignore[attr-defined]
        except Exception:
            pass

    # ── sort ────────────────────────────────────────────────

    def on_sortable_header_sort_changed(self, event: SortableHeader.SortChanged) -> None:
        col = event.col
        if col == self._sort_col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = col in ("job_id", "cpu", "gpu", "mem", "nodes", "time", "left")
        self.query_one(SortableHeader).set_sort(self._sort_col, "free", self._sort_reverse)
        if self._last_snapshot is not None:
            self.update_snapshot(self._last_snapshot)

    # ── data ────────────────────────────────────────────────

    def _rebuild_columns(self, cols) -> None:
        table = self.query_one("#jobs_table_dt", DataTable)
        table.clear(columns=True)
        self._row_keys.clear()
        for key, _label, width, _kind in cols:
            table.add_column("", key=key, width=width)
        self._current_columns = cols

    def _row_for_job(self, j: Job, mode: str) -> dict[str, Text | str]:
        if mode == "active":
            return {
                "job_id": j.job_id,
                "partition": j.partition,
                "name": _truncate(j.name, 26),
                "user": j.user,
                "state": _state_text(j.state),
                "node": _truncate(j.node_or_reason, 22),
                "time": j.time_used,
                "left": j.time_left,
                "nodes": str(j.num_nodes),
                "cpu": str(j.num_cpus),
                "gpu": j.alloc_gpu,
                "mem": j.alloc_mem,
            }
        return {
            "job_id": j.job_id,
            "partition": j.partition,
            "name": _truncate(j.name, 26),
            "user": j.user,
            "state": _state_text(j.state),
            "node": _truncate(j.node_or_reason, 22),
            "time": j.time_used,
            "cpu": str(j.num_cpus),
            "gpu": j.alloc_gpu,
            "mem": j.alloc_mem,
        }

    async def update_snapshot(self, snapshot: Snapshot) -> None:
        """Async + chunked rebuild — yields the event loop every 50 row
        mutations so input dispatch isn't held during a refresh tick.
        See _apply_filter_async for the same pattern in the filter path.
        """
        self._last_snapshot = snapshot
        table = self.query_one("#jobs_table_dt", DataTable)
        prev_scroll_x = table.scroll_x
        prev_scroll_y = table.scroll_y
        prev_cursor = table.cursor_coordinate

        jobs: list[Job] = snapshot.jobs if self.mode == "active" else getattr(snapshot, "recent_jobs", [])
        err_key = "jobs" if self.mode == "active" else "recent_jobs"

        if (err := snapshot.errors.get(err_key)) or not jobs:
            self._row_keys.clear()
            table.clear()
            n_cols = len(self._current_columns)
            if err:
                table.add_row(Text(f"⚠ {err}", style="bold red"), *([""] * (n_cols - 1)))
            else:
                kind = "active" if self.mode == "active" else "recent"
                table.add_row(Text(f"— no {kind} jobs —", style="dim italic"), *([""] * (n_cols - 1)))
            return

        matcher = make_matcher(self.filter_text, list(_JOB_FILTER_DEFAULT_FIELDS))
        jobs = [j for j in jobs if matcher(_job_filter_record(j))]
        # (Mine-only is applied server-side via `-u $USER` in the collector,
        # driven by App.mine_only.  No client-side filter here.)
        jobs.sort(key=lambda j: _sort_value(j, self._sort_col), reverse=self._sort_reverse)

        current_sort = (self._sort_col, self._sort_reverse)
        if self._applied_sort != current_sort or (not self._row_keys and table.row_count > 0):
            table.clear()
            self._row_keys.clear()
            self._applied_sort = current_sort

        new_keys: set[str] = set()
        mut_count = 0
        for j in jobs:
            row = self._row_for_job(j, self.mode)
            new_keys.add(j.job_id)
            if j.job_id in self._row_keys:
                rk = self._row_keys[j.job_id]
                for col_key, val in row.items():
                    try:
                        table.update_cell(rk, col_key, val, update_width=False)
                    except Exception:
                        pass
            else:
                values = [row[k] for k, _l, _w, _s in self._current_columns]
                rk = table.add_row(*values, key=j.job_id)
                self._row_keys[j.job_id] = rk
            mut_count += 1
            if mut_count % 50 == 0:
                await asyncio.sleep(0)

        for stale_key in list(self._row_keys.keys() - new_keys):
            try:
                table.remove_row(self._row_keys[stale_key])
            except Exception:
                pass
            del self._row_keys[stale_key]
            mut_count += 1
            if mut_count % 50 == 0:
                await asyncio.sleep(0)

        if not jobs:
            n_cols = len(self._current_columns)
            table.add_row(Text(f"— no jobs match '{self.filter_text}' —", style="dim italic"),
                          *([""] * (n_cols - 1)))

        try:
            table.cursor_coordinate = prev_cursor
            table.scroll_to(x=prev_scroll_x, y=prev_scroll_y, animate=False)
        except Exception:
            pass
