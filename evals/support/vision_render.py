"""Synthetic sources a vision smoke renders to a screenshot, and the local renderer it uses.

Each source is text already in the repository: a corpus CV's Markdown and the
posting of the ``vision_reachable`` case. Rendering runs the browser production
renders with, locally and offline; nothing is fetched.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from html import escape
from pathlib import Path

from clearscout.infrastructure.web_fetch.rendered import _installed_browser
from evals.support.cv_formats import CV_FORMATS

Renderer = Callable[[str], bytes]
SOURCES: dict[str, Callable[[], str]] = {
    "cv:academic_pdf": lambda: (CV_FORMATS / "academic_pdf" / "source.md").read_text(
        encoding="utf-8"
    ),
    "posting:vision_reachable": lambda: tomllib.loads(
        (Path(__file__).resolve().parents[1] / "cases" / "vision_reachable.toml").read_text(
            encoding="utf-8"
        )
    )["posting"],
}
_PAGE = (
    "<!doctype html><meta charset='utf-8'><body style='margin:32px;background:#fff'>"
    "<pre style='font:16px/1.5 Helvetica,Arial,sans-serif;white-space:pre-wrap;color:#111'>"
    "{text}</pre></body>"
)


def source_text(source: str) -> str:
    """Return the text one named synthetic source renders."""
    if source not in SOURCES:
        raise ValueError(f"no vision smoke source named {source!r}")
    return SOURCES[source]()


def chrome_screenshot(text: str) -> bytes:
    """Return a full-page PNG of ``text`` laid out as plain wrapped lines."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _installed_browser(playwright.chromium)
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 800})
            page.set_content(_PAGE.format(text=escape(text)))
            return page.screenshot(full_page=True, type="png")
        finally:
            browser.close()
