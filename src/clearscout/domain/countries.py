"""Read a country out of the place a job board states.

The shipped table covers the territory names maintained by Babel. A person's
table adds private or publisher-specific spellings; it never narrows what the
product can recognize as somewhere else.

A two-letter code answers only in upper case, only when it leads the text or
the slot after the last comma and ends there or at a list separator or a work
arrangement, and only when the text names no country: "USA - Waltham, MA" is
the United States, "Islamabad, PK - Hybrid" is Pakistan, and "IT Business
Analyst" names nowhere. A code claimed by more than one place is dropped,
leaving "San Francisco, CA" unresolved here.

Subdivisions carry a spelled name beside that code: "Boston, Massachusetts" is
the United States. A name more than one place claims answers for neither, the
way an ambiguous code does, leaving "Atlanta, Georgia" unresolved. Where one
spelling is matched inside another's span only the longer answers: "Newark, New
Jersey" is the United States and not the island of Jersey, while "Sudan and
South Sudan" still names both.

A board that leads with a country and a spaced dash puts the street after the
last comma, so a second country spelled only there is a street name: "GBR -
London, Canada Square" is the United Kingdom. A plain list such as "United
States, Canada" still names both.

The table reads spellings and codes only and never a city. `clearscout.domain.places`
adds a gazetteer on top of it, which is what places "San Francisco, CA" in the
United States.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

from babel import Locale
from babel.core import get_global

from clearscout.domain.identity import mention_spans

_ALIASES: dict[str, tuple[str, ...]] = {
    "United States": ("us", "u.s.", "usa", "u.s.a.", "united states of america"),
    "United Kingdom": (
        "uk",
        "u.k.",
        "gb",
        "gbr",
        "great britain",
        "britain",
        "united kingdom of great britain and northern ireland",
        "england",
        "scotland",
        "wales",
        "northern ireland",
    ),
    "Netherlands": ("holland", "the netherlands", "nld"),
    "Czechia": ("czech republic", "cze"),
    "Poland": ("pol",),
    "Serbia": ("srb",),
    "Romania": ("rou", "rom"),
    "Bulgaria": ("bgr",),
    "China": ("chn",),
    "Argentina": ("arg",),
    "Mexico": ("mex",),
    "India": ("ind",),
    "Ireland": ("irl",),
    "Germany": ("deu", "ger"),
    "France": ("fra",),
    "Spain": ("esp",),
    "Italy": ("ita",),
    "Sweden": ("swe",),
    "Norway": ("nor",),
    # "den" is a Dutch article a board writes in a place name, not a country.
    "Denmark": ("dnk",),
    "Finland": ("fin",),
    "Portugal": ("prt", "por"),
    "Switzerland": ("che", "swi"),
    "Austria": ("aut",),
    "Belgium": ("bel",),
    "Ukraine": ("ukr",),
    "Russia": ("russian federation",),
    "Slovakia": ("slovak republic",),
    "T\N{LATIN SMALL LETTER U WITH DIAERESIS}rkiye": ("turkey", "turkiye", "tur"),
    "Saudi Arabia": ("ksa",),
    "United Arab Emirates": ("uae",),
    "Japan": ("jpn",),
    "South Korea": ("kor", "republic of korea", "korea, republic of"),
    "Australia": ("aus",),
    "New Zealand": ("nzl",),
    "Singapore": ("sgp", "sgp"),
    "Brazil": ("bra",),
    "Chile": ("chl",),
    "Colombia": ("col",),
    "Israel": ("isr",),
    "Philippines": ("phl",),
    "Vietnam": ("vnm", "viet nam"),
    "Malaysia": ("mys",),
    "Indonesia": ("idn",),
    "Thailand": ("tha",),
    "Hong Kong SAR China": ("hkg", "hong kong"),
    "Taiwan": ("twn",),
    "Egypt": ("egy",),
    "South Africa": ("zaf",),
    "Nigeria": ("nga",),
    "Kenya": ("ken",),
    "Canada": ("can", "cdn"),
    "Bosnia & Herzegovina": ("bosnia and herzegowina", "bosnia-herzegovina"),
    "Eswatini": ("swaziland", "kingdom of eswatini"),
    # Bare "Macedonia" is also a region of Greece ("Central Macedonia, Greece").
    "North Macedonia": (
        "republic of north macedonia",
        "the former yugoslav republic of macedonia",
        "macedonia, the former yugoslav republic of",
    ),
    "Myanmar (Burma)": ("burma",),
    "C\N{LATIN SMALL LETTER O WITH CIRCUMFLEX}te d\N{RIGHT SINGLE QUOTATION MARK}Ivoire": (
        "ivory coast",
    ),
    "Cape Verde": ("cabo verde",),
    "Timor-Leste": ("east timor",),
    "Laos": ("lao people's democratic republic", "lao pdr"),
    "Syria": ("syrian arab republic",),
    "St. Vincent & Grenadines": ("saint vincent and the grenadines",),
    "Kyrgyzstan": ("kyrgyz republic",),
    "Vatican City": ("holy see",),
    "Congo - Kinshasa": (
        "democratic republic of the congo",
        "congo, the democratic republic of the",
        "dr congo",
    ),
    "Congo - Brazzaville": ("republic of the congo",),
    "Palestinian Territories": ("state of palestine", "palestine, state of"),
    "Macao SAR China": ("macao",),
    "Faroe Islands": ("faeroe islands",),
    "Luxembourg": ("luxemburg",),
}

# A region name that swallows a constituent nation's spelling; never a country alias.
_CANCELS_ONLY: tuple[str, ...] = ("new england",)

# Short codes that are also English words ("can", "us", "nor") name a country only as a code.
_CODE_SHAPED = re.compile(r"[a-z]{2,3}")
_WORD_SAFE_CODES = frozenset({"uk", "usa", "uae", "ksa", "cdn"})
# "us/canada" and "Canada or the us" list a code beside a country that stands on its own.
_LIST_GAP = re.compile(r"[\s/,&;|()]*(?:(?:or|and)(?:\s+the)?[\s/,&;|()]*)?")
_LIST_TAIL = re.compile(r"\s*(?:$|[/,&;|().]|(?:or|and)(?![^\W\d_]))")

_PUBLISHER_REGIONS: dict[str, tuple[str, ...]] = {
    "Europe": (
        "eu",
        "e.u.",
        "european union",
        "eea",
        "e.e.a.",
        "european economic area",
    ),
}

PUBLISHER_REGION_NAMES = frozenset(_PUBLISHER_REGIONS)


_SUBDIVISIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "United States": (
        ("AL", "Alabama"),
        ("AK", "Alaska"),
        ("AZ", "Arizona"),
        ("AR", "Arkansas"),
        ("CA", "California"),
        ("CO", "Colorado"),
        ("CT", "Connecticut"),
        ("DE", "Delaware"),
        ("DC", "District of Columbia"),
        ("FL", "Florida"),
        ("GA", "Georgia"),
        ("HI", "Hawaii"),
        ("ID", "Idaho"),
        ("IL", "Illinois"),
        ("IN", "Indiana"),
        ("IA", "Iowa"),
        ("KS", "Kansas"),
        ("KY", "Kentucky"),
        ("LA", "Louisiana"),
        ("ME", "Maine"),
        ("MD", "Maryland"),
        ("MA", "Massachusetts"),
        ("MI", "Michigan"),
        ("MN", "Minnesota"),
        ("MS", "Mississippi"),
        ("MO", "Missouri"),
        ("MT", "Montana"),
        ("NE", "Nebraska"),
        ("NV", "Nevada"),
        ("NH", "New Hampshire"),
        ("NJ", "New Jersey"),
        ("NM", "New Mexico"),
        ("NY", "New York"),
        ("NC", "North Carolina"),
        ("ND", "North Dakota"),
        ("OH", "Ohio"),
        ("OK", "Oklahoma"),
        ("OR", "Oregon"),
        ("PA", "Pennsylvania"),
        ("RI", "Rhode Island"),
        ("SC", "South Carolina"),
        ("SD", "South Dakota"),
        ("TN", "Tennessee"),
        ("TX", "Texas"),
        ("UT", "Utah"),
        ("VT", "Vermont"),
        ("VA", "Virginia"),
        ("WA", "Washington"),
        ("WV", "West Virginia"),
        ("WI", "Wisconsin"),
        ("WY", "Wyoming"),
    ),
    "Canada": (
        ("AB", "Alberta"),
        ("BC", "British Columbia"),
        ("MB", "Manitoba"),
        ("NB", "New Brunswick"),
        ("NL", "Newfoundland and Labrador"),
        ("NS", "Nova Scotia"),
        ("NT", "Northwest Territories"),
        ("NU", "Nunavut"),
        ("ON", "Ontario"),
        ("PE", "Prince Edward Island"),
        ("QC", "Quebec"),
        ("SK", "Saskatchewan"),
        ("YT", "Yukon"),
    ),
}


SUBDIVISION_CODES = frozenset(
    code for subdivisions in _SUBDIVISIONS.values() for code, _ in subdivisions
)

# AP and Canadian Press abbreviations, which a posting writes after a town in place
# of the postal code ("Lebanon, Pa.", "Halifax, N.S.").
_PRESS_ABBREVIATIONS: dict[str, tuple[str, ...]] = {
    "AL": ("Ala.",),
    "AZ": ("Ariz.",),
    "AR": ("Ark.",),
    "CA": ("Calif.", "Cal."),
    "CO": ("Colo.",),
    "CT": ("Conn.",),
    "DE": ("Del.",),
    "DC": ("D.C.",),
    "FL": ("Fla.",),
    "GA": ("Ga.",),
    "IL": ("Ill.",),
    "IN": ("Ind.",),
    "KS": ("Kan.", "Kans."),
    "KY": ("Ky.",),
    "LA": ("La.",),
    "MD": ("Md.",),
    "MA": ("Mass.",),
    "MI": ("Mich.",),
    "MN": ("Minn.",),
    "MS": ("Miss.",),
    "MO": ("Mo.",),
    "MT": ("Mont.",),
    "NE": ("Neb.", "Nebr."),
    "NV": ("Nev.",),
    "NH": ("N.H.",),
    "NJ": ("N.J.",),
    "NM": ("N.M.",),
    "NY": ("N.Y.",),
    "NC": ("N.C.",),
    "ND": ("N.D.",),
    "OK": ("Okla.",),
    "OR": ("Ore.", "Oreg."),
    "PA": ("Pa.", "Penn.", "Penna."),
    "RI": ("R.I.",),
    "SC": ("S.C.",),
    "SD": ("S.D.",),
    "TN": ("Tenn.",),
    "TX": ("Tex.",),
    "VT": ("Vt.",),
    "VA": ("Va.",),
    "WA": ("Wash.",),
    "WV": ("W.Va.", "W. Va."),
    "WI": ("Wis.", "Wisc."),
    "WY": ("Wyo.",),
    "AB": ("Alta.",),
    "BC": ("B.C.",),
    "MB": ("Man.",),
    "NB": ("N.B.",),
    "NL": ("Nfld.", "N.L."),
    "NS": ("N.S.",),
    "ON": ("Ont.",),
    "PE": ("P.E.I.",),
    "QC": ("Que.",),
    "SK": ("Sask.",),
    "NT": ("N.W.T.",),
    "YT": ("Y.T.",),
}
_PRESS_CODES = {
    form.rstrip("."): code for code, forms in _PRESS_ABBREVIATIONS.items() for form in forms
}


def _press_form(stem: str) -> str:
    """Return the pattern for one stem; only a four-letter or longer word may drop its period."""
    period = r"\.?" if stem.isalpha() and len(stem) >= 4 else r"\."
    return re.escape(stem) + period


_PRESS_FORM = re.compile(
    r",\s*(?P<form>"
    + "|".join(_press_form(stem) for stem in sorted(_PRESS_CODES, key=len, reverse=True))
    + r")(?=\s*(?:\d{5}(?:-\d{4})?\s*)?(?:[,;|/()]|\s-\s|$))"
)


def with_postal_codes(text: str) -> str:
    """Return place text with each press abbreviation after a comma written as its postal code."""
    return _PRESS_FORM.sub(lambda found: ", " + _PRESS_CODES[found["form"].rstrip(".")], text)


_LEADING_SLOT = re.compile(r"^\s*(?P<lead>[^,]+?)\s+-\s+")
# A code followed by a word is a title acronym: "IT Business Analyst", "Analyst, IT Security".
_CODE_SEPARATOR = (
    r"(?:\s*[,;|]|\s+[-/&]\s|\s+or\s|(?:\s+|\s*\()(?i:remote|hybrid|on-?site|in-office)\b)"
)
_LEADING_CODE = re.compile(r"\s*(?P<code>[A-Z]{2})(?:\s*$|" + _CODE_SEPARATOR + ")")
_TRAILING_CODE = re.compile(
    r"(?P<code>[A-Z]{2})(?:\.?\s+(?:\d{3,6}(?:-\d{3,4})?|[A-Z]\d[A-Z] ?\d[A-Z]\d))?"
    r"(?:(?:\.?\s*\([^()]*\))?\s*$|" + _CODE_SEPARATOR + ")"
)


def _code_tokens(text: str) -> tuple[str, ...]:
    """Return upper-case codes that end the leading slot or open the slot after the last comma."""
    # A street address repeats the letters elsewhere: "BLOCK 4 FL 11" is a floor.
    leading = _LEADING_CODE.match(text)
    trailing = _TRAILING_CODE.match(text.rsplit(",", 1)[-1].strip()) if "," in text else None
    return tuple(found["code"] for found in (leading, trailing) if found)


def territory_name(code: str) -> str | None:
    """Return Babel's maintained English territory name for an ISO-style code."""
    stripped = code.strip()
    # A mixed-case pair ("Co", "To") is a truncated word, never a code.
    if len(stripped) == 2 and not (stripped.isupper() or stripped.islower()):
        return None
    normalized = stripped.upper()
    if normalized == "UK":
        normalized = "GB"
    if len(normalized) == 3:
        aliases = get_global("territory_aliases").get(normalized, ())
        normalized = aliases[0] if len(aliases) == 1 else normalized
    if len(normalized) != 2:
        return None
    name = Locale.parse("en").territories.get(normalized)
    return str(name) if name else None


def territory_by_prefix(word: str) -> str | None:
    """Return the one territory name a truncated word ("Japa") begins, else None."""
    stem = word.strip().casefold()
    if len(stem) < 3 or not stem.isalpha():
        return None
    names = {
        str(name)
        for code, name in Locale.parse("en").territories.items()
        if len(str(code)) == 2 and str(name).casefold().startswith(stem)
    }
    return names.pop() if len(names) == 1 else None


# Crown dependencies and Hong Kong run their own work-permit regimes and stay out.
_TERRITORY_SOVEREIGN: dict[str, str] = {
    "PR": "US",
    "GU": "US",
    "VI": "US",
    "AS": "US",
    "MP": "US",
    "MQ": "FR",
    "GP": "FR",
    "GF": "FR",
    "RE": "FR",
    "YT": "FR",
    "IC": "ES",
}

_SOVEREIGN_BY_NAME = {
    territory_name(code): territory_name(sovereign)
    for code, sovereign in _TERRITORY_SOVEREIGN.items()
}


def territory_sovereign(name: str) -> str | None:
    """Return the sovereign country of a maintained territory name, if one is modeled."""
    return _SOVEREIGN_BY_NAME.get(name)


def subdivision_names(country: str) -> tuple[str, ...]:
    """Return the modeled subdivision names of a country, empty when none are modeled."""
    return tuple(name for _code, name in _SUBDIVISIONS.get(country, ()))


def subdivision_name(country: str, code: str) -> str | None:
    """Return the modeled subdivision name a country's two-letter code names, if any."""
    return next((name for token, name in _SUBDIVISIONS.get(country, ()) if token == code), None)


def _plain_forms(name: str) -> tuple[str, ...]:
    """Return the spelled-out and unaccented forms of a CLDR name ("St. Kitts & Nevis")."""
    spelled = re.sub(r"\s+", " ", re.sub(r"\([^()]*\)", "", name)).strip().replace("&", "and")
    forms: list[str] = []
    for form in (spelled, re.sub(r"\bSt\. ", "Saint ", spelled)):
        apostrophe = form.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
        decomposed = unicodedata.normalize("NFKD", apostrophe)
        plain = "".join(char for char in decomposed if not unicodedata.combining(char))
        forms.extend((form, plain))
    return tuple(form for form in dict.fromkeys(forms) if form != name)


def _territory_names() -> dict[str, tuple[str, ...]]:
    """Return maintained English territory names with their plain forms and board aliases."""
    return {
        str(name): (*_plain_forms(str(name)), *_ALIASES.get(str(name), ()))
        for code, name in Locale.parse("en").territories.items()
        if len(str(code)) == 2 and str(name).strip()
    }


def _place_codes() -> dict[str, str]:
    """Return every two-letter code that exactly one country or subdivision claims."""
    claims: dict[str, set[str]] = {}
    for code, name in Locale.parse("en").territories.items():
        if len(str(code)) == 2 and str(name).strip():
            claims.setdefault(str(code), set()).add(str(name))
    for country, subdivisions in _SUBDIVISIONS.items():
        for code, _ in subdivisions:
            claims.setdefault(code, set()).add(country)
    return {code: next(iter(owners)) for code, owners in claims.items() if len(owners) == 1}


def _ambiguous_names() -> set[str]:
    """Return every casefolded subdivision name that more than one place claims."""
    claims: dict[str, set[str]] = {}
    for country, subdivisions in _SUBDIVISIONS.items():
        for _, name in subdivisions:
            claims.setdefault(name.casefold(), set()).add(country)
    for country, aliases in _territory_names().items():
        for spelling in (country, *aliases):
            if spelling.casefold() in claims:
                claims[spelling.casefold()].add(country)
    return {name for name, owners in claims.items() if len(owners) > 1}


def _default_names() -> dict[str, tuple[str, ...]]:
    """Return every maintained country keyed to its aliases and subdivision names."""
    ambiguous = _ambiguous_names()
    names = {
        country: aliases
        for country, aliases in _territory_names().items()
        if country.casefold() not in ambiguous
    }
    for country, subdivisions in _SUBDIVISIONS.items():
        kept = tuple(name for _, name in subdivisions if name.casefold() not in ambiguous)
        names[country] = (*names.get(country, ()), *kept)
    return {**names, **_PUBLISHER_REGIONS}


DEFAULT_NAMES = _default_names()
_PLACE_CODES = _place_codes()


def _covers(outer: tuple[int, int], inner: tuple[int, int]) -> bool:
    """Return whether ``outer`` spans every character ``inner`` spans."""
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _slot_spans(haystack: str) -> set[tuple[int, int]]:
    """Return the whole text, its leading dash slot, and the text after its last comma or dash."""
    stripped = haystack.strip()
    start = haystack.find(stripped)
    slots = {(start, start + len(stripped))}
    if lead := _LEADING_SLOT.match(haystack):
        slots.add(lead.span("lead"))
    for separator in (",", " - "):
        cut = haystack.rfind(separator)
        remainder = haystack[cut + len(separator) :].strip() if cut >= 0 else ""
        if remainder:
            offset = haystack.find(remainder, cut + len(separator))
            slots.add((offset, offset + len(remainder)))
    return slots


def _code_reads_as_country(text: str, haystack: str, spelling: str, span: tuple[int, int]) -> bool:
    """Return whether a matched spelling stands, holding short codes to code form."""
    if not _CODE_SHAPED.fullmatch(spelling) or spelling in _WORD_SAFE_CODES:
        return True
    if len(text) == len(haystack) and text[span[0] : span[1]].isupper():
        return True
    return span in _slot_spans(haystack)


def _listed_beside(haystack: str, span: tuple[int, int], others: Sequence[tuple[int, int]]) -> bool:
    """Return whether only list punctuation or a joiner separates ``span`` from another match."""
    # "Germany or can relocate" continues a sentence; a listed code ends its item.
    if not _LIST_TAIL.match(haystack, span[1]):
        return False
    for other in others:
        first, second = sorted((span, other))
        if first[1] <= second[0] and _LIST_GAP.fullmatch(haystack, first[1], second[0]):
            return True
    return False


def _outside_street_slot(
    haystack: str, matched: Sequence[tuple[tuple[int, int], str]]
) -> tuple[tuple[tuple[int, int], str], ...]:
    """Drop a country named only after the last comma when the leading slot names another."""
    lead = _LEADING_SLOT.match(haystack)
    street = haystack.rfind(",")
    if lead is None or street < 0:
        return tuple(matched)
    leading = {
        country
        for (start, end), country in matched
        if start == lead.start("lead") and end <= lead.end("lead")
    }
    if not leading:
        return tuple(matched)
    return tuple(
        (span, country) for span, country in matched if country in leading or span[0] < street
    )


class CountryTable:
    """The countries one reading may report, and every spelling of each."""

    def __init__(self, names: Mapping[str, Sequence[str]]) -> None:
        """Build a table from country names keyed to the spellings of each."""
        self._spellings: tuple[tuple[str, str], ...] = tuple(
            (spelling.casefold(), country)
            for country, aliases in names.items()
            for spelling in (country, *aliases)
            if spelling.strip()
        )

    def spans_in(self, text: str) -> tuple[tuple[tuple[int, int], str], ...]:
        """Return each spelling span in casefolded ``text`` with the country it names."""
        haystack = text.casefold()
        found = [
            (span, country, _code_reads_as_country(text, haystack, spelling, span))
            for spelling, country in self._spellings
            for span in mention_spans(haystack, spelling)
        ]
        standing = [span for span, _, stands in found if stands]
        matched = [
            (span, country)
            for span, country, stands in found
            if stands or _listed_beside(haystack, span, standing)
        ]
        cancels = {span for phrase in _CANCELS_ONLY for span in mention_spans(haystack, phrase)}
        matched = [
            (span, country)
            for span, country in matched
            if not any(_covers(cancel, span) for cancel in cancels)
        ]
        # A match lying inside another match's span names no place of its own.
        whole = [
            (span, country)
            for span, country in matched
            if not any(other != span and _covers(other, span) for other, _ in matched)
        ]
        return _outside_street_slot(haystack, whole)

    def coded_in(self, text: str) -> tuple[str, ...]:
        """Return the countries ``text`` names by a code exactly one place claims."""
        return tuple(
            sorted({_PLACE_CODES[code] for code in _code_tokens(text) if code in _PLACE_CODES})
        )

    def names_in(self, text: str) -> tuple[str, ...]:
        """Return every distinct maintained country or region ``text`` names."""
        named = {country for _, country in self.spans_in(text)}
        return tuple(sorted(named)) if named else self.coded_in(text)

    def named_in(self, text: str) -> str:
        """Return the one country or region ``text`` names; none or two give an empty string."""
        found = self.names_in(text)
        return found[0] if len(found) == 1 else ""


DEFAULT_TABLE = CountryTable(DEFAULT_NAMES)


def table_for(names: Mapping[str, Sequence[str]]) -> CountryTable:
    """Return maintained country names extended by the person's spellings."""
    if not names:
        return DEFAULT_TABLE
    combined = dict(DEFAULT_NAMES)
    for country, spellings in names.items():
        combined[country] = tuple(dict.fromkeys((*combined.get(country, ()), *spellings)))
    return CountryTable(combined)
