# rohanboard — local project runbook

This file is the project-local runbook for `rohanboard` on `clean-reimpl-v2`. Read before acting; update proactively with anything future-you would otherwise have to ask the user about.

## Design principles

> **Click → first visible content target: ~200 ms. Full render can take longer. If a full render exceeds the budget, prefer progressive rendering (first ~50 rows in ~200 ms, remainder in the background) over delaying everything until ready. The dashboard should always feel fast, even when the data is large.**

This is load-bearing for architectural decisions. The data path (5 s tick, snapshot reactive, fanout broadcast) is decoupled from the render path on purpose — clicks should re-render from the cached snapshot, not refetch. When proposing optimizations, anchor on "what does the USER see in the first 200 ms" rather than "what's the total wall-clock to full table re-render". Phase 4d.2-E step 2.5 added `perf_block` instrumentation for this — run with `--debug` (or `ROHANBOARD_PERF_LOG=…`), the `filter/<path>_<mode>` rows carry `first_50_ms=…` and `chunks=…` extras so the breakdown is mechanical, not eyeballed.

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
- **Phase 4e+** (not yet started): multi-cluster SSH wiring.

## Layout defaults

`[layout]` is optional in cluster TOML configs (since 5c0109f). Omit it to get the rohan-style default:
- Overview → `overview_panel` (responsive 1- or 2-col composition).
- Jobs → `jobs_table`.
- Nodes → `nodes_summary` above `nodes_table`.
- Storage → `storage_panel`.

Cluster-specific TOMLs (e.g. `/tmp/wsl_single_v2_4c_ssh_config.toml`) carry only cluster-specific knobs (ssh entry, storage paths). Override `[layout]` only when a cluster has unusual UI needs.

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

## Testing

- **Unit + Pilot tests:** `uv run pytest tests/`. As of 2026-05-10 (post-4d.2-C): 86/86 passing.
- **Filter-drop regression test:** `tests/test_input_drops.py` exercises both 600 ms (synchronous-blocker) and 50 ms (debounce-coalesce) rhythms on JobsTable and NodesTable filters. The 600 ms test is the canonical drops repro per `~/.claude/projects/-home-hli/memory/rohanboard.md` §"Filter-bar character-drop variant".
- **Live tmux drops test:** see the memory file for byte-offset oracle / SGR mouse-click recipes if you need to repro on a real terminal.

## Known gotchas

- **`kind = "quota"` requires `exec = "ssh:rohan"` (login node)** — the `quota` binary is NOT installed on slurm/compute nodes, so a slurm-targeted ssh exec would silently fall back to "no quota info yet". Whenever a config carries a `[[storage.entries]]` with `kind = "quota"`, the `exec` MUST point at a host that has the `quota` binary. For rohan that's the login node alias `rohan`.
- **Filter Input id is `"filter"`, not `"filter-input"`** — both `JobsTable` and `NodesTable` mount their filter Input with `id="filter"`. Test code that does `pilot.click("#filter-input")` will silently fail. Query within the active widget: `app.query_one(JobsTable).query_one("#filter", Input)`.
- **`l` is bound to `action_jobs_tail_log` on the Jobs tab** (scoped via `App.check_action`). Test strings containing `q`/`r`/`l` are unsafe if focus drifts off the filter Input. Test string `testhello` contains `l` — Pilot keeps focus stable so it's fine in-process, but be cautious in tmux real-terminal tests.
- **`watch_filter_text` is now a no-op** — debounce in `on_input_changed` drives the rebuild via `run_worker(_apply_filter_async, exclusive=True)`. The reactive write still fires the watcher, but the watcher does nothing; do NOT re-add a synchronous rebuild there.
- **Per-node Nodes-tab columns are config-driven (Phase 4d.2-B)** — `[[nodes_table.columns]]` in the cluster TOML declares them. Today the only `source` is `"storage_prefix"`, which matches `snapshot.storage` entries whose path is `<prefix>/<node>`. To add a new source: add a branch in `NodesTable._build_extras_by_id` + (if not a StorageEntry) `NodesTable._row_for_node`; the config schema (`NodesTableColumnConfig`) is already extensible. The filter help modal `NODES_FILTER_SPEC` still hardcodes ssd/hdd as example numeric fields; harmless on LRZ (those tokens just match nothing) but a small UX wart for non-rohan clusters.
- **Storage groups are config-driven (Phase 4d.2-C)** — `group = "home"|"ssd"|"hdd"|"scratch"|"persistent"|"other"` on each `[[storage.entries]]` picks the StoragePanel render bucket. Without it, the path-based heuristic still works for rohan's `kind = "auto"` mounts. Adding a new group: append to `StoragePanel.GROUP_ORDER` and `_GROUP_TITLES` — entries with an explicit `group` matching a NEW value will land there automatically; ones with an unknown group bucket into "other".
- **kind = "dssusrinfo" on LRZ (Phase 4d.2-C)** — runs `dssusrinfo dsshome` (mode="home") or `dssusrinfo container_usage` (mode="container", with `container = "<name>"`). The combined collector chains the needed subcommands inside ONE ssh channel via an inner subshell `( a; b ) > FILE` so the redirect captures every subcommand's stdout (a `( a; b > FILE )` shape silently drops earlier commands). LRZ login is the only host with the dssusrinfo binary; running with `kind = "dssusrinfo"` against any other cluster will return empty parser output → no entry for that tick.

## Known follow-ups (planned but not yet implemented)

- **JobsTable should show "loading…" placeholder before the first parse completes** (especially on LRZ where parsing is slow at `mine_only=False`). Currently the empty state is indistinguishable from "no jobs found", confusing the user during the cold-tick window. Discriminate via a snapshot field like `snap.jobs_loaded: bool` (default False until first successful parse) and render a "loading…" row in the DataTable while False. Same treatment for NodesTable + StoragePanel if any of them have observably-slow first ticks. Surfaced 2026-05-10 by the user after watching LRZ first paint.
