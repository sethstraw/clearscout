"""Property tests for the country/region name table.

The alias table maps one canonical name to every spelling that resolves to it.
The generator supplies disjoint spellings; the property under test is that
every declared spelling resolves to its canonical country.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from clearscout.domain.countries import CountryTable

# A bounded alphabet keeps the alias space finite and every generated table
# distinguishable. All-letters keeps the tokens outside the ISO-3 code table
# so the fallback code path does not overlap with the alias path.
_ALIAS_LETTERS = st.text(alphabet="abcdefghijklmnop", min_size=4, max_size=10)


def _distinct_aliases(draw: st.DrawFn) -> tuple[str, list[str]]:
    canonical = draw(_ALIAS_LETTERS)
    aliases = draw(st.lists(_ALIAS_LETTERS, min_size=0, max_size=4, unique=True))
    return canonical, aliases


@st.composite
def _alias_table(draw: st.DrawFn) -> dict[str, list[str]]:
    """Return up to four canonicals, each with zero to four unique aliases.

    Every generated spelling is disjoint across entries, so a match is
    unambiguous by construction.
    """
    entries = draw(st.integers(min_value=1, max_value=4))
    canonicals: list[tuple[str, list[str]]] = []
    used: set[str] = set()
    for _ in range(entries):
        canonical, aliases = _distinct_aliases(draw)
        if canonical.casefold() in used or any(alias.casefold() in used for alias in aliases):
            continue
        used.add(canonical.casefold())
        used.update(alias.casefold() for alias in aliases)
        canonicals.append((canonical, aliases))
    return dict(canonicals)


@given(_alias_table())
def test_every_declared_spelling_resolves_to_its_canonical(names: dict[str, list[str]]) -> None:
    table = CountryTable(names)
    for canonical, aliases in names.items():
        for spelling in (canonical, *aliases):
            assert table.named_in(spelling) == canonical, (
                f"spelling {spelling!r} of {canonical!r} did not resolve back to it"
            )
