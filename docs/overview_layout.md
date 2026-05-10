# Overview-tab layout design

Authoritative reference for the `OverviewPanel` widget (`rohanboard/widgets/overview.py`). Read this before changing the relayout/populate logic — the structure is more constrained than it looks.

## What the user sees

The Overview tab is the dashboard's landing page. It must adapt to:
- **Width**: terminals from 80 to 300+ columns. Below `WIDE_MIN_WIDTH` it falls back to a single column ("narrow"); above, it's two columns of equal height ("wide").
- **Height**: when the natural content height fits the terminal, the panel renders in **fit** mode (with optional ASCII art absorbing slack). When it doesn't fit, **scroll** mode kicks in (single column inside a `ScrollableContainer`, content overflows below the fold).

## Widget tree

```
OverviewPanel : Vertical
└── ScrollableContainer  #root          ← scrolls only in scroll mode
    ├── Vertical         #main          ← actual content
    │   (children depend on mode + fit, see "Population" below)
    └── Vertical         #ascii_row     ← optional decorative slack absorber
        └── AsciiArt                    ← present only when there's room for it
```

The cards mounted into `#main`:

| Card | Class | Purpose |
|---|---|---|
| `ClusterTotals` | row 1 of `#main` (always) | "Cluster: rohan • 24 nodes" line + Slurm version + uptime |
| `NodesSummary` | left col (wide) / row 2 (narrow) | One-line-per-node-kind summary table |
| `HomeStorage` | left col / row 3 (narrow) | Home quota usage bar |
| `CompactJobs` | right col (wide) / row 4 (narrow) | Job tally per state + a small inline list |
| `UtilizationPanel` | right col (wide, *if it fits*) | Histogram of allocations, ramp-up curves |

## The decision in `_relayout()`

The function reads the panel's current width + height once and decides everything else from there. `update_snapshot` and `on_resize` both re-call it; the result is memoized in `self._last_layout` so a no-op re-call doesn't churn the DOM.

```
                     ┌──────────────────────┐
                     │  width < WIDE_MIN ?  │
                     └────────┬─────────────┘
                              │
              ┌─── yes ──────┐│┌────── no ─────┐
              ▼              ▼▼▼               ▼
       narrow mode                       wide mode
       natural_need =                    left_nat  = TOTALS + 1 + HOME
         totals_h + 1                    right_nat = min(left_nat, jobs_nat)   ★
         + HOME + 1                      gap       = natural_need - shorter_nat
         + jobs_nat                      util_side = "right" if natural_need ≤ h
       util_side = None                              AND gap ≥ UtilizationPanel.MIN_HEIGHT + 1
                                                  else None
              │                              │
              └──────────────┬───────────────┘
                             ▼
                  fit = natural_need ≤ h
                             │
                  ┌── yes ───┴──── no ──┐
                  ▼                     ▼
              fit mode               scroll mode
              ascii_budget = h-target  ascii_budget = 0
              show_ascii =             show_ascii = False
                budget ≥ ASCII_MIN     (1-col fallback in CSS)
```

**★** = the Phase 4d.2-C invariant. See "The cap" below.

## The cap (`right_nat = min(left_nat, jobs_nat)`)

Before Phase 4d.2-C, `_relayout` computed `right_nat` from `jobs_nat` directly and then chose `util_side` from whichever column was *shorter* — left or right. That produced four cases:

1. left > right, util fits on right → util_side = "right"
2. left < right, util fits on left → util_side = "left"
3. left ≈ right, util fits on either → ambiguous tie-break
4. neither fits → util_side = None

Cases 2 and 3 introduced asymmetry: `HomeStorage` would sometimes mount in the left col with `flex=1fr`, sometimes in the right under `UtilizationPanel`. The CSS had to carry both branches and the threshold for "scroll mode" became fuzzy at the seam.

**Capping `right_nat` at `left_nat`** collapses cases 1 + 2 + 3:
- `right_nat ≤ left_nat` by construction.
- `natural_need = left_nat` in wide-fit mode.
- `gap = left_nat - right_nat ≥ 0` is the slack on the right column.
- If gap ≥ `UtilizationPanel.MIN_HEIGHT + 1`, util mounts on the **right**. Otherwise no util.
- Util never mounts on the left.

The dead `if util_side == "left":` branch in `_populate` is gone. CompactJobs has its own `VerticalScroll` body, so capping right at left doesn't lose any job rows — they just scroll internally.

`util_min` is now `UtilizationPanel.MIN_HEIGHT` directly — not derived from `left_nat`. That keeps the threshold honest: the panel knows its own minimum.

## Mode/CSS table

| Mode | CSS class | `#main` layout | `#ascii_row` |
|---|---|---|---|
| narrow + fit | `.narrow .fit` | vertical stack | shown if budget ≥ `ASCII_MIN_TOTAL` |
| narrow + scroll | `.narrow .scroll` | vertical stack inside `ScrollableContainer` | hidden |
| wide + fit | `.wide .fit` | 2-col grid, `1fr` per col, equal height | shown if budget |
| wide + scroll | `.wide .scroll` | falls back to 1-col stack inside `ScrollableContainer` | hidden |

CSS rules live near the top of `OverviewPanel.DEFAULT_CSS` in `overview.py`.

## Empirical fit ↔ scroll thresholds

Verified live on `rohan:rb-v2-lrz` at `w=200` (Phase 4d.2-C, 2026-05-10):

| Terminal `h` | Mode | Layout |
|---|---|---|
| 60 | wide-fit | 2-col, balanced, AsciiArt fills below |
| 28 | wide-fit | 2-col, balanced, no AsciiArt |
| **27** | wide-fit | 2-col, just-tall-enough, no scrollbar |
| **26** | scroll | 1-col stacked, content overflows |
| 22 | scroll | 1-col stacked, scrollable |

Threshold is at `h=26 ↔ 27` — sharp, no flicker, no ambiguous regime. The cap is what makes it sharp.

The exact threshold height depends on:
- `_TOTALS_NATURAL` (currently 7)
- `_HOME_NATURAL` (currently 4)
- `jobs_nat = 4 + max(job_count, 1)` (depends on how many jobs are in the snapshot)
- `+1` row of margin between the totals row and the col grid

Roughly: `natural_need ≈ left_nat + 1 ≈ TOTALS_NATURAL + HOME_NATURAL + 2`.

## How to verify a layout change

1. **Unit-level**: run `uv run pytest tests/`. The existing tests don't exercise `_relayout` directly (Textual layout is hard to assert in Pilot without a real terminal); they cover the inputs (job count, home stats, util data) instead.
2. **Live at default size**: `tmux attach -t rohan` → window `rb-v2-4c-ssh` and `rb-v2-lrz` → press `1` → expect balanced 2-col Overview. Both columns should end at the same row. ASCII art absorbs any slack below.
3. **Threshold drill** (the load-bearing one for any cap-related change):
   ```
   tmux resize-window -t rohan:rb-v2-lrz -x 200 -y 27   # expect FIT, 2-col
   tmux resize-window -t rohan:rb-v2-lrz -x 200 -y 26   # expect SCROLL, 1-col stacked
   ```
   The threshold should be a single-row delta with no flicker between `_relayout` calls.
4. **Width drill**: `-x 80 -y 60` → narrow mode (1-col, fit if h is large enough).

## Where the source lives

- **Decision logic**: `rohanboard/widgets/overview.py:_relayout` (~lines 356–399).
- **DOM populate**: `rohanboard/widgets/overview.py:_populate` (~lines 409–460).
- **CSS**: `OverviewPanel.DEFAULT_CSS` near the top of the same file.
- **Cards**: each card class lives in its own widget module under `rohanboard/widgets/`.
- **Constants**: `_TOTALS_NATURAL`, `_HOME_NATURAL`, `ASCII_MIN_TOTAL`, `WIDE_MIN_WIDTH`, `UtilizationPanel.MIN_HEIGHT` — search by name.

## Phases that touched this

- **Phase 4d.1** (2026-05-08, 5c0109f) — rohan-style layout becomes the app default; `[layout]` is optional in TOML.
- **Phase 4d.2-C** (2026-05-10, d69a385) — cap `right_nat = min(left_nat, jobs_nat)`, util_min from `UtilizationPanel.MIN_HEIGHT`, removed dead `util_side == "left"` branch. (This file documents the post-Phase-4d.2-C design.)
