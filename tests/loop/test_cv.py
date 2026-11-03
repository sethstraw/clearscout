"""The parser and, more importantly, the identifier contract it promises."""

from __future__ import annotations

from pathlib import Path

import pytest

from clearscout.application.master_cv.cv import (
    EntryKind,
    content_hash,
    kind_for_section,
    normalize,
    parse_cv,
)
from clearscout.shared.errors import CvParseError


def test_parses_front_matter_and_sections(inventory) -> None:
    assert inventory.name == "JANE Q. EXAMPLE"
    assert "jane.doe@example.com" in inventory.contact_lines[0]
    assert "Professional Experience" in inventory.sections
    assert len(inventory.roles) == 3


def test_role_metadata_is_read_verbatim(inventory) -> None:
    role = inventory.role("northwind-analytics")
    assert role is not None
    assert role.title == "Staff Systems Engineer - Platform"
    assert role.title_of_record == "Staff Systems Engineer - Platform"
    assert role.dates == "March 2021 to Present"
    assert role.location == "Springfield, Illinois (Remote)"
    assert role.current is True
    assert role.start_year == 2021
    assert role.end_year is None


def test_bullets_paragraphs_and_groups_are_distinguished(inventory) -> None:
    entries = inventory.entries_for("northwind-analytics")
    kinds = {entry.kind for entry in entries}
    assert kinds == {EntryKind.BULLET, EntryKind.ROLE_CONTEXT}
    groups = {entry.group for entry in entries if entry.kind is EntryKind.BULLET}
    assert groups == {"Streaming and ingestion", "Governance and quality"}


@pytest.mark.parametrize("marker", ["-", "*", "+", "\u2022"])
def test_each_common_bullet_marker_starts_its_own_bullet(marker: str) -> None:
    document = (
        "# A B\n\na@example.com\n\n## Experience\n\n### Acme\n\n**Engineer**  \n2019 to 2021\n\n"
        f"{marker} Built the first thing.\n{marker} Built the second thing.\n"
    )
    inventory = parse_cv(Path("cv.md"), document)
    bullets = [entry.text for entry in inventory.of_kind(EntryKind.BULLET)]
    assert bullets == ["Built the first thing.", "Built the second thing."]


@pytest.mark.parametrize(
    ("dates", "start", "end", "current"),
    [
        ("01/2019 - 03/2021", 2019, 2021, False),
        ("2019-21", 2019, 2021, False),
        ("1998-02", 1998, 1998, False),
        ("2019-05 to 2021-03", 2019, 2021, False),
        ("2012-06 - 2015-01", 2012, 2015, False),
        ("2019-89", 2019, 2019, False),
        ("Jan 2019 - Mar 2021", 2019, 2021, False),
        ("Jan 2022 - Present (2 years 9 months)", 2022, None, True),
    ],
)
def test_role_years_are_read_from_dates_as_the_cv_writes_them(
    dates: str, start: int, end: int | None, current: bool
) -> None:
    document = (
        f"# A B\n\na@example.com\n\n## Experience\n\n### Acme\n\n**Engineer**  \n{dates}\n\n"
        "- Built the thing.\n"
    )
    role = parse_cv(Path("cv.md"), document).roles[0]
    assert (role.dates, role.start_year, role.end_year, role.current) == (
        dates,
        start,
        end,
        current,
    )


def test_publication_sub_bullets_travel_with_their_parent(tmp_path: Path) -> None:
    document = tmp_path / "cv.md"
    document.write_text(
        "# A B\n\na@example.com\n\n## Publications\n\n"
        "- Example J. A paper. *Journal*. 2020;1(1):1-2.\n"
        "   - **Publication date:** 1 March 2020\n",
        encoding="utf-8",
    )
    inventory = parse_cv(document)
    publication = inventory.of_kind(EntryKind.PUBLICATION)[0]
    assert publication.details == ("**Publication date:** 1 March 2020",)
    assert "Publication date" in publication.full_text


def test_identifiers_are_unique_and_content_addressed(inventory) -> None:
    ids = inventory.ids()
    assert len(ids) == len(set(ids))
    assert all("-" in entry_id for entry_id in ids)


def test_identifier_survives_presentation_only_edits(master_cv: Path, tmp_path: Path) -> None:
    original = master_cv.read_text(encoding="utf-8")
    before = {entry.text: entry.id for entry in parse_cv(master_cv).entries}

    reformatted = original.replace(
        "- Built the lineage service that traces any column back to its source system,"
        " adopted by four downstream teams",
        "-   Built the lineage service that traces any column back to its source system, "
        "adopted by four downstream teams  ",
    )
    changed = tmp_path / "cv.md"
    changed.write_text(reformatted, encoding="utf-8")
    after = {entry.text: entry.id for entry in parse_cv(changed).entries}

    lineage_before = next(
        value for key, value in before.items() if key.startswith("Built the lineage")
    )
    lineage_after = next(
        value for key, value in after.items() if key.startswith("Built the lineage")
    )
    assert lineage_before == lineage_after


def test_identifier_changes_when_the_claim_changes(master_cv: Path, tmp_path: Path) -> None:
    original = master_cv.read_text(encoding="utf-8")
    before = {entry.id for entry in parse_cv(master_cv).entries}
    edited = tmp_path / "cv.md"
    edited.write_text(original.replace("1,400 columns", "2,900 columns"), encoding="utf-8")
    after = {entry.id for entry in parse_cv(edited).entries}
    assert before != after
    assert len(before - after) == 1


def test_same_sentence_under_two_roles_gets_two_identifiers(tmp_path: Path) -> None:
    document = tmp_path / "cv.md"
    document.write_text(
        "# A B\n\na@example.com\n\n## Professional Experience\n\n"
        "### Alpha\n\n**Engineer**  \nJanuary 2020 to March 2021 | Ames, Iowa\n\n"
        "- Shipped the thing\n\n"
        "### Beta\n\n**Engineer**  \nJanuary 2018 to December 2019 | Ames, Iowa\n\n"
        "- Shipped the thing\n",
        encoding="utf-8",
    )
    inventory = parse_cv(document)
    bullets = inventory.of_kind(EntryKind.BULLET)
    assert len(bullets) == 2
    assert bullets[0].id != bullets[1].id


def test_duplicate_inside_one_scope_is_disambiguated(tmp_path: Path) -> None:
    document = tmp_path / "cv.md"
    document.write_text(
        "# A B\n\na@example.com\n\n## Professional Experience\n\n"
        "### Alpha\n\n**Engineer**  \nJanuary 2020 to March 2021 | Ames, Iowa\n\n"
        "- Shipped the thing\n- Shipped the thing\n",
        encoding="utf-8",
    )
    inventory = parse_cv(document)
    ids = [entry.id for entry in inventory.of_kind(EntryKind.BULLET)]
    assert len(set(ids)) == 2
    assert ids[1].endswith("-2")


@pytest.mark.parametrize("heading", ["Migrant Health Research", "Refunding Programs"])
def test_a_heading_that_merely_contains_a_fragment_is_not_filed_by_it(heading: str) -> None:
    """A fragment has to start a word, or a migrant-health section becomes an award."""
    assert kind_for_section(heading) is EntryKind.OTHER


@pytest.mark.parametrize("heading", ["Grants", "Grants and Funding", "Research Funding"])
def test_a_grant_is_evidence_rather_than_unfiled_text(heading: str) -> None:
    document = (
        f"# A B\n\na@example.com\n\n## {heading}\n\n"
        "- Principal investigator, Example Foundation, 2021 to 2023.\n"
    )
    inventory = parse_cv(Path("cv.md"), document)
    assert inventory.entries[0].kind is EntryKind.AWARD


@pytest.mark.parametrize(
    ("american", "british"),
    [
        ("Honors", "Honours"),
        ("Licenses", "Licences"),
        ("Professional Organizations", "Professional Organisations"),
    ],
)
def test_a_british_spelling_reaches_the_kind_its_american_twin_does(
    american: str, british: str
) -> None:
    assert kind_for_section(british) is kind_for_section(american)
    assert kind_for_section(british) is not EntryKind.OTHER


def test_content_hash_tracks_the_whole_file() -> None:
    assert content_hash("a") != content_hash("b")
    assert content_hash("a") == content_hash("a")


def test_normalize_ignores_presentation_only_differences() -> None:
    assert normalize("**Built**  the   thing.") == normalize("Built the thing")
    assert normalize("Built the thing") != normalize("Built a thing")


def test_missing_heading_is_an_actionable_error(tmp_path: Path) -> None:
    document = tmp_path / "cv.md"
    document.write_text("no heading here\n", encoding="utf-8")
    with pytest.raises(CvParseError, match="H1 name line"):
        parse_cv(document)


def test_unreadable_file_is_an_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(CvParseError, match="Cannot read"):
        parse_cv(tmp_path / "absent.md")
