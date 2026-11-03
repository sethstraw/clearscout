"""Build the packaged city index the place reader uses from geonamescache's GeoNames data.

Usage: python scripts/build_gazetteer.py src/clearscout/domain/gazetteer.json
"""

from __future__ import annotations

import argparse
import json
from importlib import metadata, resources
from pathlib import Path

from clearscout.domain.countries import DEFAULT_TABLE
from clearscout.domain.places import place_key

# GeoNames numbers Canadian provinces; boards write the postal abbreviation.
CANADIAN_ADMIN1 = {
    "01": "AB",
    "02": "BC",
    "03": "MB",
    "04": "NB",
    "05": "NL",
    "07": "NS",
    "08": "ON",
    "09": "PE",
    "10": "QC",
    "11": "SK",
    "12": "YT",
    "13": "NT",
    "14": "NU",
}

COMMON_WORDS = frozenset(
    {
        "airport",
        "apac",
        "bank",
        "bay",
        "central",
        "federal",
        "hub",
        "metro",
        "mobile",
        "of",
        "opportunity",
        "republic",
        "springs",
        "temple",
        "time",
        "union",
        "university",
        "york",
    }
)


def _admin1(city: dict[str, object]) -> str:
    """Return the postal subdivision code for a US or Canadian city, else empty."""
    country, code = str(city["countrycode"]), str(city["admin1code"])
    if country == "US":
        return code
    if country == "CA":
        return CANADIAN_ADMIN1.get(code, "")
    return ""


def build() -> dict[str, object]:
    """Return the trimmed index with its attribution."""
    source = resources.files("geonamescache").joinpath("data", "cities15000.json")
    cities = json.loads(source.read_text(encoding="utf-8")).values()
    largest: dict[str, dict[tuple[str, str], int]] = {}
    for city in cities:
        key = place_key(str(city["name"]))
        if len(key) <= 3:
            continue
        rows = largest.setdefault(key, {})
        slot = (str(city["countrycode"]), _admin1(city))
        rows[slot] = max(rows.get(slot, 0), int(city["population"]))
    places = {
        key: [
            [country, admin, population]
            for (country, admin), population in sorted(
                rows.items(), key=lambda row: (-row[1], row[0])
            )
        ]
        for key, rows in largest.items()
    }
    version = metadata.version("geonamescache")
    return {
        "attribution": {
            "creator": "GeoNames (https://www.geonames.org/)",
            "licence": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
            "licence_url": "https://creativecommons.org/licenses/by/4.0/",
            "source": (
                "GeoNames cities15000 (https://download.geonames.org/export/dump/), "
                f"as packaged by geonamescache {version}"
            ),
            "modifications": (
                "Modified by ClearScout: only the cities15000 extract (population of fifteen "
                "thousand or more), primary names only, folded to ASCII, names of three letters "
                "or fewer removed, and fields trimmed to country, US or Canadian subdivision, "
                "and population."
            ),
            "warranty": "Provided as is, without warranty of any kind.",
        },
        "places": places,
        "qualified_only": sorted(
            key for key in places if key in COMMON_WORDS or DEFAULT_TABLE.names_in(key)
        ),
    }


def main() -> None:
    """Write the byte-stable index to an explicitly requested path."""
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    arguments.output.write_text(
        json.dumps(build(), indent=0, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
