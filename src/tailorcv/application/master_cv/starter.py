"""Write a conforming master CV skeleton a person can fill in from scratch."""

from __future__ import annotations

from pathlib import Path

from tailorcv.shared.errors import ConfigError

TEMPLATE = """\
# YOUR NAME

City, Region | you@example.com | 555-555-5555 | linkedin.com/in/yourname

## Summary

Two or three sentences: the role and level you are targeting, and the strongest
evidence you bring to it. This is the only section a model rewrites freely.

## Core Skills

**Skill area:** the tools, methods, and domains in this area, comma-separated

**Another area:** every line here reads bold label, colon, then content

## Professional Experience

### Current Organization

**Your Title**
January 2020 to Present | City, Region

One or two sentences of scope, if the bullets do not make it obvious.

- An achievement with a number, a scope, and an outcome.
- Another one. Start with the verb and end with the result.
- Write everything you have done here, not just what fits one resume. This file
  is the superset; tailorcv selects from it.

### Earlier Organization

**Your Title**
June 2016 to December 2019 | City, Region

- Dates are written out and joined by "to". They sit on their own line, or after
  the title separated by a pipe. They are never bold; bold marks titles.

## Education

**Degree**, Institution, City, Region, 2012 to 2016

## Publications

- Author A, Author B. Title of the work. *Journal*. 2024;12(4):220-231.
"""


def write_template(path: Path) -> Path:
    """Write a conforming master CV skeleton.

    The file already parses, so 'tailorcv doctor' will read it and report what
    it found.
    """
    if path.exists():
        raise ConfigError(f"{path} already exists. Delete it or choose another path.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path
