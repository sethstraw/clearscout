"""The boundaries import-linter cannot express, and checks on its contracts.

Everything about which package may import which other package lives in the
`[tool.importlinter]` contracts and is checked by `lint-imports`. One rule does
not fit that model and stays here: a capability's privates are a name-level
rule. Import-linter graphs modules, so `from tailorcv.application.other import
_thing` is invisible to it, and ruff's SLF001 covers attribute access rather
than an imported name.

The contracts themselves are checked for what `lint-imports` passes silently: a
module a layers contract leaves out, a forbidden entry another entry already
covers, a one-sided exclusion between peers, and a wildcard ignore.
"""

from __future__ import annotations

import ast
import tomllib
from itertools import permutations
from pathlib import Path
from typing import Any

from tests.support.suite import REPO_ROOT

SOURCE = REPO_ROOT / "src" / "tailorcv"
NAME_LIMIT = 75


def _capability_of(module: Path) -> tuple[str, ...]:
    """Return the package whose privates this module is entitled to use.

    A capability is a package directly under `application/` and owns every
    private module inside it. A loose module at the top of `application/` is
    its own capability.
    """
    parts = module.relative_to(SOURCE).parts
    if parts[:1] != ("application",) or len(parts) < 2:
        return parts[:-1]
    return parts[:2] if module.parent.name != "application" else (*parts[:1], parts[1])


def _private_crossings(module: Path) -> list[str]:
    """Return imports of another capability's underscore-prefixed module or name."""
    owner = _capability_of(module)
    crossings: list[str] = []
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        parts = node.module.split(".")
        if parts[:2] != ["tailorcv", "application"]:
            continue
        target = tuple(parts[1:3])
        if target == owner:
            continue
        private = [part for part in parts[2:] if part.startswith("_")]
        private += [alias.name for alias in node.names if alias.name.startswith("_")]
        crossings += [
            f"{module.relative_to(SOURCE)} imports {node.module}.{name}" for name in private
        ]
    return crossings


def test_a_capability_does_not_reach_into_another_capabilitys_privates() -> None:
    """A leading underscore is the only marker of what a capability owns alone.

    Reaching past it couples two capabilities through a name that carries no
    promise, so the next change inside the owner silently breaks the borrower.
    """
    crossings = [
        crossing
        for module in sorted((SOURCE / "application").rglob("*.py"))
        for crossing in _private_crossings(module)
    ]
    assert crossings == []


def _contracts() -> list[dict[str, Any]]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts: list[dict[str, Any]] = config["tool"]["importlinter"]["contracts"]
    return contracts


def _within(module: str, ancestor: str) -> bool:
    return module == ancestor or module.startswith(f"{ancestor}.")


def _child_modules(container: str, source: Path) -> set[str]:
    package = source.joinpath(*container.split(".")[1:])
    return {
        path.stem if path.is_file() else path.name
        for path in package.iterdir()
        if (path.suffix == ".py" and path.stem != "__init__") or (path / "__init__.py").is_file()
    }


def _unlayered_modules(contracts: list[dict[str, Any]], source: Path) -> list[str]:
    """Return container children a layers contract leaves out, which it then never checks."""
    gaps: list[str] = []
    for contract in contracts:
        if contract["type"] != "layers":
            continue
        layered = {name.strip() for layer in contract["layers"] for name in layer.split("|")}
        for container in contract["containers"]:
            gaps += [
                f"{container}.{child}"
                for child in sorted(_child_modules(container, source) ^ layered)
            ]
    return gaps


def _covers(contract: dict[str, Any], source: str, forbidden: str) -> bool:
    return any(_within(source, s) for s in contract["source_modules"]) and any(
        _within(forbidden, f) for f in contract["forbidden_modules"]
    )


def _redundant_forbidden(contracts: list[dict[str, Any]]) -> list[str]:
    """Return forbidden entries that a broader entry, here or in another contract, covers."""
    forbidding = [c for c in contracts if c["type"] == "forbidden"]
    redundant: list[str] = []
    for contract in forbidding:
        entries = contract["forbidden_modules"]
        others = [c for c in forbidding if c is not contract and not c.get("ignore_imports")]
        for entry in entries:
            inside = any(other != entry and _within(entry, other) for other in entries)
            elsewhere = any(
                all(_covers(other, source, entry) for source in contract["source_modules"])
                for other in others
            )
            if inside or elsewhere:
                redundant.append(f"{contract['name']}: {entry}")
    return redundant


def _one_sided_exclusions(contracts: list[dict[str, Any]]) -> list[str]:
    """Return a peer's source module missing from a contract that forbids the peer."""
    forbidding = [c for c in contracts if c["type"] == "forbidden"]
    missing: list[str] = []
    for first, second in permutations(forbidding, 2):
        if not any(
            _covers(first, s, f) for s in first["source_modules"] for f in second["source_modules"]
        ):
            continue
        if not any(
            _covers(second, s, f) for s in second["source_modules"] for f in first["source_modules"]
        ):
            continue
        missing += [
            f"{first['name']}: {peer}"
            for peer in second["source_modules"]
            if not any(_within(peer, f) for f in first["forbidden_modules"])
        ]
    return missing


def _contract_name_problems(contracts: list[dict[str, Any]]) -> list[str]:
    names = [contract["name"] for contract in contracts]
    problems = [name for name in names if len(name) > NAME_LIMIT or not name.isascii()]
    return problems + sorted({name for name in names if names.count(name) > 1})


def _wildcard_ignores(contracts: list[dict[str, Any]]) -> list[str]:
    return [
        ignored
        for contract in contracts
        for ignored in contract.get("ignore_imports", [])
        if "*" in ignored
    ]


def test_import_contracts_cover_their_modules_without_redundant_or_loose_entries() -> None:
    contracts = _contracts()

    assert _unlayered_modules(contracts, SOURCE) == []
    assert _redundant_forbidden(contracts) == []
    assert _one_sided_exclusions(contracts) == []
    assert _contract_name_problems(contracts) == []
    assert _wildcard_ignores(contracts) == []


def test_contract_checks_report_each_kind_of_loose_contract(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    for module in ("__init__.py", "top.py", "bottom.py", "stray.py"):
        (package / module).write_text("", encoding="utf-8")
    contracts: list[dict[str, Any]] = [
        {
            "name": "pkg layers",
            "type": "layers",
            "containers": ["tailorcv.pkg"],
            "layers": ["top", "bottom"],
        },
        {
            "name": "a",
            "type": "forbidden",
            "source_modules": ["x.a"],
            "forbidden_modules": ["x.b", "x.b.inner"],
        },
        {
            "name": "b",
            "type": "forbidden",
            "source_modules": ["x.b", "x.c"],
            "forbidden_modules": ["x.a"],
        },
        {"name": "x", "type": "forbidden", "source_modules": ["x"], "forbidden_modules": ["y"]},
        {"name": "x.a", "type": "forbidden", "source_modules": ["x.a"], "forbidden_modules": ["y"]},
        {
            "name": "a",
            "type": "forbidden",
            "source_modules": ["z"],
            "forbidden_modules": ["w"],
            "ignore_imports": ["z.q -> w.*"],
        },
    ]

    assert _unlayered_modules(contracts, tmp_path) == ["tailorcv.pkg.stray"]
    assert _redundant_forbidden(contracts) == ["a: x.b.inner", "x.a: y"]
    assert _one_sided_exclusions(contracts) == ["a: x.c"]
    assert _contract_name_problems(contracts) == ["a"]
    assert _wildcard_ignores(contracts) == ["z.q -> w.*"]
