"""Modal screen with four tabbed views for one job: stdout, stderr, script, env.

Uses Textual's TabbedContent for the tab UI (matches the main window).
stdout / stderr each have their own RichLog and tail their files on a 1 s
poll regardless of which tab is active — switching tabs is instant.  script
and env are fetched once on mount in the background and rendered to their
own RichLogs as soon as the result lands.

Bundle-3 B3.5 (2026-05-11): tail goes through the executor, not local
Path I/O. Pre-B3.5 the screen called `path.exists()` / `path.open()`
directly — fine on `exec = "local"`, broken on `exec = "ssh:rohan"`
because the slurm log path lives on the cluster and the WSL host
can't see it (caused "[file does not exist yet]" on every poll). Now
each poll runs a small bash `stat + tail -c` over the executor, which
multiplexes onto the same SSH connection the collectors use. Repeated
identical errors (missing file, exec failure) are deduped per stream.
"""
from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, RichLog, Static, TabbedContent, TabPane

from ..collectors import slurm
from ..exec import Executor


# Initial fetch budget: read the trailing 64 KiB (≈hundreds of lines) so
# the user lands on recent output rather than the beginning of a long log.
_INITIAL_TAIL_BYTES = 64 * 1024


def _build_tail_script(path: str, cursor: int, initial_bytes: int = _INITIAL_TAIL_BYTES) -> str:
    """Single bash one-liner that prints:
      * `__MISSING__` (and nothing else) if `path` doesn't exist;
      * `__SIZE__=<bytes>\\n` first, then the new content from `cursor`;
      * On detected rotation (size < cursor) prepends `__ROTATED__\\n`
        before the new content and rewinds to the trailing
        `initial_bytes`.

    One ssh round-trip per poll per stream. Output is parsed by
    `_parse_tail_output` below.
    """
    qp = shlex.quote(path)
    return (
        f"if [ ! -f {qp} ]; then echo __MISSING__; exit 0; fi; "
        f"s=$(stat -c %s {qp} 2>/dev/null); "
        f"echo \"__SIZE__=$s\"; "
        f"if [ \"$s\" -lt {cursor} ]; then "
        f"  echo __ROTATED__; tail -c {initial_bytes} {qp}; "
        f"elif [ \"$s\" -gt {cursor} ]; then "
        f"  tail -c +{cursor + 1} {qp}; "
        f"fi"
    )


def _parse_tail_output(stdout: str) -> tuple[str, int | None, str]:
    """Parse `_build_tail_script` output. Returns
    `(status, new_size, content)`:
      * status: "missing" | "ok" | "rotated" | "malformed"
      * new_size: file's current size in bytes, or None for missing.
      * content: bytes after the cursor (or last 64K on rotation).
    """
    if stdout.startswith("__MISSING__"):
        return "missing", None, ""
    lines = stdout.split("\n", 2)   # split off header lines (max 3 splits)
    if not lines or not lines[0].startswith("__SIZE__="):
        return "malformed", None, ""
    try:
        size = int(lines[0].removeprefix("__SIZE__="))
    except ValueError:
        return "malformed", None, ""
    rest = "\n".join(lines[1:]) if len(lines) > 1 else ""
    if rest.startswith("__ROTATED__\n"):
        return "rotated", size, rest[len("__ROTATED__\n"):]
    if rest == "__ROTATED__":
        return "rotated", size, ""
    return "ok", size, rest


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

    def __init__(self, job_id: str, executor: Executor) -> None:
        super().__init__()
        self.job_id = job_id
        self.executor = executor
        self._info: dict[str, str] = {}
        # Bundle-3 B3.5: executor-routed tail state. The cursor is the
        # byte offset already-shown for each stream; the path string is
        # resolved from scontrol's StdOut/StdErr once on mount.
        # `last_error` dedups repeated identical failure messages — set
        # on print, cleared on a successful read.
        self._cursor: dict[str, int] = {"stdout": 0, "stderr": 0}
        self._path: dict[str, str | None] = {"stdout": None, "stderr": None}
        self._opened: dict[str, bool] = {"stdout": False, "stderr": False}
        self._last_error: dict[str, str | None] = {"stdout": None, "stderr": None}
        self._poll_handle = None
        self._poll_in_flight: bool = False
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
            self._info = await slurm.fetch_job_info(self.executor, self.job_id)
        except Exception as e:
            self._write("stdout", f"[error] could not fetch job info "
                                  f"(scontrol + sacct both failed): {e}")
            return
        if not self._info:
            self._write("stdout", f"[error] no job info returned for job {self.job_id}")
            return
        self._update_header()
        # Bundle-3 B3.5: resolve paths now (synchronous from the info
        # dict); the first poll fetches initial content via the
        # executor. Both streams poll in the background; switching
        # tabs only changes which RichLog is visible.
        self._announce_stream("stdout")
        self._announce_stream("stderr")
        self._poll_handle = self.set_interval(1.0, self._kick_poll)
        # Kick a poll immediately so the user sees content on open
        # instead of waiting up to 1 s for the first interval fire.
        asyncio.create_task(self._poll_async())
        # Render info tab synchronously from the dict we already have.
        self._render_info()
        # Background-fetch script
        asyncio.create_task(self._fetch_script())

    def on_unmount(self) -> None:
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

    def _path_for(self, stream: str) -> str | None:
        """Resolve the remote (or local) path for a stream from scontrol's
        StdOut/StdErr field. Returns None when scontrol didn't report
        one (no `--output=` and no default; rare)."""
        key = "StdOut" if stream == "stdout" else "StdErr"
        raw = self._info.get(key)
        if not raw:
            return None
        # Substitution still happens in Python — the cluster has already
        # written the file with the expanded name; bash doesn't expand
        # `%j` / `%x` on its own.
        return _substitute_slurm_path(raw, self._info)

    def _announce_stream(self, stream: str) -> None:
        """One-shot header: '[STDOUT: /path/to/log]' or '[no STDOUT path
        on file]'. Called once per mount; poll loop handles the rest."""
        path = self._path_for(stream)
        log = self.query_one(f"#log_{stream}", RichLog)
        if path is None:
            self._maybe_write_error(stream, "no_path",
                                    f"[no {stream.upper()} path on file]")
            return
        self._path[stream] = path
        log.write(f"[{stream.upper()}: {path}]")

    def _maybe_write_error(self, stream: str, kind: str, msg: str) -> None:
        """Bundle-3 B3.5 Bug 2: dedup repeated identical error messages
        per stream. Same `kind` as the last shown error → suppress.
        Different kind (or first-time-since-success) → print and remember.
        Cleared on `_clear_error_dedup` after a successful read so a
        re-failure does print once more."""
        if self._last_error.get(stream) == kind:
            return
        self._last_error[stream] = kind
        try:
            self.query_one(f"#log_{stream}", RichLog).write(msg)
        except Exception:
            pass

    def _clear_error_dedup(self, stream: str) -> None:
        self._last_error[stream] = None

    def _kick_poll(self) -> None:
        """Sync entry from `set_interval` — schedule the async poller
        but never let two in-flight polls overlap (slow ssh tick →
        skip rather than queue)."""
        if self._poll_in_flight:
            return
        asyncio.create_task(self._poll_async())

    async def _poll_async(self) -> None:
        if self._poll_in_flight:
            return
        self._poll_in_flight = True
        try:
            await asyncio.gather(
                self._poll_stream("stdout"),
                self._poll_stream("stderr"),
                return_exceptions=True,
            )
        finally:
            self._poll_in_flight = False

    async def _poll_stream(self, stream: str) -> None:
        """One poll for one stream — runs the bash tail script over the
        executor, parses, writes new content to the RichLog. Handles
        missing-file / rotation / exec-failure states with dedup."""
        path = self._path[stream]
        if path is None:
            # No path resolved at mount → nothing to do, header already
            # noted via _announce_stream.
            return
        cursor = self._cursor[stream]
        first_open = not self._opened[stream]
        # On first open, treat cursor=0 specially so we get the tail
        # (last 64K) instead of streaming from the beginning of a
        # potentially-huge file.
        if first_open and cursor == 0:
            script_path = shlex.quote(path)
            # __MISSING__-aware preamble (mirrors `_build_tail_script`)
            # plus a `tail -c <budget>` initial read; on success we set
            # the cursor to the file size so subsequent polls pick up
            # only NEW bytes after this snapshot.
            script = (
                f"if [ ! -f {script_path} ]; then echo __MISSING__; exit 0; fi; "
                f"s=$(stat -c %s {script_path} 2>/dev/null); "
                f"echo \"__SIZE__=$s\"; "
                f"tail -c {_INITIAL_TAIL_BYTES} {script_path}"
            )
        else:
            script = _build_tail_script(path, cursor)
        try:
            rc, stdout, stderr = await self.executor.run(
                ["bash", "-c", script], timeout=10.0,
            )
        except Exception as e:
            self._maybe_write_error(
                stream, "exec_failed",
                f"[exec failed for {path}: {type(e).__name__}: {e}]",
            )
            return
        if rc != 0:
            self._maybe_write_error(
                stream, "exec_rc",
                f"[exec rc={rc} for {path}: {stderr.strip()[:200]}]",
            )
            return
        status, size, content = _parse_tail_output(stdout)
        if status == "missing":
            self._maybe_write_error(
                stream, "missing",
                f"[file does not exist yet: {path}]",
            )
            return
        if status == "malformed":
            self._maybe_write_error(
                stream, "malformed",
                f"[tail produced unexpected output for {path}]",
            )
            return
        # Success path — clear any prior dedup so a NEXT failure
        # re-prints once.
        self._clear_error_dedup(stream)
        self._opened[stream] = True
        if status == "rotated":
            try:
                self.query_one(f"#log_{stream}", RichLog).write(
                    f"[file rotated; resuming from end of new file: {path}]"
                )
            except Exception:
                pass
        if content:
            log = self.query_one(f"#log_{stream}", RichLog)
            for line in content.splitlines():
                log.write(line)
        if size is not None:
            self._cursor[stream] = size

    # ── script / env — one-shot async fetch ──

    async def _fetch_script(self) -> None:
        log = self.query_one("#log_script", RichLog)
        log.write("[fetching batch script…]")
        try:
            text = await slurm.fetch_job_script(self.executor, self.job_id)
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
