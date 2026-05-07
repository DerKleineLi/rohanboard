# rohanboard async-ssh re-design (v2)

## Status

- **Approved:** 2026-05-07 (msg 5620). Step-by-step execution requested with checkpoint after each phase.
- **Current phase:** Reset (in progress — branch tag + new branch + sanity-check board).
- **Old tip preserved as:** tag `clean-reimpl-bc-attempt` at `43d2e02`.

## Background — why we're resetting

`clean-reimpl` accumulated B+C (`43d2e02`) on top of Phase 3 daemon-thread sync subprocess + Phase 2 SSHExecutor wiring. Despite throttle + post_message + chunked filter, dual-SSH global-hotkey drops persist. Investigation revealed the bottleneck is handler-side: synchronous `NodesTable.update_snapshot` (~80 nodes × 8 cell mutations, no yields) starves Textual's per-widget single-consumer message queue every refresh tick. The `_apply_filter_async` path next to it (chunked-yield) shows the asymmetry. Patching forward kept rediscovering the same class of bug. Reset and re-design with handler-side discipline baked in from day one.

## Revert target — `0612f58`

"storage: use df free_bytes directly to handle reserved blocks" (2026-04-29). Verified pre-Executor: `git log -S 'class SSHExecutor'` returns exactly `e5c4db2` (2026-05-05) which introduces `rohanboard/exec.py` complete (Executor + LocalExecutor + SSHExecutor in one 446-line commit). There is NO on-disk LocalExecutor-only era — it has to be re-authored.

Functional scope at `0612f58`:
- Single-cluster, async collectors with private `_run()` subprocess wrappers (no Executor abstraction).
- `update_snapshot` push pattern already exists.
- `Snapshot` dataclass (no jobs/jobs_mine split yet).
- log_tail TabbedContent, double-click via mouse_down, per-cell storage units, df free_bytes fix already in.

## Architecture — asyncssh

Native async ssh client. Persistent connection per cluster, native multi-session multiplex per connection. Drops Phase 2's `bulk_run` sentinel-framed dispatch (~150 LoC of plumbing) — asyncssh sessions are the right abstraction. Drops PID-scoped ControlPath surgery (no shared mux file). Drops thread bridge / mux-dead detection / Semaphore(1) lifecycle entirely.

## Branch strategy

- `clean-reimpl` left untouched as historical reference.
- Tag `clean-reimpl-bc-attempt` at `43d2e02` for browseability.
- New branch `clean-reimpl-v2` off `0612f58`.
- Cherry-picks deferred to relevant phases (NOT done at branch creation).

## Day-one constraints

1. **No main-loop blocking work.** Every refresh path must yield. `update_snapshot` chunked-async (mirror of `_apply_filter_async` pattern) lands the FIRST phase that ships a DataTable rebuild path.
2. **No cancel-storm.** Slow-handshake clusters (LRZ ~17 s cold) must NOT cancel in-flight refreshes.
3. **Single-consumer message queue respect.** Handlers must return in <50 ms; longer work goes through a worker.
4. **No subprocess.run on the asyncio loop, no thread bouncing.** Pure asyncio + asyncssh.

## Phase plan

| Phase | Scope | Test gate | Approval gate |
|---|---|---|---|
| Reset | branch tag + new branch + plan file (this phase) | tests at `0612f58` baseline pass | user smokes the v2-reset board pane |
| 4a | asyncssh + pytest-asyncio dev deps via `uv add` | tests still green | user approves dep additions |
| 4b | `exec.py` from scratch: `Executor` Protocol + `LocalExecutor` + `AsyncSSHExecutor`. Collectors take Executor as 1st arg. | new `tests/test_exec.py`; collectors parametrized on `FakeLocalExecutor` | user approves API shape |
| 4c | `cluster.py` + multi-cluster TOML + legacy single-cluster shim + `--cluster <id>` CLI | `tests/test_config.py` covers multi-cluster + legacy | user approves config schema |
| 4d | Outer cluster-tab UI + per-cluster snapshots + `c` cycle binding + scoped `_broadcast_to_cluster` + **chunked DataTable rebuild from day one** | outer-tab presence + chunked-rebuild yields + scoped-broadcast tests | user smokes outer cluster cycle |
| 4e | Wire LRZ via AsyncSSHExecutor; parser cherry-picks (storage entry parsing, GPU column) | 60 s smoke: 0 stuck procs, ≤2 connections | drop-test < 5% baseline |
| 4f | dssusrinfo + auto_lrz storage; `Snapshot.jobs` / `.jobs_mine` split; `log_path_map`; log_tail screen | LRZ storage end-to-end | user spot-checks LRZ panel |
| 4g | Overview wide+scroll fallback (layout cherry-picks) | static layout decision-table test | user smokes layout |
| 4h | B+C re-implementation: `SnapshotUpdated(Message)` thread-safe wakeup + 250 ms coalesce throttle (NOT re-using current branch's code; pattern only) | 5×5 pilot drop-test on dual-cluster | dual-SSH < 5% drop confirmed |

Effort estimate: 12-16 hr over 8 commits. Each phase commit-sized, independently testable.

## Cherry-pick targets

Pure parser/layout commits cherry-pick clean (deferred to relevant phase):
`4cde47a, 9f17161, 34b1f9c, 61f11cd, 3ee48b0, 87ed9cc, 93ac41c, ce8c8b4, 68cde1f, d70f5d7, a5f2f26`.

`800459d` (squash containing steps 2/3/4 of an older plan) is NOT cherry-pickable as one unit — split across 4c/4e/4f.

## Drop entirely

`e5c4db2` (original exec.py — wrong shape for asyncssh)
`86a3c73` (initial SSH wiring)
`284c021` (Phase 2 multi-cluster + bulk_run)
`8f06653` (thread bridge)
`ca9b1f8`, `dcb4ac1`, `7fda6a2` (mux-dead detection — irrelevant under asyncssh)
`43d2e02` (B+C — re-implemented in 4h, pattern reuse, not commit reuse)

## Phase progress log

(Filled in as phases complete.)

- **Reset (in progress 2026-05-07):**
  - Tagged `clean-reimpl-bc-attempt` at `43d2e02`. ✓/✗
  - Created branch `clean-reimpl-v2` off `0612f58`. ✓/✗
  - Verified tests pass at baseline. ✓/✗
  - Launched verification pane at `tmux:rohan:rb-v2-reset` against `/tmp/wsl_single_v2reset_config.toml`. ✓/✗
