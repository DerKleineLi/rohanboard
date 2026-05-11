"""Bundle-3 B3.2: invoke `scripts/lint_sync_call_on_async.py` as a
regular pytest case so any new Phase-H bug shape introduced in
`rohanboard/` fails `uv run pytest` directly — not just a separate
manual lint run.

The lint script's OWN behavior is pinned by
`tests/test_lint_sync_call_on_async.py` (synthetic bug + suppression
+ @work-skip cases). THIS test runs the lint against the actual
codebase via subprocess and asserts exit 0.

If you legitimately need to introduce a sync-call-on-async site
(deferred fix, not yet refactored), suppress with a trailing
`# noqa: lint-async-call` on the offending line — matches the
shape the lint already understands.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "lint_sync_call_on_async.py"


def test_lint_sync_call_on_async_codebase_clean():
    """`uv run python scripts/lint_sync_call_on_async.py` MUST exit 0
    on the current `rohanboard/` tree. Any new sync call on an
    `async def` method that isn't @work-decorated, awaited, captured,
    passed to a scheduler, or `# noqa`-suppressed will fail this test
    with the offending file + line in the diagnostic output."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # Stitch stdout + stderr into the failure message so the user
    # sees the lint diagnostic without re-running the script.
    diagnostic = (
        f"return code: {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"lint_sync_call_on_async found a new Phase-H bug shape — see "
        f"diagnostic for file:line, then either fix it or suppress "
        f"with `# noqa: lint-async-call`.\n\n{diagnostic}"
    )
