"""Bundle-3 B3.5: LogTailScreen routes through the executor + dedups
missing-file errors.

Two bugs:
  * Bug 1: pre-B3.5 the screen called `Path.exists()` / `Path.open()`
    directly. Under `exec = "ssh:rohan"` the slurm log path lives on
    the cluster — local IO always failed → "[file does not exist yet]"
    every poll. Post-B3.5 the tail goes through the executor (LocalExecutor
    or AsyncSSHExecutor) via a bash one-liner.
  * Bug 2: the missing-file message was printed every poll. Post-B3.5
    `_maybe_write_error` dedups by (stream, kind) and clears the dedup
    on a successful read.

Tests here drive the screen against a FakeExecutor whose `run`
responses are scripted per call. The unit boundary is the bash
output `__MISSING__` / `__SIZE__=…` produced by `_build_tail_script`
— we synthesize those directly without spinning up a real shell.
"""
from __future__ import annotations

from rohanboard.screens.log_tail import (
    _build_tail_script,
    _parse_tail_output,
)


# ──────────────────────────────────────────────────────────────────────
# Pure-function tests: script + parser
# ──────────────────────────────────────────────────────────────────────


def test_build_tail_script_quotes_path_safely():
    """Paths with spaces / special chars must survive `bash -c`."""
    script = _build_tail_script("/cluster/balar/job 1234.log", 0)
    # The quoted form appears, and the raw with-space form does NOT
    # leak in a way that bash would tokenize differently.
    assert "'/cluster/balar/job 1234.log'" in script
    # No unquoted instance of the same string.
    unquoted_count = script.count("/cluster/balar/job 1234.log")
    quoted_count = script.count("'/cluster/balar/job 1234.log'")
    assert unquoted_count == quoted_count, (
        f"path appears unquoted somewhere; script:\n{script}"
    )


def test_build_tail_script_uses_byte_offset_for_subsequent_polls():
    """cursor=1024 → script's `tail -c +1025` reads from byte 1025 to end."""
    script = _build_tail_script("/var/log/job.log", cursor=1024)
    assert "tail -c +1025" in script, f"missing tail offset in:\n{script}"


def test_parse_tail_output_missing():
    status, size, content = _parse_tail_output("__MISSING__\n")
    assert status == "missing"
    assert size is None
    assert content == ""


def test_parse_tail_output_ok_with_new_bytes():
    status, size, content = _parse_tail_output(
        "__SIZE__=4096\n"
        "line one\n"
        "line two\n"
    )
    assert status == "ok"
    assert size == 4096
    assert content == "line one\nline two\n"


def test_parse_tail_output_rotated_prepends_marker():
    status, size, content = _parse_tail_output(
        "__SIZE__=128\n"
        "__ROTATED__\n"
        "fresh content\n"
    )
    assert status == "rotated"
    assert size == 128
    assert content == "fresh content\n"


def test_parse_tail_output_ok_with_size_only_when_no_new_bytes():
    status, size, content = _parse_tail_output("__SIZE__=4096\n")
    assert status == "ok"
    assert size == 4096
    assert content == ""


def test_parse_tail_output_malformed_returns_malformed():
    """Junk output (e.g. shell error) doesn't crash; reports malformed."""
    status, size, content = _parse_tail_output("syntax error or whatever\n")
    assert status == "malformed"
    assert size is None
    assert content == ""


# ──────────────────────────────────────────────────────────────────────
# Pilot test: missing-file message printed ONCE, then content takes over
# ──────────────────────────────────────────────────────────────────────


from textual.app import App
from textual.widgets import RichLog

from rohanboard.screens.log_tail import LogTailScreen


class _ScriptedExecutor:
    """FakeExecutor whose `run` walks a queue of canned responses.
    The Nth invocation pops the Nth response (or repeats the last
    one if the queue runs out)."""

    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], float | None]] = []
        self._whoami: str | None = ""

    async def run(self, argv, timeout=None):
        self.calls.append((tuple(argv), timeout))
        if self._responses:
            r = self._responses.pop(0)
        else:
            r = (0, "", "")
        return r

    async def resolve_whoami(self, timeout: float = 5.0) -> str:
        return ""

    async def aclose(self) -> None:
        return None


def _missing_response() -> tuple[int, str, str]:
    return (0, "__MISSING__\n", "")


def _content_response(size: int, content: str) -> tuple[int, str, str]:
    return (0, f"__SIZE__={size}\n{content}", "")


class _StreamScriptedExecutor:
    """FakeExecutor whose response depends on the bash script content
    — lets us steer `_poll_stream` deterministically per call.

    The screen's on_mount triggers scontrol calls (fetch_job_info +
    fetch_job_script). We pattern-match those vs. the bash tail
    script via the first argv element.
    """

    def __init__(self) -> None:
        # Per-stream response queues, popped on each tail-script call
        # that mentions the corresponding path.
        self._per_stream: dict[str, list[tuple[int, str, str]]] = {}
        self.calls: list[tuple[tuple[str, ...], float | None]] = []

    def queue(self, stream_path: str, responses: list[tuple[int, str, str]]) -> None:
        self._per_stream.setdefault(stream_path, []).extend(responses)

    async def run(self, argv, timeout=None):
        self.calls.append((tuple(argv), timeout))
        argv = list(argv)
        if argv[:2] == ["bash", "-c"]:
            script = argv[2]
            for path, queue in self._per_stream.items():
                if path in script and queue:
                    return queue.pop(0)
            # Default for any uncategorized bash call: empty-success
            # (mostly the `command -v sacct` probe etc.).
            return (0, "", "")
        # scontrol show job → return a fabricated dict-shaped output.
        if argv[:3] == ["scontrol", "show", "job"]:
            return (0,
                    "JobId=42 JobName=testjob UserId=hli(1000) "
                    "JobState=RUNNING Partition=test NodeList=node01 "
                    "NumNodes=1 NumCPUs=1 "
                    "StdOut=/tmp/fake_job_42.log "
                    "StdErr=/tmp/fake_job_42.err",
                    "")
        # Everything else (scontrol write batch_script, sacct fallback, etc.)
        # → empty success; the script tab just shows "empty".
        return (0, "", "")

    async def resolve_whoami(self, timeout: float = 5.0) -> str:
        return ""

    async def aclose(self) -> None:
        return None


async def test_log_tail_routes_through_executor_and_dedups_missing():
    """Bundle-3 B3.5 (both bugs in one Pilot test):

      * Bug 1 — tail runs over the executor, NOT direct file IO. We
        assert the executor was called with `bash -c <tail-script>`.
      * Bug 2 — repeated "missing" responses produce ONE message,
        then real content takes over once the file appears.

    Drives `_poll_stream` directly so the assertion doesn't depend on
    Textual's interval scheduling. Sequence:
      1. First poll: MISSING → prints the message, sets dedup.
      2. Second poll: still MISSING → dedup suppresses.
      3. Third poll: content lands → header (NOT the missing msg)
         plus the new lines; dedup is cleared.
      4. Fourth poll: MISSING again → re-prints (dedup was cleared
         on success).
    """
    executor = _StreamScriptedExecutor()
    stdout_path = "/tmp/fake_job_42.log"
    executor.queue(stdout_path, [
        _missing_response(),                                # poll 1
        _missing_response(),                                # poll 2 (deduped)
        _content_response(20, "hello world\nline two\n"),   # poll 3
        _missing_response(),                                # poll 4 (re-prints)
    ])

    class _Harness(App):
        def __init__(self):
            super().__init__()
            self._screen: LogTailScreen | None = None

        async def on_mount(self):
            self._screen = LogTailScreen("42", executor)
            await self.push_screen(self._screen)

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause(0.4)    # let on_mount finish
        screen = app._screen
        assert screen is not None
        # Stop the interval so we drive _poll_stream by hand below.
        if screen._poll_handle is not None:
            screen._poll_handle.stop()
            screen._poll_handle = None

        # Wait for the on_mount-kicked poll to settle, then reset the
        # RichLog content + dedup so this test owns the state.
        await pilot.pause(0.3)

        log_stdout = screen.query_one("#log_stdout", RichLog)
        log_stdout.clear()
        screen._last_error["stdout"] = None
        screen._cursor["stdout"] = 0
        screen._opened["stdout"] = False

        # Drain whatever poll-1/poll-2 responses the on_mount kick used.
        # The remaining queue still has poll 3 (content) and poll 4
        # (missing). To run a clean 4-poll sequence we re-queue.
        executor._per_stream[stdout_path] = [
            _missing_response(),
            _missing_response(),
            _content_response(20, "hello world\nline two\n"),
            _missing_response(),
        ]

        # Poll 1: missing → message prints, dedup set.
        await screen._poll_stream("stdout")
        rendered_1 = _rendered_lines(log_stdout)
        assert rendered_1.count("file does not exist yet") == 1, (
            f"poll 1 should print missing once; got:\n{rendered_1}"
        )

        # Poll 2: still missing → message suppressed by dedup.
        await screen._poll_stream("stdout")
        rendered_2 = _rendered_lines(log_stdout)
        assert rendered_2.count("file does not exist yet") == 1, (
            f"poll 2 missing must be deduped (still count == 1); got:\n"
            f"{rendered_2}"
        )

        # Poll 3: content lands → new lines appear; dedup cleared.
        await screen._poll_stream("stdout")
        rendered_3 = _rendered_lines(log_stdout)
        assert "hello world" in rendered_3, (
            f"content didn't appear after success:\n{rendered_3}"
        )
        assert "line two" in rendered_3, rendered_3
        # Dedup cleared internally on the success path.
        assert screen._last_error["stdout"] is None, (
            "dedup should clear after a successful read"
        )

        # Poll 4: missing again → re-prints (dedup was cleared).
        await screen._poll_stream("stdout")
        rendered_4 = _rendered_lines(log_stdout)
        assert rendered_4.count("file does not exist yet") == 2, (
            f"poll 4 missing should print again (re-fail after success); "
            f"got count {rendered_4.count('file does not exist yet')}; "
            f"log:\n{rendered_4}"
        )

        # Bug 1 contract: at least one tail call went through `bash -c
        # <script>` on the executor.
        bash_calls = [c for c in executor.calls
                      if len(c[0]) >= 2 and c[0][0] == "bash" and c[0][1] == "-c"]
        tail_scripts = [c for c in bash_calls
                        if "__SIZE__" in c[0][2] or "__MISSING__" in c[0][2]]
        assert tail_scripts, (
            "tail polls didn't go through the executor — Bug 1 not fixed. "
            f"Bash calls were:\n"
            + "\n".join(c[0][2][:80] for c in bash_calls)
        )


def _rendered_lines(rich_log: RichLog) -> str:
    """Join RichLog's accumulated Strip lines into a single string for
    substring assertions. RichLog stores rendered text in `lines` as
    `Strip` objects; we flatten via each segment's `.text`."""
    return "\n".join(
        "".join(seg.text for seg in strip)
        for strip in rich_log.lines
    )


async def test_log_tail_local_executor_reads_real_file(tmp_path):
    """Bundle-3 B3.5 local-config sanity: `exec = "local"` → tail goes
    through `LocalExecutor`, which runs the bash script via
    `asyncio.create_subprocess_exec`. End-to-end check on a real
    file in `tmp_path`."""
    from rohanboard.exec import LocalExecutor

    log_path = tmp_path / "fake_job.log"
    log_path.write_text("line A\nline B\nline C\n")

    # Build the same scripted-info-only fake but with a real
    # LocalExecutor for the tail call. We do that by SUBCLASSING the
    # local executor and intercepting only the scontrol probes — the
    # bash tail goes through the real path.
    class _LocalWithFakeInfo(LocalExecutor):
        def __init__(self, real_log_path: str):
            super().__init__()
            self._log_path = real_log_path

        async def run(self, argv, timeout=None):
            argv = list(argv)
            if argv[:3] == ["scontrol", "show", "job"]:
                return (0,
                        f"JobId=99 JobName=tj UserId=hli(1000) "
                        f"JobState=RUNNING Partition=p NodeList=n "
                        f"StdOut={self._log_path} StdErr={self._log_path}",
                        "")
            if argv[:3] == ["scontrol", "write"]:
                return (0, "", "")    # script tab: empty
            # Everything else (bash tail) goes through the REAL
            # LocalExecutor.run — subprocess against our tmp file.
            return await super().run(argv, timeout=timeout)

    executor = _LocalWithFakeInfo(str(log_path))

    class _Harness(App):
        def __init__(self):
            super().__init__()
            self._screen: LogTailScreen | None = None

        async def on_mount(self):
            self._screen = LogTailScreen("99", executor)
            await self.push_screen(self._screen)

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        screen = app._screen
        assert screen is not None
        # Stop the interval and drive one explicit poll so we're not
        # racing the timer.
        if screen._poll_handle is not None:
            screen._poll_handle.stop()
            screen._poll_handle = None
        log_stdout = screen.query_one("#log_stdout", RichLog)
        log_stdout.clear()
        screen._cursor["stdout"] = 0
        screen._opened["stdout"] = False
        screen._last_error["stdout"] = None

        await screen._poll_stream("stdout")
        rendered = _rendered_lines(log_stdout)

        # File content shows up via the real subprocess+bash path.
        assert "line A" in rendered, f"file content missing:\n{rendered}"
        assert "line C" in rendered, rendered
        # And no "file does not exist" — the file exists locally.
        assert "file does not exist" not in rendered, (
            f"file exists but missing-message printed:\n{rendered}"
        )
