"""Export TailorCV's generated API schema for frontend type generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tailorcv.bootstrap.api import create_app


def main() -> None:
    """Write deterministic OpenAPI JSON to an explicitly requested path."""
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    arguments.output.write_text(
        json.dumps(
            create_app("sqlite+pysqlite:////nonexistent/tailorcv.sqlite3").openapi(),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
