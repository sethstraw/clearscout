"""Enforce the no-bill boundary around tests, evaluations, and CI."""

from __future__ import annotations

import socket

import pytest
from evals.runner import EvalError, forbid_network

from clearscout.infrastructure.providers.credentials import ENVIRONMENT_VARIABLES
from tests.support.suite import REPO_ROOT

ROOT = REPO_ROOT


def test_the_shared_gate_and_ci_have_no_live_provider_entrypoint() -> None:
    gate = (ROOT / "scripts" / "gate.sh").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ordinary_automation = f"{gate}\n{ci}"

    assert 'step "evals" uv_locked python -m evals' in gate
    assert "bash scripts/gate.sh" in ci
    assert "record_cassette.py" not in ordinary_automation
    assert "LIVE_GATE" not in ordinary_automation
    for variable in ENVIRONMENT_VARIABLES.values():
        assert variable not in ordinary_automation


def test_evaluations_seal_the_network_before_running_scripted_cases() -> None:
    """The observable evaluation boundary refuses a real socket connection."""
    original = (socket.socket, socket.create_connection, socket.getaddrinfo)
    try:
        forbid_network()
        with pytest.raises(EvalError, match="tried to open a network connection"):
            socket.create_connection(("example.com", 443), timeout=0.01)
    finally:
        socket.socket = original[0]  # type: ignore[misc]
        socket.create_connection = original[1]
        socket.getaddrinfo = original[2]
