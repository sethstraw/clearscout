"""``plain_layout``: the one plain-text layout every posting body reaches."""

from __future__ import annotations

import pytest

from clearscout.shared.layout import plain_layout
from tests.support.posting_bodies import BODY_SHAPES, canonical_body


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Lead a team of engineers.In this role", "Lead a team of engineers. In this role"),
        ("Ranges to $120,000.If performed in Denver", "Ranges to $120,000. If performed in Denver"),
        ("Why join?We plan trains", "Why join? We plan trains"),
    ],
)
def test_the_layout_separates_sentences_glued_together(text: str, expected: str) -> None:
    assert plain_layout(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Apply at https://northwind.example/Careers.Apply today",
        "Write to jobs@example.Test with questions",
        "Our site is northwind.Com and the depot app",
        "Built on ASP.Net and Node.js services",
        "Pay rises 3.5 percent a year",
        "Open to U.S.Citizens and holders of a Ph.D.Degree",
        "Tools e.g.Python or Go",
    ],
)
def test_the_layout_leaves_an_address_a_decimal_and_an_abbreviation_whole(text: str) -> None:
    assert plain_layout(text) == text


def test_the_layout_never_leads_a_run_of_items_with_half_a_bracket() -> None:
    text = "Northwind Rail (Nasdaq: NWR) runs regional trains • across three depots and yards"
    assert plain_layout(text) == text


def test_the_layout_sets_a_heading_apart_from_the_list_above_it() -> None:
    text = "Requirements:\n- SQL\n- Kafka\nPreferred Qualifications\n- Terraform"
    assert plain_layout(text) == (
        "Requirements:\n- SQL\n- Kafka\n\nPreferred Qualifications\n- Terraform"
    )


@pytest.mark.parametrize(
    "text",
    [
        "- Experience building pipelines with\nKafka and Terraform\n- SQL",
        "- Comfortable with AI tools (e.g., Copilot, Claude\nCode) in your workflow\n- SQL",
    ],
)
def test_the_layout_keeps_a_wrapped_item_with_its_list(text: str) -> None:
    assert plain_layout(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tools: SQL.Python.Tableau", "Tools: SQL. Python. Tableau"),
        ("Bring SQL.Python!Kafka?Yes", "Bring SQL. Python! Kafka? Yes"),
        (
            "Skills: \u2022 SQL and dbt\u200b \u2022 Kafka streams",
            "Skills:\n- SQL and dbt\n- Kafka streams",
        ),
        (
            "Skills: \u2022 \ufeffSQL and dbt \u2022 Kafka streams",
            "Skills:\n- SQL and dbt\n- Kafka streams",
        ),
        ("**\u2022 Kafka streams**\n**\u2022 SQL and dbt**", "- Kafka streams\n- SQL and dbt"),
        ("Skills\n**\u200c**\nSQL", "Skills\n\nSQL"),
        ("*\u25c6*\nKafka streams", "- Kafka streams"),
        (
            "Stack (primary: Python): \u2022 Django web apps here \u2022 Celery task queues",
            "Stack (primary: Python):\n- Django web apps here\n- Celery task queues",
        ),
        (
            "Skills:\u2022.Kafka streams and dbt \u2022 SQL and Python",
            "Skills:\n- \u2022. Kafka streams and dbt\n- SQL and Python",
        ),
    ],
)
def test_the_layout_of_its_own_output_is_unchanged(text: str, expected: str) -> None:
    once = plain_layout(text)
    assert (once, plain_layout(once)) == (expected, expected)


@pytest.mark.parametrize(("source", "body"), BODY_SHAPES)
def test_the_canonical_layout_is_its_own_fixed_point(source: str, body: str) -> None:
    canonical = canonical_body(source, body)
    assert plain_layout(canonical) == canonical


def test_a_linkedin_tag_with_its_own_suffix_stays_one_token() -> None:
    body = "Apply by Friday. #LI-Hybrid1\nNorthwind Rail #LI-Remote-US plans trains."
    assert plain_layout(body) == body
