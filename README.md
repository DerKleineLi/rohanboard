# rohanboard

Terminal dashboard for a SLURM cluster — jobs, nodes, storage quotas,
CPU/GPU/Mem utilization at a glance. Built with
[Textual](https://textual.textualize.io/).

Originally written for the `rohan` cluster at TUM, but everything that is
cluster-specific is read from a TOML config and a `~/.config/rohanboard/`
directory, so it should be reusable on any SLURM install that ships
`squeue`, `sacct`, `sinfo`, `scontrol`, `quota`, and `df`.

## Features

- **Overview** tab with a responsive 1- or 2-column layout that adapts to
  the terminal width, combining cluster totals, home-quota usage,
  active-jobs summary, and a CPU / GPU / Mem sparkline history. An
  optional animated decoration fills the slack at the bottom (colored
  Matrix rain or DOOM fire, both bundled and resolution-agnostic).
- **Jobs** tab backed by `squeue` (active) / `sacct` (recent) with:
  - sortable columns (click or keyboard),
  - a filter mini-language (e.g. `state:RUNNING gpu>=1 mem>=100G`),
  - preset chips, a "Mine only" toggle that flips `-u $USER`, and
  - double-click or Enter on a row to tail the job's stdout/stderr in a
    modal (click the backdrop or right-click to dismiss).
- **Nodes** tab with a cluster-totals summary and a per-node DataTable.
- **Storage** tab showing quota + `df` bars for auto-discovered mounts.
- Shortcuts are tab-scoped — the Jobs-only keys disappear from the
  footer when you're on another tab.

## Install

```bash
# clone & install with uv (fastest)
git clone https://github.com/DerKleineLi/rohanboard
cd rohanboard
uv sync
uv run rohanboard
```

Or with plain pip in a venv:

```bash
python -m venv .venv
. .venv/bin/activate
pip install .
rohanboard
```

`python >= 3.10` (the `match` statement is used only if present;
`tomli` is pulled in on 3.10, and 3.11+ uses the stdlib `tomllib`).

## Configure

On first run rohanboard looks for `~/.config/rohanboard/config.toml`
(respects `$XDG_CONFIG_HOME` and `ROHANBOARD_CONFIG`). Copy the example
to get started:

```bash
mkdir -p ~/.config/rohanboard
cp config.example.toml ~/.config/rohanboard/config.toml
```

Filter presets live in `~/.config/rohanboard/filters.json` — a flat list
of `{"name": "...", "expr": "..."}` objects.

**Multiple clusters:** pass `--config <path>` to point at a per-cluster
TOML (e.g. `configs/rohan.toml`, `configs/lrz.toml`). The executor
specified in each config (`exec = "ssh:<host>"` or `exec = "local"`)
handles the cluster ssh internally, so you can run several instances
side-by-side — each watching a different cluster — from one machine.

## Deployment idea: local tmux, one window per cluster

rohanboard runs locally; the executor handles the cluster ssh. A
dedicated `tmux` session with one window per cluster makes a clean
always-on monitor:

```bash
tmux new-session -d -s rohanboard -n rohan \
    'cd /path/to/rohanboard && uv run rohanboard --config configs/rohan.toml'
tmux new-window  -d -t rohanboard -n lrz \
    'cd /path/to/rohanboard && uv run rohanboard --config configs/lrz.toml'
tmux -u attach -t rohanboard
```

Older versions of this README recommended running rohanboard *on* the
login node via `ssh login.cluster -t '...'`. That pattern is
superseded by the executor abstraction — the dashboard now lives
locally and ssh-es out to whichever cluster(s) the config points at.

## Development

```bash
uv sync
uv run pytest
uv run textual run --dev rohanboard.app:RohanBoardApp   # live reload
```

The animation subsystem has a lightweight frame-time logger — set
`ROHANBOARD_ANIM_LOG=/tmp/rb.log` before launching to get a CSV of
(timestamp, widget, tick-interval-ms, work-ms, w, h) per frame.

## License

MIT. See `LICENSE`.
