from __future__ import annotations

import asyncio
import gc
from pathlib import Path
from typing import Callable

from textual import events
from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from collections import deque

from .collectors import slurm, storage
from .collectors.models import Snapshot, StorageEntry, UtilizationSample
from .config import Config, load as load_config
from .exec import Executor
from .perf import perf_block, perf_enabled, perf_log
from .widgets.jobs_table import JobsTable
from .widgets.nodes_table import NodesSummary, NodesTable
from .widgets.overview import AsciiArt, CompactJobs, HomeStorage, OverviewPanel
from .widgets.storage_panel import StoragePanel
from .widgets.utilization_panel import UtilizationPanel


HISTORY_CAP = 60


class RohanBoardApp(App):
    TITLE = "rohanboard"
    SUB_TITLE = "SLURM cluster dashboard"
    # Tighten the default 0.5 s so a lazy double-click (i.e. two slow,
    # deliberate presses) doesn't get treated as a double-click.
    CLICK_CHAIN_TIME_THRESHOLD = 0.35

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        # Jobs-specific actions — hidden and disabled off the Jobs tab via
        # `check_action` below.
        ("a", "jobs_toggle", "Active/Recent"),
        ("l", "jobs_tail_log", "Tail log"),
    ]

    snapshot: reactive[Snapshot] = reactive(Snapshot, recompose=False, layout=False)

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self.cfg = config or load_config()
        self._history: deque[UtilizationSample] = deque(maxlen=HISTORY_CAP)
        # `mine_only` drives whether `-u $USER` is passed to squeue/sacct.
        # The "Mine only" pill in the Jobs tab flips this and triggers a
        # fresh fetch — no need for client-side filtering.
        self.mine_only: bool = True
        # Phase 4c: collectors take an Executor.  Picker is config-driven:
        #   exec = "local"      → LocalExecutor (default)
        #   exec = "ssh:<host>" → AsyncSSHExecutor(host=<host>) — uses
        #                          ~/.ssh/config for resolution.
        # Lazy-connect on first run() (cold-handshake hits during initial
        # _refresh_all on mount; the spinner / "loading…" state covers it).
        # `on_unmount` calls aclose() — works for both Local (no-op) and
        # AsyncSSH (closes the persistent connection).
        self.executor: Executor = self.cfg.build_executor()
        # Widget factories can reference per-widget config (e.g. filter presets).
        node_presets = self.cfg.presets.get("nodes", [])
        job_presets = self.cfg.presets.get("jobs", [])
        self._widget_factories: dict[str, Callable[[], Widget]] = {
            "storage_panel":     StoragePanel,
            "jobs_table":        lambda: JobsTable(presets=job_presets),
            "nodes_table":       lambda: NodesTable(presets=node_presets),
            "nodes_summary":     NodesSummary,
            "utilization_panel": UtilizationPanel,
            "overview_panel":    OverviewPanel,
            "home_storage":      HomeStorage,
            "compact_jobs":      CompactJobs,
            "ascii_art":         AsciiArt,
        }
        # Dynamic numbered bindings for the configured tabs (1..9).
        for i, tab in enumerate(self.cfg.layout.tabs[:9], start=1):
            self.bind(str(i), f"show_tab('{tab.id}')", description=tab.title)
        # Resolve theme path: relative to package styles/ unless absolute.
        theme_path = Path(self.cfg.theme.file)
        if not theme_path.is_absolute():
            theme_path = Path(__file__).parent / "styles" / theme_path
        self.CSS_PATH = theme_path  # type: ignore[assignment]

    # ────────────────────────────────────────────────────────────────────
    # composition
    # ────────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        if not self.cfg.layout.tabs:
            yield Static("No tabs configured.")
            yield Footer()
            return
        with TabbedContent(initial=self.cfg.layout.tabs[0].id):
            for tab in self.cfg.layout.tabs:
                with TabPane(tab.title, id=tab.id):
                    with ScrollableContainer(classes="tab-body"):
                        for widget_id in tab.widgets:
                            factory = self._widget_factories.get(widget_id)
                            if factory is None:
                                yield Static(f"[red]Unknown widget: {widget_id}[/red]")
                            else:
                                yield factory()
        yield Footer()

    # ────────────────────────────────────────────────────────────────────
    # lifecycle + refresh
    # ────────────────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        self._apply_size_class(self.size.width)
        await self._refresh_all()
        # Use the slowest (storage) interval as the master tick; collectors
        # below it skip work via their own elapsed-since-last logic in v1
        # we just refresh everything on the shortest interval.
        interval = max(min(self.cfg.refresh.slurm_jobs, self.cfg.refresh.slurm_nodes,
                           self.cfg.refresh.storage), 1)
        # run_worker(exclusive=True) — guards against overlapping ticks if a
        # previous refresh is still draining.
        self.set_interval(interval, self._refresh_all_bg)
        # Freeze the GC generations for everything allocated during
        # startup (widget tree, caches, Styles back-references).  This
        # excludes them from future gen-2 scans, which were the root
        # cause of 50–200 ms stutters in the animation widgets.
        # See https://github.com/Textualize/textual/issues/6381
        gc.collect()
        gc.freeze()

    async def on_unmount(self) -> None:
        # Close the executor's persistent resources (no-op for LocalExecutor;
        # for AsyncSSHExecutor in Phase 4e+ this closes the asyncssh
        # connection so the process exits cleanly on quit).
        try:
            await self.executor.aclose()
        except Exception:
            pass

    async def action_refresh(self) -> None:
        self.notify("Refreshing…", timeout=1)
        await self._refresh_all()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Scope tab-specific actions to their tab.

        Textual Screen.active_bindings semantics:
          * True  → shown and active
          * None  → shown but disabled (greyed)
          * False → hidden entirely
        We want Jobs-only shortcuts to *disappear* on other tabs, not grey
        out, so we return False off-tab.
        """
        if action in ("jobs_toggle", "jobs_tail_log"):
            try:
                active = self.query_one(TabbedContent).active
            except Exception:
                active = None
            if active != "jobs":
                return False
        return True

    def on_tabbed_content_tab_activated(self, event) -> None:  # type: ignore[no-untyped-def]
        # Refresh footer / key dispatcher when user switches tabs so the
        # hidden shortcuts appear/disappear.
        self.refresh_bindings()

    def _visible_jobs_table(self) -> JobsTable | None:
        """Return the JobsTable on the *currently active* tab, or None if
        the Jobs tab isn't active.  No fallback — we don't want `a`/`l`
        to reach into a Jobs table from another tab's context."""
        try:
            active = self.query_one(TabbedContent).active
        except Exception:
            return None
        for w in self.query(JobsTable):
            pane = w.parent
            while pane is not None and not isinstance(pane, TabPane):
                pane = pane.parent
            if isinstance(pane, TabPane) and pane.id == active:
                return w
        return None

    def action_jobs_toggle(self) -> None:
        if jt := self._visible_jobs_table():
            jt.action_toggle_mode()

    def action_jobs_tail_log(self) -> None:
        if jt := self._visible_jobs_table():
            jt.action_tail_log()

    # ────────────────────────────────────────────────────────────────────
    # data
    # ────────────────────────────────────────────────────────────────────

    def _refresh_all_bg(self) -> None:
        self.run_worker(self._refresh_all(), exclusive=True, name="refresh_all")

    async def _refresh_all(self) -> None:
        snap = Snapshot()

        users_for_fetch = ["self"] if self.mine_only else ["all"]

        async def grab_jobs():
            try:
                with perf_block("collect", "jobs"):
                    snap.jobs = await slurm.fetch_jobs(self.executor, users_for_fetch)
            except Exception as e:
                snap.errors["jobs"] = str(e)

        async def grab_recent():
            try:
                with perf_block("collect", "recent"):
                    snap.recent_jobs = await slurm.fetch_recent_jobs(self.executor, users_for_fetch)
            except Exception as e:
                snap.errors["recent_jobs"] = str(e)

        async def grab_nodes():
            try:
                with perf_block("collect", "nodes"):
                    nodes = await slurm.fetch_nodes(self.executor)
                if self.cfg.slurm.include_partitions:
                    inc = set(self.cfg.slurm.include_partitions)
                    nodes = [n for n in nodes if any(p in inc for p in n.partitions)]
                if self.cfg.slurm.exclude_partitions:
                    exc = set(self.cfg.slurm.exclude_partitions)
                    nodes = [n for n in nodes if not any(p in exc for p in n.partitions)]
                snap.nodes = nodes
            except Exception as e:
                snap.errors["nodes"] = str(e)

        async def grab_storage():
            with perf_block("collect", "storage"):
                await _grab_storage_impl()

        async def _grab_storage_impl():
            entries: list[StorageEntry] = []
            for entry_cfg in self.cfg.storage_entries:
                try:
                    if entry_cfg.kind == "quota":
                        e = await storage.fetch_quota(self.executor, entry_cfg.label, entry_cfg.filesystem)
                        if e is not None:
                            entries.append(e)
                    elif entry_cfg.kind == "df":
                        if not entry_cfg.path:
                            continue
                        e = await storage.fetch_df(self.executor, entry_cfg.label, entry_cfg.path)
                        if e is not None:
                            entries.append(e)
                    elif entry_cfg.kind == "auto":
                        prefixes = entry_cfg.prefixes or ["/cluster", "/cluster_HDD"]
                        discovered = storage.discover_mounts(prefixes)
                        # df all of them in parallel
                        results = await asyncio.gather(
                            *[storage.fetch_df(self.executor, label, path) for label, path in discovered],
                            return_exceptions=True,
                        )
                        for r in results:
                            if isinstance(r, StorageEntry):
                                entries.append(r)
                except Exception as e:
                    snap.errors[f"storage:{entry_cfg.label}"] = str(e)
            snap.storage = entries

        with perf_block("refresh", "gather",
                         extra=f"jobs={len(snap.jobs)}"):
            await asyncio.gather(grab_jobs(), grab_recent(), grab_nodes(), grab_storage())

        # Append to rolling history (only if we got node data).
        if snap.nodes:
            cpu_total = sum(n.cpu_total for n in snap.nodes) or 1
            cpu_alloc = sum(n.cpu_alloc for n in snap.nodes)
            mem_total = sum(n.mem_total_mb for n in snap.nodes) or 1
            mem_alloc = sum(n.mem_alloc_mb for n in snap.nodes)
            gpu_total = sum(g.total for n in snap.nodes for g in n.gpus) or 1
            gpu_alloc = sum(g.alloc for n in snap.nodes for g in n.gpus)
            self._history.append(UtilizationSample(
                cpu=cpu_alloc / cpu_total,
                gpu=gpu_alloc / gpu_total,
                mem=mem_alloc / mem_total,
            ))
        snap.history = list(self._history)
        # Reactive write — synchronously triggers watch_snapshot fan-out.
        with perf_block("refresh", "reactive_set"):
            self.snapshot = snap

    # ────────────────────────────────────────────────────────────────────
    # responsive
    # ────────────────────────────────────────────────────────────────────

    def on_resize(self, event: events.Resize) -> None:
        self._apply_size_class(event.size.width)

    def _apply_size_class(self, width: int) -> None:
        screen = self.screen
        for cls in ("narrow", "medium", "wide"):
            screen.remove_class(cls)
        if width < 90:
            screen.add_class("narrow")
        elif width >= 130:
            screen.add_class("wide")
        else:
            screen.add_class("medium")

    def watch_snapshot(self, _old: Snapshot, new: Snapshot) -> None:
        # Defer the actual fan-out to an async broadcast so we can yield
        # to the event loop between each widget — letting animation
        # frames interleave with the refresh instead of starving them.
        self.run_worker(self._broadcast_snapshot(new),
                        exclusive=True, name="broadcast_snapshot")

    async def _broadcast_snapshot(self, new: Snapshot) -> None:
        """Push `new` into every widget that implements update_snapshot,
        yielding to the loop between each so the 20 FPS animation timer
        can fire in between.

        `batch_update()` coalesces the resulting screen refreshes into a
        single paint once the fan-out completes.
        """
        import time as _t
        with perf_block("fanout", "all"), self.batch_update():
            for widget in list(self.query("*")):
                handler = getattr(widget, "update_snapshot", None)
                if not callable(handler):
                    continue
                if perf_enabled():
                    t0 = _t.perf_counter()
                    try:
                        handler(new)
                    except Exception:
                        perf_log("widget", type(widget).__name__,
                                 (_t.perf_counter() - t0) * 1000, extra="error")
                        continue
                    perf_log("widget", type(widget).__name__,
                             (_t.perf_counter() - t0) * 1000)
                else:
                    try:
                        handler(new)
                    except Exception:
                        import traceback
                        self.log(f"update_snapshot failed for {widget!r}\n{traceback.format_exc()}")
                # Sleep ~1 ms — long enough that the 50 ms (20 FPS)
                # animation timer has a real chance to fire between
                # widget updates rather than getting starved by a tight
                # `sleep(0)` loop.
                await asyncio.sleep(0.001)
