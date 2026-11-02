"""Property tests for the fetch-policy URL classifier.

`fetch_refusal(url)` returns an empty string when a URL is fetchable, or a
short reason token otherwise. The property under test: the reason is invariant
under scheme casing, host casing, and a single trailing slash.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from tailorcv.infrastructure.web_fetch.policy import fetch_refusal

_HOST_LETTERS = st.text(alphabet="abcdefghijklmnop", min_size=3, max_size=12)
_PATH_LETTERS = st.text(alphabet="abcdefghijklmnop-", min_size=0, max_size=12)


@st.composite
def _fetchable_url(draw: st.DrawFn) -> str:
    """Return a fetchable URL: public multi-label host, lowercase scheme, no port or query."""
    left = draw(_HOST_LETTERS)
    right = draw(st.sampled_from(["com", "org", "net", "example"]))
    path = draw(_PATH_LETTERS)
    return f"https://{left}.{right}/{path}".rstrip("/")


@given(_fetchable_url())
def test_a_fetchable_url_stays_fetchable_under_scheme_and_host_casing(url: str) -> None:
    """Uppercase-scheme and uppercase-host copies still return an empty refusal."""
    assert fetch_refusal(url) == ""

    scheme, rest = url.split("://", 1)
    host, _, path = rest.partition("/")
    upper_scheme = f"{scheme.upper()}://{host}"
    upper_host = f"{scheme}://{host.upper()}"
    if path:
        upper_scheme = f"{upper_scheme}/{path}"
        upper_host = f"{upper_host}/{path}"

    assert fetch_refusal(upper_scheme) == "", (
        f"scheme casing changed the classifier's verdict for {url!r}"
    )
    assert fetch_refusal(upper_host) == "", (
        f"host casing changed the classifier's verdict for {url!r}"
    )


@given(_fetchable_url())
def test_a_single_trailing_slash_does_not_change_the_verdict(url: str) -> None:
    """Both `url` and `url/` classify the same way for a fetchable URL."""
    with_slash = url if url.endswith("/") else url + "/"
    without_slash = url.rstrip("/") if url != "https://" else url
    assert fetch_refusal(with_slash) == fetch_refusal(without_slash), (
        f"trailing slash changed the classifier's verdict for {url!r}"
    )


@given(_HOST_LETTERS)
def test_a_localhost_variant_is_always_refused(name: str) -> None:
    """Every casing of a localhost-shaped hostname is refused with a stable reason."""
    for variant in (f"https://{name}.localhost/", f"https://{name}.LOCALHOST/"):
        assert fetch_refusal(variant) == "local_hostname", (
            f"casing changed the localhost verdict for {variant!r}"
        )
