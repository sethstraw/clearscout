"""Entry point, so ``python -m evals`` is the whole interface."""

from __future__ import annotations

import sys

from evals.runner import main

if __name__ == "__main__":
    sys.exit(main())
