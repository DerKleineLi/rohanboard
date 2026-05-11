from __future__ import annotations

import asyncio
import gc
import os
import time as _time
from pathlib import Path
from typing import Callable

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from collections import deque

from .collectors import slurm, storage
from .collectors.combined import fetch_combined
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
        # Phase 4d.2-D: `a` toggles mine_only (instant, client-side); `t`
        # toggles Active vs. Recent mode (was on `a` pre-Phase-4d.2-D).
        ("a", "jobs_mine_toggle", "Mine/All"),
        ("t", "jobs_toggle", "Active/Recent"),
        ("l", "jobs_tail_log", "Tail log"),
    ]

    snapshot: reactive[Snapshot] = reactive(Snapshot, recompose=False, layout=False)
    # Phase 4d.2-D: mine_only is now a CLIENT-side filter — JobsTable and
    # CompactJobs read it at render time and slice the in-memory snapshot.
    # Toggling fires `watch_mine_only` below, which re-broadcasts the
    # cached snapshot so every subscribing widget repaints with the new
    # filter; no refetch. `init=False` skips the watcher call on initial
    # default-value handling — the first real snapshot fires the
    # broadcast via `watch_snapshot`; if we let the mine_only watcher
    # fire on init it races watch_snapshot for the same `exclusive=True`
    # worker slot and one of the two coroutines leaks unawaited.
    mine_only: reactive[bool] = reactive(True, recompose=False, layout=False, init=False)
    # Phase 4d.2-E step H: mode + sort are now ALSO app-level reactives.
    # Same architectural rationale as mine_only — a JobsTable click on
    # the Active/Recent pill (or `t` key) or a column-header sort needs
    # to fire `_broadcast_snapshot` instead of trying to drive its own
    # async `update_snapshot(...)` from a sync watcher (which silently
    # dropped the coroutine — caught by the blackbox v2 report).
    # Defaults must match JobsTable's own defaults (mode="active",
    # sort=("job_id", reverse=True)) so the first reactive write doesn't
    # broadcast a no-op flip. `init=False` skips the watcher on initial
    # default-value handling (avoids racing watch_snapshot for the same
    # exclusive worker slot — same gotcha that caught mine_only at first
    # rollout).
    mode: reactive[str] = reactive("active", recompose=False, layout=False, init=False)
    sort: reactive[tuple[str, bool]] = reactive(
        ("job_id", True), recompose=False, layout=False, init=False,
    )
    # Bundle-2 B2.4: NodesTable sort is independent of JobsTable sort
    # (different column keys + triple-kind metric dimension). Keep
    # them as separate reactives so a NodesTable header click doesn't
    # spuriously fire JobsTable's watcher. Default matches NodesTable's
    # own (name, free, False).
    nodes_sort: reactive[tuple[str, str, bool]] = reactive(
        ("name", "free", False), recompose=False, layout=False, init=False,
    )

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        self.cfg = config or load_config()
        self._history: deque[UtilizationSample] = deque(maxlen=HISTORY_CAP)
        # Bundle-3 B3.1: per-collector sticky "first successful parse"
        # flags. Set True the first tick each collector's parse landed
        # cleanly; once True, stays True across subsequent failures
        # (transient timeouts don't regress a populated widget to
        # "loading…"). Stamped onto each snapshot so widgets can gate
        # their loading-vs-empty placeholder on their own collector.
        self._jobs_loaded: bool = False
        self._nodes_loaded: bool = False
        self._storage_loaded: bool = False
        # Phase 4d.2-E: --debug CLI flag opens a per-tick text log file
        # the user can `tail -f` outside the TUI. Path comes from the
        # ROHANBOARD_DEBUG_LOG env var (set by __main__.py when --debug
        # is passed). When unset, `_debug_log` is a no-op.
        self._debug_log_path: Path | None = None
        env_log = os.environ.get("ROHANBOARD_DEBUG_LOG")
        if env_log:
            self._debug_log_path = Path(env_log).expanduser()
            try:
                self._debug_log_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                self._debug_log_path = None
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
        node_extra_columns = list(self.cfg.nodes_table.columns)
        self._widget_factories: dict[str, Callable[[], Widget]] = {
            "storage_panel":     StoragePanel,
            "jobs_table":        lambda: JobsTable(presets=job_presets),
            "nodes_table":       lambda: NodesTable(
                presets=node_presets,
                extra_columns=node_extra_columns,
            ),
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

    def _debug_log(self, msg: str) -> None:
        """Phase 4d.2-E: append a timestamped line to the --debug log
        file. No-op when --debug isn't set. Tee'd to Textual's own
        `self.log()` so the TUI's devtools console sees it too."""
        try:
            self.log(msg)
        except Exception:
            pass
        if self._debug_log_path is None:
            return
        try:
            with self._debug_log_path.open("a") as f:
                f.write(f"{_time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

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
        # Bundle-1 Sub-fix-3: whoami no longer blocks on_mount. The
        # combined collector includes a `whoami` section so REMOTE
        # username arrives WITH the first tick instead of one ssh
        # round-trip BEFORE it. Cold LRZ ProxyJump was ~10 s for the
        # standalone whoami + ~10 s for the first tick = ~20 s blank
        # window before any frame; this collapses both into one ~10 s
        # window. Mine_only filter on the very first frame falls
        # through to "show everything" until whoami parses (existing
        # cold-start behavior — `test_cold_start_no_cluster_user_shows_everything`).
        # Kick off the first refresh as a worker (decorator-driven); UI
        # paints the empty snapshot immediately and the first real tick
        # writes self.snapshot when it lands.
        self._refresh_all()
        # Use the slowest (storage) interval as the master tick; collectors
        # below it skip work via their own elapsed-since-last logic in v1
        # we just refresh everything on the shortest interval.
        interval = max(min(self.cfg.refresh.slurm_jobs, self.cfg.refresh.slurm_nodes,
                           self.cfg.refresh.storage), 1)
        # @work(exclusive=True, group="collect") on _refresh_all means
        # each tick cancels any in-flight prior tick, regardless of how
        # set_interval schedules them. The decorator returns a Worker
        # object — set_interval calls the method and ignores the result.
        self.set_interval(interval, self._refresh_all)
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

    def action_refresh(self) -> None:
        self.notify("Refreshing…", timeout=1)
        # _refresh_all is @work-decorated — calling it returns a Worker
        # and the actual coroutine runs in the worker pool. The
        # exclusive=True semantic cancels any in-flight tick first.
        self._refresh_all()

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
        if action in ("jobs_toggle", "jobs_tail_log", "jobs_mine_toggle"):
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
        # Phase 4d.2-E step 2.5: click marker so we can attribute any
        # tab-switch latency. Tabs are eagerly composed in this codebase
        # (no `lazy=True`), so the activation cost should be sub-ms —
        # the perf rows confirm or refute that.
        active = ""
        try:
            active = str(getattr(event, "tab", None) and event.tab.id) or ""
        except Exception:
            pass
        with perf_block("click", "tab_activated", extra=f"active={active}"):
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
        # Phase 4d.2-E step 2.5: click marker. Sync portion only — the
        # actual rebuild is dispatched async via watch_mode → update_snapshot.
        with perf_block("click", "mode_toggle_action"):
            if jt := self._visible_jobs_table():
                jt.action_toggle_mode()

    def action_jobs_mine_toggle(self) -> None:
        """Phase 4d.2-D: instant mine/all toggle on the Jobs tab. The
        flip is client-side — JobsTable.watch_mine_only updates the
        pill class and writes back to App.mine_only, which fires
        App.watch_mine_only → re-broadcast of the cached snapshot."""
        # Phase 4d.2-E step 2.5: click marker. Sync portion only — the
        # broadcast is dispatched via run_worker.
        with perf_block("click", "mine_toggle_action"):
            if jt := self._visible_jobs_table():
                jt.mine_only = not jt.mine_only

    def action_jobs_tail_log(self) -> None:
        if jt := self._visible_jobs_table():
            jt.action_tail_log()

    # ────────────────────────────────────────────────────────────────────
    # data
    # ────────────────────────────────────────────────────────────────────

    @work(exclusive=True, group="collect", exit_on_error=False)
    async def _refresh_all(self) -> None:
        snap = Snapshot()

        # Phase 4d.2-D: always fetch all-users data; `mine_only` is now a
        # client-side filter applied in JobsTable / CompactJobs at render
        # time. Toggling mine_only no longer triggers a refetch — both
        # views just re-render from the cached snapshot. Cost: every tick
        # carries the all-users squeue/sacct payload (~150 KB at LRZ peak,
        # noise on rohan). Win: instant `a` toggle, no spinner-reload UX.

        # Build the per-tick collector inputs from cfg.storage_entries.
        # Multiple kind=quota entries: only the first is realized — the
        # combined collector runs ONE quota probe per tick. (Multi-quota
        # configs are rare; if needed, route extras as kind=df and parse
        # them separately.) df paths/globs are concatenated into one
        # `df -B1` invocation.
        # Remote-resolved username (cached on the executor at on_mount).
        # Falls back to "" if whoami failed; downstream filters skip empty.
        cluster_user = getattr(self.executor, "_whoami", "") or ""

        quota_user: str | None = None
        quota_label: str | None = None
        quota_filesystem: str | None = None
        df_explicit: list[tuple[str, str]] = []   # (label, path) for kind=df
        df_explicit_groups: dict[str, str | None] = {}   # path → group, for kind=df
        df_globs: list[str] = []                   # shell globs for kind=auto
        dssusrinfo_subcommands: list[str] = []
        dssusrinfo_entries: list = []  # subset of storage_entries with kind=dssusrinfo
        for entry_cfg in self.cfg.storage_entries:
            if entry_cfg.kind == "quota":
                if quota_user is None:
                    quota_user = cluster_user or None
                    quota_label = entry_cfg.label
                    quota_filesystem = entry_cfg.filesystem
            elif entry_cfg.kind == "df" and entry_cfg.path:
                df_explicit.append((entry_cfg.label, entry_cfg.path))
                df_explicit_groups[entry_cfg.path] = entry_cfg.group
            elif entry_cfg.kind == "auto":
                prefixes = entry_cfg.prefixes or ["/cluster", "/cluster_HDD"]
                # Glob each prefix so the remote shell expands to all
                # autofs-visible children (df then triggers cold mounts
                # as it accesses each).
                df_globs.extend(p.rstrip("/") + "/*" for p in prefixes)
            elif entry_cfg.kind == "dssusrinfo":
                dssusrinfo_entries.append(entry_cfg)
                if entry_cfg.mode == "home":
                    dssusrinfo_subcommands.append("dsshome")
                elif entry_cfg.mode == "container":
                    dssusrinfo_subcommands.append("container_usage")

        # squeue/sacct user-filter args are always None now — we fetch
        # all users every tick (Phase 4d.2-D). The collector still
        # supports the per-user shape; we just don't use it from the
        # dashboard anymore. `[[slurm.users]]` in TOML / cfg.slurm.users
        # is also bypassed; if a future use case wants server-side
        # filtering back, gate it behind a `cfg.slurm.fetch_strategy`
        # knob rather than `mine_only`.

        # ── one ssh channel per tick — see collectors/combined.py ──
        try:
            with perf_block("collect", "combined"):
                sacct_cfg = self.cfg.refresh.sacct
                raw = await fetch_combined(
                    self.executor,
                    quota_user=quota_user,
                    df_explicit_paths=[p for _l, p in df_explicit],
                    df_prefix_globs=df_globs,
                    squeue_format=slurm.SQUEUE_O_FORMAT,
                    squeue_users=None,
                    sacct_format=slurm.SACCT_FORMAT,
                    sacct_starttime_self=sacct_cfg.starttime_self,
                    sacct_starttime_all=sacct_cfg.starttime_all,
                    sacct_user=cluster_user or None,
                    dssusrinfo_subcommands=dssusrinfo_subcommands,
                )
        except Exception as e:
            snap.errors["combined"] = str(e)
            raw = None

        # Bundle-1 Sub-fix-3: absorb the whoami section into the
        # executor's `_whoami` cache so subsequent ticks see it (quota
        # `users=["self"]` resolution + JobsTable's mine_only filter
        # both read it via `getattr(executor, '_whoami', ...)`). Empty
        # string when the section was absent / failed.
        if raw is not None and raw.whoami:
            new_whoami = raw.whoami.strip().splitlines()[-1].strip() if raw.whoami.strip() else ""
            if new_whoami and new_whoami != cluster_user:
                cluster_user = new_whoami
                try:
                    self.executor._whoami = cluster_user
                except Exception:
                    pass

        if raw is not None:
            with perf_block("refresh", "parse"):
                # squeue → jobs
                try:
                    snap.jobs = slurm.parse_squeue(raw.squeue)
                    # Bundle-3 B3.1: any successful squeue parse flips
                    # the sticky jobs_loaded flag so JobsTable +
                    # CompactJobs stop showing "loading…".
                    self._jobs_loaded = True
                    # Phase 4d.2-E: surface parser drop count so the user
                    # can see if rows are being silently rejected. Each
                    # squeue row is one line.
                    squeue_lines = raw.squeue.count("\n")
                    if not raw.squeue.endswith("\n") and raw.squeue:
                        squeue_lines += 1
                    self._debug_log(
                        f"squeue: {squeue_lines} lines, "
                        f"parsed {len(snap.jobs)} jobs "
                        f"(drop={squeue_lines - len(snap.jobs)})"
                    )
                except Exception as e:
                    snap.errors["jobs"] = str(e)
                # Bundle-2 B2.1: two sacct parses, one per snapshot.
                sacct_cfg = self.cfg.refresh.sacct
                try:
                    recent_self = slurm.parse_sacct(raw.sacct_self)
                    snap.recent_jobs_self = slurm.cap_sacct(
                        list(reversed(recent_self)),
                        sacct_cfg.max_rows_self,
                    )
                    self._debug_log(
                        f"sacct_self: parsed {len(recent_self)} jobs "
                        f"(kept {len(snap.recent_jobs_self)}; "
                        f"cap={sacct_cfg.max_rows_self}; "
                        f"starttime={sacct_cfg.starttime_self})"
                    )
                except Exception as e:
                    snap.errors["recent_jobs_self"] = str(e)
                try:
                    recent_all = slurm.parse_sacct(raw.sacct_all)
                    snap.recent_jobs_all = slurm.cap_sacct(
                        list(reversed(recent_all)),
                        sacct_cfg.max_rows_all,
                    )
                    self._debug_log(
                        f"sacct_all: parsed {len(recent_all)} jobs "
                        f"(kept {len(snap.recent_jobs_all)}; "
                        f"cap={sacct_cfg.max_rows_all}; "
                        f"starttime={sacct_cfg.starttime_all})"
                    )
                except Exception as e:
                    snap.errors["recent_jobs_all"] = str(e)
                # scontrol show node → nodes (with partition filters)
                try:
                    nodes = slurm.parse_scontrol_show_node(raw.nodes)
                    if self.cfg.slurm.include_partitions:
                        inc = set(self.cfg.slurm.include_partitions)
                        nodes = [n for n in nodes if any(p in inc for p in n.partitions)]
                    if self.cfg.slurm.exclude_partitions:
                        exc = set(self.cfg.slurm.exclude_partitions)
                        nodes = [n for n in nodes if not any(p in exc for p in n.partitions)]
                    snap.nodes = nodes
                    # Bundle-3 B3.1: scontrol parse succeeded → NodesTable
                    # stops showing "loading…" (set only on the success
                    # path; an exception above keeps the flag at its
                    # prior value, sticky).
                    self._nodes_loaded = True
                except Exception as e:
                    snap.errors["nodes"] = str(e)
                # storage: quota + df-multi + dssusrinfo
                entries: list[StorageEntry] = []
                # quota carries no `group` from config today (only one
                # quota entry is supported; the user can mark it via
                # `group="home"` in TOML if they want the group title).
                quota_group: str | None = None
                if quota_user:
                    for entry_cfg in self.cfg.storage_entries:
                        if entry_cfg.kind == "quota":
                            quota_group = entry_cfg.group
                            break
                if quota_user and raw.quota.strip():
                    try:
                        q = storage.parse_quota(
                            raw.quota,
                            label=quota_label or "home",
                            filesystem=quota_filesystem,
                        )
                        if q is not None:
                            q.group = quota_group
                            entries.append(q)
                    except Exception as e:
                        snap.errors[f"storage:{quota_label}"] = str(e)
                if raw.df.strip():
                    try:
                        df_rows = storage.parse_df_multi(raw.df)
                        # Apply explicit (label, path) overrides — kind=df
                        # entries get their user-specified label and
                        # group from config.
                        path_to_label = {p: l for l, p in df_explicit}
                        for entry in df_rows:
                            if entry.path in path_to_label:
                                entry.label = path_to_label[entry.path]
                                entry.group = df_explicit_groups.get(entry.path)
                            entries.append(entry)
                    except Exception as e:
                        snap.errors["storage:df"] = str(e)
                if dssusrinfo_entries and raw.dssusrinfo.strip():
                    for entry_cfg in dssusrinfo_entries:
                        try:
                            if entry_cfg.mode == "home":
                                e_obj = storage.parse_dssusrinfo_dsshome(
                                    raw.dssusrinfo,
                                    label=entry_cfg.label,
                                    path=entry_cfg.path,
                                )
                            elif entry_cfg.mode == "container":
                                e_obj = storage.parse_dssusrinfo_container(
                                    raw.dssusrinfo,
                                    container=entry_cfg.container or "",
                                    label=entry_cfg.label,
                                    path=entry_cfg.path,
                                )
                            else:
                                e_obj = None
                            if e_obj is not None:
                                e_obj.group = entry_cfg.group
                                entries.append(e_obj)
                        except Exception as e:
                            snap.errors[f"storage:{entry_cfg.label}"] = str(e)
                snap.storage = entries
                # Bundle-3 B3.1: at least one storage parse succeeded
                # (or the user configured no storage at all — sticky
                # flag flips True so the panel stops showing "loading…"
                # for a cluster that genuinely has no entries). The
                # storage section runs even when individual entries
                # fail; the gate is whether the combined fetch reached
                # this point.
                self._storage_loaded = True

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
        # Phase 4d.2-D: stamp the resolved REMOTE whoami onto the
        # snapshot so JobsTable / CompactJobs can filter `mine_only`
        # client-side without poking the executor at render time.
        snap.cluster_user = cluster_user
        # Bundle-3 B3.1: stamp the per-collector sticky flags. Each is
        # flipped inside its own try-block on the first successful
        # parse; the flag stays True across subsequent failed ticks
        # (transient timeouts don't regress to "loading…").
        snap.jobs_loaded = self._jobs_loaded
        snap.nodes_loaded = self._nodes_loaded
        snap.storage_loaded = self._storage_loaded
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

    def watch_mine_only(self, _old: bool, new: bool) -> None:
        """Phase 4d.2-D: mine_only is a client-side filter — flipping it
        doesn't require a refetch. Just re-broadcast the cached snapshot
        so JobsTable + CompactJobs (and anything else reading the flag)
        repaint with the new filter at the same moment.

        Sync JobsTable.mine_only so its pill class stays consistent
        when the flag is flipped via any path (App-level set, key
        binding, etc.) — JobsTable.watch_mine_only also writes back
        to App.mine_only, but Textual reactives don't refire on
        same-value assignments so there's no loop.

        Guard the broadcast against firing before the app is running —
        Textual fires watchers on initial reactive-default assignment
        too, and at that point the worker manager isn't ready. Build
        the coroutine explicitly so we can `.close()` it on early
        returns; otherwise it leaks as an orphan "coroutine was never
        awaited" warning at GC time.
        """
        # Phase 4d.2-E step 2.5: time the synchronous portion of the
        # mine_only watcher (sync JobsTable mirror + run_worker dispatch).
        # The actual fanout happens in the worker — see fanout/all + the
        # per-widget rows for that.
        with perf_block("click", "mine_toggle_app_watcher", extra=f"new={new}"):
            try:
                for jt in self.query(JobsTable):
                    if jt.mine_only != new:
                        jt.mine_only = new
            except Exception:
                pass
            # Phase 4d.2-E step G: click-driven broadcasts skip
            # `batch_update()` so the user sees the table fill
            # progressively instead of waiting for the whole fanout
            # to commit at once (~5-20 ms first-paint vs ~150 ms full).
            coro = self._broadcast_snapshot(
                getattr(self, "snapshot", Snapshot()), batch=False,
            )
            if not getattr(self, "is_running", False):
                coro.close()
                return
            try:
                self.run_worker(coro, exclusive=True, name="broadcast_snapshot")
            except Exception:
                coro.close()

    def watch_mode(self, _old: str, new: str) -> None:
        """Phase 4d.2-E step H: re-broadcast on mode flip. Mirrors
        `watch_mine_only`; same guard against firing before the app is
        running (worker manager not ready → coroutine leaks unawaited).
        JobsTable.watch_mode flips `self.app.mode = new`, which fires
        this watcher. The broadcast's per-widget `update_snapshot`
        reads the JobsTable's already-flipped local `self.mode`, so
        we don't need to mirror back to each JobsTable here.
        """
        with perf_block("click", "mode_app_watcher", extra=f"new={new}"):
            # Phase 4d.2-E step G: click-driven broadcasts skip
            # `batch_update()` so the user sees the table fill
            # progressively instead of waiting for the whole fanout
            # to commit at once (~5-20 ms first-paint vs ~150 ms full).
            coro = self._broadcast_snapshot(
                getattr(self, "snapshot", Snapshot()), batch=False,
            )
            if not getattr(self, "is_running", False):
                coro.close()
                return
            try:
                self.run_worker(coro, exclusive=True, name="broadcast_snapshot")
            except Exception:
                coro.close()

    def watch_nodes_sort(
        self,
        _old: tuple[str, str, bool],
        new: tuple[str, str, bool],
    ) -> None:
        """Bundle-2 B2.4: re-broadcast on NodesTable sort flip. Mirror
        of `watch_sort`'s shape — NodesTable's
        `on_sortable_header_sort_changed` updates its own
        `_sort_col` / `_sort_metric` / `_sort_reverse` synchronously,
        then flips `self.app.nodes_sort = (col, metric, reverse)`,
        which fires this watcher. The broadcast lands at
        `NodesTable.update_snapshot`, which reads the already-flipped
        local state and re-renders. Removes the Phase-H bug shape
        (sync sync-call on async `update_snapshot`) the Bundle-1
        lint flagged."""
        with perf_block("click", "nodes_sort_app_watcher", extra=f"new={new}"):
            coro = self._broadcast_snapshot(
                getattr(self, "snapshot", Snapshot()), batch=False,
            )
            if not getattr(self, "is_running", False):
                coro.close()
                return
            try:
                self.run_worker(coro, exclusive=True, name="broadcast_snapshot")
            except Exception:
                coro.close()

    def watch_sort(self, _old: tuple[str, bool], new: tuple[str, bool]) -> None:
        """Phase 4d.2-E step H: re-broadcast on sort flip. Same shape as
        `watch_mode` — JobsTable's on_sortable_header_sort_changed
        updates its own `_sort_col` / `_sort_reverse` synchronously,
        then flips `self.app.sort = (col, reverse)`, which fires this
        watcher. The broadcast's per-widget update_snapshot reads the
        JobsTable's already-flipped local state."""
        with perf_block("click", "sort_app_watcher", extra=f"new={new}"):
            # Phase 4d.2-E step G: click-driven broadcasts skip
            # `batch_update()` so the user sees the table fill
            # progressively instead of waiting for the whole fanout
            # to commit at once (~5-20 ms first-paint vs ~150 ms full).
            coro = self._broadcast_snapshot(
                getattr(self, "snapshot", Snapshot()), batch=False,
            )
            if not getattr(self, "is_running", False):
                coro.close()
                return
            try:
                self.run_worker(coro, exclusive=True, name="broadcast_snapshot")
            except Exception:
                coro.close()

    async def _broadcast_snapshot(self, new: Snapshot, *, batch: bool = True) -> None:
        """Push `new` into every widget that implements update_snapshot,
        yielding to the loop between each so the 20 FPS animation timer
        can fire in between.

        `batch_update()` coalesces the resulting screen refreshes into a
        single paint once the fan-out completes. For tick-driven
        broadcasts (the 5/15 s refresh) we want this — it avoids
        per-widget flicker when ~10 widgets update at once with fresh
        data. For CLICK-driven broadcasts (mine_only / mode / sort
        flip — Phase H, plus future click watchers) we want progressive
        paint — the first 50 rows of the DataTable land in <2 ms code
        time but were invisible until fanout end (~150 ms post-click).
        Caller passes `batch=False` to opt into progressive paint.
        Default stays True so existing tick callers don't change shape.

        Widgets may declare `update_snapshot` as either sync or async.
        Async handlers chunk their internal rebuild via
        `await asyncio.sleep(0)` every ~50 mutations — same pattern as
        JobsTable._apply_filter_async — so input dispatch isn't held
        for the full duration of a heavy table rebuild.
        """
        # Phase 4d.2-E: per-tick diagnostic line — shows whether
        # cluster_user is empty (whoami still resolving) AND whether
        # `j.user` strings actually match (so the mine_only filter is
        # comparing apples to apples). Tail ~/.cache/rohanboard/debug.log
        # to watch this in real time when running with --debug.
        sample_user = new.jobs[0].user if new.jobs else None
        sample_self = new.recent_jobs_self[0].user if new.recent_jobs_self else None
        sample_all = new.recent_jobs_all[0].user if new.recent_jobs_all else None
        self._debug_log(
            f"snap.cluster_user={new.cluster_user!r} "
            f"jobs={len(new.jobs)} sample_user={sample_user!r} "
            f"recent_jobs_self={len(new.recent_jobs_self)} "
            f"sample_self_user={sample_self!r} "
            f"recent_jobs_all={len(new.recent_jobs_all)} "
            f"sample_all_user={sample_all!r} "
            f"batch={batch}"
        )
        import inspect
        import time as _t
        from contextlib import nullcontext
        # Phase 4d.2-E step G: batch=False skips `batch_update()`, so
        # each widget's repaint emits as soon as its update_snapshot
        # finishes — first DataTable rows land on-screen within
        # ~5-20 ms instead of waiting for the whole fanout to complete.
        batch_cm = self.batch_update() if batch else nullcontext()
        with perf_block("fanout", "all", extra=f"batch={batch}"), batch_cm:
            for widget in list(self.query("*")):
                handler = getattr(widget, "update_snapshot", None)
                if not callable(handler):
                    continue
                t0 = _t.perf_counter() if perf_enabled() else 0.0
                try:
                    result = handler(new)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    # Phase 4d.2-E step F.1: ALWAYS log the traceback —
                    # the perf-row error marker is a fast-grep hook, NOT
                    # a substitute. Prior shape gated traceback-log
                    # behind `else` of perf_enabled(), which meant
                    # `--debug` sessions silently swallowed the very
                    # exceptions the diagnostic was meant to surface.
                    import traceback
                    tb = traceback.format_exc()
                    self._debug_log(
                        f"update_snapshot failed for {type(widget).__name__}:\n{tb}"
                    )
                    self.log(f"update_snapshot failed for {widget!r}\n{tb}")
                    if perf_enabled():
                        perf_log("widget", type(widget).__name__,
                                 (_t.perf_counter() - t0) * 1000, extra="error")
                    continue
                if perf_enabled():
                    perf_log("widget", type(widget).__name__,
                             (_t.perf_counter() - t0) * 1000)
                # Sleep ~1 ms — long enough that the 50 ms (20 FPS)
                # animation timer has a real chance to fire between
                # widget updates rather than getting starved by a tight
                # `sleep(0)` loop.
                await asyncio.sleep(0.001)
