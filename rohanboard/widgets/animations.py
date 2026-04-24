"""Procedural animated decor widgets for the Overview tab.

Each animation is a self-contained Textual widget that redraws at a fixed
FPS and adapts to the widget's own size on every frame — no pre-rendered
assets, no resolution variants.

Two styles are provided:

* ``MatrixRain``   — cmatrix-style falling katakana/ascii columns, head
                     bright white → tail dim green.  Classic "hacker"
                     aesthetic.
* ``DoomFire``     — Fabien Sanglard's DOOM PSX fire algorithm ported to
                     terminal cells, using the canonical 37-step palette.

Performance notes:

The hot path is ``frame(w, h, t) -> Text``, called 15-22 times per second.
Two pitfalls made an earlier draft stutter:

  * allocating a fresh ``rich.style.Style`` per cell (truecolor objects
    are not cheap); and
  * calling ``Text.append`` once per cell (w·h calls per frame).

Both are fixed here by: (1) pre-computing a palette of ``Style`` objects
once (indexed by intensity), and (2) building each row as a flat list of
(char, style) runs and appending consecutive same-style cells as a single
span — one ``Text.append`` per run instead of per cell.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget


# Opt-in frametime logging.  Set `ROHANBOARD_ANIM_LOG=1` (or a path) to
# enable.  Writes one CSV-ish row per tick:
#   t_wallclock, widget, interval_ms, work_ms, w, h
_LOG_ENV = os.environ.get("ROHANBOARD_ANIM_LOG")
_LOG_PATH: Path | None = None
if _LOG_ENV:
    _LOG_PATH = Path(_LOG_ENV if _LOG_ENV not in ("1", "true", "yes") else "/tmp/rohanboard_anim.log")
    try:
        _LOG_PATH.write_text("")  # truncate
    except Exception:
        _LOG_PATH = None


def _log_frame(widget: str, interval_ms: float, work_ms: float, w: int, h: int) -> None:
    if _LOG_PATH is None:
        return
    try:
        with _LOG_PATH.open("a") as f:
            f.write(f"{time.time():.3f},{widget},{interval_ms:.2f},{work_ms:.2f},{w},{h}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class _Animation(Widget):
    """Widget that advances internal state at ``FPS`` and renders each
    row on demand via ``render_line(y) -> Strip``.

    Why not ``Static.update()``?  Static serialises a Rich renderable on
    every update — parsing, segment generation, width calculation.  Our
    state is already organised as rows of (char, style) cells; feeding
    the compositor ``Strip(Segment)`` objects directly skips that entire
    render pipeline and is measurably smoother on big colored widgets.

    Subclasses implement:

      * ``on_resize_reset(w, h)``     — re-seed grid state on resize
      * ``advance(w, h, t)``          — mutate state by one frame
      * ``segments_for_row(y, w) -> list[Segment]`` — read state → segments
    """

    FPS = 15

    DEFAULT_CSS = """
    _Animation {
        height: 100%;
        width: 100%;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._t = 0
        self._last_size: tuple[int, int] = (0, 0)
        self._last_tick_t: float | None = None

    def on_mount(self) -> None:
        self.set_interval(1 / self.FPS, self._tick)

    def _tick(self) -> None:
        self._t += 1
        w, h = int(self.size.width), int(self.size.height)
        if w <= 0 or h <= 0:
            return
        if (w, h) != self._last_size:
            self._last_size = (w, h)
            self.on_resize_reset(w, h)
        t0 = time.perf_counter()
        interval_ms = (t0 - self._last_tick_t) * 1000 if self._last_tick_t else 0.0
        self._last_tick_t = t0
        try:
            self.advance(w, h, self._t)
            self.refresh()
        except Exception:
            pass
        work_ms = (time.perf_counter() - t0) * 1000
        _log_frame(type(self).__name__, interval_ms, work_ms, w, h)

    # ── compositor hook ─────────────────────────────────────
    def render_line(self, y: int) -> Strip:
        w = int(self.size.width)
        segs = self.segments_for_row(y, w) if 0 <= y < int(self.size.height) else None
        if not segs:
            return Strip([Segment(" " * max(w, 0))])
        return Strip(segs)

    # ── subclass hooks ──────────────────────────────────────
    def on_resize_reset(self, w: int, h: int) -> None:
        """Reset grid state when the pane is resized."""

    def advance(self, w: int, h: int, t: int) -> None:
        """Mutate internal state by one frame."""
        raise NotImplementedError

    def segments_for_row(self, y: int, w: int) -> list[Segment]:
        """Return pre-styled segments for row `y`."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Fast row-to-segments helper — merge runs of same-style cells.
# ---------------------------------------------------------------------------

def _row_to_segments(cells: list[tuple[str, Style]]) -> list[Segment]:
    """Turn a list of (char, style) into a minimal list of Segments by
    coalescing consecutive cells with the same style.  A 168-wide matrix
    row typically collapses from 168 cells → 10–40 Segments."""
    out: list[Segment] = []
    buf: list[str] = []
    cur_style: Style | None = None
    for ch, style in cells:
        if style is cur_style:
            buf.append(ch)
        else:
            if buf:
                out.append(Segment("".join(buf), cur_style))
            buf = [ch]
            cur_style = style
    if buf:
        out.append(Segment("".join(buf), cur_style))
    return out


# ---------------------------------------------------------------------------
# MatrixRain
# ---------------------------------------------------------------------------

_MATRIX_GLYPHS = (
    "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ"
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "@#$%&*+=<>?/\\|^~"
)

# Fewer tail shades ⇒ fewer style changes per line ⇒ fewer ANSI escape
# sequences in the rendered frame ⇒ much lighter terminal paint cost.
# Five steps reads as a smooth fade and the compositor emits ~5× fewer
# SGR codes than the earlier 16-step gradient.  Indexed colors (not
# truecolor) further shorten the escape each time.
_MATRIX_HEAD = Style(color="bright_white", bold=True)
_MATRIX_TAIL: list[Style] = [
    Style(color="color(28)"),    # dark green
    Style(color="color(34)"),
    Style(color="color(40)"),
    Style(color="color(46)"),    # bright green
    Style(color="color(83)"),    # pale green just behind the head
]
_BLANK = Style.null()


@dataclass
class _Streamer:
    head: float     # current y of the head (can be negative)
    length: int     # tail length
    speed: float    # rows per tick
    chars: list[str]
    # Ticks-until-active.  While >0 the column renders blank; gives the
    # pane a sparser, less saturated look and cuts the painted-cell count.
    dormant: int = 0


class MatrixRain(_Animation):
    """Classic cmatrix-style falling columns, colored."""

    # With the Strip/Segment render path our per-frame work is ~1.4 ms,
    # so 20 FPS fits comfortably within the 50 ms budget on the ssh+tmux
    # paint pipeline.  Feels properly "flowing" without any stutter.
    FPS = 20

    DEFAULT_CSS = """
    MatrixRain {
        height: 100%;
        width: 100%;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cols: list[_Streamer | None] = []
        # Dense grid of (char, style) updated in place each frame;
        # render_line reads directly from it, bypassing Rich's render path.
        self._grid: list[list[tuple[str, Style]]] = []

    # Fraction of columns that should be *actively raining* at any moment.
    # 0.45 ⇒ a little over half the columns are dormant on every frame,
    # which thins the painted-cell count and reads as "rain" rather than
    # "wall of static."
    DENSITY = 0.45

    # Stride — only every Nth horizontal column carries a streamer; the
    # rest are permanently blank.  STRIDE=2 halves the cells that ever
    # draw ink, STRIDE=3 thirds them.  Makes the rain look airy rather
    # than dense, and cuts paint cost proportionally.
    STRIDE = 2

    def on_resize_reset(self, w: int, h: int) -> None:
        self._cols = []
        for x in range(w):
            if x % self.STRIDE == 0:
                self._cols.append(self._spawn_streamer(h, start_above=True))
            else:
                self._cols.append(None)   # type: ignore[arg-type]
        self._grid = [[(" ", _BLANK)] * w for _ in range(h)]

    def _spawn_streamer(self, h: int, start_above: bool = False) -> _Streamer:
        length = random.randint(max(4, h // 4), max(6, h))
        speed = random.uniform(0.5, 1.0)
        head = random.uniform(-length, 0) if start_above else -random.uniform(0, length)
        chars = [random.choice(_MATRIX_GLYPHS) for _ in range(h + length + 2)]
        # At spawn time, a column is either immediately active (probability
        # DENSITY) or dormant for a random stretch (1–4 seconds at 10 FPS).
        dormant = 0 if random.random() < self.DENSITY else random.randint(10, 40)
        return _Streamer(
            head=head, length=length, speed=speed, chars=chars, dormant=dormant
        )

    def advance(self, w: int, h: int, t: int) -> None:
        grid = self._grid
        # Clear to blanks — single-row copy beats a double-loop by a lot.
        blank_row: list[tuple[str, Style]] = [(" ", _BLANK)] * w
        for y in range(h):
            grid[y] = blank_row[:]

        choice = random.choice
        glyphs = _MATRIX_GLYPHS
        tail_palette = _MATRIX_TAIL
        head_style = _MATRIX_HEAD
        tail_len = len(tail_palette) - 1

        for x, col in enumerate(self._cols):
            if col is None:
                continue
            if col.dormant > 0:
                col.dormant -= 1
                continue
            col.head += col.speed
            if col.length > 0:
                i = random.randrange(len(col.chars))
                col.chars[i] = choice(glyphs)
            head_y = int(col.head)
            tail_top = head_y - col.length
            if tail_top > h:
                self._cols[x] = self._spawn_streamer(h, start_above=False)
                continue
            y_lo = max(0, tail_top)
            y_hi = min(h - 1, head_y)
            if y_hi < 0:
                continue
            chars_ref = col.chars
            chars_n = len(chars_ref)
            col_len = max(col.length, 1)
            for y in range(y_lo, y_hi + 1):
                d = head_y - y
                ch = chars_ref[y % chars_n]
                if d == 0:
                    grid[y][x] = (ch, head_style)
                else:
                    idx = tail_len - int(tail_len * d / col_len)
                    if idx < 0:
                        idx = 0
                    grid[y][x] = (ch, tail_palette[idx])

    def segments_for_row(self, y: int, w: int) -> list[Segment]:
        if y < 0 or y >= len(self._grid):
            return [Segment(" " * w)]
        return _row_to_segments(self._grid[y])


# ---------------------------------------------------------------------------
# DoomFire — Fabien Sanglard's PSX DOOM fire effect
#   https://fabiensanglard.net/doom_fire_psx/
# ---------------------------------------------------------------------------

_DOOM_PALETTE_RGB: list[tuple[int, int, int]] = [
    (0x07, 0x07, 0x07), (0x1F, 0x07, 0x07), (0x2F, 0x0F, 0x07), (0x47, 0x0F, 0x07),
    (0x57, 0x17, 0x07), (0x67, 0x1F, 0x07), (0x77, 0x1F, 0x07), (0x8F, 0x27, 0x07),
    (0x9F, 0x2F, 0x07), (0xAF, 0x3F, 0x07), (0xBF, 0x47, 0x07), (0xC7, 0x47, 0x07),
    (0xDF, 0x4F, 0x07), (0xDF, 0x57, 0x07), (0xDF, 0x57, 0x07), (0xD7, 0x5F, 0x07),
    (0xD7, 0x5F, 0x07), (0xD7, 0x67, 0x0F), (0xCF, 0x6F, 0x0F), (0xCF, 0x77, 0x0F),
    (0xCF, 0x7F, 0x0F), (0xCF, 0x87, 0x17), (0xC7, 0x87, 0x17), (0xC7, 0x8F, 0x17),
    (0xC7, 0x97, 0x1F), (0xBF, 0x9F, 0x1F), (0xBF, 0x9F, 0x1F), (0xBF, 0xA7, 0x27),
    (0xBF, 0xA7, 0x27), (0xBF, 0xAF, 0x2F), (0xB7, 0xAF, 0x2F), (0xB7, 0xB7, 0x2F),
    (0xB7, 0xB7, 0x37), (0xCF, 0xCF, 0x6F), (0xDF, 0xDF, 0x9F), (0xEF, 0xEF, 0xC7),
    (0xFF, 0xFF, 0xFF),
]

# Collapse the 37-step palette into 8 indexed colors — same reasoning as
# the Matrix palette shrink: each distinct style in the rendered frame
# emits one SGR escape sequence, so fewer ⇒ lighter terminal paint.
# Covers the visible ramp (dark red → orange → yellow → white).
_DOOM_INDEXED = [
    "color(16)",   # ≈ black — kept for v=0-ish
    "color(52)",   # dark maroon
    "color(88)",
    "color(124)",  # red
    "color(166)",  # orange
    "color(208)",
    "color(214)",  # yellow-orange
    "color(220)",  # yellow
    "color(229)",  # pale
    "color(231)",  # white
]
_DOOM_STYLES: list[Style] = []
for _i in range(len(_DOOM_PALETTE_RGB)):
    # Map 0..36 into 0..len(_DOOM_INDEXED)-1.
    _idx = int(_i * len(_DOOM_INDEXED) / len(_DOOM_PALETTE_RGB))
    _DOOM_STYLES.append(Style(color=_DOOM_INDEXED[_idx]))
_DOOM_MAX = len(_DOOM_PALETTE_RGB) - 1
_DOOM_MID = _DOOM_MAX // 2


class DoomFire(_Animation):
    """DOOM PSX fire effect."""

    FPS = 20

    DEFAULT_CSS = """
    DoomFire {
        height: 100%;
        width: 100%;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._buf: list[list[int]] = []
        self._w = 0
        self._h = 0

    def on_resize_reset(self, w: int, h: int) -> None:
        self._w = w
        self._h = h
        self._buf = [[0] * w for _ in range(h)]
        if h > 0:
            self._buf[h - 1] = [_DOOM_MAX] * w

    def _step(self) -> None:
        w, h = self._w, self._h
        buf = self._buf
        randrange = random.randrange
        for y in range(h - 2, -1, -1):
            below = buf[y + 1]
            row = buf[y]
            for x in range(w):
                r = randrange(4)            # 0..3 decay
                dx = randrange(3) - 1       # -1, 0, or 1 wind
                src_x = x + dx
                if src_x < 0:
                    src_x = 0
                elif src_x >= w:
                    src_x = w - 1
                v = below[src_x] - r
                row[src_x] = v if v >= 0 else 0

    def advance(self, w: int, h: int, t: int) -> None:
        self._step()

    def segments_for_row(self, y: int, w: int) -> list[Segment]:
        if y < 0 or y >= self._h:
            return [Segment(" " * w)]
        src = self._buf[y]
        styles = _DOOM_STYLES
        mid = _DOOM_MID
        cells: list[tuple[str, Style]] = [(" ", _BLANK)] * w
        for x in range(min(w, self._w)):
            v = src[x]
            if v > 0:
                cells[x] = ("█" if v >= mid else "▓", styles[v])
        return _row_to_segments(cells)
