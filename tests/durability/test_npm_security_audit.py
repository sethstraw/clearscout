from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import audit_npm_security


def _result(
    code: int, payload: dict[str, Any], stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["npm", "audit", "--omit=dev", "--json"],
        code,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


def _run_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[str],
    full: subprocess.CompletedProcess[str] | None = None,
) -> int:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("scripts.audit_npm_security.shutil.which", lambda name: "/usr/bin/npm")
    whole_tree = full if full is not None else _result(0, {"vulnerabilities": {}})

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return result if "--omit=dev" in command else whole_tree

    monkeypatch.setattr("scripts.audit_npm_security.subprocess.run", run)

    return audit_npm_security.main()


def test_npm_security_audit_accepts_a_clean_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _run_audit(tmp_path, monkeypatch, _result(0, {"vulnerabilities": {}})) == 0


def test_npm_security_audit_fails_closed_on_a_json_error_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _run_audit(tmp_path, monkeypatch, _result(1, {"error": {"summary": "registry down"}})) == 2
    )


def test_npm_security_audit_fails_closed_on_an_audit_command_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _run_audit(
            tmp_path,
            monkeypatch,
            _result(2, {"vulnerabilities": {}}, "registry unavailable"),
        )
        == 2
    )


def test_npm_security_audit_rejects_unreviewed_production_advisories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {"vulnerabilities": {"vite": {"via": [{"url": "https://example.test/advisory"}]}}}

    assert _run_audit(tmp_path, monkeypatch, _result(1, report)) == 1


@pytest.mark.parametrize(("severity", "code"), [("high", 1), ("critical", 1), ("moderate", 0)])
def test_npm_security_audit_fails_on_a_high_development_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    severity: str,
    code: int,
) -> None:
    full = {"vulnerabilities": {"undici": {"severity": severity, "via": []}}}

    assert (
        _run_audit(tmp_path, monkeypatch, _result(0, {"vulnerabilities": {}}), _result(1, full))
        == code
    )
