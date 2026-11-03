"""Read visible text out of stored evidence without trusting its markup.

A posting body is a document written by a stranger. Rendering it is the
job of the browser; what a report needs is the words, with script, style
and template content left out rather than escaped and shown.
"""

from __future__ import annotations

from html.parser import HTMLParser


class _VisibleTextParser(HTMLParser):
    """Extract visible text without executing or returning active HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "template"}:
            self.hidden_depth += 1
        elif tag in {"br", "p", "div", "li", "section", "article", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "template"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in {"p", "div", "li", "section", "article", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def rendered_text(body: str, mime_type: str) -> str:
    """Return safe readable text for raw text or HTML evidence."""
    if mime_type.split(";", 1)[0].strip().lower() != "text/html":
        return body
    parser = _VisibleTextParser()
    parser.feed(body)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
