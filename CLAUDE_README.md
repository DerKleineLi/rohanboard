# rohanboard — local project runbook

This file is the project-local runbook for `rohanboard` on `clean-reimpl-v2`. Read before acting; update proactively with anything future-you would otherwise have to ask the user about.

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
- **Phase 4e+** (not yet started): multi-cluster SSH wiring.

## Layout defaults

`[layout]` is optional in cluster TOML configs (since 5c0109f). Omit it to get the rohan-style default:
- Overview → `overview_panel` (responsive 1- or 2-col composition).
- Jobs → `jobs_table`.
- Nodes → `nodes_summary` above `nodes_table`.
- Storage → `storage_panel`.

Cluster-specific TOMLs (e.g. `/tmp/wsl_single_v2_4c_ssh_config.toml`) carry only cluster-specific knobs (ssh entry, storage paths). Override `[layout]` only when a cluster has unusual UI needs.

## Smoke-test pane

The canonical Phase 4c+ smoke pane is `rohan:rb-v2-4c-ssh` (NOT `rohan:rohanboard-wsl` — that one is the older `clean-reimpl` multi-cluster build with `c Cycle cluster` binding).

Restart cleanly via `tmux respawn-pane -k`:

```
tmux respawn-pane -k -t rohan:rb-v2-4c-ssh \
  'cd ~/workspace/rohanboard-local && uv run rohanboard --config /tmp/wsl_single_v2_4c_ssh_config.toml'
```

(Per `tmux_ops.md` §from feedback_tui_restart_pattern — `respawn-pane` is the clean restart, NOT send-keys + q.)

After respawn, capture-pane after ~10 s to confirm: cluster name in header, Storage `home` populated, Jobs row visible, NodesSummary table populated, footer reads `q Quit r Refresh 1 Overview 2 Jobs 3 Nodes 4 Storage`.

## Testing

- **Unit + Pilot tests:** `uv run pytest tests/`. As of 2026-05-08: 33/33 passing.
- **Filter-drop regression test:** `tests/test_input_drops.py` exercises both 600 ms (synchronous-blocker) and 50 ms (debounce-coalesce) rhythms on JobsTable and NodesTable filters. The 600 ms test is the canonical drops repro per `~/.claude/projects/-home-hli/memory/rohanboard.md` §"Filter-bar character-drop variant".
- **Live tmux drops test:** see the memory file for byte-offset oracle / SGR mouse-click recipes if you need to repro on a real terminal.

## Known gotchas

- **`kind = "quota"` requires `exec = "ssh:rohan"` (login node)** — the `quota` binary is NOT installed on slurm/compute nodes, so a slurm-targeted ssh exec would silently fall back to "no quota info yet". Whenever a config carries a `[[storage.entries]]` with `kind = "quota"`, the `exec` MUST point at a host that has the `quota` binary. For rohan that's the login node alias `rohan`.
- **Filter Input id is `"filter"`, not `"filter-input"`** — both `JobsTable` and `NodesTable` mount their filter Input with `id="filter"`. Test code that does `pilot.click("#filter-input")` will silently fail. Query within the active widget: `app.query_one(JobsTable).query_one("#filter", Input)`.
- **`l` is bound to `action_jobs_tail_log` on the Jobs tab** (scoped via `App.check_action`). Test strings containing `q`/`r`/`l` are unsafe if focus drifts off the filter Input. Test string `testhello` contains `l` — Pilot keeps focus stable so it's fine in-process, but be cautious in tmux real-terminal tests.
- **`watch_filter_text` is now a no-op** — debounce in `on_input_changed` drives the rebuild via `run_worker(_apply_filter_async, exclusive=True)`. The reactive write still fires the watcher, but the watcher does nothing; do NOT re-add a synchronous rebuild there.
