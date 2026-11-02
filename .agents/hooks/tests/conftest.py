"""Fixtures that load and run the agent hooks as their clients do."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

HOOKS = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HOOKS / f"{name}.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def load_hook() -> Callable[[str], ModuleType]:
    """Load a hook script as a module by its file stem."""
    return _load


@pytest.fixture
def run_hook() -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run a hook script with a JSON payload on stdin, as Claude Code does."""

    def run(
        name: str, payload: dict, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, str(HOOKS / f"{name}.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            check=False,
        )

    return run
