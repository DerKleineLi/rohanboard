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
    """`free / used / total` with the green/yellow/white scheme.

    Each value picks its own unit so a small free space next to a large total
    reads naturally — e.g. on a near-full 35 TiB filesystem with 321 GiB free,
    show "321 GiB / 33.1 TiB / 35.2 TiB" rather than "0.3 / 33.1 / 35.2 TiB".
    Each value gets its own unit suffix.  ≥ 100 in the chosen unit prints as
    integer; smaller values get one decimal place."""
    triples = [
        (free_bytes, "bold green"),
        (used_bytes, "yellow"),
        (total_bytes, ""),
    ]
    fallback_unit, fallback_divisor = pick_byte_unit(total_bytes)
    text = Text()
    for i, (val, style) in enumerate(triples):
        if val > 0:
            unit, divisor = pick_byte_unit(val)
        else:
            unit, divisor = fallback_unit, fallback_divisor
        scaled = val / divisor
        places = 0 if unit == "B" or scaled >= 100 else 1
        text.append(f"{scaled:.{places}f} {unit}", style=style or None)
        if i < len(triples) - 1:
            text.append(" / ")
    return text
