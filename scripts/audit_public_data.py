"""Fail on a machine path, private email, live secret, or real employer in a public file."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from clearscout.infrastructure.web_fetch.boards import BOARD_PATTERNS

MACHINE_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
SECRET = re.compile(r"\b(?:sk|sk-ant|ghp|github_pat)[_-][A-Za-z0-9_-]{12,}\b")
SAFE_EMAIL_DOMAINS = {"example.com", "example.org", "example.test"}
SAFE_EMAIL_ADDRESSES = {"sethstraw@users.noreply.github.com"}
SYNTHETIC_SECRET_WORDS = {
    "abcdefgh",
    "do-not",
    "secret",
    "should-never",
    "someone-else",
    "test",
}
TESTS = "tests/"
EVALS = "evals/"
WEB_SOURCE = "web/src/"

# Tests name an employer only from this list. A board platform name is not an
# employer name and is never matched here.
SYNTHETIC_EMPLOYERS = (
    "Acme",
    "Alpha",
    "Aster Systems",
    "Beta",
    "Example",
    "Gamma",
    "Globex",
    "Initech",
    "Initrode",
    "Northwind",
    "Synthetic",
)

# An eval case needs one employer distinguishable from the other cases, so that
# corpus draws on a wider set of fictional companies than the fixtures do. It is
# closed in the same way: a name outside it fails the scan.
EVAL_EMPLOYERS = (
    "Adatum",
    "Alpine Ridge",
    "Aster Systems",
    "Blue Yonder",
    "Coho",
    "Contoso",
    "Fabrikam",
    "Fourth Coffee",
    "Kestrel Meadow",
    "Litware",
    "Lucerne",
    "Northbridge",
    "Northwind",
    "Proseware",
    "Ridgeline",
    "Trey",
    "Volt Harbour",
    "Wingtip",
    "Woodgrove",
)

# Each scanned area and the employer names its files may use.
EMPLOYER_SCOPES = (
    (TESTS, SYNTHETIC_EMPLOYERS),
    (EVALS, EVAL_EMPLOYERS),
    (WEB_SOURCE, (*SYNTHETIC_EMPLOYERS, *EVAL_EMPLOYERS)),
)

# An employer field carries the name as displayed; a lowercase value is a
# different closed vocabulary.
EMPLOYER_FIELD = re.compile(
    r"\b(?:[Cc]ompany|[Ee]mployer|hiring(?:_organization|Organization))"
    r"\"?\s*[:=]\s*\"([A-Z][^\"]*)\""
)

# `SourceListing` states the employer as its first field.
EMPLOYER_ARGUMENT = re.compile(r"\bSourceListing\(\s*\"([^\"]+)\"")

# Board API request paths name a tenant that `BOARD_PATTERNS` does not reach.
BOARD_API_TENANT = re.compile(
    r"(?:boards-api\.greenhouse\.io/v1/boards"
    r"|api\.lever\.co/v0/postings"
    r"|api\.ashbyhq\.com/posting-api/job-board)/(?P<org>[A-Za-z0-9._-]+)"
)

SKIP = {
    "scripts/audit_public_data.py",
    "THIRD_PARTY_NOTICES.md",
    # Generated license text may contain its upstream holder's contact details.
    "THIRD_PARTY_LICENSES.txt",
}

# Bundles skip email matching; path and secret checks still apply.
GENERATED_BUNDLES = "web/dist/assets/"


def repository_files() -> list[str]:
    """Return reviewable Git files plus the ignored production bundle."""
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git is unavailable")
    result = subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
    )
    names = {
        raw_name.decode("utf-8", errors="strict")
        for raw_name in result.stdout.split(b"\0")
        if raw_name
    }
    bundle = Path("web/dist")
    if bundle.is_dir():
        names.update(
            path.as_posix()
            for path in bundle.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    return sorted(names)


def employer_names(text: str) -> set[str]:
    """Return every employer a file names as a field, a listing argument, or a board tenant."""
    names = {match.group(1) for match in EMPLOYER_FIELD.finditer(text)}
    names.update(match.group(1) for match in EMPLOYER_ARGUMENT.finditer(text))
    names.update(match.group("org") for match in BOARD_API_TENANT.finditer(text))
    for platform, pattern in BOARD_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            host = groups.get("host") or ""
            declared_tenant = groups.get("tenant") if platform == "workday" else ""
            tenant = declared_tenant or groups.get("org") or host.split(".", 1)[0]
            # A fingerprinted career site is matched by its platform's markup,
            # which names no tenant, and a numeric account id names no employer.
            if tenant and not tenant.isdigit():
                names.add(tenant)
    return names


def _folded(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def allowed_employers(name: str) -> tuple[str, ...]:
    """Return the employer names one file may use, empty when it is not scanned."""
    for prefix, allowed in EMPLOYER_SCOPES:
        if name.startswith(prefix):
            return allowed
    return ()


def is_synthetic_employer(name: str, allowed: tuple[str, ...]) -> bool:
    """Answer whether a name opens with one of the employers its area allows."""
    return _folded(name).startswith(tuple(_folded(item) for item in allowed))


def main() -> int:
    """Scan tracked, proposed, and packaged generated files."""
    try:
        names = repository_files()
    except FileNotFoundError:
        print("Public-data scan failed: git is unavailable")
        return 2
    findings: list[str] = []
    for name in names:
        if name in SKIP:
            continue
        path = Path(name)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if MACHINE_PATH.search(text):
            findings.append(f"{name}: machine-specific absolute path")
        if not name.startswith(GENERATED_BUNDLES):
            for match in EMAIL.finditer(text):
                if (
                    match.group(0).lower() not in SAFE_EMAIL_ADDRESSES
                    and match.group(1).lower() not in SAFE_EMAIL_DOMAINS
                ):
                    findings.append(f"{name}: non-synthetic email domain {match.group(1)}")
        for match in SECRET.finditer(text):
            token = match.group(0).lower()
            if not (
                name.startswith(TESTS) and any(word in token for word in SYNTHETIC_SECRET_WORDS)
            ):
                findings.append(f"{name}: key-shaped value")
        allowed = allowed_employers(name)
        if allowed:
            for employer in sorted(employer_names(text)):
                if not is_synthetic_employer(employer, allowed):
                    findings.append(
                        f'{name}: employer name "{employer}" is not synthetic; '
                        f"use one of {', '.join(allowed)}"
                    )
    if findings:
        print("Public-data scan failed:")
        for finding in findings:
            print(f"  {finding}")
        return 1
    print(
        "Public-data scan passed: no machine path, private email, unapproved key-shaped value, "
        "or non-synthetic employer name."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
