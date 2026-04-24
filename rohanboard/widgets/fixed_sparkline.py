"""Sparkline widget that normalises to a fixed [0, 1] range.

Textual's stock ``Sparkline`` auto-normalises each bar to the min/max of the
series, so a steady stream of 30% values renders as a flat line rather than
a low bar.  This subclass overrides the renderable to use a fixed 0–1 range
so bar heights always reflect absolute values.
"""
from __future__ import annotations

from typing import Sequence

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from textual.app import RenderResult as TextualRenderResult
from textual.renderables._blend_colors import blend_colors
from textual.renderables.sparkline import Sparkline as SparklineRenderable
from textual.widgets import Sparkline as SparklineWidget


class _FixedRangeRenderable(SparklineRenderable):
    """Renderable that clamps values to [0, 1] instead of auto-normalising."""

    BARS = "▁▂▃▄▅▆▇█"

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = self.width or options.max_width
        height = self.height or 1

        if not self.data:
            for _ in range(height - 1):
                yield Segment.line()
            yield Segment("▁" * width, self.min_color)
            return

        bar_line_segments = len(self.BARS)         # 8
        bar_segments = bar_line_segments * height - 1

        summary_function = self.summary_function
        min_color = self.min_color.color
        max_color = self.max_color.color
        assert min_color is not None and max_color is not None

        buckets = tuple(self._buckets(list(self.data), num_buckets=width))
        if not buckets:
            for _ in range(height - 1):
                yield Segment.line()
            yield Segment(" " * width, None)
            return

        for i in reversed(range(height)):
            current_bar_part_low = i * bar_line_segments
            current_bar_part_high = (i + 1) * bar_line_segments

            bucket_index = 0.0
            bars_rendered = 0
            step = len(buckets) / width
            while bars_rendered < width:
                partition = buckets[int(bucket_index)]
                summary = summary_function(partition)
                # Clamp to [0, 1] — this is the whole point of the subclass.
                height_ratio = max(0.0, min(1.0, float(summary)))
                bar_index = int(height_ratio * bar_segments)

                if bar_index < current_bar_part_low:
                    bar = " "
                    style: Style | None = None
                elif bar_index >= current_bar_part_high:
                    bar = "█"
                    style = Style.from_color(
                        blend_colors(min_color, max_color, height_ratio)
                    )
                else:
                    bar = self.BARS[bar_index % bar_line_segments]
                    style = Style.from_color(
                        blend_colors(min_color, max_color, height_ratio)
                    )

                bars_rendered += 1
                bucket_index += step
                yield Segment(bar, style)

            if i > 0:
                yield Segment.line()


class FixedSparkline(SparklineWidget):
    """Sparkline widget that uses a fixed 0–1 range (no auto-normalisation)."""

    def render(self) -> TextualRenderResult:
        data = self.data or []
        _, base = self.background_colors
        min_color = base + (
            self.get_component_styles("sparkline--min-color").color
            if self.min_color is None
            else self.min_color
        )
        max_color = base + (
            self.get_component_styles("sparkline--max-color").color
            if self.max_color is None
            else self.max_color
        )
        return _FixedRangeRenderable(
            data,
            width=self.size.width,
            height=self.size.height,
            min_color=min_color.rich_color,
            max_color=max_color.rich_color,
            summary_function=self.summary_function,
        )
