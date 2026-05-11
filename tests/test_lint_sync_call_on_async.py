"""Pin the Phase-H regression lint at green for the current codebase.

`scripts/lint_sync_call_on_async.py` AST-walks `rohanboard/` looking for
the bug shape Phase 4d.2-E step H caught: a sync method calling an
`async def` method on `self` without awaiting / scheduling the result.

If a future change re-introduces the pattern (e.g. someone writes
`self.update_snapshot(snap)` inside a sync click handler), this test
fails — which is exactly what we want. Suppress a known/deferred site
on the offending line with `# noqa: lint-async-call`.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "lint_sync_call_on_async.py"


def _load_lint_module():
    spec = importlib.util.spec_from_file_location(
        "lint_sync_call_on_async", _SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lint_sync_call_on_async"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_lint_sync_call_on_async_finds_zero_sites_on_current_codebase():
    """Bundle-1 Sub-fix-5: the lint pins the current codebase at zero
    flagged sites. Two NodesTable sites carry an explicit
    `# noqa: lint-async-call` suppression with a TODO; everything else
    routes through `await` / `run_worker` / `@work` correctly."""
    lint = _load_lint_module()
    target = _REPO_ROOT / "rohanboard"
    flagged = lint.scan_tree(target)
    assert flagged == [], (
        f"lint flagged {len(flagged)} site(s):\n"
        + "\n".join(
            f"  {p}:{ln} self.{m}(...) — {src}"
            for p, ln, m, src in flagged
        )
    )


def test_lint_detects_the_phase_h_bug_shape(tmp_path):
    """Synthetic file with the bug shape MUST be flagged. Otherwise
    the green-on-current-codebase assertion above is a tautology."""
    lint = _load_lint_module()
    f = tmp_path / "buggy.py"
    f.write_text(
        "class W:\n"
        "    async def update_snapshot(self, s): ...\n"
        "    def on_click(self):\n"
        "        self.update_snapshot('snap')\n"
    )
    flagged = lint.scan_file(f)
    assert len(flagged) == 1, f"expected 1 flagged site, got: {flagged}"
    _path, lineno, method, _src = flagged[0]
    assert method == "update_snapshot"
    assert lineno == 4


def test_lint_does_not_flag_correct_patterns(tmp_path):
    """Patterns that DO schedule / capture the coroutine: await,
    `coro =`, return, scheduling helpers. None should be flagged."""
    lint = _load_lint_module()
    f = tmp_path / "ok.py"
    f.write_text(
        "class W:\n"
        "    async def foo(self): ...\n"
        "\n"
        "    async def bar(self):\n"
        "        await self.foo()\n"
        "        coro = self.foo()\n"
        "        return self.foo()\n"
        "\n"
        "    def on_click(self):\n"
        "        self.run_worker(self.foo(), exclusive=True)\n"
    )
    flagged = lint.scan_file(f)
    assert flagged == [], f"unexpected flags: {flagged}"


def test_lint_skips_at_work_decorated_methods(tmp_path):
    """Textual's `@work` decorator wraps an async method so it can be
    called synchronously — the call spawns a Worker. Don't flag those."""
    lint = _load_lint_module()
    f = tmp_path / "decorated.py"
    f.write_text(
        "from textual import work\n"
        "\n"
        "class App:\n"
        "    @work(exclusive=True)\n"
        "    async def _refresh_all(self): ...\n"
        "\n"
        "    def action_refresh(self):\n"
        "        self._refresh_all()    # @work makes this sync-callable\n"
    )
    flagged = lint.scan_file(f)
    assert flagged == [], f"@work-decorated method should not flag: {flagged}"


def test_lint_honors_noqa_suppression(tmp_path):
    """A known-and-deferred bug site can be marked with a trailing
    `# noqa: lint-async-call` to keep the lint green while the fix
    is on the books elsewhere."""
    lint = _load_lint_module()
    f = tmp_path / "suppressed.py"
    f.write_text(
        "class W:\n"
        "    async def update_snapshot(self, s): ...\n"
        "    def on_click(self):\n"
        "        self.update_snapshot('s')  # noqa: lint-async-call\n"
    )
    flagged = lint.scan_file(f)
    assert flagged == [], f"noqa should suppress: {flagged}"
