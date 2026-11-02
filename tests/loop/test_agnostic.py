"""The tool must work for any career, not one shape of career.

The deterministic half of the pipeline, run over a short, plain record: four
years, two roles, no publications, no education, no credentials, no domain
vocabulary. Output quality is not asserted.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from tailorcv.application.config import Settings
from tailorcv.application.master_cv.cv import EntryKind, parse_cv
from tailorcv.application.tailoring import batch, elicit, fit
from tailorcv.application.tailoring.fit.facts.roles import read_family
from tailorcv.application.tailoring.guardrails import Guardrails
from tailorcv.application.tailoring.stages.context import (
    asserted_text,
    citable_entries,
    copied_text,
    record_block,
)
from tailorcv.application.tailoring.stages.jobspec import Importance, JobSpec, Requirement
from tailorcv.application.tailoring.stages.match import Classification, EvidenceMap, Match, Strength
from tailorcv.application.tailoring.stages.select import build_plan
from tailorcv.domain import taxonomy
from tests.support.suite import FIXTURES

SHORT_CAREER = (FIXTURES / "short_career.md").read_text(encoding="utf-8")

ONE_ROLE = """# JO PARK

Toronto | jo@example.com

## SUMMARY

Analyst.

## PROFESSIONAL EXPERIENCE

### Northwind

**Analyst**
January 2024 to Present | Toronto

- Built the weekly revenue report
"""


@pytest.fixture
def short_cv(tmp_path: Path) -> Path:
    """Write the short, non-academic, non-clinical record to disk."""
    path = tmp_path / "CV.md"
    path.write_text(SHORT_CAREER, encoding="utf-8")
    return path


def spec_for(*requirements: tuple[str, str, Importance]) -> JobSpec:
    """Build a posting spec from plain tuples."""
    return JobSpec(
        title="Backend Engineer",
        company="Acme",
        seniority=None,
        location=None,
        requirements=tuple(
            Requirement(id=rid, text=text, importance=importance)
            for rid, text, importance in requirements
        ),
        keywords=(),
        hard_gates=(),
        origin="test",
    )


def test_a_record_with_no_publications_plans_without_them(short_cv: Path) -> None:
    """The publication floor and budget must not assume a publication record."""
    inventory = parse_cv(short_cv)
    assert inventory.of_kind(EntryKind.PUBLICATION) == ()

    plan = build_plan(inventory, spec_for(), EvidenceMap(), Settings())
    assert plan.publication_ids == ()
    assert plan.drop_order == ()


def test_a_record_with_no_education_section_still_assembles(short_cv: Path) -> None:
    """Education is a section many mid-career records omit entirely."""
    inventory = parse_cv(short_cv)
    assert inventory.of_kind(EntryKind.EDUCATION) == ()
    plan = build_plan(inventory, spec_for(), EvidenceMap(), Settings())
    assert plan.education_ids == ()


def test_a_single_role_record_does_not_divide_by_zero(tmp_path: Path) -> None:
    """One role, one bullet, no earlier career. The smallest real input."""
    path = tmp_path / "CV.md"
    path.write_text(ONE_ROLE, encoding="utf-8")
    inventory = parse_cv(path)
    assert len(inventory.roles) == 1
    plan = build_plan(inventory, spec_for(), EvidenceMap(), Settings())
    assert plan.recent_roles


def test_selection_works_with_no_rules_configured(short_cv: Path) -> None:
    """Guardrails are optional user-supplied rules."""
    inventory = parse_cv(short_cv)
    rules = Guardrails()
    assert not rules.work_authorization
    assert not rules.protected
    # The name, contact details, and copied role metadata remain stated facts.
    assert copied_text(inventory, rules).splitlines() == [
        "ALEX MORENO",
        "Berlin, Germany | alex@example.com",
        "Kettle Logistics",
        "Backend Engineer",
        "March 2022 to Present",
        "Berlin, Germany",
        "Bruno GmbH",
        "Junior Developer",
        "August 2020 to February 2022",
        "Leipzig, Germany",
    ]
    assert "(No additional distinction rules are configured.)" in rules.prompt_block()


def test_matching_is_not_shown_the_places_a_person_used_to_work(short_cv: Path) -> None:
    """A posting asking to live somewhere must not be answered by an old workplace.

    The critic sees every copied line so that it judges none of them. Matching
    reads the same block as facts a requirement about location may be answered
    by, and a role's location is where the work was, not where the applicant is.
    """
    inventory = parse_cv(short_cv)
    rules = Guardrails()

    asserted = asserted_text(inventory, rules)

    assert asserted == "Berlin, Germany | alex@example.com"
    assert "Leipzig, Germany" in copied_text(inventory, rules)


def test_the_record_block_carries_a_short_career(short_cv: Path) -> None:
    """The prompt prefix must not assume a large inventory to group."""
    inventory = parse_cv(short_cv)
    block = record_block(inventory)
    assert "Kettle Logistics" in block
    assert "materialized view" in block


BROAD_RECORD = """# RILEY OKONKWO

Lisbon, Portugal | riley@example.com

## SUMMARY

Quality lead in freight operations.

## PROFESSIONAL EXPERIENCE

### Northwind

**Quality Lead**
January 2020 to Present | Lisbon

- Wrote the depot audit checklist every regional site now works from

## CERTIFICATIONS

- Certified Internal Auditor, Institute of Internal Auditors, 2021

## APPOINTMENTS

- Visiting fellow in operations research, Riverbend Polytechnic, 2022 to 2024

## PRESENTATIONS

- Okonkwo R. Depot audits at scale. Regional Freight Forum. 2023.

## WRITING

- Okonkwo R. What a depot audit misses. Trade monthly column. 2024.

## MEMBERSHIPS

- Member, Association of Freight Quality Professionals

## AWARDS

- Depot Safety Award, 2022
"""


@pytest.mark.parametrize(
    ("kind", "claim"),
    [
        (EntryKind.CREDENTIAL, "Certified Internal Auditor"),
        (EntryKind.APPOINTMENT, "Visiting fellow in operations research"),
        (EntryKind.PRESENTATION, "Depot audits at scale"),
        (EntryKind.WRITING, "What a depot audit misses"),
        (EntryKind.MEMBERSHIP, "Association of Freight Quality Professionals"),
        (EntryKind.AWARD, "Depot Safety Award"),
    ],
)
def test_a_record_built_on_standing_rather_than_bullets_is_citable(
    tmp_path: Path, kind: EntryKind, claim: str
) -> None:
    """Careers that carry their weight outside experience bullets must still be citable."""
    path = tmp_path / "CV.md"
    path.write_text(BROAD_RECORD, encoding="utf-8")
    inventory = parse_cv(path)
    entry = inventory.of_kind(kind)[0]

    assert entry.id in {item.id for item in citable_entries(inventory)}
    assert claim in record_block(inventory)


def test_the_bullet_caps_are_settings_and_not_facts_about_resumes(short_cv: Path) -> None:
    """A twelve-year single role and six short ones want different shapes."""
    inventory = parse_cv(short_cv)
    entries = [entry.id for entry in citable_entries(inventory) if entry.id.startswith("b-")]
    matches = tuple(
        Match(
            requirement_id=f"r{index:02d}",
            entry_ids=(entry_id,),
            classification=Classification.DIRECT,
            strength=Strength.STRONG,
        )
        for index, entry_id in enumerate(entries, 1)
    )
    spec = spec_for(
        *((f"r{index:02d}", f"requirement {index}", Importance.MUST_HAVE) for index in range(1, 5))
    )
    evidence = EvidenceMap(matches=matches)

    generous = build_plan(inventory, spec, evidence, Settings(pages=4))
    strict = build_plan(inventory, spec, evidence, Settings(pages=4, max_bullets_per_role=1))
    # Four pages outlast this record's evidence, so a strict cap reorders and reports, not drops.
    assert strict.role_bullets == generous.role_bullets
    assert generous.passed_cap == 0
    assert strict.passed_cap == sum(max(0, len(ids) - 1) for ids in strict.role_bullets.values())
    assert strict.passed_cap > 0


BACKEND_POSTING = """
Backend Engineer, Kettle Logistics

Responsibilities:
- Own a rest api behind a microservice boundary and keep its unit tests honest
- Take part in code review and pay down technical debt in the routing service

Requirements:
- 4 years of experience writing server side software
"""


def test_the_deterministic_reading_works_for_a_career_it_was_not_built_against(
    short_cv: Path,
) -> None:
    """The rule-based reading carries the only market vocabulary in the program.

    It must stay independent of any one person's record, so this runs it over
    four years, two roles, and no papers.
    """
    inventory = parse_cv(short_cv)
    reading = fit.read(BACKEND_POSTING, inventory, today=date(2026, 6, 1))
    assert reading.role_family.value == "Software engineering"
    assert reading.years.value == 4
    assert reading.record_years.value == 2026 - 2020
    # This fixture has no publications, degree, or credentials.
    assert inventory.of_kind(EntryKind.PUBLICATION, EntryKind.EDUCATION) == ()


def test_the_reading_abstains_rather_than_reaching_for_a_familiar_answer(
    short_cv: Path,
) -> None:
    """A posting outside the table's vocabulary must produce nothing, not the nearest thing."""
    inventory = parse_cv(short_cv)
    reading = fit.read(
        "Lighthouse Keeper, Northern Isles\n\nRequirements:\n- Comfort with solitude\n",
        inventory,
        today=date(2026, 6, 1),
    )
    assert reading.role_family.value is None
    assert reading.specialty.value is None
    assert reading.seniority.value is None


def test_the_role_taxonomy_is_a_market_map_rather_than_one_persons_config() -> None:
    """The property that separates a taxonomy from a personal settings file.

    The public table spans professional functions and industries where a long,
    evidence-rich CV can support meaningful comparison. A table clustered
    around one person's adjacent markets belongs in their settings file.
    """
    families = taxonomy.FAMILIES
    sectors = {family.sector for family in families}
    keys = {family.key for family in families}
    assert {
        "software_engineering",
        "data_science",
        "product_management",
        "mechanical_engineering",
        "civil_engineering",
        "natural_sciences",
        "architecture_surveying",
        "nursing",
        "medicine_and_pharmacy",
        "clinical_research",
        "medical_writing",
        "finance",
        "banking_insurance",
        "risk_management",
        "legal",
        "business_analysis",
        "management_consulting",
        "operations",
        "procurement",
        "industrial_quality",
        "facilities_asset_management",
        "education",
        "learning_development",
        "student_services",
        "government_policy",
        "nonprofit_leadership",
        "editorial",
        "media_production",
        "design",
        "sales",
        "customer_success",
        "marketing",
        "human_resources",
        "construction_real_estate",
        "mining_energy",
        "forestry_conservation",
    } <= keys
    assert sectors == {
        "software and data",
        "health and life sciences",
        "business",
        "engineering and natural sciences",
        "education, law, social and public service",
        "communications, media and design",
        "revenue and customer growth",
        "professional operations and quality",
        "built environment and real estate",
        "resources, energy and environment",
    }
    largest = max(sum(1 for family in families if family.sector == sector) for sector in sectors)
    assert largest / len(families) <= 0.30, "one sector holds more than 30% of the table"
    assert len({family.key for family in families}) == len(families)
    specialties = [item.key for family in families for item in family.specialties]
    assert len(set(specialties)) == len(specialties), "two specialties share a key"


def test_the_professional_taxonomy_does_not_expand_into_frontline_job_search() -> None:
    titles = {title for family in taxonomy.FAMILIES for title in family.titles}
    assert titles.isdisjoint(
        {
            "cashier",
            "cook",
            "production worker",
            "machine operator",
            "truck driver",
            "bus driver",
            "heavy equipment operator",
            "construction labourer",
            "retail salesperson",
            "camera operator",
        }
    )


def test_life_science_data_covers_current_adjacent_markets() -> None:
    """Keep the general map broad across clinical, scientific, and governed data work."""
    family = next(item for item in taxonomy.FAMILIES if item.key == "life_science_data")
    assert {
        "clinical informatics lead",
        "clinical trial technology product owner",
        "digital pathology product manager",
        "pharmaceutical data steward",
    } <= set(family.titles)
    assert {
        "clinical_informatics",
        "clinical_trial_technology",
        "digital_pathology",
        "pharmaceutical_data_governance",
    } <= {item.key for item in family.specialties}


def test_the_reading_rules_know_nothing_about_which_families_exist() -> None:
    """Hand the scorer two invented families and it reads them by the same rules.

    The failure this guards against is a threshold, a bonus, or a special case
    written for one family, which is how a general table quietly becomes a
    personal one while still looking general. A table of two markets nobody in
    this repository works in cannot be special-cased by accident.
    """
    keeping = taxonomy.Family(
        key="lighthouse_keeping",
        label="Lighthouse keeping",
        sector="invented",
        titles=("lighthouse keeper",),
        terms=("lamp room", "fog signal", "watch rota"),
    )
    blowing = taxonomy.Family(
        key="glassblowing",
        label="Glassblowing",
        sector="invented",
        titles=("glassblower",),
        terms=("annealing oven", "punty rod", "gather of glass"),
    )
    lexicon = taxonomy.Taxonomy(families=(keeping, blowing))

    family, answer = read_family(lexicon, "Lighthouse Keeper", "Run the lamp room and fog signal.")
    assert family is keeping
    assert answer.value == "Lighthouse keeping"

    # Ambiguous invented markets are abstentions, not guesses.
    tied_text = "You will keep the watch rota and the punty rod."
    _, tied = read_family(lexicon, "Craftsperson", tied_text)
    assert tied.value is None


def test_discovery_asks_about_whatever_it_is_given(short_cv: Path) -> None:
    """The one feature that writes to a CV must not assume what the CV is about.

    A question is a template and a subject somebody else found, and nothing in
    elicitation knows what a technology, a credential, or a field is.
    """
    inventory = parse_cv(short_cv)
    for subject in ("a scaffolding ticket", "Rust", "paediatric dosing"):
        line = elicit.Line(subject=subject)
        question = line.next_question()
        assert question is not None
        assert subject in question.text
        answered = line.with_reply(elicit.Reply(text="No."))
        assert answered.closed

    # Seniority follows responsibility, not subject matter.
    reading = elicit.reads_as("I supported the night shift when the kiln went down.")
    assert reading.value is elicit.Ownership.SUPPORT
    material = elicit.propose(
        elicit.Line(
            subject="the kiln",
            answered=True,
            replies=(elicit.Reply(text="I supported the night shift when the kiln went down."),),
        ),
        text="Supported the night shift when the kiln went down.",
        scope="the night shift",
        placement=elicit.Placement(role_key=inventory.roles[0].key),
        inventory=inventory,
    )
    assert material.level is elicit.Ownership.SUPPORT
    assert material.category is EntryKind.BULLET


def test_batch_comparison_reads_two_postings_it_knows_nothing_about() -> None:
    """Shared requirements are shared words, not recognized subjects.

    The comparison has no list of technologies and no idea what a trade is. Two
    postings about bricklaying share a requirement here for exactly the reason
    two postings about Kubernetes would.
    """
    specs = {
        "first": spec_for(
            ("r01", "NVQ Level 3 in trowel occupations", Importance.MUST_HAVE),
            ("r02", "Own transport to sites across the county", Importance.MUST_HAVE),
        ),
        "second": spec_for(
            ("r01", "Trowel occupations qualification, or time served", Importance.MUST_HAVE),
            ("r02", "Comfortable working at height on a tower", Importance.MUST_HAVE),
        ),
    }
    evidence = {
        key: EvidenceMap(
            matches=tuple(
                Match(
                    requirement_id=requirement.id,
                    classification=Classification.GAP,
                    strength=Strength.NONE,
                )
                for requirement in spec.requirements
            )
        )
        for key, spec in specs.items()
    }
    shared, gaps = batch.compare(specs, evidence)
    assert [cluster.joined_by for cluster in shared] == [("occupations", "trowel")]
    assert gaps[0].leverage == 2
    assert gaps[0].subject == "occupations and trowel"


def test_nothing_in_the_package_names_its_author_where_behaviour_depends_on_it() -> None:
    """Identity belongs in packaging metadata and in the user's own settings.

    A name in `pyproject.toml`, the license, or the user agent identifies the
    software and is correct. A name anywhere that changes what the program does
    would mean it only works for one person.
    """
    root = Path(__file__).resolve().parent.parent.parent
    source = root / "src" / "tailorcv"
    prompts = root / "prompts"
    suite = root / "tests"
    client = root / "web" / "src"
    for path in (
        *source.rglob("*.py"),
        *prompts.glob("*.md"),
        *suite.rglob("*.py"),
        *client.rglob("*.ts"),
        *client.rglob("*.tsx"),
    ):
        if path == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8").casefold()
        # Match whole words so repository URLs remain valid.
        for token in ("seth", "strawbridge"):
            assert not re.search(rf"\b{token}\b", text), f"{path.name} names {token}"


def test_no_real_contact_details_are_committed() -> None:
    """Every phone number in source and tests is one of an explicit fictional set."""
    root = Path(__file__).resolve().parent.parent
    telephone = re.compile(r"(\+\d[\d ().-]{8,}\d)|(\b\d{3}[.-]\d{3}[.-]\d{4}\b)")
    fictional = {"555-555-5555", "+351 21 000 0000"}
    for path in (*(root / "src").rglob("*.py"), *(root / "tests").rglob("*.py")):
        if path == Path(__file__).resolve():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            found = telephone.search(line)
            assert not found or found.group(0) in fictional, f"{path.name}: {line.strip()}"


def test_no_domain_vocabulary_is_baked_into_the_prompts() -> None:
    """The prompts must read the same for a nurse, a bricklayer, and a compiler engineer."""
    prompts = Path(__file__).resolve().parent.parent / "prompts"
    for path in prompts.glob("*.md"):
        if path.name in {"README.md", "MANUAL_EVAL.md"}:
            continue
        text = path.read_text(encoding="utf-8").casefold()
        for token in ("clinical", "pharmaceutical", "patient", "biomedical", "healthcare"):
            assert token not in text, f"{path.name} assumes a {token} career"
