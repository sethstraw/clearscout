"""Reading a posting written by a stranger without handing on its markup."""

from __future__ import annotations

import pytest

from clearscout.application.documents.rendered_text import rendered_text

SCRIPT = "<p>Lead the platform.</p><script>alert('x')</script><p>Remote.</p>"


@pytest.mark.parametrize(
    ("body", "mime_type", "expected"),
    [
        # Plain text is already the words, and rewriting it would corrupt a
        # posting that legitimately contains angle brackets.
        ("Salary < 200k > 150k", "text/plain", "Salary < 200k > 150k"),
        ("<p>kept</p>", "text/plain", "<p>kept</p>"),
        # A parameterized HTML type is still HTML.
        ("<p>Lead the platform.</p>", "text/html; charset=utf-8", "Lead the platform."),
        ("<p>Lead the platform.</p>", "TEXT/HTML", "Lead the platform."),
        # Script, style and template carry no words a reader wants, and showing
        # them escaped would be worse than leaving them out.
        (SCRIPT, "text/html", "Lead the platform.\nRemote."),
        ("<style>p{color:red}</style><p>Body.</p>", "text/html", "Body."),
        ("<template><p>Never shown.</p></template><p>Body.</p>", "text/html", "Body."),
        # Block structure is what makes the result readable rather than one run-on line.
        ("<li>One</li><li>Two</li>", "text/html", "One\nTwo"),
        # Character references become the characters they name.
        ("<p>Pay &amp; benefits</p>", "text/html", "Pay & benefits"),
        ("", "text/html", ""),
    ],
)
def test_only_the_visible_words_survive(body: str, mime_type: str, expected: str) -> None:
    assert rendered_text(body, mime_type) == expected


@pytest.mark.parametrize(
    "body",
    [
        SCRIPT,
        "<img src=x onerror=alert(1)>Posting text.",
        "<a href='javascript:alert(1)'>Apply</a>",
        "<div onclick='steal()'>Lead the platform.</div>",
        "<script>alert('unclosed')",
    ],
)
def test_nothing_active_reaches_the_reader(body: str) -> None:
    """The report shows words. A tag, a handler, or a script body is not a word."""
    result = rendered_text(body, "text/html")
    for fragment in ("<", ">", "alert", "onerror", "onclick", "javascript:"):
        assert fragment not in result, f"{fragment!r} survived in {result!r}"
