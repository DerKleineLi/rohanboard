from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rohanboard.app import RohanBoardApp
from rohanboard.config import DEFAULT_CONFIG_PATH, load as load_config


# Phase 4d.2-E: where `--debug` lands its log files. Two files because
# perf is CSV (machine-friendly) and the dashboard's diagnostic log is
# free-form text.
DEFAULT_DEBUG_DIR = Path("~/.cache/rohanboard").expanduser()
DEFAULT_PERF_LOG = DEFAULT_DEBUG_DIR / "perf.log"
DEFAULT_DEBUG_LOG = DEFAULT_DEBUG_DIR / "debug.log"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rohanboard", description="Terminal dashboard for the rohan SLURM cluster.")
    p.add_argument("--config", "-c", type=Path, default=None,
                   help=f"path to config.toml (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--theme", "-t", type=str, default=None,
                   help="theme file name in styles/ or absolute path; overrides [theme].file from config")
    p.add_argument("--debug", action="store_true",
                   help=(
                       f"enable perf timings + diagnostic logs. Writes to "
                       f"{DEFAULT_PERF_LOG} (CSV) and {DEFAULT_DEBUG_LOG} "
                       f"(per-tick log). Tail the latter to watch refresh "
                       f"timings and cluster_user/jobs/sacct diagnostics."
                   ))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    debug = args.debug or os.environ.get("ROHANBOARD_DEBUG", "").lower() in (
        "1", "true", "yes",
    )
    if debug:
        # Turn on perf-block CSV.
        from rohanboard import perf
        perf.enable(DEFAULT_PERF_LOG)
        # Stamp the env so the App reads it on construction (we route
        # `App.log` to a file in addition to Textual's internal logger).
        os.environ["ROHANBOARD_DEBUG_LOG"] = str(DEFAULT_DEBUG_LOG)
        # Also touch the file so `tail -f` works out of the gate.
        DEFAULT_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        DEFAULT_DEBUG_LOG.write_text(
            f"# rohanboard --debug log — {DEFAULT_DEBUG_LOG}\n"
            f"# perf CSV: {DEFAULT_PERF_LOG}\n"
        )
        print(f"rohanboard: debug logs → {DEFAULT_DEBUG_LOG}", file=sys.stderr)
        print(f"            perf CSV  → {DEFAULT_PERF_LOG}", file=sys.stderr)
    cfg = load_config(args.config)
    if args.theme:
        cfg.theme.file = args.theme
    RohanBoardApp(config=cfg).run()


if __name__ == "__main__":
    main()
