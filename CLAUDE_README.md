# rohanboard — local project runbook

This file is the project-local runbook for `rohanboard` on `clean-reimpl-v2`. Read before acting; update proactively with anything future-you would otherwise have to ask the user about.

## Design principles

> **Click → first visible content target: ~200 ms. Full render can take longer. If a full render exceeds the budget, prefer progressive rendering (first ~50 rows in ~200 ms, remainder in the background) over delaying everything until ready. The dashboard should always feel fast, even when the data is large.**

This is load-bearing for architectural decisions. The data path (5 s tick, snapshot reactive, fanout broadcast) is decoupled from the render path on purpose — clicks should re-render from the cached snapshot, not refetch. When proposing optimizations, anchor on "what does the USER see in the first 200 ms" rather than "what's the total wall-clock to full table re-render". Phase 4d.2-E step 2.5 added `perf_block` instrumentation for this — run with `--debug` (or `ROHANBOARD_PERF_LOG=…`), the `filter/<path>_<mode>` rows carry `first_50_ms=…` and `chunks=…` extras so the breakdown is mechanical, not eyeballed.

## Architectural invariants (post-4d.2-E, 2026-05-11) — load-bearing rules

These rules were established by Phase F+H+A+G and MUST be honored by any future render-path change.

- **Snapshot-broadcast is the only render path.** Every widget update goes through `App._broadcast_snapshot(snap, *, batch=…)`. Click handlers MUST NOT call `widget.update_snapshot(...)` directly — `update_snapshot` is `async def`, sync callers silently drop the coroutine. Phase H caught three such sites; all now route through App-level reactives. If you find a new `self.update_snapshot(...)` call from a sync context, that's a bug. Anchor file: `rohanboard/app.py:_broadcast_snapshot`.

- **Click-driven state changes flip App reactives, never the widget's own.** Three currently exist: `app.mine_only`, `app.mode`, `app.sort` (all in `rohanboard/app.py`, near line 64). Each has a matching `App.watch_*` watcher that calls `_broadcast_snapshot(snap, batch=False)`. To add a new click-driven state, follow the same pattern: class-level reactive + matching watcher + `batch=False` broadcast. Don't invent a new fanout mechanism.

- **Tick-driven broadcasts use `batch=True` (default); click-driven use `batch=False`.** The 5 s/15 s refresh tick wraps the fanout in `batch_update()` to avoid per-widget flicker when ~10 widgets repaint at once with fresh data. Click broadcasts skip `batch_update()` so the first 50 DataTable rows paint as soon as the widget's chunked rebuild yields them, instead of waiting for the whole fanout to commit. The two paths are NOT interchangeable — flipping a tick to batch=False risks flicker; flipping a click to batch=True restores the old 1000 ms+ visible latency. See `rohanboard/app.py:_broadcast_snapshot` for the `contextlib.nullcontext()` idiom.

- **`OverviewPanel._relayout` cache is split into TWO keys.** `layout_key = (mode, util_side, show_ascii, fit, compact_jobs_flex)` triggers `_populate` (full tear-down + re-mount cycle). `height_key = (target, ascii_budget, nodesummary_extra, home_extra)` triggers `_apply_heights` (in-place `widget.styles.height = …`). If a future change adds a new layout-decision field, decide: does it affect which widgets are mounted / in which slot / with which class? → `layout_key`. Does it only resize existing widgets? → `height_key`. Putting a cheap height change into the layout_key resurrects the ~830 ms paint cascade Phase A closed. Anchor file: `rohanboard/widgets/overview.py:_relayout` + `_apply_heights`.

- **Error logging in `_broadcast_snapshot`'s `except Exception` is ALWAYS additive — never substitutive.** Traceback to `_debug_log` (debug.log file) AND `self.log()` (Textual internal) AND perf-row to perf.log. Phase F.1 caught the prior shape (`if perf_enabled(): perf_log_error else: traceback`) which silently swallowed 1192 JobsTable errors over a 73-minute live session. Generalize the rule: if `perf_enabled()` or `--debug` gates extra logging, the gate must add to the diagnostic surface, never replace another diagnostic.

- **Job-ID sort keys are tuples, never raw int or str.** Slurm sacct can return array-task IDs (`5641869_[1-10]`), step IDs (`1234.batch`), het-job components (`1234+0`). `int(job_id)` raises on those; mixing int and str return values in the same `list.sort()` raises `TypeError`. Use the `(int_prefix, full_id_string)` pattern from `_JOBID_LEADING_INT_RE` + `_sort_value` in `rohanboard/widgets/jobs_table.py`. Pinned by `tests/test_jobs_table_sort.py` (7 cases including mixed-collection sort-without-TypeError).

- **Black-box measurement is the canonical perf surface for "is this click fast enough" questions, not `perf_block`.** `perf_block` measures code-execution time (~125–195 ms for a typical broadcast). The user's perception is visible-content time, which can lag code by ~5 ms (post-G) to ~875 ms (pre-G batch_update cascade). For any latency claim, drive the keystroke via `tmux send-keys`, capture-pane on an absolute-deadline schedule (e.g. `[20, 50, 100, 200, 350, 500, 750, 1000, …]` ms from T0), and report the first dt where the target content first appears. tmux's pane-buffer poll cadence appears to be ~100–200 ms — that's the floor of what this harness can detect. The throwaway harnesses `/tmp/blackbox_click.py` and `/tmp/blackbox_seq.py` are reusable templates.

## Project location + branch

- **Path:** `~/workspace/rohanboard-local/` on WSL.
  - Memory file `~/.claude/projects/-home-hli/memory/rohanboard.md` mentions `~/workspace/rohanboard` (no `-local`); that path is **outdated**. The active git tree is `-local`.
- **Branch:** `clean-reimpl-v2` (work-in-progress; not yet merged to main).
- **Origin:** likely a separate `origin/clean-reimpl-v2` tracker — confirm before pushing; do not force-push.

## Phase status

- **Phase 4a** (40da8f1): asyncssh runtime + pytest-asyncio dev deps.
- **Phase 4b** (24efb4c): Executor abstraction + AsyncSSHExecutor stub.
- **Phase 4c** (in WIP, checkpointed at c1fe302): single-cluster SSH wiring (LocalExecutor + AsyncSSHExecutor live; collectors use them via `cfg.build_executor()`). The user has been smoke-testing in `rohan:rb-v2-4c-ssh` against `/tmp/wsl_single_v2_4c_ssh_config.toml` (`exec = "ssh:rohan"`).
- **Phase 4d** (landed 2026-05-08, e312974 + Phase 4d-tests): chunked async DataTable rebuild (`_apply_filter_async` with `await asyncio.sleep(0)` every 50 row mutations) + 150 ms filter debounce on both `JobsTable` and `NodesTable`. Cherry-picked from `clean-reimpl` `e7c17b4`. Fixes the per-keystroke synchronous full-table rebuild that starved Textual's input dispatch under refresh-tick pressure (root cause of the persistent filter-bar character drops at slow rhythm).
- **Phase 4d.1 + layout-defaults** (landed 2026-05-08, 8f88cce → afdf878): six commits bringing rohan-style polish from `main` over the 4d input-drop fix:
  - `8f88cce` foundational `GpuSpec.vram` + `display` (extracted from main 800459d).
  - `d27cff8` cherry-pick of `a5f2f26` — GPU cell renders `<free/alloc/total>` first, then `<kind+vram>` label.
  - `5c0109f` rohan-style layout becomes the app default; `[layout]` is now optional in TOML configs.
  - `c6d530e` cherry-pick of `4cde47a` — MIG-node aggregation at node level (no fictitious negative free counts).
  - `54377ef` cherry-pick of `9f17161` — `_norm_kind` canonical formatter (uppercases non-MIG kinds, preserves MIG profiles).
  - `afdf878` cherry-pick of `3ee48b0` — static VRAM fallback `_KIND_VRAM_FALLBACK` for clusters without hyphenated `AvailableFeatures`.
- **Phase 4d.2-A** (landed 2026-05-10): three small fixes that unblocked LRZ + a fresh `configs/lrz.toml`.
  - `2513cb3` `parse_df_multi` dedupes by source, not mount path (LRZ `home` and `scratch` share path `/dss/dsshome1` → were collapsing into one row).
  - `36ec185` chunked `update_snapshot` keeps the UI responsive during broadcast (LRZ's slow first paint was starving keystroke dispatch).
  - `ad4f38d` `parse_node` tolerates `N/A` in numeric fields (LRZ DOWN nodes were crashing the parser).
  - `f925402` `configs/lrz.toml` — `exec = "ssh:lrz"`, df-only storage (no `quota`), bumped first-refresh interval for the cold-ProxyJump ≈ 10.5 s window.
  - `0897219` `users = ["self"]` resolved by the executor's `whoami` (so a config can be `users = ["self"]` and Just Work on whichever cluster).
- **Phase 4d.2-B** (landed 2026-05-10): generic `[[nodes_table.columns]]` schema — per-node storage columns are no longer hardcoded ssd/hdd in `nodes_table.py`. Each cluster declares its own columns in TOML; the only handler today is `source = "storage_prefix"` (matches snapshot.storage entries with path `<prefix>/<node>`). LRZ omits the section entirely → no SSD/HDD columns. Rohan declares two columns (`/cluster`, `/cluster_HDD`) → identical layout to before. Filter expressions auto-pick up `<col.id>_free / _used / _total` per declared column.
- **Phase 4d.2-C** (landed 2026-05-10): explicit `group` field on `[[storage.entries]]` + LRZ `kind = "dssusrinfo"` per-user quota.
  - `_classify` honors `entry.group` first; rohan's path-based fallback (`/cluster` → ssd, `/cluster_HDD` → hdd) still drives auto-discovered mounts.
  - `StoragePanel.GROUP_ORDER` extended to `("home", "ssd", "hdd", "scratch", "persistent", "other")`.
  - `kind = "dssusrinfo"` (LRZ login) accepts `mode = "home"` (uses `dssusrinfo dsshome`) or `mode = "container"` with `container = "<name>"` (uses `dssusrinfo container_usage`). Parser is tolerant: empty input / unknown container / unbounded total all return None so a transient hiccup doesn't crash the tick.
  - Combined-collector gotcha: a section value with `;` chained commands MUST be wrapped in an inner `( ... )` subshell — `( a; b ) > FILE` redirects the whole subshell, but `( a; b > FILE )` only redirects `b`. Multi-subcommand sections without the wrap silently drop the first command's output. Locked by `test_fetch_combined_dssusrinfo_section_uses_inner_subshell`.
  - LRZ TOML now: home (dssusrinfo dsshome) + scratch (df aggregate — dssusrinfo doesn't cover scratch) + MCML DSS (dssusrinfo container_usage). Smoke verified: home 89%, scratch 78%, persistent 98%.
- **Phase 4d.2-E** (landed 2026-05-11): perceived-latency overhaul. Six commits in two clusters:
  - **F.1 / F.2** — diagnostic fix + non-numeric job-ID sort. `_broadcast_snapshot`'s error handler made traceback emission additive (never substitutive over the perf-row error marker) — caught 1192 silent `widget,JobsTable,error` rows over a 73-min live session. Root cause then revealed as `_sort_value` raising `TypeError` because sacct returned array-task / step / het IDs that didn't `int()`-parse; fix returns `(int_prefix, full_id_string)` tuples so all sort keys are comparable. Pinned by `tests/test_jobs_table_sort.py`.
  - **H.1 / H.2** — `app.mode` + `app.sort` reactives + delete three sync `self.update_snapshot(...)` calls on the async `update_snapshot`. Reproduced the user's 10 s `a → t` outlier (silent dropped coroutine waiting for next 15 s tick to land); after fix the worst-case visible-time dropped from 13013 ms → 3062 ms.
  - **A** — `OverviewPanel._relayout` cache-key split into `layout_key` (mounts) and `height_key` (in-place styles). Bare `_job_count` flips that don't cross thresholds now skip `_populate` entirely. Helps mode flips (zero `_job_count` change) most; heavy Mine→All flips that cross the util_side threshold still remount (unavoidable — UtilizationPanel actually moves columns).
  - **G** — `_broadcast_snapshot(snap, *, batch=True)` parameterized. Tick path keeps default (flicker-free 5 s/15 s refresh); click watchers pass `batch=False` for progressive paint. Closes the ~830 ms gap between fanout/all end and first-visible content.
  - Black-box measurements: LRZ user `a → 3 s wait → t` composite worst-case 13013 ms (pre) → 3026 ms (post). `vis_t` since-`t` 6500 ms blank → 100 ms. `vis_a` 1500 ms → 200–500 ms. Tick stability unaffected.
- **Phase 4e+** (not yet started): multi-cluster SSH wiring.

## Layout defaults

`[layout]` is optional in cluster TOML configs (since 5c0109f). Omit it to get the rohan-style default:
- Overview → `overview_panel` (responsive 1- or 2-col composition).
- Jobs → `jobs_table`.
- Nodes → `nodes_summary` above `nodes_table`.
- Storage → `storage_panel`.

Cluster-specific TOMLs (e.g. `/tmp/wsl_single_v2_4c_ssh_config.toml`) carry only cluster-specific knobs (ssh entry, storage paths). Override `[layout]` only when a cluster has unusual UI needs.

## Config knobs

- **`[refresh]` intervals** — `slurm_jobs = 5` / `slurm_nodes = 30` / `storage = 60` (seconds). LRZ bumps `slurm_jobs` to ≥ 15 to absorb the ~10 s cold-ProxyJump on first ssh round-trip.
- **`[refresh.sacct]`** (Bundle 2 B2.1, 2026-05-11) — nested table with four dotted keys: `starttime.self` (default `"now-7days"`), `starttime.all` (default `"now-1day"`), `max_rows.self` (default `null` = no cap), `max_rows.all` (default `null` = no cap). Two parallel sacct queries run per tick (same ssh round-trip): `-u $USER --starttime=<self>` populates `snap.recent_jobs_self`, `-a --starttime=<all>` populates `snap.recent_jobs_all`. JobsTable's Recent mode reads from whichever the `mine_only` toggle selects → mine_only flip on Recent becomes a snapshot SWAP, not a refetch. Bundle 1's `[refresh] sacct_max_rows` field is REMOVED — replaced by the two per-snapshot caps. Snapshot field `recent_jobs` is REMOVED; readers must migrate to `recent_jobs_self` or `recent_jobs_all`.

## Smoke-test pane

The canonical Phase 4c+ smoke pane is `rohan:rb-v2-4c-ssh` (NOT `rohan:rohanboard-wsl` — that one is the older `clean-reimpl` multi-cluster build with `c Cycle cluster` binding). LRZ smoke runs in a separate window `rohan:rb-v2-lrz` so it doesn't clobber the rohan pane.

Restart cleanly via `tmux respawn-pane -k`:

```
tmux respawn-pane -k -t rohan:rb-v2-4c-ssh \
  'cd ~/workspace/rohanboard-local && uv run rohanboard --config configs/rohan.toml'
tmux respawn-pane -k -t rohan:rb-v2-lrz \
  'cd ~/workspace/rohanboard-local && uv run rohanboard --config configs/lrz.toml'
```

(Per `tmux_ops.md` §from feedback_tui_restart_pattern — `respawn-pane` is the clean restart, NOT send-keys + q.)

After respawn, capture-pane after ~10 s to confirm: cluster name in header, Storage `home` populated, Jobs row visible, NodesSummary table populated, footer reads `q Quit r Refresh 1 Overview 2 Jobs 3 Nodes 4 Storage`.

- **LogTailScreen reads via the executor, not local file IO (Bundle 3 B3.5, 2026-05-11)** — pre-B3.5 the `l`-key log-tail screen called `Path.exists()` / `Path.open()` directly. Under `exec = "ssh:rohan"` the slurm log path lives on the cluster — local stat always failed → "[file does not exist yet]" printed every poll. Post-fix `LogTailScreen._poll_stream` runs a bash one-liner (`stat -c %s` + `tail -c +<cursor+1>`) via `self.executor.run([...])`, multiplexing onto the same SSH connection the collectors use. Sister-paths audit (2026-05-11): `log_tail.py` was the ONLY executor-bypass site — all other Path.open usage in rohanboard reads local config / debug logs intentionally.
- **LogTailScreen dedups repeated identical error messages (Bundle 3 B3.5)** — `_maybe_write_error(stream, kind, msg)` sets `self._last_error[stream] = kind` on print, suppresses on identical re-prints, clears on a successful read. Kinds: `"missing"`, `"exec_failed"`, `"exec_rc"`, `"malformed"`, `"no_path"`. A different kind (e.g. file existed then permission denied) prints the new error.

## Lint scripts

- **`scripts/lint_sync_call_on_async.py`** (Bundle 1 Sub-fix 5, 2026-05-11) — AST-walks `rohanboard/` for the Phase-H anti-pattern: a sync method calling `self.<async_method>(...)` with the result discarded (coroutine silently dropped). Excludes `@work`-decorated methods (Textual makes them sync-callable). Suppress a known-and-deferred site with a trailing `# noqa: lint-async-call`. The lint's OWN behavior is pinned by `tests/test_lint_sync_call_on_async.py`; the CODEBASE is pinned green by `tests/test_lint_codebase_async.py` (Bundle 3 B3.2) — a regular pytest case that invokes the script via subprocess. New Phase-H bugs fail `uv run pytest` directly; no separate CI runner needed.
- **NodesTable Phase-H sister-paths** (Bundle 2 B2.4, 2026-05-11) — `nodes_table.py:on_sortable_header_sort_changed` previously called sync `self.update_snapshot(...)` on the async method (Phase-H bug shape). Now flips `app.nodes_sort: reactive[tuple[str, str, bool]]` and `App.watch_nodes_sort` re-broadcasts. `_apply_filter_debounced`'s exception fallback is a no-op (matches the JobsTable shape Phase 4d.2-E step H established). All Bundle-1 `# noqa: lint-async-call` suppressions in this file are GONE — lint runs naturally green.

## Testing

- **Unit + Pilot tests:** `uv run pytest tests/`. As of 2026-05-11 (post-4d.2-E): 111/111 passing.
- **Filter-drop regression test:** `tests/test_input_drops.py` exercises both 600 ms (synchronous-blocker) and 50 ms (debounce-coalesce) rhythms on JobsTable and NodesTable filters. The 600 ms test is the canonical drops repro per `~/.claude/projects/-home-hli/memory/rohanboard.md` §"Filter-bar character-drop variant".
- **Live tmux drops test:** see the memory file for byte-offset oracle / SGR mouse-click recipes if you need to repro on a real terminal.

## Known gotchas

- **`kind = "quota"` requires `exec = "ssh:rohan"` (login node)** — the `quota` binary is NOT installed on slurm/compute nodes, so a slurm-targeted ssh exec would silently fall back to "no quota info yet". Whenever a config carries a `[[storage.entries]]` with `kind = "quota"`, the `exec` MUST point at a host that has the `quota` binary. For rohan that's the login node alias `rohan`.
- **Filter Input id is `"filter"`, not `"filter-input"`** — both `JobsTable` and `NodesTable` mount their filter Input with `id="filter"`. Test code that does `pilot.click("#filter-input")` will silently fail. Query within the active widget: `app.query_one(JobsTable).query_one("#filter", Input)`.
- **`l` is bound to `action_jobs_tail_log` on the Jobs tab** (scoped via `App.check_action`). Test strings containing `q`/`r`/`a`/`t`/`l` are unsafe if focus drifts off the filter Input. The filter-drop repro string was historically `testhello` (contains `l`); switched to **`xenoview`** in Bundle 3 B3.4 (2026-05-11) — zero collisions with the App's `q r a t l` bindings. Memory recipe and `tests/test_input_drops.py` both use the new string.
- **CompactJobs height = `_job_count` on Overview (Bundle 2 B2.5, 2026-05-11)** — `OverviewPanel.update_snapshot` sets `self._job_count` from `cluster_user`-filtered jobs UNCONDITIONALLY (matches B2.3's content path on CompactJobs). Pre-B2.5 it read `app.mine_only` and on `mine_only=False` fell through to `len(snap.jobs)` (all-users count), so `_jobs_natural()` reserved space for the all-users count while CompactJobs's body rendered only the user's jobs → large blank below. If you add a new sizing path for any Overview child widget, anchor it on what the widget will ACTUALLY render, not the global toggle.
- **`watch_filter_text` is now a no-op** — debounce in `on_input_changed` drives the rebuild via `run_worker(_apply_filter_async, exclusive=True)`. The reactive write still fires the watcher, but the watcher does nothing; do NOT re-add a synchronous rebuild there.
- **Per-node Nodes-tab columns are config-driven (Phase 4d.2-B)** — `[[nodes_table.columns]]` in the cluster TOML declares them. Today the only `source` is `"storage_prefix"`, which matches `snapshot.storage` entries whose path is `<prefix>/<node>`. To add a new source: add a branch in `NodesTable._build_extras_by_id` + (if not a StorageEntry) `NodesTable._row_for_node`; the config schema (`NodesTableColumnConfig`) is already extensible. Bundle 3 B3.3 (2026-05-11) made the filter-help modal also config-driven — `build_nodes_filter_spec(cfg.nodes_table.columns)` builds numeric fields + example tokens from the declared columns. LRZ (no columns) sees a generic `mem_free>=500G` example; rohan sees `ssd_free>=1T` from its first declared column.
- **Storage groups are config-driven (Phase 4d.2-C)** — `group = "home"|"ssd"|"hdd"|"scratch"|"persistent"|"other"` on each `[[storage.entries]]` picks the StoragePanel render bucket. Without it, the path-based heuristic still works for rohan's `kind = "auto"` mounts. Adding a new group: append to `StoragePanel.GROUP_ORDER` and `_GROUP_TITLES` — entries with an explicit `group` matching a NEW value will land there automatically; ones with an unknown group bucket into "other".
- **kind = "dssusrinfo" on LRZ (Phase 4d.2-C)** — runs `dssusrinfo dsshome` (mode="home") or `dssusrinfo container_usage` (mode="container", with `container = "<name>"`). The combined collector chains the needed subcommands inside ONE ssh channel via an inner subshell `( a; b ) > FILE` so the redirect captures every subcommand's stdout (a `( a; b > FILE )` shape silently drops earlier commands). LRZ login is the only host with the dssusrinfo binary; running with `kind = "dssusrinfo"` against any other cluster will return empty parser output → no entry for that tick.

## Known follow-ups (planned but not yet implemented)

- (none currently — Bundle 3 B3.1 resolved the "loading…" placeholder follow-up.)
