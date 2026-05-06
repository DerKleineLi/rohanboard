from __future__ import annotations

import asyncio
import gc
import os
import shlex
from pathlib import Path
from typing import Callable

shlex_quote = shlex.quote

from textual import events
from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from collections import deque

from .cluster import Cluster
from .collectors import slurm, storage
from .collectors.models import Snapshot, StorageEntry, UtilizationSample
from .config import Config, load as load_config
from .exec import LocalExecutor
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
        # Step-3 multi-cluster cycle.  No-ops in single-cluster mode (see
        # check_action below — hidden when there's only one cluster).
        ("c", "cycle_cluster", "Cycle cluster"),
    ]

    # `snapshot` is the *active cluster's* snapshot.  Kept for back-compat
    # with widgets that read `self.app.snapshot` directly (OverviewPanel).
    # Per-cluster snapshots live on `self.snapshots[cluster_id]`.
    snapshot: reactive[Snapshot] = reactive(Snapshot, recompose=False, layout=False)

    def __init__(self, config: Config | None = None,
                 cluster_id: str | None = None) -> None:
        super().__init__()
        self.cfg = config or load_config()
        if not self.cfg.clusters:
            raise RuntimeError("config has no clusters; nothing to render")
        # Step 3: pick the *initial* active cluster.  Multi-cluster TOML now
        # renders as outer tabs; --cluster <id> just sets which tab is open
        # at startup.  If unset, the first cluster wins.
        if cluster_id is not None:
            matches = [c for c in self.cfg.clusters if c.id == cluster_id]
            if not matches:
                ids = [c.id for c in self.cfg.clusters]
                raise RuntimeError(f"no cluster with id={cluster_id!r}; available={ids}")
            self.active_cluster_id: str = matches[0].id
        else:
            self.active_cluster_id = self.cfg.clusters[0].id
        # Per-cluster snapshot + utilization history.
        self.snapshots: dict[str, Snapshot] = {c.id: Snapshot() for c in self.cfg.clusters}
        self._histories: dict[str, deque[UtilizationSample]] = {
            c.id: deque(maxlen=HISTORY_CAP) for c in self.cfg.clusters
        }
        # Per-cluster "tick is currently running" flag — the timer fires
        # every `refresh.interval` seconds, but on slow clusters (LRZ)
        # the tick may take longer than the interval. We SKIP a new tick
        # rather than cancel the in-flight one (which would mean no LRZ
        # tick ever completes, leaving the Overview stuck on "loading…").
        self._tick_inflight: dict[str, bool] = {c.id: False for c in self.cfg.clusters}
        # `mine_only` drives whether `-u $USER` is passed to squeue/sacct.
        # The "Mine only" pill in the Jobs tab flips this and triggers a
        # fresh fetch — no need for client-side filtering.  Per-cluster so
        # that toggling on the rohan tab doesn't unexpectedly refetch LRZ.
        self.mine_only: dict[str, bool] = {c.id: True for c in self.cfg.clusters}
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
        # Dynamic numbered bindings for the configured tabs (1..9).  In
        # multi-cluster mode the inner tabs of the *active* cluster are
        # what these switch — see action_show_tab.
        for i, tab in enumerate(self.cfg.layout.tabs[:9], start=1):
            self.bind(str(i), f"show_tab('{tab.id}')", description=tab.title)
        # Resolve theme path: relative to package styles/ unless absolute.
        theme_path = Path(self.cfg.theme.file)
        if not theme_path.is_absolute():
            theme_path = Path(__file__).parent / "styles" / theme_path
        self.CSS_PATH = theme_path  # type: ignore[assignment]

    # ────────────────────────────────────────────────────────────────────
    # active cluster helpers
    # ────────────────────────────────────────────────────────────────────

    @property
    def _cluster(self) -> Cluster:
        """The currently-active cluster (drives `self.snapshot` + Jobs/Logs).

        Kept as a property — single-cluster code that reads `self._cluster`
        keeps working unchanged; multi-cluster code that needs to refresh a
        non-active cluster reaches into `self.cfg.clusters` directly.
        """
        for c in self.cfg.clusters:
            if c.id == self.active_cluster_id:
                return c
        # Fallback: should never happen unless active_cluster_id was nuked.
        return self.cfg.clusters[0]

    @property
    def executor(self):
        """Active cluster's executor.  Used by LogTailScreen."""
        return self._cluster.executor

    # ────────────────────────────────────────────────────────────────────
    # composition
    # ────────────────────────────────────────────────────────────────────

    def _mk_widget(self, widget_id: str) -> Widget:
        factory = self._widget_factories.get(widget_id)
        if factory is None:
            return Static(f"[red]Unknown widget: {widget_id}[/red]")
        return factory()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        if not self.cfg.layout.tabs:
            yield Static("No tabs configured.")
            yield Footer()
            return
        if not self.cfg.clusters:
            yield Static("No clusters configured.")
            yield Footer()
            return

        if len(self.cfg.clusters) == 1:
            # Single-cluster: render directly (no outer cluster strip).
            # Preserves the pre-step-3 single-cluster UX exactly.  Inner
            # TabPane ids match the layout's tab.id verbatim — this is
            # what existing tests + the action_show_tab path expect.
            cluster = self.cfg.clusters[0]
            with TabbedContent(initial=self.cfg.layout.tabs[0].id):
                for tab in self.cfg.layout.tabs:
                    with TabPane(tab.title, id=tab.id):
                        with ScrollableContainer(classes="tab-body"):
                            for widget_id in tab.widgets:
                                yield self._mk_widget(widget_id)
        else:
            # Multi-cluster: outer cluster tabs wrap the inner Overview/
            # Jobs/Nodes/Storage layout per cluster.  Each cluster gets its
            # own widget instances so per-cluster state (filters, sort,
            # cursor) is independent.
            initial_outer = f"cluster_{self.active_cluster_id}"
            with TabbedContent(initial=initial_outer, id="cluster_tabs"):
                for cluster in self.cfg.clusters:
                    with TabPane(cluster.title, id=f"cluster_{cluster.id}"):
                        with TabbedContent(
                            initial=f"{cluster.id}__{self.cfg.layout.tabs[0].id}",
                            id=f"inner_tabs_{cluster.id}",
                        ):
                            for tab in self.cfg.layout.tabs:
                                with TabPane(tab.title, id=f"{cluster.id}__{tab.id}"):
                                    with ScrollableContainer(classes="tab-body"):
                                        for widget_id in tab.widgets:
                                            yield self._mk_widget(widget_id)
        yield Footer()

    # ────────────────────────────────────────────────────────────────────
    # lifecycle + refresh
    # ────────────────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        self._apply_size_class(self.size.width)
        await self._refresh_all()
        # PER-CLUSTER scheduling (issue #2 follow-up): each cluster ticks
        # at its OWN interval rather than a shared min(). On a multi-
        # cluster setup with rohan@5 s + LRZ@30 s, the legacy global
        # tick fired LRZ every 5 s — but LRZ's commands take ~10 s
        # warm, so every LRZ tick got cancelled by the next 5 s tick.
        # Decoupling lets rohan stay at 5 s while LRZ refreshes at its
        # own slower cadence.
        for c in self.cfg.clusters:
            ivl = max(min(c.refresh.slurm_jobs, c.refresh.slurm_nodes,
                          c.refresh.storage), 1.0)
            # run_worker(exclusive=True) here would still cancel an
            # overlapping LRZ tick, but with per-cluster intervals the
            # overlap window only happens when the cluster is actually
            # slower than its own configured interval — which is the
            # cluster admin's signal to bump that cluster's interval.
            self.set_interval(ivl, lambda c=c: self._refresh_one_cluster_bg(c))
        # Freeze the GC generations for everything allocated during
        # startup (widget tree, caches, Styles back-references).  This
        # excludes them from future gen-2 scans, which were the root
        # cause of 50–200 ms stutters in the animation widgets.
        # See https://github.com/Textualize/textual/issues/6381
        gc.collect()
        gc.freeze()

    def _refresh_one_cluster_bg(self, cluster: Cluster) -> None:
        """Per-cluster background tick. Uses a SKIP-IF-RUNNING pattern
        rather than `exclusive=True` cancellation — a slow LRZ tick that
        takes 10 s should NOT get cancelled every 5 s by the next tick
        (under cancellation, no LRZ tick ever completes, leaving the
        Overview stuck on "loading…"). Instead, if a tick is still in
        flight, the new tick is dropped silently. The user sees the
        previous-good snapshot until the in-flight tick completes."""
        if self._tick_inflight.get(cluster.id, False):
            return
        self._tick_inflight[cluster.id] = True

        async def _run():
            try:
                await self._refresh_cluster_and_publish(cluster)
            finally:
                self._tick_inflight[cluster.id] = False

        self.run_worker(_run(), exclusive=False, name=f"refresh_{cluster.id}")

    async def _refresh_cluster_and_publish(self, cluster: Cluster) -> None:
        """Refresh ONE cluster + push the active reactive when it's the
        active cluster. Used by the per-cluster timer."""
        with perf_block("refresh", f"{cluster.id}.tick"):
            await self._refresh_cluster(cluster)
        # If this is the active cluster, fire the reactive.
        if cluster.id == self.active_cluster_id:
            active_snap = self.snapshots.get(cluster.id)
            if active_snap is not None:
                with perf_block("refresh", "reactive_set"):
                    self.snapshot = active_snap

    async def action_refresh(self) -> None:
        self.notify("Refreshing…", timeout=1)
        await self._refresh_all()

    def action_show_tab(self, tab_id: str) -> None:
        # Inner-tab switch for the *active* cluster.  Reach into the inner
        # TabbedContent if it exists (multi-cluster mode); else flat mode.
        if len(self.cfg.clusters) > 1:
            try:
                inner = self.query_one(
                    f"#inner_tabs_{self.active_cluster_id}", TabbedContent
                )
                inner.active = f"{self.active_cluster_id}__{tab_id}"
                return
            except Exception:
                pass
        # Single-cluster: TabPane ids are the bare tab.id.
        try:
            self.query_one(TabbedContent).active = tab_id
        except Exception:
            pass

    def action_cycle_cluster(self) -> None:
        if len(self.cfg.clusters) <= 1:
            return
        ids = [c.id for c in self.cfg.clusters]
        try:
            cur = ids.index(self.active_cluster_id)
        except ValueError:
            cur = 0
        new_id = ids[(cur + 1) % len(ids)]
        self.active_cluster_id = new_id
        try:
            tabs = self.query_one("#cluster_tabs", TabbedContent)
            tabs.active = f"cluster_{new_id}"
        except Exception:
            pass
        # Refresh-bindings so the footer reflects the active cluster's
        # tab-scoped shortcuts (e.g. `a`/`l` only on Jobs).
        self.refresh_bindings()
        # Update the snapshot reactive so OverviewPanel etc. that read
        # `self.app.snapshot` see the new active cluster's data.
        self.snapshot = self.snapshots[new_id]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Scope tab-specific actions to their tab.

        Textual Screen.active_bindings semantics:
          * True  → shown and active
          * None  → shown but disabled (greyed)
          * False → hidden entirely
        We want Jobs-only shortcuts to *disappear* on other tabs, not grey
        out, so we return False off-tab.  The cluster-cycle shortcut is
        hidden when there's only one cluster.
        """
        if action == "cycle_cluster" and len(self.cfg.clusters) <= 1:
            return False
        if action in ("jobs_toggle", "jobs_tail_log"):
            inner = self._active_inner_tab()
            if inner != "jobs":
                return False
        return True

    def _active_inner_tab(self) -> str | None:
        """Return the active inner-tab name (e.g. "jobs") for the active
        cluster.  Works in both single and multi-cluster mode.

        In multi-cluster mode the inner pane ids are "<cluster>__<tab>",
        so we strip the prefix.  In single-cluster mode the ids are the
        bare tab.id.
        """
        if len(self.cfg.clusters) > 1:
            try:
                inner = self.query_one(
                    f"#inner_tabs_{self.active_cluster_id}", TabbedContent
                )
                active = inner.active
            except Exception:
                return None
            if not active:
                return None
            return active.split("__", 1)[1] if "__" in active else active
        try:
            active = self.query_one(TabbedContent).active
        except Exception:
            return None
        return active or None

    def on_tabbed_content_tab_activated(self, event) -> None:  # type: ignore[no-untyped-def]
        # Refresh footer / key dispatcher when user switches tabs so the
        # hidden shortcuts appear/disappear.  Also catch outer-tab clicks
        # to update active_cluster_id.
        try:
            tab_id = getattr(getattr(event, "pane", None), "id", None) or ""
        except Exception:
            tab_id = ""
        if tab_id.startswith("cluster_"):
            new_id = tab_id[len("cluster_"):]
            if new_id in self.snapshots and new_id != self.active_cluster_id:
                self.active_cluster_id = new_id
                self.snapshot = self.snapshots[new_id]
        self.refresh_bindings()

    def _visible_jobs_table(self) -> JobsTable | None:
        """Return the JobsTable on the *currently active* tab of the
        *currently active cluster*, or None if the Jobs tab isn't active.
        """
        if self._active_inner_tab() != "jobs":
            return None
        # Pane id is bare tab.id in single-cluster mode, prefixed in multi.
        target_pane_id = (
            f"{self.active_cluster_id}__jobs"
            if len(self.cfg.clusters) > 1 else "jobs"
        )
        for w in self.query(JobsTable):
            pane = w.parent
            while pane is not None and not isinstance(pane, TabPane):
                pane = pane.parent
            if isinstance(pane, TabPane) and pane.id == target_pane_id:
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
        """Refresh every cluster's snapshot in parallel.

        Each cluster gets an independent Snapshot built from its own
        executor + filters; per-cluster errors are isolated (a broken LRZ
        SSH master never blanks the rohan tab).
        """
        with perf_block("refresh", "all_clusters"):
            await asyncio.gather(
                *[self._refresh_cluster(c) for c in self.cfg.clusters],
                return_exceptions=True,
            )
        # After all clusters refresh, push the active cluster's snapshot
        # through the reactive so the global broadcast triggers.
        active_snap = self.snapshots.get(self.active_cluster_id)
        if active_snap is not None:
            with perf_block("refresh", "reactive_set"):
                self.snapshot = active_snap

    async def _refresh_cluster(self, cluster: Cluster) -> None:
        snap = Snapshot()
        mine_only = self.mine_only.get(cluster.id, True)
        users_for_fetch = ["self"] if mine_only else ["all"]
        executor = cluster.executor

        # ── BULK PATH (issue #2): coalesce the 4 collector ssh calls
        # ── into ONE round-trip per cluster. The previous pattern fanned
        # ── out via asyncio.gather under SSHExecutor's per-host
        # ── Semaphore(1), which serialised them — 4 × 1.4 s warm-call
        # ── = 5.6 s on LRZ, busting the 5 s tick budget and getting
        # ── cancelled mid-flight by run_worker(exclusive=True). With
        # ── bulk_run we acquire the semaphore once, fire one bash -c
        # ── pipeline that BACKGROUNDs each command (parallel) and
        # ── waits, sentinel-framed output, parse each collector's
        # ── text on the client. Per saved memory
        # ── feedback_asyncio_subprocess_cancel_leaks.md "Trap: Semaphore
        # ── (1) + asyncio.gather of N is fully serial" — coalescing at
        # ── the executor layer is the canonical fix.
        if hasattr(executor, "bulk_run") and callable(executor.bulk_run):
            await self._refresh_cluster_bulk(cluster, snap, mine_only, users_for_fetch)
        else:
            # Legacy parallel-gather path. Kept for executors that don't
            # implement bulk_run (test stubs, future custom executors).
            await self._refresh_cluster_legacy(cluster, snap, mine_only, users_for_fetch)

        # If this refresh tick produced no nodes AND a previous tick had
        # populated the snapshot, KEEP the previous data rather than
        # blanking the UI to "no node data yet". This is the fall-back
        # for issue #2 on LRZ where some ticks complete quickly (warm
        # mux + fast scontrol) but others get cancelled mid-flight by
        # the next tick — without this guard, every cancelled tick
        # blanks the LRZ Overview for ~5 s before the next successful
        # tick repopulates it.
        prev = self.snapshots.get(cluster.id)
        if (
            prev is not None
            and not snap.nodes
            and not snap.jobs
            and not snap.recent_jobs
            and not snap.storage
            and (prev.nodes or prev.jobs or prev.storage)
        ):
            # Carry over the prior good data; preserve any errors from
            # this tick so the user can still see what failed.
            errors = dict(snap.errors)
            snap = prev
            for k, v in errors.items():
                snap.errors[k] = v

        # Append to per-cluster rolling history (only if we got node data).
        history = self._histories[cluster.id]
        if snap.nodes:
            cpu_total = sum(n.cpu_total for n in snap.nodes) or 1
            cpu_alloc = sum(n.cpu_alloc for n in snap.nodes)
            mem_total = sum(n.mem_total_mb for n in snap.nodes) or 1
            mem_alloc = sum(n.mem_alloc_mb for n in snap.nodes)
            gpu_total = sum(g.total for n in snap.nodes for g in n.gpus) or 1
            gpu_alloc = sum(g.alloc for n in snap.nodes for g in n.gpus)
            history.append(UtilizationSample(
                cpu=cpu_alloc / cpu_total,
                gpu=gpu_alloc / gpu_total,
                mem=mem_alloc / mem_total,
            ))
        snap.history = list(history)
        # Stash on the per-cluster dict; broadcast to widgets in this
        # cluster's pane.
        self.snapshots[cluster.id] = snap
        await self._broadcast_to_cluster(cluster.id, snap)

    async def _refresh_cluster_bulk(
        self,
        cluster: Cluster,
        snap: Snapshot,
        mine_only: bool,
        users_for_fetch: list[str],
    ) -> None:
        """Coalesced ssh round-trip path for issue #2.

        Builds argv for jobs / recent / nodes / storage upfront and hands
        them to executor.bulk_run as ONE batch. SSHExecutor packs them
        into a single sentinel-framed bash pipeline; LocalExecutor runs
        them in parallel. Either way the per-host Semaphore(1) on
        SSHExecutor is acquired exactly once per refresh tick.
        """
        executor = cluster.executor

        # ── 1. Build all argv upfront. Argv-building for jobs/recent
        # ── needs a whoami round-trip on first call; do that BEFORE
        # ── bulk_run so the bulk pipeline doesn't get blocked behind
        # ── the whoami subprocess inside the same semaphore acquisition.
        # ── Subsequent ticks hit the cached whoami and this is free.
        try:
            jobs_argv = await slurm.build_jobs_argv(executor, users_for_fetch)
            recent_argv = await slurm.build_recent_jobs_argv(executor, users_for_fetch)
            jobs_mine_argv = (
                jobs_argv if mine_only
                else await slurm.build_jobs_argv(executor, ["self"])
            )
        except Exception as e:
            snap.errors["build_argv"] = str(e)
            return

        bulk: dict[str, list[str]] = {
            "jobs": jobs_argv,
            "recent": recent_argv,
            "nodes": slurm.nodes_argv(),
        }
        if not mine_only:
            bulk["jobs_mine"] = jobs_mine_argv

        # Storage entries — one bulk slot per cluster-config entry.
        # Each entry contributes its OWN ssh command; auto / quota / df /
        # auto_lrz all reduce to a shell-level command that bulk_run
        # frames with sentinels.
        storage_slots: list[tuple[str, str, object]] = []  # (slot_name, kind, cfg)
        for entry_cfg in cluster.storage_entries:
            kind = entry_cfg.kind
            slot = f"storage:{entry_cfg.label}"
            if kind == "quota":
                bulk[slot] = ["bash", "-lc", f"quota -wp -f {shlex_quote(entry_cfg.filesystem or '')}"]
                storage_slots.append((slot, kind, entry_cfg))
            elif kind == "df":
                if not entry_cfg.path:
                    continue
                bulk[slot] = ["df", "-B1", entry_cfg.path]
                storage_slots.append((slot, kind, entry_cfg))
            elif kind == "auto":
                # `auto` would need TWO round-trips on the legacy path:
                # /proc/mounts to discover paths, THEN df them. We fold
                # both into ONE bash pipeline so bulk_run still gets a
                # single slot. The script:
                #   1. Print /proc/mounts (parsed client-side).
                #   2. Awk-filter mountpoints under any prefix (skipping
                #      pseudo-fstypes), then df -B1 them all.
                # Sentinel separates the two sections so the demux works
                # even if /proc/mounts contains text matching df headers.
                prefixes = entry_cfg.prefixes or ["/cluster", "/cluster_HDD"]
                # Awk script: emit mountpoints whose path is === a prefix
                # OR starts with prefix+"/". Skip the pseudo-fstypes the
                # legacy discover_mounts() skipped (kept in DEFAULT_SKIP_FSTYPES).
                # The python-side parse will still uniq by (source, fstype)
                # using the /proc/mounts dump above.
                prefix_args = " ".join(shlex_quote(p.rstrip("/")) for p in prefixes)
                # Use printf %s to keep the awk text out of f-string formatting
                # surprises. Heredoc-ish via a quoted single-arg string.
                awk_filter = (
                    "{ for(i in PFX){ p=PFX[i]; "
                    "if($2==p || index($2,p\"/\")==1){ "
                    "if(!skip[$3]){ print $2 } break } } }"
                )
                script = (
                    "echo ---PROCMOUNTS---; "
                    "cat /proc/mounts; "
                    "echo ---DFBLOCK---; "
                    f"PATHS=$(awk -v PFXLIST={shlex_quote(prefix_args)} "
                    "'BEGIN{ split(PFXLIST,PFX,\" \"); "
                    "split(\"fuse.mergerfs fuse.sshfs tmpfs devtmpfs proc sysfs cgroup cgroup2 autofs overlay\",a,\" \"); "
                    "for(i in a) skip[a[i]]=1 } "
                    f"{awk_filter}' /proc/mounts | sort -u); "
                    "if [ -n \"$PATHS\" ]; then df -B1 $PATHS 2>/dev/null || true; fi"
                )
                bulk[slot] = ["bash", "-c", script]
                storage_slots.append((slot, kind, entry_cfg))
            elif kind == "auto_lrz":
                bulk[slot] = ["bash", "-lc", storage.lrz_discover_script()]
                storage_slots.append((slot, kind, entry_cfg))

        # ── 2. ONE bulk_run call — single ssh acquisition for the cluster.
        with perf_block("refresh", f"{cluster.id}.bulk"):
            try:
                texts = await executor.bulk_run(bulk, timeout=30.0)
            except Exception as e:
                snap.errors["bulk"] = str(e)
                texts = {}

        # ── 3. Parse each text in turn. Per-collector errors are isolated.
        with perf_block("collect", f"{cluster.id}.jobs"):
            try:
                snap.jobs = slurm.parse_squeue(texts.get("jobs", ""))
                if not texts.get("jobs"):
                    snap.errors["jobs"] = "empty bulk output"
            except Exception as e:
                snap.errors["jobs"] = str(e)

        if mine_only:
            snap.jobs_mine = snap.jobs
        else:
            with perf_block("collect", f"{cluster.id}.jobs_mine"):
                try:
                    snap.jobs_mine = slurm.parse_squeue(texts.get("jobs_mine", ""))
                except Exception as e:
                    snap.errors["jobs_mine"] = str(e)

        with perf_block("collect", f"{cluster.id}.recent"):
            try:
                snap.recent_jobs = slurm.parse_recent_jobs(texts.get("recent", ""))
            except Exception as e:
                snap.errors["recent_jobs"] = str(e)

        with perf_block("collect", f"{cluster.id}.nodes"):
            try:
                nodes = slurm.parse_scontrol_show_node(texts.get("nodes", ""))
                if cluster.slurm.include_partitions:
                    inc = set(cluster.slurm.include_partitions)
                    nodes = [n for n in nodes if any(p in inc for p in n.partitions)]
                if cluster.slurm.exclude_partitions:
                    exc = set(cluster.slurm.exclude_partitions)
                    nodes = [n for n in nodes if not any(p in exc for p in n.partitions)]
                snap.nodes = nodes
            except Exception as e:
                snap.errors["nodes"] = str(e)

        with perf_block("collect", f"{cluster.id}.storage"):
            entries: list[StorageEntry] = []
            for slot, kind, entry_cfg in storage_slots:
                text = texts.get(slot, "")
                try:
                    if kind == "quota":
                        e = storage.parse_quota(text, entry_cfg.label, entry_cfg.filesystem)
                        if e is not None:
                            entries.append(e)
                    elif kind == "df":
                        e = storage.parse_df(text, label=entry_cfg.label, path=entry_cfg.path)
                        if e is not None:
                            entries.append(e)
                    elif kind == "auto":
                        # Demux discovered + df from the inline shell helper.
                        sep_mounts = "---PROCMOUNTS---"
                        sep_df = "---DFBLOCK---"
                        mounts_text = ""
                        df_text = ""
                        if sep_mounts in text:
                            _, rest = text.split(sep_mounts, 1)
                            if sep_df in rest:
                                mounts_text, df_text = rest.split(sep_df, 1)
                            else:
                                mounts_text = rest
                        # Discover paths from the mounts dump.
                        prefixes = entry_cfg.prefixes or ["/cluster", "/cluster_HDD"]
                        norm_prefixes = [p.rstrip("/") for p in prefixes]
                        seen: set[tuple[str, str]] = set()
                        discovered: list[tuple[str, str]] = []
                        for line in mounts_text.splitlines():
                            parts = line.split()
                            if len(parts) < 3:
                                continue
                            source, mp, fstype = parts[0], parts[1], parts[2]
                            if fstype in storage.DEFAULT_SKIP_FSTYPES:
                                continue
                            if not any(mp == p or mp.startswith(p + "/") for p in norm_prefixes):
                                continue
                            key = (source, fstype)
                            if key in seen:
                                continue
                            seen.add(key)
                            discovered.append((mp, mp))
                        discovered.sort(key=lambda x: x[0].lower())
                        if discovered and df_text.strip():
                            paths = [p for _, p in discovered]
                            by_path = storage.parse_df_multi(df_text, paths)
                            for label, path in discovered:
                                triple = by_path.get(path)
                                if triple is None:
                                    continue
                                total, used, avail = triple
                                entries.append(StorageEntry(
                                    label=label,
                                    used_bytes=used,
                                    total_bytes=total,
                                    avail_bytes=avail,
                                    source="df",
                                    path=path,
                                ))
                    elif kind == "auto_lrz":
                        lrz_entries = storage.parse_lrz_discover(text)
                        entries.extend(lrz_entries)
                except Exception as e:
                    snap.errors[f"storage:{entry_cfg.label}"] = str(e)
            snap.storage = entries

    async def _refresh_cluster_legacy(
        self,
        cluster: Cluster,
        snap: Snapshot,
        mine_only: bool,
        users_for_fetch: list[str],
    ) -> None:
        """Original per-collector parallel-gather path. Used when the
        executor doesn't expose bulk_run (test stubs, future custom
        executors). For LocalExecutor this is identical to the bulk path
        in wall-clock terms (no semaphore on local)."""
        executor = cluster.executor

        async def grab_jobs():
            try:
                with perf_block("collect", f"{cluster.id}.jobs"):
                    snap.jobs = await slurm.fetch_jobs(executor, users_for_fetch)
            except Exception as e:
                snap.errors["jobs"] = str(e)
            if mine_only:
                snap.jobs_mine = snap.jobs
                return
            try:
                with perf_block("collect", f"{cluster.id}.jobs_mine"):
                    snap.jobs_mine = await slurm.fetch_jobs(executor, ["self"])
            except Exception as e:
                snap.errors["jobs_mine"] = str(e)

        async def grab_recent():
            try:
                with perf_block("collect", f"{cluster.id}.recent"):
                    snap.recent_jobs = await slurm.fetch_recent_jobs(executor, users_for_fetch)
            except Exception as e:
                snap.errors["recent_jobs"] = str(e)

        async def grab_nodes():
            try:
                with perf_block("collect", f"{cluster.id}.nodes"):
                    nodes = await slurm.fetch_nodes(executor)
                if cluster.slurm.include_partitions:
                    inc = set(cluster.slurm.include_partitions)
                    nodes = [n for n in nodes if any(p in inc for p in n.partitions)]
                if cluster.slurm.exclude_partitions:
                    exc = set(cluster.slurm.exclude_partitions)
                    nodes = [n for n in nodes if not any(p in exc for p in n.partitions)]
                snap.nodes = nodes
            except Exception as e:
                snap.errors["nodes"] = str(e)

        async def grab_storage():
            with perf_block("collect", f"{cluster.id}.storage"):
                await _grab_storage_impl()

        async def _grab_storage_impl():
            entries: list[StorageEntry] = []
            for entry_cfg in cluster.storage_entries:
                try:
                    if entry_cfg.kind == "quota":
                        e = await storage.fetch_quota(executor, entry_cfg.label, entry_cfg.filesystem)
                        if e is not None:
                            entries.append(e)
                    elif entry_cfg.kind == "df":
                        if not entry_cfg.path:
                            continue
                        e = await storage.fetch_df(executor, entry_cfg.label, entry_cfg.path)
                        if e is not None:
                            entries.append(e)
                    elif entry_cfg.kind == "auto":
                        prefixes = entry_cfg.prefixes or ["/cluster", "/cluster_HDD"]
                        discovered = await storage.discover_mounts(executor, prefixes)
                        results = await storage.fetch_df_multi(executor, discovered)
                        for r in results:
                            if isinstance(r, StorageEntry):
                                entries.append(r)
                    elif entry_cfg.kind == "auto_lrz":
                        lrz_entries = await storage.discover_lrz_storage(executor)
                        entries.extend(lrz_entries)
                except Exception as e:
                    snap.errors[f"storage:{entry_cfg.label}"] = str(e)
            snap.storage = entries

        with perf_block("refresh", f"{cluster.id}.gather"):
            await asyncio.gather(grab_jobs(), grab_recent(), grab_nodes(), grab_storage())

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
        # The reactive is fired by `_refresh_all` (post-fan-out) and by
        # `action_cycle_cluster` (when switching active tab).  Cycling
        # alone shouldn't re-broadcast every cluster — _broadcast_to_cluster
        # is what handles each cluster's pane during refresh.  But we DO
        # need this here for OverviewPanel, which queries `self.app.snapshot`
        # directly during compose / on_mount; setting the reactive triggers
        # its watchers normally.  No-op here: the per-cluster broadcast
        # already happened in _refresh_cluster.
        return

    async def _broadcast_to_cluster(self, cluster_id: str, snap: Snapshot) -> None:
        """Push `snap` into every widget that lives inside the outer
        TabPane for `cluster_id` (multi-cluster mode) or every widget
        in the tree (single-cluster mode).

        Yields to the loop between widgets so animation timers can fire
        between fan-out steps.
        """
        import time as _t
        multi = len(self.cfg.clusters) > 1
        target_pane_id = f"cluster_{cluster_id}"

        with perf_block("fanout", cluster_id), self.batch_update():
            for widget in list(self.query("*")):
                handler = getattr(widget, "update_snapshot", None)
                if not callable(handler):
                    continue
                if multi and not _is_under_pane(widget, target_pane_id):
                    continue
                if perf_enabled():
                    t0 = _t.perf_counter()
                    try:
                        handler(snap)
                    except Exception:
                        perf_log("widget", type(widget).__name__,
                                 (_t.perf_counter() - t0) * 1000, extra="error")
                        continue
                    perf_log("widget", type(widget).__name__,
                             (_t.perf_counter() - t0) * 1000)
                else:
                    try:
                        handler(snap)
                    except Exception:
                        import traceback
                        self.log(f"update_snapshot failed for {widget!r}\n{traceback.format_exc()}")
                # Sleep ~1 ms — long enough that the 50 ms (20 FPS)
                # animation timer has a real chance to fire between
                # widget updates rather than getting starved by a tight
                # `sleep(0)` loop.
                await asyncio.sleep(0.001)


def _is_under_pane(widget: Widget, target_pane_id: str) -> bool:
    """True iff `widget` is a descendant of a TabPane with id=target_pane_id.

    Walks up the parent chain.  Used by `_broadcast_to_cluster` to keep
    each cluster's snapshot from leaking into the wrong cluster's tab.
    """
    node = widget.parent
    while node is not None:
        if isinstance(node, TabPane) and node.id == target_pane_id:
            return True
        node = node.parent
    return False
