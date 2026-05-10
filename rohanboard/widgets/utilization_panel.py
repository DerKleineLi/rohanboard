"""Three sparklines (CPU, GPU, Mem) stacked vertically.

Fills its allotted height via `height: 100%`; the sparklines share the body
equally with `height: 1fr`, so the panel scales naturally when its parent
hands it more or less room — no imperative sizing.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from ..collectors.models import Snapshot
from .fixed_sparkline import FixedSparkline as Sparkline


class UtilizationPanel(Widget):
    # Phase 4d.2-E: lowered from 13 → 8 by adapting to a single-row
    # layout (3 sparklines side-by-side rather than stacked) when the
    # caller hands us less than COMPACT_THRESHOLD rows. The absolute
    # floor (border 2 + title 1 + margins 2 + 1 label-row + 1 spark-row
    # + padding 1 = 8) is what `min-height` enforces below; the
    # OverviewPanel's `decide_layout` uses this constant to decide
    # whether util fits in the column slack.
    MIN_HEIGHT = 8
    # Below this height (in rows of the panel itself, including border
    # + padding) we render the three metrics side-by-side. Above it,
    # the classic vertical stack with full sparkline body.
    COMPACT_THRESHOLD = 12

    DEFAULT_CSS = """
    UtilizationPanel {
        height: 1fr;
        min-height: 8;
        min-width: 40;
        padding: 0 2 1 2;
        margin: 0;
        border: round $primary;
    }
    UtilizationPanel > .title {
        text-style: bold;
        height: 1;
        margin: 1 0;
    }
    UtilizationPanel > .body {
        height: 1fr;
        min-height: 0;
        layout: vertical;
    }
    UtilizationPanel .metric_cell {
        height: 1fr;
        min-height: 2;
        layout: vertical;
    }
    UtilizationPanel .label {
        height: 1;
        color: $text-muted;
    }
    UtilizationPanel FixedSparkline {
        height: 1fr;
        min-height: 1;
    }
    UtilizationPanel #spark_cpu > .sparkline--max-color { color: $success; }
    UtilizationPanel #spark_cpu > .sparkline--min-color { color: $success-darken-2; }
    UtilizationPanel #spark_gpu > .sparkline--max-color { color: $warning; }
    UtilizationPanel #spark_gpu > .sparkline--min-color { color: $warning-darken-2; }
    UtilizationPanel #spark_mem > .sparkline--max-color { color: $accent; }
    UtilizationPanel #spark_mem > .sparkline--min-color { color: $accent-darken-2; }

    /* Compact mode: body becomes 3 horizontal cells, each with its
       own label-on-top sparkline-below stack. Triggered when the
       allocated height is too small for the vertical 3-stack. */
    UtilizationPanel.compact > .body {
        layout: grid;
        grid-size: 3 1;
        grid-gutter: 0 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Utilization", classes="title")
        with Vertical(classes="body"):
            for metric in ("cpu", "gpu", "mem"):
                with Vertical(classes="metric_cell"):
                    yield Static(metric.upper(), classes="label", id=f"lbl_{metric}")
                    yield Sparkline([0.0], summary_function=max, id=f"spark_{metric}")

    def on_resize(self) -> None:
        """Toggle the compact (single-row) layout based on the height the
        parent layout actually handed us. Hysteresis isn't critical
        here — the threshold is well away from typical fit sizes."""
        h = int(self.size.height or 0)
        if 0 < h < self.COMPACT_THRESHOLD:
            self.add_class("compact")
        else:
            self.remove_class("compact")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        history = snapshot.history
        if not history:
            return
        cpu = [max(0.0, min(1.0, s.cpu)) for s in history]
        gpu = [max(0.0, min(1.0, s.gpu)) for s in history]
        mem = [max(0.0, min(1.0, s.mem)) for s in history]
        latest = history[-1]
        labels = {
            "cpu": f"CPU  [bold]{latest.cpu*100:5.1f}%[/bold]",
            "gpu": f"GPU  [bold]{latest.gpu*100:5.1f}%[/bold]",
            "mem": f"Mem  [bold]{latest.mem*100:5.1f}%[/bold]",
        }
        data = {"cpu": cpu, "gpu": gpu, "mem": mem}
        for m in ("cpu", "gpu", "mem"):
            try:
                self.query_one(f"#lbl_{m}", Static).update(labels[m])
                self.query_one(f"#spark_{m}", Sparkline).data = data[m]
            except Exception:
                pass
