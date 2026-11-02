"""Print a normalized CSS cascade manifest for before-and-after comparison."""

from __future__ import annotations

import re
import sys
from pathlib import Path

IMPORT = re.compile(r'@import\s+"(?P<href>[^"]+)"(?:\s+layer\((?P<layer>[^)]+)\))?\s*;')
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def imported_files(entry: Path) -> list[tuple[Path, str]]:
    """Return each imported stylesheet and its layer, in cascade order."""
    text = COMMENT.sub("", entry.read_text(encoding="utf-8"))
    found = []
    for match in IMPORT.finditer(text):
        href = match.group("href")
        found.append(((entry.parent / href).resolve(), match.group("layer") or "unlayered"))
    return found


def rules(text: str, prefix: str = "") -> list[tuple[str, str]]:
    """Return (selector, declarations) for every rule, descending into at-blocks.

    An at-block such as `@media (max-width: 60rem)` becomes part of the selector
    prefix, so a rule that moves between media queries is not mistaken for one
    that stayed put.
    """
    out: list[tuple[str, str]] = []
    depth = 0
    start = 0
    head = ""
    for index, character in enumerate(text):
        if character == "{":
            if depth == 0:
                head = text[start:index]
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                body = text[text.index("{", start) + 1 : index]
                selector = " ".join(head.split())
                if selector.startswith("@") and "{" in body:
                    out.extend(rules(body, f"{prefix}{selector} | "))
                else:
                    declarations = "; ".join(
                        " ".join(part.split()) for part in body.split(";") if part.strip()
                    )
                    out.append((f"{prefix}{selector}", declarations))
                start = index + 1
    return out


def manifest(root: Path) -> list[str]:
    """Return one line per rule, numbered in the order a browser would apply it."""
    lines: list[str] = []
    for path, layer in imported_files(root / "web" / "src" / "styles.css"):
        text = COMMENT.sub("", path.read_text(encoding="utf-8"))
        for selector, declarations in rules(text):
            lines.append(f"{len(lines):04d}\t{layer}\t{selector}\t{{{declarations}}}")
    return lines


def main() -> int:
    """Print the manifest for the repository this is run from."""
    lines = manifest(Path.cwd().resolve())
    print("\n".join(lines))
    print(f"# {len(lines)} rules", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
