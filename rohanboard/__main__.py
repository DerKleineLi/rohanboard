from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rohanboard.app import RohanBoardApp
from rohanboard.config import DEFAULT_CONFIG_PATH, load as load_config


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rohanboard", description="Terminal dashboard for the rohan SLURM cluster.")
    p.add_argument("--config", "-c", type=Path, default=None,
                   help=f"path to config.toml (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--theme", "-t", type=str, default=None,
                   help="theme file name in styles/ or absolute path; overrides [theme].file from config")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config(args.config)
    if args.theme:
        cfg.theme.file = args.theme
    RohanBoardApp(config=cfg).run()


if __name__ == "__main__":
    main()
