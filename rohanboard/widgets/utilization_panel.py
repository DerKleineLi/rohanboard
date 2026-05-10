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
    # Useful min height — border(2) + title(1) + title margins(2) + padding
    # bot(1) + body(3 labels=3 + 3 sparks @≥1 each=3) = 12; +1 for the
    # margin between cards in the OverviewPanel grid. Layouts that can't
    # spare this many rows should NOT mount a UtilizationPanel at all.
    # The CSS `min-height: 8` below is the absolute floor (sparks at 1
    # row, no breathing room); the dashboard's `UTIL_MIN_HEIGHT` checks
    # use this constant instead so both numbers live next to the widget
    # they describe.
    MIN_HEIGHT = 13

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
    """

    def compose(self) -> ComposeResult:
        yield Static("Utilization", classes="title")
        with Vertical(classes="body"):
            for metric in ("cpu", "gpu", "mem"):
                yield Static(metric.upper(), classes="label", id=f"lbl_{metric}")
                yield Sparkline([0.0], summary_function=max, id=f"spark_{metric}")

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
