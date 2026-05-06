# rohanboard — runbook for Claude

## Where it lives
- Source on **rohan** at `~/workspace/rohanboard`. From WSL, edit via the sshfs mount at `~/mounts/rohan/workspace/rohanboard/`.
- **WSL-local clone** at `~/workspace/rohanboard-local/` — used to run rohanboard locally on WSL pointing at `ssh:slurm`+`ssh:lrz` (multi-cluster TOML). Tracks the same `origin/main` but commits to it stay LOCAL until manually pushed.
- Running TUI lives in tmux session `rohan` window `monitor` (the dashboard is meant to stay running there). Don't kill the user's running instance casually — they read it. Press `r` to refresh after a code change, or `tmux respawn-pane -k` if a full restart is needed.
- WSL-local instance lives in `tmux:rohan:rohanboard-wsl` (yes, named "rohan" session even though it's local — the session name is just where the user collects monitoring panes). Launch:
  ```
  cd ~/workspace/rohanboard-local && uv run rohanboard --config /tmp/wsl_multi_config.toml
  ```
  Restart with `tmux respawn-pane -k -t rohan:rohanboard-wsl '<launch cmd>'`. NEVER `send-keys 'q'` (mouse-event leak risk).

## Run / test
- All Python tooling on rohan goes through `uv`. Tests:
  ```
  tmux_exec rohanboard "cd ~/workspace/rohanboard && uv run --quiet pytest -q"
  ```
- The `rohanboard` tmux session has its own ssh window — use `:auto` (rename `login` to `auto` once and reuse). The `:app` window holds a foreground TUI instance for ad-hoc debugging.

## Executor abstraction (since 2026-05-04)
- All collectors take an `Executor` (see `rohanboard/exec.py`) as their first argument. `LocalExecutor` is the only fully-functional impl today; `SSHExecutor` is a stub (constructor-only) that raises `NotImplementedError` on use — the real impl lands in step 3 of the multi-cluster plan (`/tmp/rohanboard_lrz_plan_v2.md`).
- `RohanBoardApp.__init__` resolves `self._cluster` from `cfg.clusters` (with optional `cluster_id`) and sets `self.executor = self._cluster.executor`. The executor is threaded into every `slurm.fetch_*` / `storage.fetch_*` / `storage.discover_mounts` call. `LogTailScreen` reads `self.app.executor` for `fetch_job_info` / `fetch_job_script`.
- `LocalExecutor.run` raises `RuntimeError` on non-zero rc (with `rc=N` in the message). Callers that historically tolerated non-zero (`fetch_quota`: over-quota; `fetch_df`: missing path) catch `RuntimeError` and fall through to a parse or return None.
- `discover_mounts` is now async (reads `/proc/mounts` via `executor.run(["cat", ...])`) so it works for both local and future SSH executors. Callers must `await`.
- New tests live in `tests/test_exec.py`. `pytest-asyncio` is now a dev dep with `asyncio_mode = "auto"` in pyproject.

## Multi-cluster config (step 2, since 2026-05-04)
- Per-cluster fields live on `Cluster` (rohanboard/cluster.py): `id`, `title`, `executor`, `slurm` (SlurmFilterConfig), `storage_entries`, `refresh`, `log_path_map`, `storage_mode`. Top-level `Config` keeps shared fields (`layout`, `theme`, `overview`, `presets`) plus `clusters: list[Cluster]`.
- New TOML schema (multi-cluster):
  ```toml
  [[clusters]]
  id = "rohan"
  title = "rohan"
  exec = "local"            # or "ssh:<host>"
  [clusters.slurm]          # same shape as legacy top-level [slurm]
  users = ["self"]
  [clusters.refresh]        # optional override
  slurm_jobs = 5
  [clusters.storage]
  mode = "auto"             # or "pinned" (default)
  [[clusters.storage.entries]]
  label = "..."; kind = "df"; path = "..."
  [[clusters.log_path_map]]
  remote = "/dss/dsshome1"; local = "/home/hli/mounts/lrz/dsshome1"
  ```
- Legacy single-cluster TOML (top-level `[slurm]` / `[[storage.entries]]` / `[refresh]`) is silently rewritten into one cluster with `id="rohan"`, `title="rohan"`, `exec="local"`. The user's existing `~/.config/rohanboard/config.toml` keeps working unchanged.
- `Config.slurm` / `Config.storage_entries` / `Config.refresh` are back-compat properties proxying to `clusters[0]`. New code should prefer `app._cluster.<field>`.
- CLI: `--cluster <id>` selects a specific cluster from a multi-cluster TOML. With no flag, the first cluster wins; if there are 2+ clusters in the TOML, a warning is logged ("multi-cluster UI not yet implemented (step 3)").
- `exec = "ssh:<host>"` constructs a stub `SSHExecutor` — the cluster parses, but actually rendering it raises `NotImplementedError`. Pick a single cluster via `--cluster <id>` to dodge the stub until step 3 ships the real SSH path.
- Tests: `tests/test_config.py` covers legacy → single-cluster shim, multi-cluster parse, storage entries + log_path_map, duplicate id detection.

## Snapshot job lists (since 2026-05-05)
- `Snapshot.jobs` is what the Jobs tab displays — honours the `mine_only` toggle (server-side via `-u $USER`).
- `Snapshot.jobs_mine` is ALWAYS the current user's active jobs only — Overview's "Active jobs" card reads this.
- Refresh tick: 1 squeue call when `mine_only=True` (lists are aliased), 2 calls when `mine_only=False`.
- This avoids the bug where Overview leaked all-users' jobs whenever the Jobs tab was set to "all".

## Overview layout — wide+scroll fallback (since 2026-05-05)
- `_relayout` caps the right column's natural height at the window height in WIDE mode. CompactJobs has an internal `VerticalScroll`; when the right card is `flex` (1fr), its body just scrolls.
- Without the cap, a 100+-job count pushed `natural_need > h`, flipped to `scroll` mode, and `OverviewPanel.scroll #main { layout: vertical }` collapsed the 2-col grid into a single column — squeezing the Home pane.
- Wide mode now reports `fit=True` whenever the LEFT column (Totals + Home, both bounded heights) fits. Only narrow mode and tiny-height fallback flip to scroll.
- See `tests/test_overview_layout.py` for the static decision-table check.

## Step 3 — Per-cluster UI (since 2026-05-04)
- `RohanBoardApp.snapshots: dict[str, Snapshot]` — keyed by cluster id; per-cluster `_histories` for utilization sparklines. The legacy reactive `self.snapshot` is now a *view* — it always holds whichever snapshot belongs to `self.active_cluster_id`. Set on each refresh tick (after all clusters parallel-fan-out) and on `c` keypress.
- `RohanBoardApp._refresh_all` fans out across ALL clusters in parallel via `asyncio.gather(*[self._refresh_cluster(c) ...])`. Each `_refresh_cluster(cluster)` builds a fresh Snapshot using `cluster.executor` + `cluster.slurm` filter + `cluster.storage_entries`, then awaits `_broadcast_to_cluster(cluster.id, snap)` which scopes the widget fan-out to widgets *under that cluster's outer TabPane*. A single-cluster TOML keeps the old behaviour because `_is_under_pane` is only consulted in multi mode.
- `compose()` branches on `len(self.cfg.clusters)`:
  - 1 cluster → unchanged single-cluster `TabbedContent(initial=tabs[0].id)` with bare TabPane ids (`overview`, `jobs`, …). Regression-safe.
  - 2+ clusters → outer `TabbedContent(id="cluster_tabs")` with `TabPane(id="cluster_<cid>")` per cluster, each containing an inner `TabbedContent(id="inner_tabs_<cid>")` with `TabPane(id="<cid>__<tab.id>")`. The double-underscore separator survives a clean `split("__", 1)`.
- Hotkey `c` → `action_cycle_cluster`: cycles active id, sets the outer `TabbedContent.active`, re-fires `refresh_bindings`. Hidden via `check_action` when there's only 1 cluster (no `c` in the footer in single-cluster mode). The numbered 1..9 hotkeys still pick *inner* tabs of the *active* cluster (`action_show_tab` reaches into the right inner `TabbedContent` by id).
- Per-widget cluster awareness: `JobsTable._owning_cluster_id()` and `OverviewPanel._owning_snapshot()` walk up the parent chain to find the outer `cluster_<id>` TabPane. JobsTable's "Mine only" toggle now writes to `app.mine_only[cid]` (per-cluster dict) so flipping it on rohan doesn't unexpectedly change the LRZ refetch behaviour.
- `app.mine_only` is now `dict[str, bool]` (was `bool`). Single-cluster code can still do `app.mine_only[<single_cid>]`.
- `app.executor` is now a property → `self._cluster.executor` → the *active* cluster's executor. `LogTailScreen` reads `self.app.executor` so log-tail follows whichever cluster the JobsTable lived in (it was already the active one when the user double-clicked).
- `SSHExecutor` is now a real implementation (was stubbed in step 2). Defaults: `BatchMode=yes`, `ServerAliveInterval=30`/`Count=3`, `ControlMaster=auto`, `ControlPersist=10m`, `ControlPath=~/.ssh/cm-%r@%h:%p`. `bash -lc` wrapper by default so login-shell env vars (`$MCMLSCRATCH` on LRZ) resolve. `use_login_shell=False` to bypass.
- The test multi-config at `~/test_multi_config.toml` deliberately uses `exec = "local"` for both clusters — exercises the outer-tab UI without needing rohan→lrz SSH. Will flip to `exec = "ssh:lrz"` in step 4 once LRZ-specific parsers (Gres regex extension, dssusrinfo) land.
- Multi-cluster review in `tmux:rohan:rohanboard-v2-multi`. Single-cluster regression review in `tmux:rohan:rohanboard-v1-`.

## Step 4 — GPU column + LRZ storage (since 2026-05-04)
- **GPU column (universal)**: `_NODE_COLUMNS` in `widgets/nodes_table.py` carries a `gpu_kind` column (label "GPU") between Partition and CPU. Renders `<kind> <vram>` (LRZ: "H100 92GB"), `<kind>` (rohan classic: "rtx_a6000"), or `—` (CPU node). Sortable lexicographically by kind then numerically by VRAM via `_gpu_kind_vram_sort_key`.
- **NodesSummary._cluster_totals** keys GPU rows by `g.display` so A100 80GB and A100 40GB show as **separate rows** on LRZ. On rohan, `vram=None` → keyed by kind alone.
- **GPU regex** (`collectors/slurm.py:_GRES_GPU_RE`): kind segment is *optional* — matches `gpu:rtx_a6000:8` (rohan classic, kind="rtx_a6000"), `gpu:8(S:0-1)` (LRZ classic, kind=""), `gpu:3g.20gb:16(S:0-1)` (MIG profile, kind="3g.20gb"). Trailing `(S:...)` socket-locality suffix optional.
- **AvailableFeatures regex** (`_AVAIL_FEAT_GPU_RE`): `^([A-Za-z0-9]+)-(\d+(?:\.\d+)?[A-Za-z]+)$` — fills in (kind, vram) for nodes whose Gres lacks the kind segment. Matches `H100-92GB`, `A100-80GB`, `V100-16GB`. CPU-only features (`CPU-350GB`, `CPU-AMD`) are explicitly skipped (callers gate on `not f.startswith("CPU-")`).
- **Resolution order** in `_parse_node_block`: (1) Gres has KIND segment → kind from Gres, vram from AvailableFeatures only if kind matches; (2) Gres lacks kind → both from AvailableFeatures; (3) neither → empty GpuSpec list (CPU node renders "—").
- **LRZ storage auto-discovery** (`collectors/storage.py:discover_lrz_storage`): in 2 SSH round-trips it (a) runs `bash -lc 'echo HOME=$HOME; echo MCML=$MCMLSCRATCH; echo ---DSSUSRINFO_BEGIN---; dssusrinfo all'` and parses out env vars + dssusrinfo body, (b) runs `df -B1` on each path in parallel. Quotas from dssusrinfo override `total_bytes`; df's `avail_bytes` is preserved for the writable-amount column. Returns a `list[StorageEntry]` ordered home, scratch, then containers alphabetically.
- **`parse_dssusrinfo`** is **defensive** by design — undocumented format, missing sections leave fields empty rather than raising. Banner-delimited sections (`*****` blocks); section titles matched case-insensitively after `.lower().rstrip(":")`. Triggers on `"following dss containers"`, `"dss container usage"`, `"dss homedir info"`. Quota row regex tolerates `"Container MAX"` and `"Unlimited"` (returns `None`).
- **App.refresh tick** (`app.py:_grab_storage_impl`): when a cluster's storage entries include `kind == "auto_lrz"`, calls `discover_lrz_storage(executor)` and merges results into `snap.storage`. The label `auto_lrz` in TOML is just a free-form discriminator — `cluster.py:StorageEntryConfig.kind` accepts any string.
- **Test coverage**:
  - `tests/test_slurm.py` step-4 block: 8 tests for the new GPU column — H100/A100-80/A100-40/V100/CPU LRZ nodes, plus rohan a100/a6000 regression. Each fixture is a 1-node `scontrol show node <name>` capture under `tests/fixtures/lrz_node_*.txt` and `rohan_node_*.txt`.
  - `tests/test_storage.py`: `parse_dssusrinfo` happy path + empty-input defensive case.
  - `tests/test_storage_lrz.py`: end-to-end `discover_lrz_storage` with a `FakeExecutor` that hands back canned dssusrinfo + df responses; covers home/scratch/container assembly and missing-dssusrinfo fallback.
- **Test multi-config** at `~/test_multi_config.toml` is on **rohan** (not WSL): rohan/local + lrz/`ssh:lrz` + `[[clusters.storage.entries]] kind = "auto_lrz"`. The lrz tab populates from rohan via the `lrz` ssh alias on rohan.
- **Review windows**: `tmux:rohan:rohanboard-v1` (single-cluster regression) + `tmux:rohan:rohanboard-v2-multi` (full multi-cluster, press `c` to switch to LRZ).

## SSH lifecycle (since 2026-05-05) — wedge protection
- `SSHExecutor` (`rohanboard/exec.py`) bounds the ControlMaster to the rohanboard process. Three coupled mechanisms — see commit "Bind ssh mux lifetime to rohanboard process":
  1. **`ControlPersist=60`** (was `10m`). An idle mux self-cleans 60 s after the last call. Means a wedged mux can't outlive python by more than a minute even if shutdown hooks somehow miss it.
  2. **Pre-flight `ssh -O check` + atexit/SIGTERM `ssh -O exit`.** Every `.run` checks the mux is alive (3 s budget) before reusing it; if dead, the socket is `os.unlink`'d and a fresh master created. On shutdown (`atexit` + chained SIGTERM/SIGINT handlers), every registered executor runs `ssh -O exit <host>` and unlinks its socket. Empty socket dir gets `rmdir`'d.
  3. **In-flight semaphore (`asyncio.Semaphore(1)` per host).** At-most-one probe in flight per cluster. Without this, when the mux wedged on 2026-05-05 the render loop fired a fresh ssh probe every tick — 1,259 stuck `ssh` children accumulated in 24 min, all hanging in `n_tty_read` on the dead socket. The semaphore makes a slow probe block the next dispatch (caller surfaces "stale" via its own `wait_for`) instead of stacking.
- Socket dir is `/tmp/rohanboard-ssh-cm/`. Wedge-recovery from CLI: `pkill -f 'ssh.*ControlPath=/tmp/rohanboard-ssh-cm'; rm -rf /tmp/rohanboard-ssh-cm/; tmux respawn-pane -k -t rohan:rohanboard-wsl '<launch cmd>'`.
- Why `ControlPersist=yes`/`infinite` was wrong: it orphans the mux to `systemd` (PID 1) so `kill <python_pid>` doesn't stop it. Across a python restart, the orphan mux keeps owning the socket file at `/tmp/rohanboard-ssh-cm/cm-…`, but the ssh server side may have died — every new `BatchMode=yes` connection through that socket hangs forever in `n_tty_read`. Bounded persist + explicit `-O exit` on shutdown makes mux lifetime ⊆ python lifetime + 60 s.
- Tests: `tests/test_app_multi_cluster.py` lifecycle block — `test_ssh_executor_default_persist_is_finite`, `test_ssh_executor_registers_for_shutdown`, `test_ssh_executor_shutdown_master_runs_ssh_O_exit`, `test_ssh_executor_inflight_semaphore_caps_at_one`, `test_ssh_executor_preflight_check_unwedges_dead_mux`, `test_mux_ctl_argv_uses_same_control_path`, etc. All mock `subprocess.run` / `asyncio.create_subprocess_exec` — no real ssh.
- Note: the slurm-side rohanboard repo (`~/workspace/rohanboard` on slurm) has the same `exec.py` from the step-3 SSHExecutor work but **DOES NOT** yet have this lifecycle patch. If/when the user runs rohanboard remotely against another cluster, sync the same patch over.

## Storage accounting (gotcha)
- `StorageEntry.free_bytes` returns df's `Available` column when set (i.e. **excludes reserved-for-root blocks**, ~5% on ext, but can be 100x on full filesystems with strict reservations like the cluster's balar). Falls back to `total - used` only for quota entries (no avail).
- The per-node Nodes table (`widgets/nodes_table.py:_storage_cell`) renders `entry.free_bytes` — the writable amount.
- The Cluster totals card (`widgets/nodes_table.py:NodesSummary.update_snapshot`) must aggregate `e.free_bytes` per mount, **not** `total_bytes - used_bytes`. Otherwise the totals card overstates free by gigabytes-to-terabytes vs. what the table shows. Fixed 2026-04-29.

## 8-issue review fixes (since 2026-05-05)

### `ssh:slurm` is wrong for monitoring rohan — use `ssh:rohan` (#7)
- `ssh slurm` is the *sshd-job on a compute node* (per `reference_rohan_workflow.md`); it can't see most of `/cluster/`, can't see `/cluster_HDD/` at all, and `quota` errors out (`No such file: /local/aquota.user`).
- For rohanboard monitoring, `exec` MUST point to a head/login node. The WSL multi-config at `/tmp/wsl_multi_config.toml` was patched to `exec = "ssh:rohan"` on 2026-05-05.
- The `slurm` alias is still the right ssh target for *executing* sbatch / interactive work — but NOT a data source for rohanboard.

### Coalesce per-host data into one ssh round-trip per refresh tick (#1)
- `Semaphore(1)` per host is correct (wedge protection). Don't widen it. Fix interaction lag at the COLLECTOR layer: gather all needed paths, then make ONE `df -B1 path1 path2 ... pathN` call (or analog).
- `collectors/storage.fetch_df_multi(executor, [(label, path), ...])` is the canonical helper. `parse_df_multi(text, paths)` demuxes the multi-row output, walking up the path tree to the actual mount when df reports a parent directory.
- Two consumers updated: `app._grab_storage_impl` for `kind == "auto"` (rohan) and `discover_lrz_storage` for LRZ home/scratch/containers. Refresh tick on rohan dropped from ~24×RTT to 1×RTT.
- Pattern generalises: ANY future per-host data (sinfo-by-partition, scontrol-show-job batch, etc.) should follow "collector batches paths/items, executor runs once". Don't sidestep the semaphore.

### MIG nodes aggregate at node level — no per-profile alloc (#5)
- Slurm's AllocTRES exposes only ONE flat `gres/gpu=N` for a MIG node. There is NO per-profile breakdown of which slices are allocated. Per-profile alloc numbers are FICTITIOUS.
- Detection (in `collectors/slurm._parse_node_block`): `len(gres_entries) >= 2` AND any kind matches `<N>g.<M>gb` (`_MIG_PROFILE_RE`).
- For MIG nodes, emit ONE synthetic GpuSpec at node level: `kind = "MIG (3g.40gb+2g.20gb+1g.10gb)"`, `total = sum of profile counts`, `alloc = AllocTRES gres/gpu`. Loses per-profile counts but stops producing negative free numbers in cluster totals.
- Non-MIG path (rohan classic, plain H100/A100/V100) is unchanged — single Gres entry, alloc attributed to it directly, vram from AvailableFeatures.
- See saved memory `feedback_slurm_mig_alloctres_flat.md` for the underlying rule.

### LRZ avail must cap at quota headroom (#6)
- When `dssusrinfo` quota overrides `total_bytes`, df's `avail_bytes` is the FILESYSTEM-WIDE free space (e.g. /dss/dsshome1 = 317 TiB) — pairing it with a 100 GB quota produces `free 317 TiB / total 100 GB` nonsense.
- `discover_lrz_storage` now caps `avail = min(df_avail, max(0, quota_total - quota_used))`. Same rule for home and DSS containers. When df returns nothing, fall back to pure quota headroom (was None previously).

### Overview NodesSummary natural height comes from the snapshot (#4)
- `OverviewPanel._TOTALS_NATURAL = 15` was rohan-tuned. LRZ has 7 distinct GPU kinds (H100 + A100-80 + A100-40 + V100 + 3 MIG profiles) → real card height ~20. The undersized constant let `#ascii_row` paint over the bottom of HomeStorage.
- Replace with `_totals_natural()`: derives `_TOTALS_FIXED_OVERHEAD - 1*(no SSD) - 1*(no HDD) + gpu_row_count + slack`. `update_snapshot` counts distinct `GpuSpec.display` labels and tracks SSD/HDD presence.
- General Textual rule: don't hardcode "natural" heights for content that depends on snapshot data. Compute from data, OR use `height: auto` in CSS and let Textual size from content.

### GPU column format: `<free/alloc/total> <kind+vram>` (#2)
- Numbers first, label second — both per-row (`_gpu_cell`) and in NodesSummary GPU rows. Numbers are the load-bearing data and must align across rows.

### Nodes tab also mounts `nodes_summary` above `nodes_table` (#8)
- `config._default_layout` includes `["nodes_summary", "nodes_table"]` for the Nodes tab. Cluster totals are now visible BOTH in Overview (inside OverviewPanel) and at the top of the Nodes tab.

### Recent change log
- `4cde47a` slurm: MIG node-level aggregation
- `68cde1f` overview: dynamic NodesSummary natural height
- `c7eea4f` storage: coalesce df into one round-trip
- `34b1f9c` storage: cap LRZ avail at quota headroom
- `d70f5d7` nodes: mount nodes_summary in Nodes tab default layout
- `a5f2f26` nodes: GPU column triple-first ordering
- `800459d` checkpoint: multi-cluster (steps 2/3/4) + LRZ storage WIP
- (config-only, not in git) `/tmp/wsl_multi_config.toml` rohan exec → `ssh:rohan` (#7)

## 4-issue fixes (since 2026-05-06)

### whoami-based remote user detection (`#3`-related)
- `SSHExecutor` now resolves the remote login via `whoami` on first probe rather than guessing from `~/.ssh/config`. Avoids the "Connection closed by user" path when local and remote usernames diverge (LRZ user `di35dob` ≠ local `hli`). Cached for the executor's lifetime.
- See commit `7fda6a2` exec: detect remote user via whoami, suppress rc=255 on cancel.

### `rc=255` on cancelled ssh children no longer surfaces as an error (`#3`)
- `LocalExecutor.run` and `SSHExecutor.run` now treat `CancelledError`-induced 255 exits as silent (the asyncio cancel kills the ssh child with SIGTERM → 255, which is NOT a user-actionable error). Logged at debug, not propagated as `RuntimeError`.
- See same commit `7fda6a2`.

### LRZ storage discover coalesced into ONE ssh round-trip (`#1`)
- `discover_lrz_storage(executor)` previously did 2 ssh calls (env+dssusrinfo, then `df -B1`). It now combines them into a single `bash -lc` command that prints env vars, dssusrinfo, then df. Roughly halves LRZ tick latency.
- See commit `61f11cd` storage: coalesce LRZ discover into ONE ssh round-trip.

### Overview wide-mode layout: content-sized columns + 1fr ASCII slack absorber (`#2`)
- The OLD pattern was `grid-rows: 1fr` on `#main` plus an imperative `main.styles.height = max(left_nat, right_nat)`. Problem: `UtilizationPanel.DEFAULT_CSS` sets `height: 1fr`, so when the right column held a flex Util card, it pushed the SHARED grid row past the imperative target — the LEFT column ended up shorter than its allocation and a 1–3 row dead gap appeared below `HomeStorage`.
- NEW pattern: drop `grid-rows: 1fr` and the imperative height; let each `.col` size to natural content (`height: auto`); ASCII pane below `#main` is `1fr` and absorbs whatever vertical slack remains. No card stretches inside `#main`. The `_make_util` helper imperatively pins Util to `UTIL_MIN_HEIGHT = 13` rows so it doesn't demand `1fr` of its content-sized parent column.
- General Textual rule: when a 2-column grid is supposed to leave a flexible region BELOW it, prefer content-sized columns + a sibling `1fr` element below over `grid-rows: 1fr` + imperative height balancing. Imperative-height + child `1fr` cards is the trap.
- Tests: `tests/test_overview_layout.py` — three new sizes (100×30 narrow, 140×40 wide+fit, 180×50 wide+fit) plus a multi-size LRZ-vs-rohan target check.
- See commit `93ac41c` overview: content-sized columns + 1fr ASCII slack absorber.

### Canonical GPU label across clusters: `<KIND uppercase> <VRAM>` (`#4`)
- Render-time helper `_norm_kind(kind, vram)` in `widgets/nodes_table.py`:
  - non-MIG kinds: uppercased (`a100` → `A100`, `rtx_3090` → `RTX_3090`, `rtx_a6000` → `RTX_A6000`)
  - MIG profile kinds (regex `^\d+g\.\d+gb$`): preserved lowercase (`3g.40gb`)
  - VRAM appended unchanged when present (`A100 80GB`); kind alone when None.
- Applied at THREE render sites: `_gpu_cell` (per-node table), `_cluster_totals` (NodesSummary aggregation key), and `OverviewPanel.update_snapshot` (gpu_row_count for layout). The model-level `GpuSpec.display` is **unchanged** — canonicalization is render-layer only, per spec.
- Tests: `tests/test_nodes_table.py` — `_norm_kind` lowercase→upper, idempotent upper, underscore preserved, MIG preserved lowercase, VRAM-None, empty kind cases. Existing `_gpu_cell` rohan-fixture test updated to assert `RTX_A6000` instead of `rtx_a6000`.
- See commit `9f17161` nodes: canonical GPU kind format across clusters.

### Recent change log (continued)
- `7fda6a2` exec: detect remote user via whoami, suppress rc=255 on cancel
- `61f11cd` storage: coalesce LRZ discover into ONE ssh round-trip
- `93ac41c` overview: content-sized columns + 1fr ASCII slack absorber
- `9f17161` nodes: canonical GPU kind format across clusters

## 4-issue v2 fixes (since 2026-05-06)

### Wide+fit Overview: `Horizontal` not `grid` (#2 v2)
- The 93ac41c "content-sized columns + 1fr ASCII slack absorber" used `layout: grid; grid-size: 2`. CSS grid rows always pad SHORTER columns to the row's max-child height, leaving bare-border padding below the shorter column (the user-reported "2-line gap below HomeStorage / Util card"). There is no grid knob for "let each column be its own height."
- v2 fix: `layout: horizontal` with two `width: 1fr` Verticals. Each column sizes INDEPENDENTLY to its own content; `#main` is `height: auto` (= taller column); `#ascii_row` BELOW is still `1fr` and absorbs vertical slack. The `:first-of-type` selector adds the inter-column spacer.
- General Textual rule: **for two columns of independent height with a slack region BELOW, use `Horizontal(left, right)` + a `1fr` widget below — NOT `grid`.** Grid is for tabular layouts where ROW alignment is desired; for column-stacked cards with different heights it produces visible padding bugs.
- Tests: `tests/test_overview_layout.py` — seven new Pilot-based tests mount a real OverviewPanel inside a stub App at 100x30 / 140x40 / 180x50 (rohan-shape and LRZ-shape), introspect post-mount widget regions, and assert each `.col`'s last child's bottom is no more than 1 row above the column's own bottom. Plus a sanity-check that `#main` resolves to HorizontalLayout in fit mode.
- See commit `87ed9cc` overview: replace grid with horizontal layout for independent column heights.

### LRZ ssh detect-and-reconnect (#3)
- Problem: ServerAliveInterval=30/Count=3 = 90 s for the client to notice LRZ silently killed the TCP connection. Combined with `_warm_master(timeout=30)` > tick interval (5 s) on a cold ProxyJump (10.5 s), every tick gets cancelled mid-handshake by `run_worker(exclusive=True)`, the cancel-teardown holds the per-host semaphore, and the next tick surfaces "another probe is already in flight" while waiting up to 15 s for the semaphore.
- Fixes:
  1. **Tighter ssh opts (`_DEFAULT_OPTS`)**: `ServerAliveInterval=20`, `ServerAliveCountMax=3` (~60 s detection vs 90 s); `ConnectTimeout=10` bounds the cold-handshake.
  2. **Shielded `_warm_master`**: split into `_warm_master_inner` + outer wrapper that runs the inner inside `asyncio.shield`. A `run_worker(exclusive=True)` cancel from the next refresh tick now propagates to the awaiter (CancelledError) but the inner subprocess keeps running to completion — the next tick reuses the same warm. The inner is bounded by its own timeout (default `max(timeout, 30s)`); on timeout `_mark_mux_dead` is called.
  3. **`_mark_mux_dead` helper**: sync, best-effort `ssh -O exit` (2 s budget) + unlink socket + flip `_master_ready=False`. Called from `_warm_master`'s TimeoutError path AND from explicit teardown (e.g. shutdown). NOT called from per-call TimeoutError — a slow-but-reachable cluster (busy LRZ scontrol) shouldn't tear down its mux just because the call exceeded 15 s; the warm-master pre-flight `ssh -O check` catches a truly-dead mux on the next call.
- Cold/warm timing budget: cold first tick on LRZ ~10.5 s warm + actual call; subsequent ticks <2 s. If you see "another probe is already in flight" on the LRZ tab, that's the SECOND tick's wait_for-acquire timing out behind a still-warming first tick. It clears on the third tick.
- Tests: `tests/test_exec.py` — `test_ssh_executor_run_per_call_timeout_does_NOT_mark_mux_dead` (slow != broken; mux must persist), `test_ssh_executor_recovers_after_timeout_on_next_call` (first call hangs → TimeoutError; second call succeeds with warm mux still up), `test_ssh_executor_warm_master_shielded_from_outer_cancel` (inner runs to natural completion despite outer cancel). Existing `test_ssh_executor_kills_subprocess_on_cancellation` patched to stub `_mux_check_blocking` so cancel hits the COMMAND path (still cancellable end-to-end), not the now-shielded warm.
- See commit `ca9b1f8` exec: per-call timeout marks mux dead + shielded warm-master + follow-up `<NEW>` exec: don't mark mux dead on slow-call timeout.

### Static VRAM fallback for kinds without hyphenated AvailableFeatures (#4 v2)
- 9f17161 introduced `_norm_kind` for canonical render labels but didn't address the **upstream** issue: rohan reports `AvailableFeatures=rtx_a6000` (no hyphen+VRAM suffix), so `_AVAIL_FEAT_GPU_RE = ^([A-Za-z0-9]+)-(\d+...)$` doesn't match and `g.vram` stays None. Render shows `RTX_A6000` instead of `RTX_A6000 48GB`.
- Fix: `_KIND_VRAM_FALLBACK` static dict in `collectors/slurm.py`, keyed by lowercased Gres-kind. Applied AFTER the AvailableFeatures resolution path — a hyphenated cluster (LRZ A100-80GB) is never overridden; unknown kinds fall through to vram=None.
- Default entries: `a100→80GB, a6000→48GB, rtx_a6000→48GB, rtx_3090→24GB, rtx_2080→11GB, gtx_1080→8GB, h100→80GB, v100→32GB, p100→16GB`. **Extend** when adding a new cluster whose `AvailableFeatures` lacks `<kind>-<VRAM>` AND the kind isn't in the dict. Keep MIG profiles OUT — they already encode VRAM in the kind itself (`3g.20gb`).
- Tests: rohan a100/a6000 now assert `"80GB"`/`"48GB"`; LRZ A100-80GB regression test confirms native VRAM wins over the table; unknown-kind test confirms opt-in-per-kind behavior.
- See commit `3ee48b0` slurm: static VRAM fallback for kinds without hyphenated AvailableFeatures.

### Burst-keys input-lag test (#1)
- Pilot-based test in `tests/test_app_multi_cluster.py`: bursts `1,2,3,4` at 30 ms intervals against an app with a slow LRZ executor (8 s/call). Asserts the LAST press lands on the storage tab within 200 ms.
- Post-#3 the test passes at ~10 ms residual lag (the asyncio-sleep slow-executor doesn't actually block the loop, so the test guards the high-level invariant that an awaitable-slow cluster doesn't block input handling). PRE-#3 lag in the live tmux app was 300-500 ms (cancel-storm + subprocess teardown stalling the loop); the #3 shield + per-call timeout + mark-mux-dead removed that storm.
- Manual stronger reproduction (NOT in CI): `tmux send-keys -t rohan:rohanboard-debug-2 1 1 2 3 4` + capture-pane after 100/200/500 ms. Recipe documented in the test docstring.
- Per the v2 plan's STOP guideline (residual lag < threshold post-#3 alone), no separate #1-only fix landed.
- See commit `ba3da9d` tests: burst-keys input-lag guard for issue #1.

### Recent change log (continued)
- `3ee48b0` slurm: static VRAM fallback for kinds without hyphenated AvailableFeatures (#4 v2)
- `87ed9cc` overview: replace grid with horizontal layout for independent column heights (#2 v2)
- `ca9b1f8` exec: per-call timeout marks mux dead + shielded warm-master (#3)
- `ba3da9d` tests: burst-keys input-lag guard for issue #1
- `dcb4ac1` exec: don't mark mux dead on slow-call timeout (only on warm-timeout)

## 3-issue v3 fixes (since 2026-05-06)

### `bulk_run` coalesces N collector ssh calls into 1 round-trip (#2)
- `Executor.bulk_run(name_to_argv, timeout)` is the canonical fix for "per-host Semaphore(1) + asyncio.gather of N is fully serial" (per saved memory `feedback_asyncio_subprocess_cancel_leaks.md`).
- **SSHExecutor.bulk_run** packs all N commands into ONE `bash -c` script that BACKGROUNDs each subshell (`(cmd > tmpfile) &`) and `wait`s, then emits sentinel-framed output (`---RBOUTBEGIN:<token>:<name>---` / `---RBOUTEND:...`). Token is a 16-char random hex per call so a collector's stdout that contains a literal sentinel cannot confuse demux.
- Wall-clock drops from `sum(per-call)` to `max(per-call)` — for LRZ that's ~10 s (dssusrinfo) instead of ~5+ s sequential.
- **LocalExecutor.bulk_run** runs commands in parallel via `asyncio.gather` (no semaphore on local).
- **Rule**: any `executor.run × N inside an asyncio.gather` IS A BUG. Use `bulk_run`.
- The orchestrator (`_refresh_cluster_bulk` in `app.py`) builds 4 collector argvs upfront (`slurm.build_jobs_argv`, `build_recent_jobs_argv`, `nodes_argv`, plus the storage entry's argv) and calls `bulk_run` ONCE per cluster tick. Single semaphore acquisition.
- Tmpvars in the bulk script use INDEX-based names (`_rb_tmp_0`, `_rb_tmp_1`...) — name-derived tmpvars break bash when the slot has spaces / parens (e.g. `storage:home (quota)` from the rohan config).
- Tests: `tests/test_exec.py::test_bulk_run_*` (5 tests) — N→1 reduction, single-acquisition, unguessable-per-call tokens, LocalExecutor parity.

### Per-cluster scheduling + skip-if-running (#2 follow-up)
- Each cluster gets its OWN timer at its OWN `min(refresh.*)` interval (was: one shared timer at `min` across ALL clusters). LRZ's slow ticks (~10 s with cold ssh) no longer cancel rohan's fast ticks (5 s).
- **Skip-if-running** replaces `exclusive=True` worker cancellation: if a tick for cluster X is still in flight when its timer fires again, the new tick is silently DROPPED. The previous tick is allowed to complete naturally; the user sees the previous-good snapshot until then. Without this, every LRZ tick taking >5 s gets cancelled before broadcast → "loading…" forever.
- Implementation: `App._tick_inflight: dict[str, bool]`. The timer callback checks `_tick_inflight[cluster.id]` and returns early if true.
- **Preserve previous snapshot on empty bulk**: if a tick produces 0 nodes/jobs/storage AND a previous tick succeeded, the orchestrator keeps the prior `self.snapshots[cid]` rather than blanking it. Belt-and-braces guard for the cancel-mid-flight case.
- **Defer `_push_snapshot` via `call_after_refresh`** in `OverviewPanel.update_snapshot` — newly-mounted cards from `_populate` haven't finished compose() yet; a synchronous push silently fails on `query_one(...)` against half-mounted children. Without this defer, LRZ Overview stayed stuck on "loading…" even when fanouts WERE firing.

### Filter-bar debounce at 150 ms (#1)
- `JobsTable.on_input_changed` now schedules a `set_timer(0.15, _apply_filter_debounced)` instead of synchronously rebuilding the DataTable on every keystroke.
- Without debounce, every keystroke ran a full DataTable rebuild that — under refresh-tick pressure (the cancel-storm from #2) — starved Textual's input dispatch and dropped ~50% of slow keystrokes (typing "test hello" with 600 ms gaps yielded "ttel" pre-fix).
- 150 ms is short enough to feel responsive (table updates within 150 ms of user pause) and long enough to coalesce typing.
- `watch_filter_text` is now a NO-OP — debounce drives all rebuilds.
- Test: `test_filter_debounce_coalesces_keystrokes` in `test_app_multi_cluster.py` — Pilot-driven, types 5 chars in <150 ms, asserts EXACTLY 1 rebuild fired (not 5). Caveat per saved memory `feedback_tmux_burstkey_input_test.md`: live tmux test (one-key-at-a-time at 600 ms gap) is the gold standard; Pilot can't fully reproduce loop starvation under real-world refresh pressure.

### Bottom-aligned columns via flex-last-card + runtime assertion (#3)
- The 87ed9cc `layout: horizontal` fix made each column content-sized — no padding inside the row, but the SHORTER column ended early, leaving a 4-row asymmetric gap before the ASCII pane (149×44 measured: left col `╰` row 28, right col `╰` row 24).
- v3 fix: `#main` height is pinned imperatively to `target = max(left_nat, right_nat)` (so `.col { height: 100% }` resolves to a concrete value) AND the LAST card in each column gets `.flex` (CSS `height: 1fr`) so it absorbs the column's slack. The visible `╰` of the last card bottom-aligns with the sibling col's last `╰`.
- The original v3-plan-suggested `col-spacer` approach (a 1fr Static at the bottom of each column) DOESN'T WORK in Textual because `Vertical` doesn't honor `1fr` children inside `height: auto` parents — the spacer collapsed to 0 rows.
- `_make_util` imperatively pins UtilizationPanel to `UTIL_MIN_HEIGHT = 13` so it doesn't demand 1fr of its content-sized parent column. When Util ends up as the LAST card (right col with util_side="right"), `_populate` clears that imperative height (`util.styles.height = None`) so the `.flex` CSS rule wins.
- **Runtime assertion** (env-gated): `_verify_layout_aligned` raises `AssertionError` if `last_card_left.region.bottom != last_card_right.region.bottom (±1)`. Wired into `_populate` via `call_after_refresh`. Off in production (cost), on in CI/dev with `ROHANBOARD_LAYOUT_ASSERT=1`.
- General Textual rule: when you need bottom-alignment of two columns of different content height, the canonical pattern is:
  1. Set the parent's height imperatively to `max(child_natural)` (NOT `auto` — that creates a circular sizing dep with `100%` children).
  2. Set children to `height: 100%` so they stretch to the parent.
  3. Give the LAST card in each child `height: 1fr` so it absorbs slack.
- Tests: 4 new `test_columns_bottom_align_in_wide_fit` parametrized cases at 140×40 / 180×50 × rohan/lrz shapes + 2 new runtime-assertion tests.

### Recent change log (continued)
- `841f917` exec+app: bulk_run + per-cluster scheduling (#2)
- `594fcc2` jobs_table: debounce filter at 150 ms (#1)
- `ce8c8b4` overview: bottom-align columns + runtime layout assert (#3)
