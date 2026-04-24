"""Modal screen that tails a job's stdout/stderr."""
from __future__ import annotations

from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, RichLog, Static

from ..collectors import slurm


def _substitute_slurm_path(template: str, info: dict[str, str]) -> str:
    """Expand the small set of Slurm filename patterns we encounter in practice."""
    nodelist = info.get("NodeList", "") or ""
    first_node = nodelist.split(",")[0].split("[")[0] if nodelist else ""
    subs = {
        "%j": info.get("JobName", ""),
        "%x": info.get("JobName", ""),
        "%u": info.get("UserId", "").split("(")[0],
        "%A": info.get("JobId", ""),
        "%J": info.get("JobId", ""),
        "%a": info.get("ArrayTaskId", ""),
        "%N": first_node,
        "%n": "0",
        "%t": "0",
    }
    out = template
    for k, v in subs.items():
        out = out.replace(k, v)
    return out.replace("%%", "%")


class LogTailScreen(ModalScreen):
    DEFAULT_CSS = """
    LogTailScreen {
        align: center middle;
    }
    LogTailScreen > Vertical {
        width: 90%;
        height: 90%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    LogTailScreen .header {
        height: auto;
        padding-bottom: 1;
        text-style: bold;
    }
    LogTailScreen .pill_row {
        height: 1;
        padding-bottom: 1;
    }
    LogTailScreen .stream_pill {
        width: auto;
        height: 1;
        padding: 0 1;
        margin-right: 1;
        background: $boost;
        color: $text-muted;
    }
    LogTailScreen .stream_pill.-active {
        background: $accent;
        color: $background;
        text-style: bold;
    }
    LogTailScreen .stream_pill:hover {
        background: $primary 70%;
    }
    LogTailScreen RichLog {
        height: 1fr;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("q", "dismiss", "Close"),
        Binding("escape", "dismiss", "Close", show=False),
        Binding("e", "toggle_stream", "Stdout/Stderr"),
    ]

    def __init__(self, job_id: str) -> None:
        super().__init__()
        self.job_id = job_id
        self._info: dict[str, str] = {}
        self._stream = "stdout"
        self._path: Path | None = None
        self._fh = None
        self._poll_handle = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Loading job {self.job_id}…", classes="header", id="header")
            with Horizontal(classes="pill_row"):
                yield Static(" stdout ", classes="stream_pill -active", id="pill_stdout")
                yield Static(" stderr ", classes="stream_pill", id="pill_stderr")
            yield RichLog(highlight=False, markup=False, wrap=False, id="log")
            yield Footer()

    async def on_mount(self) -> None:
        try:
            self._info = await slurm.fetch_job_info(self.job_id)
        except Exception as e:
            self.query_one("#log", RichLog).write(f"[error] scontrol show job failed: {e}")
            return
        self._open_stream()
        self._poll_handle = self.set_interval(1.0, self._poll)

    def on_unmount(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        if self._poll_handle is not None:
            self._poll_handle.stop()

    def on_click(self, event: events.Click) -> None:
        # Right-click anywhere, or any click on the modal's translucent
        # backdrop (i.e. the ModalScreen itself, outside the inner panel)
        # dismisses.  Left-clicks inside the inner Vertical pass through so
        # users can still interact with the log / scroll.
        if getattr(event, "button", 1) == 3:
            self.dismiss()
            return
        target = getattr(event, "control", None) or getattr(event, "widget", None)
        wid = getattr(target, "id", None) or ""
        if wid == "pill_stdout" and self._stream != "stdout":
            self._switch_stream("stdout")
            return
        if wid == "pill_stderr" and self._stream != "stderr":
            self._switch_stream("stderr")
            return
        if target is self:
            self.dismiss()

    def _switch_stream(self, new: str) -> None:
        self._stream = new
        self.query_one("#pill_stdout", Static).set_class(new == "stdout", "-active")
        self.query_one("#pill_stderr", Static).set_class(new == "stderr", "-active")
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self.query_one("#log", RichLog).clear()
        self._open_stream()

    def action_toggle_stream(self) -> None:
        self._switch_stream("stderr" if self._stream == "stdout" else "stdout")

    def _path_for_stream(self) -> Path | None:
        key = "StdOut" if self._stream == "stdout" else "StdErr"
        raw = self._info.get(key)
        if not raw:
            return None
        return Path(_substitute_slurm_path(raw, self._info))

    def _open_stream(self) -> None:
        path = self._path_for_stream()
        log = self.query_one("#log", RichLog)
        header = self.query_one("#header", Static)
        if path is None:
            header.update(f"job {self.job_id} — no {self._stream.upper()} path on file")
            return
        self._path = path
        header.update(
            f"job {self.job_id}  [{self._info.get('JobName', '?')}]  "
            f"{self._stream.upper()}: {path}   [dim](e: switch · q: close)[/dim]"
        )
        if not path.exists():
            log.write(f"[file does not exist yet: {path}]")
            return
        try:
            self._fh = path.open("r", errors="replace")
            lines = self._fh.readlines()
            self._fh.seek(0, 2)
            for line in lines[-200:]:
                log.write(line.rstrip("\n"))
        except OSError as e:
            log.write(f"[open failed: {e}]")
            self._fh = None

    def _poll(self) -> None:
        if self._fh is None:
            self._open_stream()
            return
        log = self.query_one("#log", RichLog)
        try:
            new = self._fh.read()
        except OSError as e:
            log.write(f"[read failed: {e}]")
            return
        if not new:
            return
        for line in new.splitlines():
            log.write(line)
