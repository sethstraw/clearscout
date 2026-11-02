"""Write a minimal one-page PDF, so a reader is tested on the real format.

pypdfium2 reads PDFs but cannot author a text object, so the bytes are written
here rather than through a library the project does not depend on. Strings reach
the content stream in the order given, which is the stream order a reader sees.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

PAGE_WIDTH = 612
PAGE_HEIGHT = 792
FONT_SIZE = 10

Placed = tuple[int, int, str]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")


def _show(x: int, y: int, text: str) -> str:
    return f"BT /F1 {FONT_SIZE} Tf 1 0 0 1 {x} {y} Tm ({_escape(text)}) Tj ET\n"


def content_stream(placed: Sequence[Placed]) -> bytes:
    """Return the page content showing each string at its own point, in the order given."""
    return "".join(_show(x, y, text) for x, y, text in placed).encode("ascii")


def document(placed: Sequence[Placed], font: str = "Helvetica") -> bytes:
    """Return a one-page PDF carrying the content stream, with its cross-reference table."""
    stream = content_stream(placed)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length %d >>\nstream\n%bendstream" % (len(stream), stream),
        # Without an encoding the reader falls back to StandardEncoding, where byte
        # 0x27 is a right single quotation mark rather than an apostrophe.
        (f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} /Encoding /WinAnsiEncoding >>").encode(
            "ascii"
        ),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%b\nendobj\n" % (number, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        start,
    )
    return bytes(out)


def text_pdf(path: Path, placed: Sequence[Placed], font: str = "Helvetica") -> Path:
    """Write a one-page PDF showing each string at its own point in one of the base fonts."""
    path.write_bytes(document(placed, font))
    return path
