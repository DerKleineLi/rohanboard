"""Tests for rohanboard.config — focus on the Phase 4c `exec` field +
build_executor picker. Multi-cluster TOML lands in 4e and gets its own file."""
from __future__ import annotations

from pathlib import Path

import pytest

from rohanboard.config import Config, load
from rohanboard.exec import AsyncSSHExecutor, LocalExecutor


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


# ──────────────────────────────────────────────────────────────────────────
# Default exec_spec
# ──────────────────────────────────────────────────────────────────────────

def test_default_exec_is_local(tmp_path):
    """A config with no `exec` line defaults to LocalExecutor — preserves
    the 0612f58 baseline behavior for existing single-cluster configs."""
    cfg = load(_write(tmp_path, ""))
    assert cfg.exec_spec == "local"
    assert isinstance(cfg.build_executor(), LocalExecutor)


def test_explicit_exec_local(tmp_path):
    cfg = load(_write(tmp_path, 'exec = "local"\n'))
    assert cfg.exec_spec == "local"
    assert isinstance(cfg.build_executor(), LocalExecutor)


# ──────────────────────────────────────────────────────────────────────────
# ssh:<host> form
# ──────────────────────────────────────────────────────────────────────────

def test_exec_ssh_rohan(tmp_path):
    cfg = load(_write(tmp_path, 'exec = "ssh:rohan"\n'))
    assert cfg.exec_spec == "ssh:rohan"
    ex = cfg.build_executor()
    assert isinstance(ex, AsyncSSHExecutor)
    assert ex.host == "rohan"
    # No connection opened by build_executor — that's lazy on first run().
    assert ex._conn is None


def test_exec_ssh_arbitrary_host(tmp_path):
    """Hostname can be anything resolvable via ~/.ssh/config; the executor
    just stashes the string."""
    cfg = load(_write(tmp_path, 'exec = "ssh:my-laptop.local"\n'))
    ex = cfg.build_executor()
    assert isinstance(ex, AsyncSSHExecutor)
    assert ex.host == "my-laptop.local"


# ──────────────────────────────────────────────────────────────────────────
# Validation errors
# ──────────────────────────────────────────────────────────────────────────

def test_exec_garbage_raises_at_load(tmp_path):
    """Malformed `exec` fails at load time — not at first run() — so the
    user sees the error immediately on startup, not on the first refresh."""
    with pytest.raises(ValueError, match="unknown exec"):
        load(_write(tmp_path, 'exec = "garbage"\n'))


def test_exec_ssh_empty_host_raises_at_load(tmp_path):
    with pytest.raises(ValueError, match="must include a host"):
        load(_write(tmp_path, 'exec = "ssh:"\n'))


# ──────────────────────────────────────────────────────────────────────────
# build_executor() also validates (defense in depth: someone could
# instantiate Config() in code with a bad exec_spec)
# ──────────────────────────────────────────────────────────────────────────

def test_build_executor_rejects_bad_spec_in_code():
    cfg = Config(exec_spec="not-a-valid-spec")
    with pytest.raises(ValueError, match="unknown exec"):
        cfg.build_executor()


def test_build_executor_rejects_empty_ssh_host_in_code():
    cfg = Config(exec_spec="ssh:")
    with pytest.raises(ValueError, match="must include a host"):
        cfg.build_executor()
