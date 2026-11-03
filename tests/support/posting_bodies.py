"""Synthetic posting bodies in each construction boards publish, and their canonical text."""

from __future__ import annotations

import pytest

from clearscout.infrastructure.web_fetch.board_mappings import plain_description
from clearscout.shared.layout import plain_layout

REQUIRED_HEADING, PREFERRED_HEADING = "Requirements", "Preferred Qualifications"

_INTRO = (
    "Northwind Rail runs the timetable eleven depots plan against, and this team keeps it honest."
)
_MUSTS = ("5+ years of experience building data pipelines", "Fluency in Python and SQL")
_WANTS = ("Experience with Kafka", "Familiarity with Terraform")
_NBSP, _ZERO_WIDTH = "&nbsp;", chr(0x200B)
_BULLET_DOT, _MIDDLE_DOT, _SYMBOL_BULLET = chr(0x2022), chr(0xB7), chr(0xF0B7)


def _li(items: tuple[str, ...]) -> str:
    return "".join(f"<li>{item}</li>" for item in items)


def _each(tag: str, items: tuple[str, ...], lead: str = "", end: str = "") -> str:
    return "".join(f"<{tag}>{lead}{item}{end}</{tag}>" for item in items)


def _listed(heading: str = "<h3>{}</h3>", wrap: str = "ul") -> str:
    return (
        f"<p>{_INTRO}</p>{heading.format(REQUIRED_HEADING)}<{wrap}>{_li(_MUSTS)}</{wrap}>"
        f"{heading.format(PREFERRED_HEADING)}<{wrap}>{_li(_WANTS)}</{wrap}>"
    )


def _paragraphs(heading: str = "<p><b>{}</b></p>", end: str = "", tag: str = "p") -> str:
    return (
        f"<{tag}>{_INTRO}</{tag}>{heading.format(REQUIRED_HEADING)}{_each(tag, _MUSTS, end=end)}"
        f"{heading.format(PREFERRED_HEADING)}{_each(tag, _WANTS, end=end)}"
    )


def _glyphs(needs: str, wants: str = "", space: str = " ") -> str:
    """Return one paragraph per item, each opening with a glyph."""
    return (
        f"<p>{_INTRO}</p><p><b>{REQUIRED_HEADING}</b></p>{_each('p', _MUSTS, needs + space)}"
        f"<p><b>{PREFERRED_HEADING}</b></p>{_each('p', _WANTS, (wants or needs) + space)}"
    )


def _broken(heading: str, marks: str = "") -> str:
    """Return a paragraph per section, its heading and items separated by line breaks."""

    def lines(name: str, items: tuple[str, ...]) -> str:
        leads = marks.split() or [""] * len(items)
        text = "<br>".join(
            f"{lead} {item}".strip() for lead, item in zip(leads, items, strict=True)
        )
        return f"<p>{heading.format(name)}<br>{text}</p>"

    return f"<p>{_INTRO}</p>{lines(REQUIRED_HEADING, _MUSTS)}{lines(PREFERRED_HEADING, _WANTS)}"


def _plain(heading: str, lead: str, glue: str = "\n", *, capitals: bool = False) -> str:
    def section(name: str, items: tuple[str, ...]) -> str:
        return glue.join(
            (heading.format(name.upper() if capitals else name), *(lead + i for i in items))
        )

    return glue.join(
        (_INTRO, "", section(REQUIRED_HEADING, _MUSTS), "", section(PREFERRED_HEADING, _WANTS))
    )


def _run_on(glyph: str, *, leading: bool) -> str:
    def section(name: str, items: tuple[str, ...]) -> str:
        if leading:
            return f"{name}: " + " ".join(f"{glyph} {item}" for item in items)
        return f"{name}: " + f" {glyph} ".join(items)

    return f"{section(REQUIRED_HEADING, _MUSTS)}\n\n{section(PREFERRED_HEADING, _WANTS)}"


def _escaped(markup: str, times: int) -> str:
    for _ in range(times):
        markup = markup.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return markup


def _table_rows() -> str:
    def rows(name: str, items: tuple[str, ...]) -> str:
        return f"<tr><th>{name}</th></tr>" + "".join(f"<tr><td>{i}</td></tr>" for i in items)

    return (
        f"<p>{_INTRO}</p><table>{rows(REQUIRED_HEADING, _MUSTS)}"
        f"{rows(PREFERRED_HEADING, _WANTS)}</table>"
    )


# Every shape states two requirements, one of them five years, and two preferred
# qualifications. "markup" goes through the board converter; "text" takes the pass
# a PDF, a text file, and a vision transcript take.
BODY_SHAPES = [
    pytest.param("markup", _listed(), id="ul under h3"),
    pytest.param("markup", _escaped(_listed("<p><strong>{}</strong></p>"), 1), id="escaped once"),
    pytest.param("markup", _escaped(_listed(), 2), id="escaped twice"),
    pytest.param("markup", f"<![CDATA[{_listed()}]]>", id="CDATA wrapper"),
    pytest.param(
        "markup",
        _listed().replace("<li>", "<li><p>").replace("</li>", "</p></li>"),
        id="li holding a p",
    ),
    pytest.param("markup", _listed(wrap="ol"), id="ordered list"),
    pytest.param(
        "markup",
        _listed('<p><span style="font-weight:700">{}</span></p>'),
        id="bold span over a list",
    ),
    pytest.param(
        "markup",
        _listed("<p><u>{}</u></p>").replace(
            f"</p><p><u>{REQUIRED_HEADING}", f"<br><br><u>{REQUIRED_HEADING}"
        ),
        id="underlined heading after a break",
    ),
    pytest.param(
        "markup", _listed().replace("<li>", f"<li>{_NBSP}{_ZERO_WIDTH}"), id="invisible spaces"
    ),
    pytest.param(
        "markup",
        _listed().replace("<li>5+ years of ", "<li>5+ years of\n      "),
        id="hard-wrapped markup source",
    ),
    pytest.param("markup", _glyphs(_BULLET_DOT), id="bullet paragraphs"),
    pytest.param("markup", _glyphs(_ZERO_WIDTH + _BULLET_DOT), id="zero-width before a bullet"),
    pytest.param("markup", _glyphs(_MIDDLE_DOT, space=_NBSP * 5 + " "), id="word middle dot"),
    pytest.param("markup", _glyphs("o", space=_NBSP * 2 + " "), id="word letter o"),
    pytest.param("markup", _glyphs(chr(0x27A2)), id="arrowhead"),
    pytest.param("markup", _glyphs(chr(0x2705)), id="check mark emoji"),
    pytest.param("markup", _glyphs(chr(0x25C6)), id="diamond"),
    pytest.param("markup", _glyphs(chr(0x25BA), chr(0x2713)), id="pointer and tick"),
    pytest.param("markup", _glyphs(_SYMBOL_BULLET), id="symbol font bullet"),
    pytest.param("markup", _glyphs(chr(0x2013)), id="en dash"),
    pytest.param("markup", _glyphs("-", space=""), id="hyphen, no space"),
    pytest.param("markup", _glyphs("*", space=""), id="asterisk, no space"),
    pytest.param("markup", _broken("<b>{}:</b>", f"{_BULLET_DOT} {_BULLET_DOT}"), id="br bullets"),
    pytest.param("markup", _broken("<b>{}:</b>", "1) 2)"), id="br, 1)"),
    pytest.param("markup", _broken("<b>{}:</b>", "(1) (2)"), id="br, (1)"),
    pytest.param("markup", _broken("<b>{}:</b>", "a. b."), id="br, a."),
    pytest.param("markup", _broken("{}:"), id="br under a colon heading"),
    pytest.param("markup", _broken("<b>{}</b>"), id="br under a bold heading"),
    pytest.param("markup", _paragraphs(tag="div"), id="div per item under a bold heading"),
    pytest.param(
        "markup", _paragraphs("<p><strong>{}</strong></p>"), id="p per item under a bold heading"
    ),
    pytest.param("markup", _paragraphs("<p>{}:</p>"), id="p per item under a colon heading"),
    pytest.param(
        "markup",
        _paragraphs("<p>{}</p>", end=".")
        .replace(REQUIRED_HEADING, REQUIRED_HEADING.upper())
        .replace(PREFERRED_HEADING, PREFERRED_HEADING.upper()),
        id="capitals heading over sentence items",
    ),
    pytest.param(
        "markup",
        f"<p>{_INTRO}</p><p><strong>{REQUIRED_HEADING}:</strong> {'; '.join(_MUSTS)}.</p>"
        f"<p><strong>{PREFERRED_HEADING}:</strong> {'; '.join(_WANTS)}.</p>",
        id="run-in heading, semicolon items",
    ),
    pytest.param(
        "markup",
        "".join(f"<p>{line}</p>" for line in _run_on(_BULLET_DOT, leading=True).split("\n\n")),
        id="inline bullet run",
    ),
    pytest.param(
        "markup",
        "".join(f"<p>{line}</p>" for line in _run_on(_MIDDLE_DOT, leading=False).split("\n\n")),
        id="inline middle dot run",
    ),
    pytest.param(
        "markup",
        f"<p>{_INTRO}</p><table><tr><td><b>{REQUIRED_HEADING}</b></td><td><ul>{_li(_MUSTS)}"
        f"</ul></td></tr><tr><td><b>{PREFERRED_HEADING}</b></td><td><ul>{_li(_WANTS)}</ul>"
        "</td></tr></table>",
        id="table, heading cell beside a list cell",
    ),
    pytest.param("markup", _table_rows(), id="table, one row per item"),
    pytest.param(
        "markup",
        f"<p>{_INTRO}</p><dl><dt>{REQUIRED_HEADING}</dt>{_each('dd', _MUSTS)}"
        f"<dt>{PREFERRED_HEADING}</dt>{_each('dd', _WANTS)}</dl>",
        id="description list",
    ),
    pytest.param(
        "markup",
        f"<section><p>{_INTRO}</p></section><section><h2>{REQUIRED_HEADING}</h2><ol>"
        f"{_li(_MUSTS)}</ol></section><section><h2>{PREFERRED_HEADING}</h2><ol>{_li(_WANTS)}"
        "</ol></section>",
        id="sections, h2 over ol",
    ),
    pytest.param(
        "markup", _plain("**{}**", "* ").replace("**\n", "**\n\n"), id="markdown bold heading"
    ),
    pytest.param("markup", _plain("## {}", "- "), id="markdown heading"),
    pytest.param(
        "markup", _plain("{}", _BULLET_DOT + " ", capitals=True), id="text, capitals over bullets"
    ),
    pytest.param("markup", _plain("{}", ""), id="text, bare heading over unmarked lines"),
    pytest.param("markup", _plain("{}:", "- ", "\r"), id="text, lone CR"),
    pytest.param("markup", _plain("{}:", "- ", chr(0x2028)), id="text, line separator"),
    pytest.param("text", _plain("{}", _MIDDLE_DOT + " "), id="text only, middle dot"),
    pytest.param("text", _plain("{}", _SYMBOL_BULLET + " ", "\r\n"), id="text only, CRLF, symbol"),
    pytest.param("text", _run_on(_BULLET_DOT, leading=True), id="text only, inline bullet run"),
    pytest.param("text", _plain("{}", "", glue="\n"), id="text only, bare heading, unmarked"),
    pytest.param("text", _plain("{}", _BULLET_DOT + "\n"), id="text only, bullet on its own line"),
    pytest.param(
        "text", _plain("{}", "- ").replace("\n\n", "\n"), id="text only, one newline per block"
    ),
]


def canonical_body(source: str, body: str) -> str:
    """Return the text the reader gets from a body of the given source kind."""
    return plain_description(body) if source == "markup" else plain_layout(body)
