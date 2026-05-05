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
