"""Jobs DataTable with active/recent toggle, sort, and filter."""
from __future__ import annotations

import asyncio
import os
import re
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
from ..perf import perf_block, perf_enabled, perf_log
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


# Phase 4d.2-E step F.2: sacct rows can carry non-numeric job IDs —
# array tasks ("1234_5"), step IDs ("1234.batch"), het-job components
# ("1234+0"). Prior `int(job.job_id)` raised ValueError on those,
# which the outer `except Exception` swallowed by falling through to
# `return job.job_id` (a STRING). Result: `jobs.sort` mixed `int` and
# `str` and raised `TypeError: '<' not supported between instances of
# 'str' and 'int'`. The exception was then swallowed by the broadcast
# loop and the table just stopped updating. Caught live on LRZ Recent
# + mine_only=False where ~50 sacct rows reliably included array tasks.
_JOBID_LEADING_INT_RE = re.compile(r"^(\d+)")


def _sort_value(job: Job, col: str):
    try:
        if col == "job_id":
            # Parse the leading integer prefix; sort by (prefix, full
            # string) so all rows return tuples that compare cleanly
            # regardless of suffix shape. Pure-numeric IDs land as
            # (N, "N") which still orders by the numeric part first.
            jid = job.job_id or ""
            m = _JOBID_LEADING_INT_RE.match(jid)
            return (int(m.group(1)) if m else -1, jid)
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
    JobsTable .empty_placeholder {
        display: none;
        height: 1fr;
        min-height: 1;
        width: 100%;
        padding: 1 2;
    }
    JobsTable .empty_placeholder.-visible { display: block; }
    JobsTable DataTable.-hidden { display: none; }
    JobsTable SortableHeader.-hidden { display: none; }
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
            # Full-width empty-state placeholder. Rendering "— no active
            # jobs —" inside the DataTable's first column truncated it
            # to "— no acti…" on narrow Job columns. Show this Static
            # instead — hides the table + header while visible so the
            # message renders at the widget's full width.
            yield Static("", id="empty_placeholder", classes="empty_placeholder")

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
        # Phase 4d.2-E step 2.5: this watcher runs on the sync click
        # path — column rebuild + header remount + sync update_snapshot
        # call. Time the whole thing so we can see if mode flip itself
        # is the latency hotspot vs. the broadcast.
        with perf_block("click", "mode_watcher", extra=f"new={new}"):
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
            # Phase 4d.2-E step H: do NOT call `self.update_snapshot(...)`
            # here — it's async, so the coroutine would be silently
            # dropped from this sync watcher (caught by blackbox v2 as
            # the 10s `a → t` bug). Instead, flip the App-level `mode`
            # reactive, which fires `App.watch_mode` → re-broadcast →
            # this widget's `update_snapshot` runs cleanly under the
            # broadcast worker.
            try:
                self.app.mode = new                  # type: ignore[attr-defined]
            except Exception:
                pass

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
            # Fallback (no event loop / test path): formerly called
            # `self.update_snapshot(self._last_snapshot)` synchronously,
            # which silently dropped the coroutine (Phase 4d.2-E step H).
            # The legitimate test path runs its own pilot driver and
            # doesn't hit this fallback in practice; if a future test
            # NEEDS a synchronous rebuild it should call the helper
            # directly via `await widget.update_snapshot(snap)` inside
            # its own pilot.pause()-bracketed sequence.
            pass

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
        mine_only = self._app_mine_only()

        if (err := snapshot.errors.get(err_key)) or not jobs:
            self._row_keys.clear()
            table.clear()
            if err:
                self._clear_empty_placeholder()
                n_cols = len(self._current_columns)
                table.add_row(Text(f"⚠ {err}", style="bold red"), *([""] * (n_cols - 1)))
            else:
                self._render_empty_placeholder(
                    self._empty_state_message(mine_only, snapshot.cluster_user)
                )
            return

        matcher = make_matcher(self.filter_text, list(_JOB_FILTER_DEFAULT_FIELDS))
        jobs = [j for j in jobs if matcher(_job_filter_record(j))]
        # Phase 4d.2-D: client-side mine_only filter. Read from
        # `app.mine_only` (the App's reactive — single source of truth)
        # so flipping it via any path triggers consistent filtering
        # across JobsTable + CompactJobs. cluster_user is stamped on
        # each snapshot by app._refresh_all (resolved REMOTE whoami).
        # On cold start (cluster_user == "") fall through to "show
        # everything" — better than blocking the table behind an async
        # whoami probe.
        if mine_only and snapshot.cluster_user:
            jobs = [j for j in jobs if j.user == snapshot.cluster_user]
        jobs.sort(key=lambda j: _sort_value(j, self._sort_col), reverse=self._sort_reverse)

        current_sort = (self._sort_col, self._sort_reverse)
        if self._applied_sort != current_sort or (not self._row_keys and table.row_count > 0):
            table.clear()
            self._row_keys.clear()
            self._applied_sort = current_sort

        # Phase 4d.2-E step 2.5: per-chunk telemetry — see same block in
        # `update_snapshot` for the rationale. Both paths share the loop
        # shape; both emit `filter/<path>_<mode>` rows so the breakdown
        # awk can split them.
        new_keys: set[str] = set()
        mut_count = 0
        chunks = 0
        max_gap_ms = 0.0
        first_50_ms: float | None = None
        rebuild_t0 = time.perf_counter() if perf_enabled() else 0.0
        last_yield = rebuild_t0
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
            if perf_enabled() and first_50_ms is None and mut_count >= 50:
                first_50_ms = (time.perf_counter() - rebuild_t0) * 1000
            if mut_count % 50 == 0:
                if perf_enabled():
                    now = time.perf_counter()
                    gap = (now - last_yield) * 1000
                    if gap > max_gap_ms:
                        max_gap_ms = gap
                    last_yield = now
                chunks += 1
                await asyncio.sleep(0)

        for stale_key in list(self._row_keys.keys() - new_keys):
            try:
                table.remove_row(self._row_keys[stale_key])
            except Exception:
                pass
            del self._row_keys[stale_key]
            mut_count += 1
            if mut_count % 50 == 0:
                if perf_enabled():
                    now = time.perf_counter()
                    gap = (now - last_yield) * 1000
                    if gap > max_gap_ms:
                        max_gap_ms = gap
                    last_yield = now
                chunks += 1
                await asyncio.sleep(0)

        if perf_enabled():
            total_ms = (time.perf_counter() - rebuild_t0) * 1000
            if first_50_ms is not None:
                extra = (
                    f"rows={len(jobs)} chunks={chunks} "
                    f"max_gap_ms={max_gap_ms:.1f} first_50_ms={first_50_ms:.1f}"
                )
            else:
                extra = (
                    f"rows={len(jobs)} chunks={chunks} "
                    f"max_gap_ms={max_gap_ms:.1f} first_50_ms=NA"
                )
            perf_log("filter", f"apply_filter_async_{self.mode}", total_ms, extra=extra)

        if not jobs:
            self._render_empty_placeholder(
                self._empty_state_message(mine_only, snapshot.cluster_user)
            )
        else:
            self._clear_empty_placeholder()

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

    def _app_mine_only(self) -> bool:
        """Phase 4d.2-D: read mine_only from the App (single source of
        truth). Falls back to the local JobsTable reactive if the App
        attribute isn't available (test harnesses that mount the
        widget without an App)."""
        try:
            return bool(self.app.mine_only)          # type: ignore[attr-defined]
        except Exception:
            return self.mine_only

    def watch_mine_only(self, _old: bool, new: bool) -> None:
        if not self.is_mounted:
            return
        pill = self.query_one("#pill_mine", Static)
        if new:
            pill.add_class("-active")
        else:
            pill.remove_class("-active")
        # Phase 4d.2-D: propagate the JobsTable's local state to
        # App.mine_only (which is a reactive the App watches). The App's
        # `watch_mine_only` re-broadcasts the cached snapshot to every
        # widget so this JobsTable AND CompactJobs repaint with the new
        # filter at the same moment. NO refetch.
        try:
            self.app.mine_only = new                 # type: ignore[attr-defined]
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
        # Phase 4d.2-E step H: see same comment in `watch_mode`. Flip
        # the App-level `sort` reactive instead of calling the async
        # update_snapshot from this sync handler.
        try:
            self.app.sort = (self._sort_col, self._sort_reverse)   # type: ignore[attr-defined]
        except Exception:
            pass

    # ── data ────────────────────────────────────────────────

    def _empty_state_message(self, mine_only: bool, cluster_user: str) -> str:
        """Parametrized empty/no-match placeholder text by mode + filter.

        active mode → "no active jobs"; recent mode → "no recent jobs".
        Filter-mismatch and mine_only branches keep the existing
        breadcrumbs so the user can tell WHY the table is empty.
        """
        kind = "active" if self.mode == "active" else "recent"
        if self.filter_text:
            suffix = " (mine only)" if mine_only and cluster_user else ""
            return f"— no jobs match '{self.filter_text}'{suffix} —"
        if mine_only and cluster_user:
            return f"— no {kind} jobs for {cluster_user} —"
        return f"— no {kind} jobs —"

    def _render_empty_placeholder(self, msg: str) -> None:
        """Show full-width empty Static; hide DataTable + SortableHeader.
        Caller is responsible for clearing any stale rows from the table
        (its rows are invisible while hidden but the row_count survives)."""
        try:
            placeholder = self.query_one("#empty_placeholder", Static)
            placeholder.update(Text(msg, style="dim italic"))
            placeholder.add_class("-visible")
            self.query_one("#jobs_table_dt", DataTable).add_class("-hidden")
            self.query_one(SortableHeader).add_class("-hidden")
        except Exception:
            pass

    def _clear_empty_placeholder(self) -> None:
        """Hide the empty-state Static; re-show DataTable + SortableHeader."""
        try:
            placeholder = self.query_one("#empty_placeholder", Static)
            placeholder.remove_class("-visible")
            self.query_one("#jobs_table_dt", DataTable).remove_class("-hidden")
            self.query_one(SortableHeader).remove_class("-hidden")
        except Exception:
            pass

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
        mine_only = self._app_mine_only()

        if (err := snapshot.errors.get(err_key)) or not jobs:
            self._row_keys.clear()
            table.clear()
            if err:
                self._clear_empty_placeholder()
                n_cols = len(self._current_columns)
                table.add_row(Text(f"⚠ {err}", style="bold red"), *([""] * (n_cols - 1)))
            else:
                self._render_empty_placeholder(
                    self._empty_state_message(mine_only, snapshot.cluster_user)
                )
            return

        matcher = make_matcher(self.filter_text, list(_JOB_FILTER_DEFAULT_FIELDS))
        jobs = [j for j in jobs if matcher(_job_filter_record(j))]
        # Phase 4d.2-D: client-side mine_only — see `_apply_filter_async`
        # for the same predicate; both paths must filter identically so
        # the table content matches whichever rebuild path landed last.
        if mine_only and snapshot.cluster_user:
            jobs = [j for j in jobs if j.user == snapshot.cluster_user]
        jobs.sort(key=lambda j: _sort_value(j, self._sort_col), reverse=self._sort_reverse)

        current_sort = (self._sort_col, self._sort_reverse)
        if self._applied_sort != current_sort or (not self._row_keys and table.row_count > 0):
            table.clear()
            self._row_keys.clear()
            self._applied_sort = current_sort

        # Phase 4d.2-E step 2.5: per-chunk telemetry. Track when the
        # FIRST 50 rows are visible (the user's "first content"
        # threshold) AND the max yield-gap (longest stretch the loop
        # ran without yielding to input dispatch).
        new_keys: set[str] = set()
        mut_count = 0
        chunks = 0
        max_gap_ms = 0.0
        first_50_ms: float | None = None
        rebuild_t0 = time.perf_counter() if perf_enabled() else 0.0
        last_yield = rebuild_t0
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
            if perf_enabled() and first_50_ms is None and mut_count >= 50:
                first_50_ms = (time.perf_counter() - rebuild_t0) * 1000
            if mut_count % 50 == 0:
                if perf_enabled():
                    now = time.perf_counter()
                    gap = (now - last_yield) * 1000
                    if gap > max_gap_ms:
                        max_gap_ms = gap
                    last_yield = now
                chunks += 1
                await asyncio.sleep(0)

        for stale_key in list(self._row_keys.keys() - new_keys):
            try:
                table.remove_row(self._row_keys[stale_key])
            except Exception:
                pass
            del self._row_keys[stale_key]
            mut_count += 1
            if mut_count % 50 == 0:
                if perf_enabled():
                    now = time.perf_counter()
                    gap = (now - last_yield) * 1000
                    if gap > max_gap_ms:
                        max_gap_ms = gap
                    last_yield = now
                chunks += 1
                await asyncio.sleep(0)

        if perf_enabled():
            total_ms = (time.perf_counter() - rebuild_t0) * 1000
            if first_50_ms is not None:
                extra = (
                    f"rows={len(jobs)} chunks={chunks} "
                    f"max_gap_ms={max_gap_ms:.1f} first_50_ms={first_50_ms:.1f}"
                )
            else:
                extra = (
                    f"rows={len(jobs)} chunks={chunks} "
                    f"max_gap_ms={max_gap_ms:.1f} first_50_ms=NA"
                )
            perf_log("filter", f"update_snapshot_{self.mode}", total_ms, extra=extra)

        if not jobs:
            self._render_empty_placeholder(
                self._empty_state_message(mine_only, snapshot.cluster_user)
            )
        else:
            self._clear_empty_placeholder()

        try:
            table.cursor_coordinate = prev_cursor
            table.scroll_to(x=prev_scroll_x, y=prev_scroll_y, animate=False)
        except Exception:
            pass
