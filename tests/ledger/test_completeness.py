from __future__ import annotations

from dataclasses import replace

import pytest

from clearscout.application.portability.completeness import (
    CompletenessInputs,
    evaluate_completeness,
)


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("posting_body", "posting_body"),
        ("score_evidence", "score_evidence"),
        ("fit_report", "fit_report"),
        ("observed_status_eligibility", "observed_status_eligibility"),
        ("normalized_identity", "normalized_identity"),
    ],
)
def test_each_completeness_failure_is_named_and_actionable(field: str, code: str) -> None:
    complete = CompletenessInputs(True, True, True, True, True)
    result = evaluate_completeness("job-1", replace(complete, **{field: False}))
    assert result["status"] == "incomplete"
    assert result["missing"] == [code]
    check = next(item for item in result["checks"] if item["code"] == code)
    assert not check["complete"]
    assert check["action"].startswith("/jobs/job-1?tab=")


def test_complete_result_has_no_missing_items() -> None:
    result = evaluate_completeness("job-1", CompletenessInputs(True, True, True, True, True))
    assert result["status"] == "complete"
    assert result["missing"] == []
