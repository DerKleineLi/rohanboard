"""Filter expression parser + evaluators for Jobs / Nodes.

Syntax (AND between tokens):
  bare_word           — substring match across all default fields
  field:substr        — substring match in a specific field
  field>=N / <= / > / < / =   — numeric compare (int or float)

Example (node filter):
  a6000 gpu_free>=1 state:idle
  → match nodes where *any* default field contains "a6000", AND gpu_free >= 1,
    AND state contains "idle".
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Predicate:
    kind: str              # "any" | "sub" | "num"
    field: str             # "" for "any"
    op: str                # ">=", "<=", ">", "<", "=", or "" (sub / any)
    values: tuple          # OR-alternatives (strings for sub/any, floats for num)


_NUM_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(==|>=|<=|=|>|<)(.+)$")


_SIZE_UNITS = {
    "":    1,
    "B":   1,
    "K":   1024,   "KB":  1024,   "KIB": 1024,
    "M":   1024 ** 2, "MB":  1024 ** 2, "MIB": 1024 ** 2,
    "G":   1024 ** 3, "GB":  1024 ** 3, "GIB": 1024 ** 3,
    "T":   1024 ** 4, "TB":  1024 ** 4, "TIB": 1024 ** 4,
    "P":   1024 ** 5, "PB":  1024 ** 5, "PIB": 1024 ** 5,
}
_SIZE_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*([A-Za-z]*)$")


def parse_size(value: str) -> float | None:
    """Parse a number optionally followed by a byte-unit suffix.

    Returns bytes as a float, or None if unparseable.
    `100`, `100B`, `100G`, `1.5TB`, `2PiB` → 100 / 100 / 100·2^30 / 1.5·2^40 / 2·2^50.
    """
    m = _SIZE_RE.match(value.strip())
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    suffix = m.group(2).upper()
    mult = _SIZE_UNITS.get(suffix)
    if mult is None:
        return None
    return n * mult


def _split_alts(s: str) -> tuple[str, ...]:
    """`a6000|a100` → ('a6000', 'a100'). Backslash-escape to keep a literal `|`."""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            buf.append(s[i + 1])
            i += 2
            continue
        if c == "|":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf))
    return tuple(a for a in out if a)


def parse(expr: str) -> list[Predicate]:
    out: list[Predicate] = []
    for tok in expr.split():
        m = _NUM_RE.match(tok)
        if m:
            field, op, val = m.group(1), m.group(2), m.group(3)
            if op == "==":
                # Exact text match (case-insensitive) with `|` alternatives.
                alts = tuple(a.lower() for a in _split_alts(val))
                if alts:
                    out.append(Predicate("exact", field, "==", alts))
                continue
            # Numeric: try plain float first, then unit-suffixed size.
            alts: list[float] = []
            bad = False
            for piece in _split_alts(val):
                try:
                    alts.append(float(piece))
                    continue
                except ValueError:
                    pass
                sz = parse_size(piece)
                if sz is not None:
                    alts.append(sz)
                else:
                    bad = True
                    break
            if not bad and alts:
                out.append(Predicate("num", field, op, tuple(alts)))
                continue
            # Fall through to substring match below.
        if ":" in tok:
            field, val = tok.split(":", 1)
            alts = tuple(a.lower() for a in _split_alts(val))
            if alts:
                out.append(Predicate("sub", field, "", alts))
            continue
        alts = tuple(a.lower() for a in _split_alts(tok))
        if alts:
            out.append(Predicate("any", "", "", alts))
    return out


def _op_pass(v, op: str, target) -> bool:
    try:
        if op == ">=": return v >= target
        if op == "<=": return v <= target
        if op == ">":  return v >  target
        if op == "<":  return v <  target
        if op == "=":  return v == target
    except TypeError:
        return False
    return False


def match(record: dict[str, Any], preds: list[Predicate], default_text_fields: list[str]) -> bool:
    for p in preds:
        if p.kind == "any":
            hay = " ".join(str(record.get(f, "")) for f in default_text_fields).lower()
            if not any(alt in hay for alt in p.values):
                return False
        elif p.kind == "sub":
            v = record.get(p.field)
            if v is None:
                return False
            hay = str(v).lower()
            if not any(alt in hay for alt in p.values):
                return False
        elif p.kind == "exact":
            v = record.get(p.field)
            if v is None:
                return False
            low = str(v).lower()
            if not any(low == alt for alt in p.values):
                return False
        elif p.kind == "num":
            v = record.get(p.field)
            if v is None or not isinstance(v, (int, float)):
                return False
            if not any(_op_pass(v, p.op, t) for t in p.values):
                return False
    return True


def make_matcher(expr: str, default_text_fields: list[str]) -> Callable[[dict[str, Any]], bool]:
    if not expr.strip():
        return lambda _record: True
    preds = parse(expr)
    return lambda record: match(record, preds, default_text_fields)


# ──────────────────────────────────────────────────────────────────────────
# Preset loading
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_PRESETS_PATH = Path(
    os.environ.get("ROHANBOARD_FILTERS")
    or (Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "rohanboard" / "filters.json")
)


def load_presets(path: Path | None = None) -> dict[str, list[dict]]:
    """Returns {"nodes": [{"name":..., "expr":...}, ...], "jobs": [...]}."""
    path = Path(path or DEFAULT_PRESETS_PATH).expanduser()
    if not path.exists():
        return {"nodes": [], "jobs": []}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"nodes": [], "jobs": []}
    out = {"nodes": [], "jobs": []}
    for target in ("nodes", "jobs"):
        raw = data.get(target, [])
        if isinstance(raw, list):
            out[target] = [
                {"name": str(p.get("name", "")), "expr": str(p.get("expr", ""))}
                for p in raw
                if isinstance(p, dict) and p.get("name") and "expr" in p
            ]
    return out
