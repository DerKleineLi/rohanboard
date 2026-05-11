#!/usr/bin/env python3
"""AST lint for the Phase-H anti-pattern: sync call on an `async def` method.

Phase 4d.2-E step H caught three sites where a sync click handler called
`self.update_snapshot(...)` directly — `update_snapshot` is `async def`,
so Python created the coroutine, the call returned it, the handler
discarded the return value, and the coroutine was never awaited. The
broadcast silently never ran; the user saw a 10–13 s outlier where the
table stayed stale until the next 15 s tick landed.

This script walks every `rohanboard/` Python file, builds an AST, and
for each class:

  1. Collects the names of `async def` methods on that class — but
     EXCLUDES those decorated with `@work` (Textual's decorator wraps
     the coroutine and makes the call site sync).
  2. Walks every `Call` whose function is `self.<name>` for one of those
     names.
  3. Flags it ONLY when the call's result is discarded (parent is an
     `Expr` statement). Patterns like `await self.foo()`, `coro =
     self.foo()`, `self.run_worker(self.foo(), ...)`, and `return
     self.foo()` correctly capture or schedule the coroutine and are
     NOT flagged.

Suppress an individual line with a trailing `# noqa: lint-async-call`
comment. Use this for known-bug sites that are scheduled for a future
fix, so the lint stays green for current code while the issue is on
the books.

Exit code:
  * 0 → no flagged sites.
  * 1 → at least one flagged site.
  * 2 → input path missing.

Run manually:
    uv run python scripts/lint_sync_call_on_async.py
    uv run python scripts/lint_sync_call_on_async.py path/to/dir
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def _decorator_is_work(dec: ast.expr) -> bool:
    """True if the decorator is `@work` or `@work(...)` — Textual's
    work decorator wraps an async method so callers can invoke it
    synchronously (it spawns a Worker and returns immediately)."""
    if isinstance(dec, ast.Name):
        return dec.id == "work"
    if isinstance(dec, ast.Call):
        f = dec.func
        if isinstance(f, ast.Name):
            return f.id == "work"
        if isinstance(f, ast.Attribute):
            return f.attr == "work"
    if isinstance(dec, ast.Attribute):
        return dec.attr == "work"
    return False


def _async_methods_on_class(cls: ast.ClassDef) -> set[str]:
    """Names of `async def` methods on this class, EXCLUDING those
    decorated with `@work` (those are sync-callable)."""
    names: set[str] = set()
    for item in cls.body:
        if not isinstance(item, ast.AsyncFunctionDef):
            continue
        if any(_decorator_is_work(d) for d in item.decorator_list):
            continue
        names.add(item.name)
    return names


def _is_self_async_call(node: ast.AST, names: set[str]) -> str | None:
    """Return the method name if `node` is `self.<name>(...)` for some
    `<name>` in `names`. Otherwise None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if not isinstance(func.value, ast.Name) or func.value.id != "self":
        return None
    return func.attr if func.attr in names else None


def _result_is_discarded(call_node: ast.Call, parent: ast.AST | None) -> bool:
    """True if `call_node`'s value goes nowhere — its parent is an
    `Expr` statement. This is the Phase-H bug shape.

    Patterns that are NOT discarded (returned False):
      * `await self.foo()` (parent is Await, not Expr)
      * `coro = self.foo()` (parent is Assign/AnnAssign, value used)
      * `return self.foo()` (parent is Return)
      * `self.run_worker(self.foo(), ...)` (parent is Call)
      * `[self.foo()]` (parent is List/Tuple/etc.)
    """
    return isinstance(parent, ast.Expr) and parent.value is call_node


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def scan_file(path: Path) -> list[tuple[Path, int, str, str]]:
    """Return a list of (path, lineno, method_name, source_line) tuples
    for each flagged site in `path`."""
    try:
        source = path.read_text()
    except (UnicodeDecodeError, OSError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    parent_map = _build_parent_map(tree)
    source_lines = source.splitlines()
    flagged: list[tuple[Path, int, str, str]] = []

    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        async_methods = _async_methods_on_class(cls)
        if not async_methods:
            continue
        for sub in ast.walk(cls):
            method = _is_self_async_call(sub, async_methods)
            if method is None:
                continue
            assert isinstance(sub, ast.Call)
            parent = parent_map.get(id(sub))
            if not _result_is_discarded(sub, parent):
                continue
            line = (
                source_lines[sub.lineno - 1]
                if 0 < sub.lineno <= len(source_lines)
                else ""
            )
            if "noqa: lint-async-call" in line:
                continue
            flagged.append((path, sub.lineno, method, line.strip()))
    return flagged


def scan_tree(root: Path) -> list[tuple[Path, int, str, str]]:
    all_flagged: list[tuple[Path, int, str, str]] = []
    for py in sorted(root.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        all_flagged.extend(scan_file(py))
    return all_flagged


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path("rohanboard")
    if not root.exists():
        print(f"lint: target {root} does not exist", file=sys.stderr)
        return 2

    all_flagged = scan_tree(root)

    if not all_flagged:
        print(f"lint: scanned {root} — 0 flagged sites.")
        return 0

    for path, line, method, src in all_flagged:
        print(
            f"{path}:{line}: suspect sync call on async method "
            f"`self.{method}(...)`: {src}",
            file=sys.stderr,
        )
    print(
        f"\nlint: {len(all_flagged)} flagged site(s).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
