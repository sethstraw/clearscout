"""Audit and inventory every installed Python and locked web dependency.

Two policies, because two questions. A distributed dependency reaches a user
through the wheel, the sdist, or the built SPA, so it has to be shippable
inside Apache-2.0: only an exact allowlist of permissive expressions passes
automatically, and strong copyleft, proprietary, unknown, ambiguous, and
weak-copyleft expressions fail unless the exact package and version has a
documented review exception. A development-only dependency reaches nobody, so
copyleft is permitted; it fails only on a network-copyleft expression or on
metadata declaring no usable grant at all.

This split is about distribution and nothing else. The security audits still
cover both tiers, because a compromised linter runs on a maintainer's machine
whether or not it ships.

Run with the locked development dependency installed::

    uv run python scripts/audit_licenses.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

PERMISSIVE_EXPRESSIONS = {
    "(MIT OR CC0-1.0)",
    "Apache Software License",
    "Apache Software License; BSD License",
    "Apache Software License; MIT License",
    "Apache-2.0",
    "Apache-2.0 AND CNRI-Python",
    "Apache-2.0 OR BSD-2-Clause",
    "Apache-2.0 OR BSD-3-Clause",
    "BSD License",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BlueOak-1.0.0",
    "CC0-1.0",
    "ISC",
    "ISC License (ISCL)",
    "MIT",
    "MIT AND ISC",
    "MIT AND PSF-2.0",
    "MIT License",
    "MIT-0",
    "PSF-2.0",
    "Python-2.0",
    "Python Software Foundation License",
}
STRONG_COPYLEFT = ("AGPL", "GPL", "SSPL", "OSL", "EUPL", "CC-BY-SA")
PROHIBITED = ("PROPRIETARY", "UNKNOWN", "OTHER/PROPRIETARY", "UNLICENSED")
WEAK_OR_AMBIGUOUS = ("MPL", "LGPL", "dependency licenses")
# Network copyleft is prohibited for every dependency tier.
NETWORK_COPYLEFT = ("AGPL", "SSPL")
IGNORE_PYTHON = {"clearscout"}

# Inventory platform-specific locked packages even when the current interpreter omits them.
PLATFORM_CONDITIONAL_PYTHON = {
    "secretstorage": (
        "SecretStorage",
        "3.5.0",
        "BSD-3-Clause",
        "https://github.com/mitya57/secretstorage",
    ),
    "cffi": (
        "cffi",
        "2.1.1",
        "MIT-0",
        "https://cffi.readthedocs.io/en/latest/whatsnew.html",
    ),
    "colorama": (
        "colorama",
        "0.4.6",
        "BSD License",
        "https://github.com/tartley/colorama",
    ),
    "cryptography": (
        "cryptography",
        "50.0.2",
        "Apache-2.0 OR BSD-3-Clause",
        "https://github.com/pyca/cryptography",
    ),
    "jeepney": (
        "jeepney",
        "0.9.0",
        "MIT",
        "https://gitlab.com/takluyver/jeepney",
    ),
    "pycparser": (
        "pycparser",
        "3.0",
        "BSD-3-Clause",
        "https://github.com/eliben/pycparser",
    ),
    "pywin32-ctypes": (
        "pywin32-ctypes",
        "0.2.3",
        "BSD-3-Clause",
        "https://github.com/enthought/pywin32-ctypes",
    ),
    "tzdata": (
        "tzdata",
        "2026.5",
        "Apache-2.0",
        "https://github.com/python/tzdata",
    ),
}

# Each exception is bound to an exact version, so a bump needs a fresh review.
REVIEWED_EXCEPTIONS = {
    ("python", "certifi", "2026.10.23"),
    ("python", "pypdfium2", "5.14.0"),
    ("python", "tld", "0.13.2"),
}


@dataclass(frozen=True)
class Dependency:
    """One exact dependency record used by policy and notices."""

    ecosystem: str
    name: str
    version: str
    license: str
    url: str
    usage: str
    distributed: bool
    package_root: Path | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the exact identity used for manual review exceptions."""
        return (self.ecosystem, self.name.lower(), self.version)


def main() -> int:
    """Audit both ecosystems and optionally write the exact notices inventory."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-notices", type=Path)
    parser.add_argument("--write-license-texts", type=Path)
    arguments = parser.parse_args()
    try:
        distributed = _distributed_python_names()
        python_dependencies = _python_dependencies(distributed)
        dependencies = [
            *python_dependencies,
            *_platform_python_dependencies(
                Path("uv.lock"),
                Path("scripts/platform-conditional-requirements.txt"),
                python_dependencies,
                distributed,
            ),
            *_npm_dependencies(Path("web/package-lock.json")),
        ]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Dependency inventory failed: {exc}")
        return 2

    outcomes = [(item, _classification(item)) for item in dependencies]
    shipped = sum(1 for item in dependencies if item.distributed)
    print(f"{len(dependencies)} exact dependency records")
    print(f"  {shipped:3}  distributed")
    print(f"  {len(dependencies) - shipped:3}  development only")
    for category, count in Counter(result for _, result in outcomes).most_common():
        print(f"  {count:3}  {category}")

    reviewed = [item for item, result in outcomes if result == "reviewed exception"]
    blockers = [(item, result) for item, result in outcomes if result.startswith("BLOCKER:")]
    if reviewed:
        print("\nVersion-bound manual reviews:")
        for item in reviewed:
            print(f"  {item.ecosystem:6} {item.name} {item.version} - {item.license}")
    if arguments.write_notices is not None:
        _write_notices(arguments.write_notices, outcomes)
        print(f"\nWrote {arguments.write_notices}")
    if arguments.write_license_texts is not None:
        try:
            _write_bundled_license_texts(arguments.write_license_texts, outcomes)
        except OSError as exc:
            print(f"\nBLOCKER: {exc}")
            return 1
        print(f"Wrote {arguments.write_license_texts}")
    if blockers:
        print("\nRelease blockers:")
        for item, result in blockers:
            print(f"  {item.ecosystem:6} {item.name} {item.version} - {item.license}")
            print(f"         {result.removeprefix('BLOCKER: ')}")
        return 1
    print("\nLicense policy passed. No unreviewed weak, strong, ambiguous, or unknown license.")
    return 0


def _distributed_python_names() -> frozenset[str]:
    """Return the normalized names uv resolves without the development group.

    The export carries environment markers, so a package selected only on Linux
    or Windows is still named here and is still distributed.
    """
    command = [
        "uv",
        "export",
        "--no-dev",
        "--frozen",
        "--no-emit-project",
        "--no-hashes",
        "--format",
        "requirements-txt",
    ]
    result = subprocess.run(  # noqa: S603 - fixed executable and argument list
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError("uv export failed; the distributed set is unknown. " + result.stderr.strip())
    names = {
        requirement.partition("==")[0].strip().lower().replace("_", "-")
        for line in result.stdout.splitlines()
        if line and not line.startswith(("#", " ", "\t", "-"))
        for requirement in [line.partition(";")[0]]
        if "==" in requirement
    }
    if not names:
        raise ValueError("uv export produced no distributed package names")
    return frozenset(names)


def _python_dependencies(distributed: frozenset[str]) -> list[Dependency]:
    command = [sys.executable, "-m", "piplicenses", "--format=json", "--with-urls"]
    result = subprocess.run(  # noqa: S603 - fixed executable and argument list
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(
            "pip-licenses is unavailable; install the locked development dependencies. "
            + result.stderr.strip()
        )
    packages: list[dict[str, Any]] = json.loads(result.stdout)
    dependencies: list[Dependency] = []
    for package in packages:
        key = str(package["Name"]).lower()
        if key in IGNORE_PYTHON:
            continue
        if key in PLATFORM_CONDITIONAL_PYTHON:
            dependency = _platform_dependency(key, distributed)
            if str(package["Version"]) != dependency.version:
                raise ValueError(
                    f"installed {dependency.name} version does not match the reviewed lock metadata"
                )
            dependencies.append(dependency)
            continue
        dependencies.append(
            Dependency(
                ecosystem="python",
                name=str(package["Name"]),
                version=str(package["Version"]),
                license=str(package["License"]),
                url=str(package.get("URL") or "https://pypi.org/project/" + package["Name"]),
                usage=_python_usage(key, distributed),
                distributed=_is_distributed(key, distributed),
            )
        )
    return sorted(dependencies, key=lambda item: (item.name.lower(), item.version))


def _is_distributed(name: str, distributed: frozenset[str]) -> bool:
    return name.lower().replace("_", "-") in distributed


def _python_usage(name: str, distributed: frozenset[str]) -> str:
    if _is_distributed(name, distributed):
        return "distributed Python runtime"
    return "Python development tooling"


def _platform_python_dependencies(
    lockfile: Path,
    requirements_file: Path,
    installed: list[Dependency],
    distributed: frozenset[str],
) -> list[Dependency]:
    """Return locked platform-only packages absent from this interpreter."""
    lock = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ValueError("uv.lock has no package array")
    locked = {
        str(package.get("name", "")).lower(): str(package.get("version", ""))
        for package in packages
        if isinstance(package, dict)
    }
    audited_requirements = {
        name.lower(): version
        for line in requirements_file.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
        for name, separator, version_and_hash in [line.partition("==")]
        if separator
        for version in [version_and_hash.split()[0]]
    }
    expected_requirements = {
        name.lower(): version for name, version, _, _ in PLATFORM_CONDITIONAL_PYTHON.values()
    }
    if audited_requirements != expected_requirements:
        raise ValueError("platform security requirements and license inventory disagree")
    installed_names = {item.name.lower() for item in installed}
    supplements: list[Dependency] = []
    for key, (_, expected_version, _, _) in PLATFORM_CONDITIONAL_PYTHON.items():
        if locked.get(key) != expected_version:
            raise ValueError(f"platform-conditional lock metadata needs review for {key}")
        if key not in installed_names:
            supplements.append(_platform_dependency(key, distributed))
    return sorted(supplements, key=lambda item: item.name.lower())


def _platform_dependency(key: str, distributed: frozenset[str]) -> Dependency:
    name, version, license_name, url = PLATFORM_CONDITIONAL_PYTHON[key]
    shipped = _is_distributed(key, distributed)
    return Dependency(
        ecosystem="python",
        name=name,
        version=version,
        license=license_name,
        url=url,
        usage="platform-conditional " + ("runtime" if shipped else "development tooling"),
        distributed=shipped,
    )


def _npm_dependencies(lockfile: Path) -> list[Dependency]:
    lock = json.loads(lockfile.read_text(encoding="utf-8"))
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise ValueError("web/package-lock.json has no packages object")
    dependencies: dict[tuple[str, str, str, str], Dependency] = {}
    for location, package in packages.items():
        if not location or not isinstance(package, dict):
            continue
        name = str(location).rsplit("node_modules/", 1)[-1]
        version = str(package.get("version") or "")
        license_name = str(package.get("license") or "UNKNOWN")
        # npm marks a package dev only when every path to it starts in
        # devDependencies, so anything reachable from production is not marked.
        shipped = not package.get("dev")
        usage = "bundled web runtime" if shipped else "web build/test tooling"
        key = (name, version, license_name, usage)
        dependencies[key] = Dependency(
            ecosystem="npm",
            name=name,
            version=version,
            license=license_name,
            url=f"https://www.npmjs.com/package/{quote(name, safe='@/')}/v/{quote(version)}",
            usage=usage,
            distributed=shipped,
            package_root=Path("web") / str(location),
        )
    return sorted(
        dependencies.values(),
        key=lambda item: (item.name.lower(), item.version, item.usage),
    )


def _classification(item: Dependency) -> str:
    expression = item.license.strip()
    if item.key in REVIEWED_EXCEPTIONS:
        return "reviewed exception"
    if not item.distributed:
        return _development_classification(expression)
    upper = expression.upper()
    if any(term in upper for term in PROHIBITED):
        return "BLOCKER: proprietary, unknown, or unlicensed"
    if any(term in upper for term in STRONG_COPYLEFT):
        return "BLOCKER: strong copyleft or an unresolved strong-copyleft alternative"
    if any(term.lower() in expression.lower() for term in WEAK_OR_AMBIGUOUS):
        return "BLOCKER: weak copyleft or ambiguous expression requires manual review"
    if expression not in PERMISSIVE_EXPRESSIONS:
        return "BLOCKER: license expression is not on the exact permissive allowlist"
    return "permissive"


def _development_classification(expression: str) -> str:
    """Classify a package that no distribution of this project contains."""
    upper = expression.upper()
    if any(term in upper for term in PROHIBITED):
        return "BLOCKER: development dependency declares no usable grant"
    if any(term in upper for term in NETWORK_COPYLEFT):
        return "BLOCKER: development dependency is network copyleft"
    return "development only, not distributed"


def _write_notices(path: Path, outcomes: list[tuple[Dependency, str]]) -> None:
    rows = [
        "# Third-party notices",
        "",
        "Generated from the installed Python dependency tree and `web/package-lock.json`.",
        "Versions are exact. A generated inventory is not legal advice; see",
        "`docs/licensing-exceptions.md` for version-bound manual reviews.",
        "",
        "Rows marked `development only, not distributed` are build, lint, type and",
        "test tooling. No wheel, sdist or SPA bundle contains them, so they are held",
        "to a reduced licence policy and are listed here as a supply-chain record",
        "rather than as a notice obligation.",
        "",
        "| Ecosystem | Package | Version | Usage | License | Review category | Project |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item, category in sorted(
        outcomes,
        key=lambda pair: (pair[0].ecosystem, pair[0].name.lower(), pair[0].version, pair[0].usage),
    ):
        values = (
            item.ecosystem,
            item.name,
            item.version,
            item.usage,
            item.license,
            category,
        )
        escaped = [value.replace("|", "\\|") for value in values]
        project = "Not provided" if item.url == "UNKNOWN" else f"[upstream]({item.url})"
        rows.append("| " + " | ".join((*escaped, project)) + " |")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_bundled_license_texts(path: Path, outcomes: list[tuple[Dependency, str]]) -> None:
    sections = [
        "CLEARSCOUT BUNDLED WEB DEPENDENCY LICENSES",
        "",
        "Generated from exact production packages in web/package-lock.json.",
        "The package names and license texts below remain the property of their owners.",
    ]
    seen: set[tuple[str, str]] = set()
    for item, _ in sorted(outcomes, key=lambda pair: (pair[0].name.lower(), pair[0].version)):
        if item.ecosystem != "npm" or not item.distributed:
            continue
        identity = (item.name, item.version)
        if identity in seen:
            continue
        seen.add(identity)
        if item.package_root is None or not item.package_root.is_dir():
            raise OSError(f"installed package directory is missing for {item.name} {item.version}")
        candidates = sorted(
            candidate
            for candidate in item.package_root.iterdir()
            if candidate.is_file()
            and candidate.name.lower().startswith(("license", "licence", "copying", "notice"))
        )
        # Record the license grant declared in a package manifest.
        declared_only = not candidates
        sections.extend(
            [
                "",
                "=" * 78,
                # Regenerate THIRD_PARTY_LICENSES.txt when the record header changes.
                f"{item.name} {item.version} - {item.license}",
                item.url,
            ]
        )
        if declared_only:
            sections.extend(
                [
                    "",
                    "--- no notice file shipped ---",
                    "The published package contains no LICENSE, COPYING or NOTICE",
                    f"file. Its package manifest declares {item.license}.",
                ]
            )
        for candidate in candidates:
            if candidate.stat().st_size > 2 * 1024 * 1024:
                raise OSError(f"license text is unexpectedly large: {candidate}")
            sections.extend(
                [
                    "",
                    f"--- {candidate.name} ---",
                    "\n".join(
                        line.rstrip()
                        for line in candidate.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()
                    ),
                ]
            )
    path.write_text("\n".join(sections) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
