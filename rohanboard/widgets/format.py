"""Shared display helpers — keep colour + layout of resource counts identical
everywhere.  The convention used across the dashboard is:

    [green]free[/green] / [yellow]alloc[/yellow] / [white]total[/white]
"""
from __future__ import annotations

from rich.text import Text


_BYTE_UNITS = (
    ("PiB", 1024 ** 5),
    ("TiB", 1024 ** 4),
    ("GiB", 1024 ** 3),
    ("MiB", 1024 ** 2),
    ("KiB", 1024),
    ("B",   1),
)


def pick_byte_unit(reference_bytes: int) -> tuple[str, int]:
    """Choose a single unit big enough that `reference_bytes` rounds to ≥ 1."""
    for name, divisor in _BYTE_UNITS:
        if reference_bytes >= divisor:
            return name, divisor
    return "B", 1


def fat(free: int, alloc: int, total: int, suffix: str = "", *, width: int = 0) -> Text:
    """`free / alloc / total[suffix]` with the canonical green/yellow/white scheme."""
    fmt = f"{{:>{width}d}}" if width else "{:d}"
    t = Text()
    t.append(fmt.format(free), style="bold green")
    t.append(" / ")
    t.append(fmt.format(alloc), style="yellow")
    t.append(" / ")
    t.append(fmt.format(total))
    if suffix:
        t.append(suffix)
    return t


def fat_float(free: float, alloc: float, total: float, suffix: str = "", *, places: int = 0, width: int = 0) -> Text:
    fmt = f"{{:>{width}.{places}f}}" if width else f"{{:.{places}f}}"
    t = Text()
    t.append(fmt.format(free), style="bold green")
    t.append(" / ")
    t.append(fmt.format(alloc), style="yellow")
    t.append(" / ")
    t.append(fmt.format(total))
    if suffix:
        t.append(suffix)
    return t


def fat_bytes(free_bytes: int, used_bytes: int, total_bytes: int) -> Text:
    """Same green/yellow/white triple, sized to a single unit so columns align."""
    unit, divisor = pick_byte_unit(total_bytes)
    places = 0 if unit == "B" else 1
    return fat_float(
        free_bytes / divisor,
        used_bytes / divisor,
        total_bytes / divisor,
        suffix=f" {unit}",
        places=places,
    )
