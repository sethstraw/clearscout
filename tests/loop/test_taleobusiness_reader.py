"""Taleo Business Edition career sites list every opening, body included, in one public RSS feed."""

from __future__ import annotations

import html

import httpx
import pytest

from tailorcv.infrastructure.web_fetch.board_adapters import BoardApiFetcher, identify_board
from tailorcv.infrastructure.web_fetch.boards import board_identity
from tests.support.http import policy_client

_ROOT = "https://acme.tbe.taleo.net/acme01/ats"
_CAREERS = f"{_ROOT}/careers/v2/jobSearch?org=ACMEUNIV&cws=37"
_FEED = f"{_ROOT}/servlet/Rss?org=ACMEUNIV&cws=37"


def _item(rid: int, title: str, place: str) -> str:
    body = f"<p><strong>Who We Are</strong></p><p>Acme University hires a {title}.</p>"
    return (
        f"<item><title>{title}</title>"
        f"<link>{_ROOT}/careers/requisition.jsp?org=ACMEUNIV&amp;cws=37&amp;rid={rid}</link>"
        f"<description>   Who We Are   Acme University hires a {title}.</description>"
        "<pubDate>Wed, 21 Oct 2026 17:28:23 GMT</pubDate>"
        f"<taleo:reqId>{rid}</taleo:reqId><taleo:location>{place}</taleo:location>"
        "<taleo:department>8560</taleo:department>"
        f"<taleo:html-description>{html.escape(body)}</taleo:html-description></item>"
    )


def _feed(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        f'<?xml-stylesheet type="text/xsl" href="{_ROOT}/rss/taleorssfeed.xsl" ?>\n'
        '<rss xmlns:taleo="urn:TBERss" version="2.0">\n<channel>\n'
        "<title>Acme University Job Feed</title>\n"
        "<link>https://acme.tbe.taleo.net/dispatcher/servlet/DispatcherServlet"
        "?org=ACMEUNIV&amp;act=redirectCws&amp;cws=37</link>\n"
        "<language>en_CA</language><pubDate>10/21/26 5:28 PM</pubDate><ttl>60</ttl>\n"
        + "".join(items)
        + "</channel></rss>"
    )


def _fetcher(body: str, requested: list[str]) -> BoardApiFetcher:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url), text=body)

    return BoardApiFetcher(client=policy_client(answer))


def test_a_career_site_reads_every_posting_its_rss_feed_lists() -> None:
    requested: list[str] = []
    fetcher = _fetcher(
        _feed(
            _item(7362, "Director, Campus Safety", "Springfield (Eligible for Hybrid Work)"),
            _item(7401, "Research Data Analyst", "Shelbyville"),
        ),
        requested,
    )

    read = fetcher.listings("Acme University", _CAREERS)

    assert requested == [_FEED]
    assert [(item.requisition_id, item.title, item.location) for item in read.listings] == [
        ("7362", "Director, Campus Safety", "Springfield (Eligible for Hybrid Work)"),
        ("7401", "Research Data Analyst", "Shelbyville"),
    ]
    assert read.listings[0].url == f"{_ROOT}/careers/requisition.jsp?org=ACMEUNIV&cws=37&rid=7362"
    assert "hires a Director, Campus Safety." in read.listings[0].published_description
    assert read.complete is True


def test_a_page_linking_the_career_site_names_its_board_by_org_and_section() -> None:
    page = f'<a href="{html.escape(_CAREERS)}">Current openings</a>'

    identified = identify_board(page)

    assert identified is not None and identified[0] == "taleobusiness"
    assert board_identity(_CAREERS) == ("taleobusiness", "acme.tbe.taleo.net/acmeuniv/37")
    assert board_identity(_FEED) == board_identity(_CAREERS)


def test_an_answer_that_is_not_the_feed_refuses_the_board() -> None:
    page = "<html><head><title>Come Back Soon | Taleo Business Edition</title></head></html>"

    with pytest.raises(ValueError, match="did not return an XML feed"):
        _fetcher(page, []).listings("Acme University", _CAREERS)
