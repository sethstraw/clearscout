"""Read a CV out of whatever file someone actually has.

Every format converges on numbered lines of text, in reading order, with a note
on whether the original emphasized each one. The next stage cites those line
numbers behind every field. A .docx is read as zipped XML and yields only text
and bold.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from clearscout.infrastructure.web_fetch.ingest import IMAGE_SUFFIXES
from clearscout.shared.errors import IngestError

WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MARKDOWN_EMPHASIS = re.compile(r"^\s*(?:#{1,6}\s+|\*\*(?P<bold>[^*]+)\*\*\s*$)")
MINIMUM_USEFUL_LINES = 8
# pdfium writes U+0002, and some exporters a soft hyphen, where a line break split a word.
SPLIT_WORD = re.compile(r"(?<=[a-z])[\x02\u00ad][ \t]*(?:\r?\n[ \t]*)?(?=[a-z])")
# A real hyphen at a line break belongs to the word, as in cross-functional.
HYPHEN_AT_BREAK = re.compile(r"(?<=[a-z])-[ \t]*\r?\n[ \t]*(?=[a-z])")
LIGATURES = str.maketrans(
    {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\ufb05": "st",
        "\ufb06": "st",
    }
)
WORD_PART = re.compile(r"^word/(?P<kind>header|footer)(?P<number>\d*)\.xml$")


@dataclass(frozen=True)
class SourceLine:
    """One line of the original document, with its position kept."""

    number: int
    text: str
    emphasised: bool = False

    def render(self) -> str:
        """Render the line for a prompt, with its number and any emphasis."""
        marker = " [bold]" if self.emphasised else ""
        return f"{self.number:>3}{marker}: {self.text}"


@dataclass(frozen=True)
class SourceDocument:
    """A CV reduced to numbered lines, whatever it arrived as."""

    origin: str
    method: str
    lines: tuple[SourceLine, ...]

    def numbered(self) -> str:
        """Render every line for a prompt."""
        return "\n".join(line.render() for line in self.lines)

    def by_number(self, number: int) -> SourceLine | None:
        """Return one line by its number."""
        return next((line for line in self.lines if line.number == number), None)


def read(path: Path, vision: object | None = None) -> SourceDocument:
    """Read a CV from any supported format into numbered lines."""
    suffix = path.suffix.lower()
    if not path.is_file():
        raise IngestError(f"No such file: {path}")
    if suffix == ".docx":
        return _finish(path, "Word document", _from_docx(path))
    if suffix == ".pdf":
        return _finish(path, "PDF", _from_pdf(path))
    if suffix in {".md", ".markdown", ".txt"}:
        return _finish(path, "text", _from_text(path.read_text(encoding="utf-8")))
    if suffix in IMAGE_SUFFIXES:
        return _finish(path, "screenshot", _from_image(path, vision))
    raise IngestError(
        f"Cannot read {suffix or 'a file with no extension'}.\n"
        "  Supported: .docx, .pdf, .md, .txt, and screenshots.\n"
        "  For .doc, .pages, or .rtf, export or save-as one of those first."
    )


def _finish(path: Path, method: str, lines: list[tuple[str, bool]]) -> SourceDocument:
    """Number the lines and refuse a document with almost nothing in it."""
    numbered = tuple(
        SourceLine(number=index, text=text.translate(LIGATURES), emphasised=bold)
        for index, (text, bold) in enumerate(lines, 1)
    )
    if len(numbered) < MINIMUM_USEFUL_LINES:
        raise IngestError(
            f"Only {len(numbered)} line(s) of text came out of {path}.\n"
            "  If it is a scan or an image-only PDF, screenshot it and import the image."
        )
    return SourceDocument(origin=str(path), method=method, lines=numbered)


def _from_text(text: str) -> list[tuple[str, bool]]:
    """Split plain text or Markdown into lines, rejoining words a line break hyphenated."""
    lines: list[tuple[str, bool]] = []
    joined = HYPHEN_AT_BREAK.sub("-", SPLIT_WORD.sub("", text))
    for raw in joined.replace("\x02", "-").replace("\u00ad", "").splitlines():
        stripped = raw.strip()
        if not stripped or set(stripped) <= {"-", "=", "_", "*"}:
            continue
        match = MARKDOWN_EMPHASIS.match(stripped)
        lines.append((stripped, match is not None))
    return lines


def _from_pdf(path: Path) -> list[tuple[str, bool]]:
    """Extract the text layer of a PDF, page by page and column by column, with its bold lines."""
    # A CV is laid out for a reader, so a sidebar beside the roles is a column
    # rather than a table meant to be read across.
    from clearscout.infrastructure.web_fetch.ingest.pdf import extract_bold_lines, extract_text

    # The two readings space a line differently, since one follows the stream and
    # the other reads each column from its own bounds, so they meet on characters.
    bold = {_spaceless(text) for text, _ in _from_text("\n".join(extract_bold_lines(path)))}
    return [
        (text, emphasised or _spaceless(text) in bold)
        for text, emphasised in _from_text(extract_text(path, columns=True))
    ]


def _spaceless(text: str) -> str:
    """Return a line's characters with every space dropped."""
    return "".join(text.split())


def _from_docx(path: Path) -> list[tuple[str, bool]]:
    """Extract paragraph text and bold runs from a Word document.

    Page headers come before the body and footers after it. Table cells contain
    paragraphs too, so iterating every paragraph in document order picks up
    two-column layouts without reading table structure.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            document = archive.read("word/document.xml")
            parts = sorted(
                (match.group("kind"), int(match.group("number") or 0), archive.read(name))
                for name in archive.namelist()
                if (match := WORD_PART.match(name))
            )
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise IngestError(
            f"Could not read {path} as a Word document: {error}\n"
            "  If it is an older .doc, open it and save as .docx first."
        ) from error

    headers = [content for kind, _, content in parts if kind == "header"]
    footers = [content for kind, _, content in parts if kind == "footer"]
    lines = _unique(line for part in headers for line in _docx_lines(part))
    lines += _docx_lines(document)
    lines += _unique(line for part in footers for line in _docx_lines(part))
    return lines


def _unique(lines: Iterable[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Keep the first copy of each line; Word sections often repeat one header."""
    return list(dict.fromkeys(lines))


def _docx_lines(part: bytes) -> list[tuple[str, bool]]:
    """Return one document part's lines, splitting paragraphs at line breaks."""
    root = ElementTree.fromstring(part)  # noqa: S314 - local file, not a network payload
    lines: list[tuple[str, bool]] = []
    for paragraph in root.iter(f"{WORD_NAMESPACE}p"):
        emphasised = _is_emphasised(paragraph)
        for segment in _paragraph_text(paragraph).split("\n"):
            # Tabs align fields such as an employer and its dates.
            text = " | ".join(cell.strip() for cell in segment.split("\t") if cell.strip())
            if text:
                lines.append((text, emphasised))
    return lines


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    """Return a paragraph's text, with a tab for each tab and a newline for each break."""
    pieces: list[str] = []
    for run in paragraph.iter(f"{WORD_NAMESPACE}r"):
        for node in run:
            if node.tag == f"{WORD_NAMESPACE}t":
                pieces.append(node.text or "")
            elif node.tag == f"{WORD_NAMESPACE}tab":
                pieces.append("\t")
            elif node.tag in {f"{WORD_NAMESPACE}br", f"{WORD_NAMESPACE}cr"}:
                pieces.append("\n")
    return "".join(pieces)


def _is_emphasised(paragraph: ElementTree.Element) -> bool:
    """Return whether a whole paragraph is bold or styled as a heading."""
    style = paragraph.find(f"{WORD_NAMESPACE}pPr/{WORD_NAMESPACE}pStyle")
    if style is not None and "head" in (style.get(f"{WORD_NAMESPACE}val") or "").lower():
        return True
    runs = [
        run
        for run in paragraph.iter(f"{WORD_NAMESPACE}r")
        if (run.findtext(f"{WORD_NAMESPACE}t") or "").strip()
    ]
    bold = [run for run in runs if run.find(f"{WORD_NAMESPACE}rPr/{WORD_NAMESPACE}b") is not None]
    return bool(runs) and len(bold) == len(runs)


def _from_image(path: Path, vision: object | None) -> list[tuple[str, bool]]:
    """Transcribe a screenshot, which needs a model that can see."""
    from clearscout.infrastructure.web_fetch.ingest.image import media_type_of

    if vision is None or not callable(vision):
        raise IngestError(
            f"Reading {path.name} needs a model call and none was available.\n"
            "  Run 'clearscout doctor' to see whether a key is configured."
        )
    transcript = vision([path.read_bytes()], media_type_of(path))
    if not isinstance(transcript, str):  # pragma: no cover - programming error
        raise IngestError("The vision reader returned something other than text.")
    return _from_text(transcript)
