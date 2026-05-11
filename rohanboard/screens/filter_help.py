"""Modal help popup for the filter input — clickable variables insert into the filter."""
from __future__ import annotations

from dataclasses import dataclass

from typing import Callable

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static


@dataclass
class FilterHelpSpec:
    title: str
    text_fields: list[str]
    numeric_fields: list[str]
    examples: list[str]


class FilterHelpModal(ModalScreen[None]):
    """Stay-open filter help.

    - Click a field chip → fires `on_insert(fragment)` *without* closing.
    - Click anywhere else / right-click / Esc / q → close.
    """

    DEFAULT_CSS = """
    FilterHelpModal {
        align: center middle;
    }
    FilterHelpModal > Vertical {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    FilterHelpModal .title {
        text-style: bold;
        color: $accent;
        height: 1;
        padding-bottom: 1;
    }
    FilterHelpModal .section_title {
        text-style: bold;
        height: 1;
        padding: 1 0 0 0;
        color: $text-muted;
    }
    FilterHelpModal .example {
        height: 1;
        color: $text-muted;
    }
    FilterHelpModal .hint {
        height: 1;
        padding-top: 1;
        color: $text-muted;
        text-style: italic;
    }
    FilterHelpModal .chip_grid {
        layout: grid;
        grid-size: 3;
        grid-gutter: 0 1;
        grid-rows: 1;
        height: auto;
    }
    FilterHelpModal .var {
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $accent;
    }
    FilterHelpModal .var:hover {
        background: $primary 70%;
        color: $background;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("q", "cancel", "Close"),
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(self, spec: FilterHelpSpec, on_insert: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self.spec = spec
        self._on_insert = on_insert

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Filter — {self.spec.title}", classes="title")

            yield Static(
                "Syntax (combine tokens with spaces → AND):\n"
                "  [bold]word[/bold]            substring in any default field\n"
                "  [bold]field:substr[/bold]    substring in specific field\n"
                "  [bold]field>=N[/bold] / <= / > / < / =    numeric compare",
                classes="example",
            )

            yield Static("Text fields  [dim](click to insert `field:`)[/dim]", classes="section_title")
            with Grid(classes="chip_grid"):
                for name in self.spec.text_fields:
                    yield Static(name, classes="var", id=f"var_text_{name}")

            yield Static("Numeric fields  [dim](click to insert `field>=`)[/dim]", classes="section_title")
            with Grid(classes="chip_grid"):
                for name in self.spec.numeric_fields:
                    yield Static(name, classes="var", id=f"var_num_{name}")

            yield Static("Examples:", classes="section_title")
            for ex in self.spec.examples:
                yield Static(f"  {ex}", classes="example")

            yield Static(
                "click variable → inserts into filter · q / Esc / right-click / click outside → close",
                classes="hint",
            )

    def on_click(self, event: events.Click) -> None:
        w = getattr(event, "widget", None) or getattr(event, "control", None)
        wid = getattr(w, "id", None) or ""
        button = getattr(event, "button", 1)  # 1 = left, 3 = right
        if button == 3:
            self.dismiss(None)
            return
        if wid.startswith("var_text_"):
            self._insert(wid[len("var_text_"):] + ":")
            return
        if wid.startswith("var_num_"):
            self._insert(wid[len("var_num_"):] + ">=")
            return
        # Click in empty chrome (modal backdrop / the outer Screen itself) → close.
        # We detect this by bubbling: the event's immediate widget is the Screen.
        if w is self:
            self.dismiss(None)

    def _insert(self, fragment: str) -> None:
        if self._on_insert is not None:
            self._on_insert(fragment)

    def action_cancel(self) -> None:
        self.dismiss(None)


def build_nodes_filter_spec(cfg_columns=None) -> "FilterHelpSpec":
    """Bundle-3 B3.3: per-cluster Nodes filter help. The base numeric
    fields (cpu/gpu/mem) are universal; the storage fields and one
    example token are built from `cfg.nodes_table.columns` so a cluster
    that doesn't declare `[[nodes_table.columns]]` (e.g. LRZ) doesn't
    see rohan-specific `ssd_free` / `hdd_used` tokens that match nothing
    on its tree.

    `cfg_columns` is a list of `NodesTableColumnConfig` (or anything
    with `.id` + `.header` attrs); pass `None` / `[]` to get the
    generic fallback.
    """
    cfg_columns = list(cfg_columns or [])
    base_numeric = [
        "cpu_free", "cpu_alloc", "cpu_total",
        "gpu_free", "gpu_alloc", "gpu_total",
        "mem_free", "mem_alloc", "mem_total",
    ]
    per_column_numeric: list[str] = []
    for col in cfg_columns:
        per_column_numeric.extend([
            f"{col.id}_free", f"{col.id}_used", f"{col.id}_total",
        ])

    examples = [
        "a6000 gpu_free>=1         rtx_a6000 nodes with free GPU",
        "state==IDLE               exact match (excludes IDLE+DRAIN)",
        "gpu_kind:a6000|a100       either GPU family",
    ]
    if cfg_columns:
        first = cfg_columns[0]
        examples.append(
            f"{first.id}_free>=1T              ≥ 1 TiB of {first.header} free"
        )
    else:
        examples.append(
            "mem_free>=500G            nodes with ≥ 500 GiB mem headroom"
        )
    examples.append("a100 mem_free>=500G       a100 with big mem headroom")

    return FilterHelpSpec(
        title="Nodes",
        text_fields=["name", "state", "partitions", "gpu_kind"],
        numeric_fields=base_numeric + per_column_numeric,
        examples=examples,
    )

JOBS_FILTER_SPEC = FilterHelpSpec(
    title="Jobs",
    text_fields=["id", "name", "user", "state", "partition", "node", "tres"],
    numeric_fields=["cpu", "gpu", "nodes", "mem"],
    examples=[
        "state:RUNNING               running jobs only",
        "state==RUNNING|PENDING      exactly running or pending",
        "user:hli gpu>=1             my GPU jobs",
        "cpu>=8                      jobs allocating 8+ CPUs",
        "mem>=100G                   jobs asking ≥ 100 GiB memory",
    ],
)
