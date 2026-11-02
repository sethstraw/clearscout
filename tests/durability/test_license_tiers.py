"""The license gate asks two different questions, and has to keep asking both."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

import pytest

from tests.support.suite import REPO_ROOT


def _audit_licenses() -> ModuleType:
    path = REPO_ROOT / "scripts" / "audit_licenses.py"
    spec = importlib.util.spec_from_file_location("audit_licenses", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The dataclass decorator resolves the module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AUDIT = _audit_licenses()
# One `uv export` for the whole module; it reads the lockfile and nothing else.
DISTRIBUTED = AUDIT._distributed_python_names()


def _dependency(license_expression: str, *, distributed: bool):
    return AUDIT.Dependency(
        ecosystem="npm",
        name="example",
        version="1.0.0",
        license=license_expression,
        url="https://example.invalid",
        usage="bundled web runtime" if distributed else "web build/test tooling",
        distributed=distributed,
    )


@pytest.mark.parametrize("expression", ["MPL-2.0", "GPL-3.0-only", "CC-BY-4.0", "EUPL-1.2"])
def test_copyleft_passes_for_a_package_no_distribution_contains(expression: str) -> None:
    assert AUDIT._classification(_dependency(expression, distributed=False)) == (
        "development only, not distributed"
    )


@pytest.mark.parametrize("expression", ["MPL-2.0", "GPL-3.0-only", "CC-BY-4.0", "EUPL-1.2"])
def test_the_same_expression_still_blocks_a_distributed_package(expression: str) -> None:
    assert AUDIT._classification(_dependency(expression, distributed=True)).startswith("BLOCKER:")


@pytest.mark.parametrize("expression", ["AGPL-3.0", "SSPL-1.0", "UNKNOWN", "UNLICENSED"])
def test_network_copyleft_and_absent_grants_block_in_both_tiers(expression: str) -> None:
    assert AUDIT._classification(_dependency(expression, distributed=False)).startswith("BLOCKER:")
    assert AUDIT._classification(_dependency(expression, distributed=True)).startswith("BLOCKER:")


def test_a_reviewed_exception_still_clears_the_distributed_tier() -> None:
    version = next(v for e, n, v in AUDIT.REVIEWED_EXCEPTIONS if (e, n) == ("python", "certifi"))
    certifi = AUDIT.Dependency(
        ecosystem="python",
        name="certifi",
        version=version,
        license="Mozilla Public License 2.0 (MPL 2.0)",
        url="https://example.invalid",
        usage="distributed Python runtime",
        distributed=True,
    )
    assert AUDIT._classification(certifi) == "reviewed exception"


def test_no_reviewed_exception_names_a_package_the_development_tier_covers() -> None:
    """An exception for a development package is unreachable, so it is a mistake."""
    unreachable = [
        (ecosystem, name, version)
        for ecosystem, name, version in AUDIT.REVIEWED_EXCEPTIONS
        if ecosystem == "python" and not AUDIT._is_distributed(name, DISTRIBUTED)
    ]
    assert unreachable == []


def test_distributed_names_exclude_the_development_group() -> None:
    assert "anthropic" in DISTRIBUTED
    assert "keyring" in DISTRIBUTED
    assert "ruff" not in DISTRIBUTED
    assert "pytest" not in DISTRIBUTED


@pytest.mark.parametrize(
    ("name", "expected"),
    [("pytest_cov", False), ("Anthropic", True), ("typing_extensions", True)],
)
def test_names_are_compared_in_normalized_form(name: str, expected: bool) -> None:
    assert AUDIT._is_distributed(name, DISTRIBUTED) is expected


def test_a_missing_project_url_does_not_become_a_broken_markdown_link(tmp_path) -> None:
    dependency = _dependency("MIT", distributed=False)
    dependency = AUDIT.Dependency(**{**dependency.__dict__, "url": "UNKNOWN"})
    notices = tmp_path / "THIRD_PARTY_NOTICES.md"

    AUDIT._write_notices(notices, [(dependency, "development only, not distributed")])

    content = notices.read_text(encoding="utf-8")
    assert "| Not provided |" in content
    assert "[upstream](UNKNOWN)" not in content
