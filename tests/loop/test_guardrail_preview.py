"""The deterministic guardrail preview, checked against a master CV's text."""

from __future__ import annotations

from clearscout.application.tailoring.guardrail_preview import (
    GuardrailPreviewReport,
    preview_against_master_cv,
)
from clearscout.application.tailoring.guardrails import Guardrails

CV = """\
# Jane Q. Example

Springfield, Illinois | jane@example.com | 555-0100

---

## Professional Experience

### Northwind Analytics

- Rebuilt the ingestion tier on Kafka, cutting latency across every feed.
"""


def test_forbidden_match_is_reported_with_offset() -> None:
    rules = Guardrails(forbidden=["Kafka"])
    report = preview_against_master_cv(rules, CV)
    [match] = report.forbidden
    assert match.matched is True
    assert match.offsets == [CV.index("Kafka")]
    assert match.redraftable is True


def test_forbidden_match_in_another_case_is_reported_with_offsets() -> None:
    rules = Guardrails(forbidden=["kafka"])
    report = preview_against_master_cv(rules, CV)
    [match] = report.forbidden
    assert match.matched is True
    assert match.offsets == [CV.index("Kafka")]


def test_protected_phrase_normalized_presence_reports_all_bare_first_word_occurrences() -> None:
    cv = (
        "# Jane Q. Example\n\n"
        "Northwind Analytics is where the ingestion tier was rebuilt.\n"
        "I later described it as northwind   analytics in an interview.\n"
        "Northwind alone, without its full name, should never trigger the rule.\n"
        "NORTHWIND ANALYTICS shaped the rest of the career.\n"
    )
    rules = Guardrails(protected=["Northwind Analytics"])
    report = preview_against_master_cv(rules, cv)
    [match] = report.protected
    assert match.matched is True
    assert match.offsets == [
        cv.index("Northwind Analytics is"),
        cv.index("northwind   analytics"),
        cv.index("NORTHWIND ANALYTICS"),
    ]
    bare_first_word = cv.index("Northwind alone")
    assert bare_first_word not in match.offsets


def test_required_missing_is_reported_redraftable_false() -> None:
    rules = Guardrails(required=["Certified Kubernetes Administrator"])
    report = preview_against_master_cv(rules, CV)
    [match] = report.required
    assert match.matched is False
    assert match.offsets == []
    assert match.redraftable is False


def test_distinctions_are_not_matched_because_they_ship_as_guidance() -> None:
    cv = CV + "\nLed delivery; did not own the platform.\n"
    rules = Guardrails(distinctions=["Led delivery; did not own the platform."])
    report = preview_against_master_cv(rules, cv)
    assert report.forbidden == []
    assert report.required == []
    assert report.protected == []
    assert "distinctions" not in GuardrailPreviewReport.model_fields


def test_work_authorization_precedence_flags_a_disagreement_between_cv_and_drafting_line() -> None:
    cv = CV.replace(
        "Springfield, Illinois | jane@example.com | 555-0100",
        "Springfield, Illinois | jane@example.com | 555-0100\n\nAuthorized to work in Canada.",
    )
    rules = Guardrails(work_authorization="Authorized to work in the United States.")
    report = preview_against_master_cv(rules, cv)
    precedence = report.work_authorization_precedence
    assert precedence.cv_line == "Authorized to work in Canada."
    assert precedence.drafting_line == "Authorized to work in the United States."
    assert precedence.effective == "Authorized to work in the United States."
    assert precedence.drops_cv_line is True
