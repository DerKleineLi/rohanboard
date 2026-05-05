"""Modal screen with four tabbed views for one job: stdout, stderr, script, env.

Uses Textual's TabbedContent for the tab UI (matches the main window).
stdout / stderr each have their own RichLog and tail their files on a 1 s
poll regardless of which tab is active — switching tabs is instant.  script
and env are fetched once on mount in the background and rendered to their
own RichLogs as soon as the result lands.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, RichLog, Static, TabbedContent, TabPane

from ..collectors import slurm


TABS: tuple[str, ...] = ("stdout", "stderr", "script", "info")

# Curated subset of scontrol show job / sacct fields, grouped for the info
# tab.  Keys come from parse_scontrol_show_job (scontrol naming) or are
# remapped by parse_sacct_row to the same keys, so this works for both
# live and completed jobs.
_INFO_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Identity", ("JobId", "JobName", "UserId", "GroupId", "Account", "JobState",
                  "Reason", "Partition")),
    ("Timing",   ("SubmitTime", "StartTime", "EndTime", "RunTime", "TimeLimit",
                  "TimeMin", "EligibleTime", "DeadLine")),
    ("Resources",("NodeList", "BatchHost", "NumNodes", "NumCPUs", "NumTasks",
                  "CPUs/Task", "MinMemoryNode", "MinMemoryCPU", "TRES",
                  "Features", "Gres", "Licenses")),
    ("Paths",    ("WorkDir", "StdOut", "StdErr", "StdIn", "Command")),
    ("Other",    ("Priority", "Nice", "QOS", "Comment", "AdminComment",
                  "Switches", "Network", "Contiguous")),
)
_RICHLOG_KW = dict(highlight=False, markup=False, wrap=False)


def _substitute_slurm_path(template: str, info: dict[str, str]) -> str:
    """Expand the small set of Slurm filename patterns we encounter in practice."""
    nodelist = info.get("NodeList", "") or ""
    first_node = nodelist.split(",")[0].split("[")[0] if nodelist else ""
    # Per Slurm filename pattern docs (sbatch(1)):
    #   %j = jobid (numeric)        %x = job name
    #   %A = array master jobid     %a = array task id
    #   %J = jobid.stepid           %u = user name
    #   %N = first node             %n = node-rel index   %t = task index
    subs = {
        "%j": info.get("JobId", ""),
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
    LogTailScreen TabbedContent {
        height: 1fr;
    }
    LogTailScreen TabPane {
        padding: 0;
    }
    LogTailScreen RichLog {
        height: 1fr;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("q", "dismiss", "Close"),
        Binding("escape", "dismiss", "Close", show=False),
        Binding("e", "next_tab", "Cycle stdout/stderr/script/env"),
    ]

    def __init__(self, job_id: str) -> None:
        super().__init__()
        self.job_id = job_id
        self._info: dict[str, str] = {}
        # Per-stream file handles + polling state
        self._fh: dict[str, object | None] = {"stdout": None, "stderr": None}
        self._path: dict[str, Path | None] = {"stdout": None, "stderr": None}
        self._poll_handle = None
        # script cache — populated by background fetch
        self._script_done = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Loading job {self.job_id}…", classes="header", id="header")
            with TabbedContent(initial="stdout"):
                with TabPane("stdout", id="stdout"):
                    yield RichLog(id="log_stdout", **_RICHLOG_KW)
                with TabPane("stderr", id="stderr"):
                    yield RichLog(id="log_stderr", **_RICHLOG_KW)
                with TabPane("script", id="script"):
                    yield RichLog(id="log_script", **_RICHLOG_KW)
                with TabPane("info", id="info"):
                    yield RichLog(id="log_info", **dict(_RICHLOG_KW, markup=True))
            yield Footer()

    async def on_mount(self) -> None:
        try:
            self._info = await slurm.fetch_job_info(self.app.executor, self.job_id)
        except Exception as e:
            self._write("stdout", f"[error] could not fetch job info "
                                  f"(scontrol + sacct both failed): {e}")
            return
        if not self._info:
            self._write("stdout", f"[error] no job info returned for job {self.job_id}")
            return
        self._update_header()
        # Prime stdout + stderr by opening their files now.  Both poll in the
        # background; switching tabs only changes which RichLog is visible.
        self._open_stream("stdout")
        self._open_stream("stderr")
        self._poll_handle = self.set_interval(1.0, self._poll)
        # Render info tab synchronously from the dict we already have.
        self._render_info()
        # Background-fetch script
        asyncio.create_task(self._fetch_script())

    def on_unmount(self) -> None:
        for fh in self._fh.values():
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
        if self._poll_handle is not None:
            self._poll_handle.stop()

    def action_next_tab(self) -> None:
        tc = self.query_one(TabbedContent)
        idx = TABS.index(tc.active) if tc.active in TABS else 0
        tc.active = TABS[(idx + 1) % len(TABS)]

    # ── Header ──

    def _update_header(self) -> None:
        header = self.query_one("#header", Static)
        header.update(
            f"job {self.job_id}  [{self._info.get('JobName', '?')}]  "
            f"state={self._info.get('JobState', '?')}   "
            f"[dim](e: cycle · q: close · click any tab to switch)[/dim]"
        )

    # ── stdout / stderr — file-tail loop ──

    def _path_for(self, stream: str) -> Path | None:
        key = "StdOut" if stream == "stdout" else "StdErr"
        raw = self._info.get(key)
        if not raw:
            return None
        return Path(_substitute_slurm_path(raw, self._info))

    def _open_stream(self, stream: str) -> None:
        path = self._path_for(stream)
        log = self.query_one(f"#log_{stream}", RichLog)
        if path is None:
            log.write(f"[no {stream.upper()} path on file]")
            return
        self._path[stream] = path
        log.write(f"[{stream.upper()}: {path}]")
        if not path.exists():
            log.write(f"[file does not exist yet: {path}]")
            return
        try:
            fh = path.open("r", errors="replace")
            lines = fh.readlines()
            fh.seek(0, 2)
            self._fh[stream] = fh
            for line in lines[-200:]:
                log.write(line.rstrip("\n"))
        except OSError as e:
            log.write(f"[open failed: {e}]")
            self._fh[stream] = None

    def _poll(self) -> None:
        for stream in ("stdout", "stderr"):
            fh = self._fh.get(stream)
            if fh is None:
                # File may have appeared since we last tried (e.g. job started
                # writing).  Re-attempt open.
                self._open_stream(stream)
                continue
            log = self.query_one(f"#log_{stream}", RichLog)
            try:
                new = fh.read()
            except OSError as e:
                log.write(f"[read failed: {e}]")
                continue
            if not new:
                continue
            for line in new.splitlines():
                log.write(line)

    # ── script / env — one-shot async fetch ──

    async def _fetch_script(self) -> None:
        log = self.query_one("#log_script", RichLog)
        log.write("[fetching batch script…]")
        try:
            text = await slurm.fetch_job_script(self.app.executor, self.job_id)
        except Exception as e:
            text = f"[batch script unavailable: {e}]"
        self._script_done = True
        log.clear()
        if not text:
            log.write("[batch script empty]")
            return
        for line in text.splitlines():
            log.write(line)

    def _render_info(self) -> None:
        """Render curated job-info fields from the same dict that gave us the
        stdout/stderr paths.  Useful for understanding *what we knew* about
        the job (identity, timing, resources, paths) and where it came from.
        Replaces what would otherwise be the env tab — Slurm 24+ removed
        `scontrol getenvironment` so real env retrieval isn't possible here."""
        log = self.query_one("#log_info", RichLog)
        log.clear()
        if not self._info:
            log.write("[no job info]")
            return

        source = self._info.get("_source", "(unknown)")
        log.write(f"[bold]Source:[/bold] [dim]{source}[/dim]")
        log.write("")

        # Compute width for left column
        all_keys = [k for _, keys in _INFO_SECTIONS for k in keys]
        width = max((len(k) for k in all_keys), default=12)

        for section_name, keys in _INFO_SECTIONS:
            present = [(k, self._info[k]) for k in keys if k in self._info and self._info[k]]
            if not present:
                continue
            log.write(f"[bold]── {section_name} ──[/bold]")
            for k, v in present:
                log.write(f"  {k.ljust(width)}  {v}")
            log.write("")

        # Anything else not covered by the curated sections — show in a
        # "Misc" block so users can still spot rare fields (Reservation,
        # ReqNodeList, MinTmpDiskNode, ArrayJobId, …) without code changes.
        covered = {k for _, keys in _INFO_SECTIONS for k in keys}
        misc = [(k, v) for k, v in self._info.items()
                if k not in covered and not k.startswith("_") and v]
        if misc:
            log.write("[bold]── Misc ──[/bold]")
            for k, v in sorted(misc):
                log.write(f"  {k.ljust(width)}  {v}")

    # ── Click-to-dismiss on backdrop ──

    def on_click(self, event: events.Click) -> None:
        # Right-click anywhere or click on the modal's translucent backdrop
        # dismisses.  TabbedContent handles its own clicks for switching.
        if getattr(event, "button", 1) == 3:
            self.dismiss()
            return
        target = getattr(event, "control", None) or getattr(event, "widget", None)
        if target is self:
            self.dismiss()

    # ── Helpers ──

    def _write(self, stream: str, text: str) -> None:
        try:
            self.query_one(f"#log_{stream}", RichLog).write(text)
        except Exception:
            pass
