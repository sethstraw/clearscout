"""Fail on source comments that break the mechanical part of the comment policy.

Rules checked over the repository's own tracked and unignored source: a comment
block carries at most three lines of text, an unfinished-work marker names an
issue or an owner, no sentence opens with a justification clause, a callable's
or class's docstring stays within the line count of the code beneath it, and
neither a comment nor a docstring names a review code, names another file, or
narrates a change. A file header, a doc block, and a module docstring carry no
length bound.

Not checked: whether a comment or a docstring states a fact worth keeping, which
a person reads.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from functools import cache
from operator import itemgetter
from pathlib import Path

C_FAMILY_SUFFIXES = frozenset({".cjs", ".css", ".js", ".mjs", ".ts", ".tsx"})
HASH_SUFFIXES = frozenset(
    {".command", ".ini", ".mako", ".py", ".sh", ".toml", ".txt", ".yml", ".yaml"}
)
HASH_FILE_NAMES = frozenset(
    {".editorconfig", ".envrc.example", ".gitignore", ".prettierignore", "justfile"}
)
SEMICOLON_FILES = frozenset({".editorconfig"})
SEMICOLON_SUFFIXES = frozenset({".ini"})
MARKUP_SUFFIXES = frozenset({".html", ".md", ".svg"})
SCRIPT_SHEBANG = re.compile(r"#!\S*(?:/|\s)(?:bash|sh|zsh|python3?)\b")
GENERATED_FILES = frozenset({"web/src/api/schema.d.ts"})
FIXTURE_DIRECTORIES = ("evals/cases/", "tests/fixtures/", "tests/cassettes/")
MAXIMUM_BLOCK_LINES = 3
OPENING_JUSTIFICATION = re.compile(
    r"(?:\A|(?<=[.;:]) )\s*(?:because|rather than)\b|\bwhich is why\b", re.IGNORECASE
)
UNOWNED_MARKER = re.compile(r"\b(?:TODO|FIXME|HACK)\b(?![^\n]*(?:#\d+|@[A-Za-z0-9_-]+))")
# A linter code carries three digits, so the bare form stops at two.
REVIEW_CODE = re.compile(
    r"\b(?:decision|defect|finding|item|rule)\s+\d+\b|\bD\d{1,2}\b", re.IGNORECASE
)
DOCUMENT_POINTER = re.compile(
    r"\b[\w./-]*\.(?:py|ts|tsx|md)\b"
    r"|\bsee\s+(?:the\s+)?[\w.-]*/"
    r"|\bsee\s+(?:the\s+)?(?:\w+\s+){0,2}(?:doc|docs|document|documentation|guide|README)\b",
    re.IGNORECASE,
)
# A subject before "used to" separates the past tense from "the facts used to judge".
CHANGE_NARRATION = re.compile(
    r"\b(?:it|this|that|they|we)\s+used to\b|\bwas changed\b|\binstead of the old\b",
    re.IGNORECASE,
)
DATA_PATH_SUFFIXES = frozenset({".toml", ".yaml", ".yml"})
DECLARATION_NODES = (ast.AnnAssign, ast.Assign, ast.Pass)
HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})
DOCUMENTED_NODES = (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef, ast.Module)

type Documented = ast.AsyncFunctionDef | ast.ClassDef | ast.FunctionDef | ast.Module
type Finding = tuple[int, str]


def review_code(text: str) -> str | None:
    """Return the review or decision code the text names, or None where it names none."""
    match = REVIEW_CODE.search(text)
    return match.group(0) if match else None


def document_pointer(text: str) -> str | None:
    """Return the file or document the text points at, or None where it points at none."""
    match = DOCUMENT_POINTER.search(text)
    return match.group(0).strip() if match else None


def change_narration(text: str) -> str | None:
    """Return the words with which the text narrates a change, or None where it narrates none."""
    match = CHANGE_NARRATION.search(text)
    return match.group(0) if match else None


@cache
def repository_file_names() -> frozenset[str]:
    """Return the base name of every tracked or unignored file."""
    return frozenset(Path(name).name for name in listed_files())


def points_elsewhere(path: Path, pointer: str) -> bool:
    """Report whether a pointer names another repository file, outside configuration data."""
    if path.suffix in DATA_PATH_SUFFIXES or Path(pointer).name == path.name:
        return False
    # A file name the repository does not hold is an artifact the code writes, not a pointer.
    return not Path(pointer).suffix or Path(pointer).name in repository_file_names()


def wording_violations(path: Path, line: int, kind: str, text: str) -> list[Finding]:
    """Return the wording violations one comment block or docstring carries."""
    pointer = document_pointer(text)
    named = (
        (review_code(text), "names a review code"),
        (pointer if pointer and points_elsewhere(path, pointer) else None, "names another file"),
        (change_narration(text), "narrates a change"),
    )
    return [(line, f"{kind} {rule} {found!r}") for found, rule in named if found is not None]


def has_script_shebang(path: Path) -> bool:
    """Report whether a file opens with a shell or Python shebang."""
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline()
    except (OSError, UnicodeDecodeError):
        return False
    return SCRIPT_SHEBANG.match(first) is not None


def comment_syntax(path: Path) -> str | None:
    """Return the comment syntax a file uses: "c", "hash", "markup", or None."""
    if path.suffix in C_FAMILY_SUFFIXES:
        return "c"
    if path.suffix in MARKUP_SUFFIXES:
        return "markup"
    if path.suffix in HASH_SUFFIXES or path.name in HASH_FILE_NAMES:
        return "hash"
    if not path.suffix and path.is_file() and has_script_shebang(path):
        return "hash"
    return None


def is_checkable(path: Path) -> bool:
    """Report whether one path is comment-bearing source that the policy covers."""
    name = path.as_posix()
    return (
        comment_syntax(path) is not None
        and not any(
            name == generated or name.endswith(f"/{generated}") for generated in GENERATED_FILES
        )
        and not any(directory in name for directory in FIXTURE_DIRECTORIES)
        and path.is_file()
    )


def source_files() -> list[Path]:
    """Return the repository's comment-bearing files, less generated and fixture ones."""
    return [Path(name) for name in listed_files() if is_checkable(Path(name))]


def listed_files() -> list[str]:
    """Return every tracked or unignored path, sorted."""
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git is unavailable")
    result = subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
    )
    return sorted(
        {raw_name.decode("utf-8", errors="strict") for raw_name in result.stdout.split(b"\0")}
        - {""}
    )


def comment_text(line: str, syntax: str, semicolons: bool = False) -> str | None:
    """Return one line's comment text, or None where the line is not a comment."""
    stripped = line.lstrip()
    if semicolons and stripped.startswith(";"):
        return stripped[1:].strip()
    if syntax == "c":
        if stripped.startswith("//"):
            return stripped[2:].strip()
        if stripped.startswith("/*") or stripped.startswith("*"):
            body = stripped.removeprefix("/**").removeprefix("/*").removeprefix("*")
            return body.removesuffix("*/").strip()
        return None
    if stripped.startswith("#") and not stripped.startswith("#!"):
        return stripped[1:].strip()
    return None


def block_violations(path: Path, start: int, body: list[str], documenting: bool) -> list[Finding]:
    """Return the violations one comment block carries, judging its wording as joined prose."""
    found: list[Finding] = []
    length = len([line for line in body if line])
    if length > MAXIMUM_BLOCK_LINES and not documenting:
        found.append((start, f"comment block has {length} lines"))
    prose = " ".join(body)
    if OPENING_JUSTIFICATION.search(prose):
        found.append((start, "a sentence opens as a justification"))
    found.extend(wording_violations(path, start, "comment", prose))
    return found


def markup_comment_violations(path: Path) -> list[Finding]:
    """Return every violation the <!-- --> comments of one file carry."""
    found: list[Finding] = []
    start: int | None = None
    body: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        rest = line
        while rest:
            if start is None:
                opening = rest.find("<!--")
                if opening < 0:
                    break
                start, body, rest = number, [], rest[opening + 4 :]
            closing = rest.find("-->")
            text = rest if closing < 0 else rest[:closing]
            if text.strip():
                body.append(text.strip())
            if UNOWNED_MARKER.search(text):
                found.append((number, "TODO/FIXME/HACK lacks an issue or owner"))
            if closing < 0:
                break
            found.extend(block_violations(path, start, body, documenting=False))
            start, rest = None, rest[closing + 3 :]
    return found


def comment_violations(path: Path) -> list[Finding]:
    """Return every comment violation in one file, in line order."""
    syntax = comment_syntax(path)
    if syntax is None:
        return []
    if syntax == "markup":
        return markup_comment_violations(path)
    semicolons = path.suffix in SEMICOLON_SUFFIXES or path.name in SEMICOLON_FILES
    found: list[Finding] = []
    start: int | None = None
    body: list[str] = []
    documenting = False
    before_any_code = True
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = comment_text(line, syntax, semicolons)
        if text is None:
            if start is not None:
                found.extend(block_violations(path, start, body, documenting))
                before_any_code = False
            elif line.strip() and not line.startswith("#!"):
                before_any_code = False
            start, body, documenting = None, [], False
            continue
        if start is None:
            start = number
            # A file header and a doc block document an interface and carry no length bound.
            documenting = before_any_code or line.lstrip().startswith("/**")
        body.append(text)
        if UNOWNED_MARKER.search(text):
            found.append((number, "TODO/FIXME/HACK lacks an issue or owner"))
    if start is not None:
        found.extend(block_violations(path, start, body, documenting))
    return found


def code_lines(source_lines: list[str], node: Documented) -> int:
    """Return the count of non-blank source lines beneath a node's docstring."""
    statements = node.body[1:]
    if not statements:
        return 0
    first = statements[0].lineno
    last = statements[-1].end_lineno or statements[-1].lineno
    return len([line for line in source_lines[first - 1 : last] if line.strip()])


def is_declaration_only(node: Documented) -> bool:
    """Report whether a class body beneath its docstring declares members and nothing else."""
    if not isinstance(node, ast.ClassDef):
        return False
    return all(
        isinstance(statement, DECLARATION_NODES)
        or (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
        for statement in node.body[1:]
    )


def is_route_handler(node: Documented) -> bool:
    """Report whether a callable is registered on a router, which publishes its docstring."""
    return not isinstance(node, (ast.ClassDef, ast.Module)) and any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in HTTP_METHODS
        for decorator in node.decorator_list
    )


def docstring_violations(path: Path) -> list[Finding]:
    """Return every docstring violation in one Python file."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=path.as_posix())
    except SyntaxError:
        return []
    source_lines = source.splitlines()
    found: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, DOCUMENTED_NODES):
            continue
        docstring = ast.get_docstring(node, clean=True)
        if docstring is None:
            continue
        lines = [line for line in docstring.splitlines() if line.strip()]
        start = node.body[0].lineno
        found.extend(wording_violations(path, start, "docstring", " ".join(lines)))
        if isinstance(node, ast.Module) or is_declaration_only(node) or is_route_handler(node):
            continue
        beneath = code_lines(source_lines, node)
        if beneath and len(lines) > beneath:
            found.append(
                (start, f"docstring has {len(lines)} lines and the code beneath it has {beneath}")
            )
    return found


def file_violations(path: Path) -> list[str]:
    """Return every policy violation in one file, in line order."""
    found = comment_violations(path)
    if path.suffix == ".py":
        found.extend(docstring_violations(path))
    return [f"{path.as_posix()}:{line}: {rule}" for line, rule in sorted(found, key=itemgetter(0))]


def violations(paths: Sequence[Path] | None = None) -> list[str]:
    """Return every comment-policy violation in the repository, or in the named files."""
    checked = source_files() if paths is None else [path for path in paths if is_checkable(path)]
    found: list[str] = []
    for path in checked:
        found.extend(file_violations(path))
    return found


def main(argv: Sequence[str] = ()) -> int:
    """Print comment-policy violations and fail when there are any."""
    parser = argparse.ArgumentParser(description="Check the mechanical comment policy.")
    parser.add_argument(
        "--files",
        nargs="+",
        type=Path,
        help="check only these files; the default checks the whole repository",
    )
    arguments = parser.parse_args(argv)
    found = violations(arguments.files)
    if not found:
        print("Comment-policy check found no violations.")
        return 0
    print("Comment-policy violations:")
    print("\n".join(f"  {violation}" for violation in found))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
