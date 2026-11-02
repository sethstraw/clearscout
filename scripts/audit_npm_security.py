"""Fail on any npm production advisory.

Development dependencies fail the audit only on a high or critical advisory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

BLOCKING_DEVELOPMENT_SEVERITIES = {"high", "critical"}


def main() -> int:
    """Run npm's audit over production, then over the whole tree."""
    npm = shutil.which("npm")
    if npm is None:
        print("BLOCKER: npm is unavailable")
        return 2
    production = _audited(npm, production_only=True)
    if isinstance(production, int):
        return production
    production_status = _review_vulnerabilities(production)
    if production_status != 0:
        return production_status
    whole_tree = _audited(npm, production_only=False)
    if isinstance(whole_tree, int):
        return whole_tree
    return _review_development(whole_tree)


def _audited(npm: str, *, production_only: bool) -> dict[str, Any] | int:
    result = _run_npm_audit(npm, production_only=production_only)
    report = _audit_report(result)
    if report is None:
        return 2
    response_status = _audit_response_status(result, report)
    if response_status is not None:
        return response_status
    vulnerabilities: dict[str, Any] = report["vulnerabilities"]
    return vulnerabilities


def _run_npm_audit(npm: str, *, production_only: bool) -> subprocess.CompletedProcess[str]:
    scope = ["--omit=dev"] if production_only else []
    return subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [npm, "audit", *scope, "--json"],
        cwd="web",
        capture_output=True,
        text=True,
        check=False,
    )


def _audit_report(result: subprocess.CompletedProcess[str]) -> dict[str, Any] | None:
    try:
        report: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("BLOCKER: npm audit did not return JSON")
        print(result.stderr.strip())
        return None
    return report


def _audit_response_status(
    result: subprocess.CompletedProcess[str], report: dict[str, Any]
) -> int | None:
    if result.returncode not in (0, 1):
        print(f"BLOCKER: npm audit failed with exit code {result.returncode}")
        if result.stderr.strip():
            print(result.stderr.strip())
        return 2
    if "vulnerabilities" not in report:
        print("BLOCKER: npm audit response has no vulnerability map")
        return 2
    vulnerabilities = report["vulnerabilities"]
    if not isinstance(vulnerabilities, dict):
        print("BLOCKER: npm audit response has no vulnerability map")
        return 2
    if result.returncode != 0 and not vulnerabilities:
        print("BLOCKER: npm audit failed without reporting vulnerabilities")
        return 2
    return None


def _review_vulnerabilities(vulnerabilities: dict[str, Any]) -> int:
    if vulnerabilities:
        print("BLOCKER: production advisories: " + ", ".join(sorted(vulnerabilities)))
        return 1
    print("npm production audit passed: no advisory.")
    return 0


def _review_development(vulnerabilities: dict[str, Any]) -> int:
    blocking = sorted(
        name
        for name, record in vulnerabilities.items()
        if isinstance(record, dict) and record.get("severity") in BLOCKING_DEVELOPMENT_SEVERITIES
    )
    if blocking:
        print("BLOCKER: high or critical development advisories: " + ", ".join(blocking))
        return 1
    print("npm development audit passed: no high or critical advisory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
