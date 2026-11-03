"""Build a master CV, screen jobs, and create evidence-grounded resume Markdown."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("clearscout")
except PackageNotFoundError:  # pragma: no cover - only an uninstalled source checkout
    __version__ = "0+unknown"
