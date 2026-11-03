"""The approval workflow, one refusal at a time.

A reviewer may reject anything and may supply replacement text. The question is
whether either can put text on a resume that the master CV does not support, or
attach a person's judgment to lines they never saw.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from clearscout.application.tailoring import approval as approval_store
from clearscout.application.tailoring import changes
from clearscout.application.tailoring.layout import markdown_lines
from clearscout.application.tailoring.pipeline import MaterializedRun, RunCommand, RunResult, run
from clearscout.application.tailoring.pipeline.models import PageSelection
from clearscout.application.tailoring.stages.verify import CRITIC_CHECK
from clearscout.bootstrap.cli import app
from clearscout.shared.errors import ApprovalError
from clearscout.shared.results import Finding, Outcome
from tests.support.doubles import ScriptedProvider
from tests.support.payloads import script
from tests.support.suite import POSTING, SUITE_KEY_VARIABLE, settings_for

runner = CliRunner()

OTHER_POSTING = (
    "Principal Warehouse Engineer at Initech. "
    "You will own dimensional modelling, batch orchestration, and cost control. "
    "Required: PostgreSQL, Terraform, and eight years of experience. "
    "Hybrid in Chicago. No visa sponsorship is available. "
) * 4


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    provider = ScriptedProvider(script(sources))
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    return sources


def posting_at(tmp_path: Path, text: str = POSTING, name: str = "posting.txt") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def go(master_cv: Path, tmp_path: Path, **kwargs: Any) -> RunResult:
    pages = kwargs.pop("pages", 2)
    source = kwargs.pop("source", None) or posting_at(tmp_path)
    approve = kwargs.pop("approve", None)
    if approve is not None:
        kwargs["approval"] = approval_store.load(approve)
    kwargs.setdefault("force", True)
    return run(
        RunCommand(
            source=source,
            settings=settings_for(master_cv, tmp_path, pages),
            cache_enabled=False,
            **kwargs,
        )
    )


def materialized(result: RunResult) -> MaterializedRun:
    assert result.materialized is not None
    return result.materialized


def review_path(result: RunResult) -> Path:
    review = materialized(result).review
    assert review is not None
    return review


def reviewed(result: RunResult) -> dict[str, Any]:
    """Return the review as a reviewer hands it back after accepting every flagged line."""
    payload = unreviewed(result)
    for item in payload["proposals"]:
        if item["decision"] == changes.REVIEW:
            item["decision"] = changes.ACCEPT
    return payload


def unreviewed(result: RunResult) -> dict[str, Any]:
    payload = json.loads(review_path(result).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def write_manifest(tmp_path: Path, payload: dict[str, Any], name: str = "approval.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def at(payload: dict[str, Any], slot_suffix: str) -> dict[str, Any]:
    """Return the first proposal whose slot ends with ``slot_suffix``."""
    return next(item for item in payload["proposals"] if item["slot"].endswith(slot_suffix))


def critic_findings(result: RunResult) -> list[Finding]:
    return [item for item in result.verification.findings if item.check == CRITIC_CHECK]


def by_id(payload: dict[str, Any], proposal_id: str) -> dict[str, Any]:
    return next(item for item in payload["proposals"] if item["id"] == proposal_id)


def refusal(master_cv: Path, tmp_path: Path, payload: dict[str, Any], **kwargs: Any) -> str:
    with pytest.raises(ApprovalError) as error:
        go(master_cv, tmp_path, approve=write_manifest(tmp_path, payload), **kwargs)
    return str(error.value)


def test_every_run_writes_the_review_without_being_asked(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    result = go(master_cv, tmp_path)
    written = materialized(result)
    assert review_path(result).is_file()
    assert (written.directory / changes.CHANGES_MD).is_file()

    payload = reviewed(result)
    assert payload["proposals"]
    assert all(item["digest"] for item in payload["proposals"])


def test_an_approval_accepting_everything_writes_the_same_document(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    first = go(master_cv, tmp_path)
    approved = go(
        master_cv, tmp_path, approve=write_manifest(tmp_path, reviewed(first)), force=False
    )

    assert materialized(approved).document.read_text(encoding="utf-8") == materialized(
        first
    ).document.read_text(encoding="utf-8")
    assert materialized(approved).directory != materialized(first).directory
    assert "accepted every proposed line" in " ".join(approved.notes)
    # The scripted draft was never critiqued, and approving it does not change that.
    assert first.verification.critic is None
    assert critic_findings(first)
    assert critic_findings(approved) == critic_findings(first)


def test_a_rejected_line_leaves_the_document(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    first = go(master_cv, tmp_path)
    payload = reviewed(first)
    target = at(payload, "skills[0]")
    target["decision"] = "reject"

    approved = go(master_cv, tmp_path, approve=write_manifest(tmp_path, payload), force=False)
    text = materialized(approved).document.read_text(encoding="utf-8")

    assert target["text"].split(":", 1)[1].strip() not in text
    assert "1 proposed line(s) were rejected" in " ".join(approved.notes)


def test_the_stored_length_estimate_measures_the_approved_document(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A rejected line leaves the page, so the estimate made before approval no longer holds."""
    payload = reviewed(go(master_cv, tmp_path))
    at(payload, "skills[0]")["decision"] = "reject"

    approved = go(master_cv, tmp_path, approve=write_manifest(tmp_path, payload), force=False)
    written = materialized(approved)
    stored = json.loads((written.directory / "run.json").read_text(encoding="utf-8"))
    selection = PageSelection.model_validate(stored["selection"])

    assert sum(selection.drafted_lines.values()) == pytest.approx(
        markdown_lines(written.document.read_text(encoding="utf-8"))
    )


def test_replacement_text_that_passes_the_checks_reaches_the_page(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    first = go(master_cv, tmp_path)
    payload = reviewed(first)
    target = at(payload, "bullet[0]")
    target["decision"] = "reject"
    target["replacement"] = {
        "text": "Kept the ingestion tier honest for the analytics teams.",
        "source_ids": target["evidence_ids"],
    }

    approved = go(master_cv, tmp_path, approve=write_manifest(tmp_path, payload), force=False)
    text = materialized(approved).document.read_text(encoding="utf-8")

    assert "Kept the ingestion tier honest" in text
    assert approved.verification.passed
    assert approved.verification.critic is None
    assert [finding.outcome for finding in critic_findings(approved)] == [Outcome.WARN]
    assert "replacement text" in critic_findings(approved)[0].detail


def test_replacement_text_citing_nothing_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    target = at(payload, "bullet[0]")
    target["decision"] = "reject"
    target["replacement"] = {"text": "Ran the entire data organisation."}

    message = refusal(master_cv, tmp_path, payload, force=False)
    assert "sources cited" in message
    assert "cite no evidence" in message
    assert "clearscout discover" in message


def test_replacement_text_citing_evidence_outside_the_run_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    target = at(payload, "bullet[0]")
    target["decision"] = "reject"
    target["replacement"] = {"text": "Did the work.", "source_ids": ["b-notarealentry"]}

    message = refusal(master_cv, tmp_path, payload, force=False)
    assert "cited unknown evidence: b-notarealentry" in message


def test_a_figure_absent_from_the_cited_evidence_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    target = at(payload, "bullet[0]")
    target["decision"] = "reject"
    target["replacement"] = {
        "text": "Cut ingestion latency by 97 percent.",
        "source_ids": target["evidence_ids"],
    }

    message = refusal(master_cv, tmp_path, payload, force=False)
    assert "numbers traced" in message
    assert "97" in message


def test_a_replacement_naming_an_unknown_requirement_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    target = at(payload, "bullet[0]")
    target["decision"] = "reject"
    target["replacement"] = {
        "text": "Kept the ingestion tier honest.",
        "source_ids": target["evidence_ids"],
        "requirement_ids": ["r99"],
    }

    message = refusal(master_cv, tmp_path, payload, force=False)
    assert "does not have: r99" in message


def test_an_edit_to_identity_or_history_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    immutable = next(item for item in payload["proposals"] if item["immutable"])
    immutable["decision"] = "reject"

    message = refusal(master_cv, tmp_path, payload, force=False)
    assert "copied from your master CV" in message
    assert "Edit the master CV" in message


def test_a_rejection_of_an_already_omitted_line_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    omitted = next(
        item for item in payload["proposals"] if item["kind"] == "omitted" and not item["immutable"]
    )
    omitted["decision"] = "reject"

    assert "already omitted" in refusal(master_cv, tmp_path, payload, force=False)


def test_an_accepted_line_may_not_also_carry_replacement_text(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    at(payload, "bullet[0]")["replacement"] = {"text": "Something else."}

    assert "accepted and also carries" in refusal(master_cv, tmp_path, payload, force=False)


def test_an_unrecognised_decision_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    at(payload, "bullet[0]")["decision"] = "maybe"

    assert "Use 'accept' or 'reject'" in refusal(master_cv, tmp_path, payload, force=False)


def test_a_line_left_marked_for_review_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = unreviewed(go(master_cv, tmp_path))
    flagged = [item["id"] for item in payload["proposals"] if item["decision"] == changes.REVIEW]
    assert flagged

    assert f"{flagged[0]} has decision 'review'" in refusal(
        master_cv, tmp_path, payload, force=False
    )


def test_an_approval_against_a_changed_posting_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    other = posting_at(tmp_path, OTHER_POSTING, "other.txt")

    message = refusal(master_cv, tmp_path, payload, source=other, force=False)
    assert "the posting changed" in message.replace(",", "")


def test_an_approval_against_a_changed_master_cv_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    edited = tmp_path / "edited_cv.md"
    shutil.copy(master_cv, edited)
    edited.write_text(
        master_cv.read_text(encoding="utf-8").replace(
            "Rebuilt the ingestion tier", "Rebuilt the whole ingestion platform"
        ),
        encoding="utf-8",
    )

    message = refusal(edited, tmp_path, payload, force=False)
    assert "the master CV" in message
    assert "different run" in message


def test_an_approval_against_changed_settings_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))

    message = refusal(master_cv, tmp_path, payload, pages=3, force=False)
    assert "the settings" in message


def test_a_manifest_edited_after_review_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    at(payload, "bullet[0]")["text"] = "Ran the entire data organisation."

    message = refusal(master_cv, tmp_path, payload, force=False)
    assert "edited after it was written" in message


def test_a_tampered_rationale_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    at(payload, "bullet[0]")["rationale"] = "Trust me, this one is fine."

    assert "edited after it was written" in refusal(master_cv, tmp_path, payload, force=False)


def test_source_text_changed_after_the_preview_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A manifest can be made self-consistent again, and is still not about this run.

    Recomputing the digests after editing an excerpt defeats the check that the
    file was not touched. It does not defeat the comparison against a fresh
    review, which is the one that says the text a person read is not the text
    this run proposes.
    """
    payload = reviewed(go(master_cv, tmp_path))
    target = at(payload, "bullet[0]")
    target["evidence"][0]["text"] = "Something the master CV never said."
    content = {key: target[key] for key in changes.Proposal.__dataclass_fields__ if key != "digest"}
    content["evidence"] = [[item["source"], item["text"]] for item in target["evidence"]]
    target["digest"] = changes.digest_of(payload["run"], content)
    payload["digest"] = changes.set_digest(
        payload["run"], tuple(item["digest"] for item in payload["proposals"])
    )

    message = refusal(master_cv, tmp_path, payload, force=False)
    assert "stale" in message


def test_an_approval_missing_a_proposal_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    payload["proposals"].pop()

    message = refusal(master_cv, tmp_path, payload, force=False)
    assert "does not decide" in message


def test_an_approval_deciding_a_line_this_run_did_not_propose_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    invented = json.loads(json.dumps(payload["proposals"][0]))
    invented["id"] = "p99"
    payload["proposals"].append(invented)

    assert "did not propose" in refusal(master_cv, tmp_path, payload, force=False)


def test_a_reordered_approval_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    payload["proposals"].reverse()

    assert "different order" in refusal(master_cv, tmp_path, payload, force=False)


def test_an_approval_written_for_another_schema_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    payload["schema"] = changes.SCHEMA + 1

    assert "review schema" in refusal(master_cv, tmp_path, payload, force=False)


def test_an_unreadable_or_malformed_approval_is_refused_before_anything_is_spent(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    missing = tmp_path / "absent.json"
    with pytest.raises(ApprovalError, match="Cannot read"):
        go(master_cv, tmp_path, approve=missing)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ApprovalError, match="not valid JSON"):
        go(master_cv, tmp_path, approve=broken)

    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"proposals": []}), encoding="utf-8")
    with pytest.raises(ApprovalError, match="not a usable approval file"):
        go(master_cv, tmp_path, approve=wrong)

    listed = tmp_path / "listed.json"
    listed.write_text(json.dumps([{"id": "p01"}]), encoding="utf-8")
    with pytest.raises(ApprovalError, match="does not contain an approval object"):
        go(master_cv, tmp_path, approve=listed)

    assert not scripted


def test_rejecting_every_line_of_a_role_removes_its_heading(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    first = go(master_cv, tmp_path)
    payload = reviewed(first)
    role_key = next(
        item["slot"].split("/")[1]
        for item in payload["proposals"]
        if item["slot"].startswith("experience/")
    )
    for item in payload["proposals"]:
        if item["slot"].startswith(f"experience/{role_key}/"):
            item["decision"] = "reject"

    approved = go(master_cv, tmp_path, approve=write_manifest(tmp_path, payload), force=False)
    text = materialized(approved).document.read_text(encoding="utf-8")
    organization = next(role.organization for role in _roles(master_cv) if role.key == role_key)

    assert f"### {organization}" not in text
    assert "role(s) lost every line" in " ".join(approved.notes)
    assert approved.verification.passed
    # No heading anywhere in the document is left with nothing under it.
    assert _no_dangling_heading(text)


def test_rejecting_every_line_of_experience_is_refused(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    payload = reviewed(go(master_cv, tmp_path))
    for item in payload["proposals"]:
        if item["slot"].startswith("experience/") or item["slot"] == "earlier_career":
            item["decision"] = "reject"

    message = refusal(master_cv, tmp_path, payload, force=False)
    assert "no experience section" in message


def test_an_exact_replay_of_an_approval_produces_the_same_package(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    first = go(master_cv, tmp_path)
    payload = reviewed(first)
    at(payload, "skills[0]")["decision"] = "reject"
    manifest = write_manifest(tmp_path, payload)

    once = go(master_cv, tmp_path, approve=manifest, force=False)
    twice = go(master_cv, tmp_path, approve=manifest, force=True)

    assert materialized(once).document.read_text(encoding="utf-8") == materialized(
        twice
    ).document.read_text(encoding="utf-8")
    assert materialized(once).directory != materialized(twice).directory

    again = go(master_cv, tmp_path, approve=manifest, force=False)
    assert again.reused
    assert materialized(again).directory in {
        materialized(once).directory,
        materialized(twice).directory,
    }


def test_a_run_without_an_approval_is_not_reused_for_one_with_it(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    first = go(master_cv, tmp_path, force=False)
    payload = reviewed(first)
    at(payload, "skills[0]")["decision"] = "reject"
    approved = go(master_cv, tmp_path, approve=write_manifest(tmp_path, payload), force=False)

    assert not approved.reused
    assert materialized(approved).directory != materialized(first).directory

    manifest = json.loads(
        (materialized(approved).directory / "run.json").read_text(encoding="utf-8")
    )
    assert manifest["inputs"]["approval"]


def test_the_written_review_records_the_decisions_that_were_applied(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    first = go(master_cv, tmp_path)
    payload = reviewed(first)
    target = at(payload, "skills[0]")
    target["decision"] = "reject"
    approved = go(master_cv, tmp_path, approve=write_manifest(tmp_path, payload), force=False)

    written = reviewed(approved)
    assert by_id(written, target["id"])["decision"] == "reject"
    assert by_id(written, target["id"])["digest"] == target["digest"]


def test_the_command_line_applies_an_approval_end_to_end(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    config = tmp_path / "clearscout.toml"
    config.write_text(
        f'cv = "{master_cv}"\noutput_dir = "{tmp_path / "runs"}"\ncache = false\n',
        encoding="utf-8",
    )
    source = posting_at(tmp_path)
    first = runner.invoke(app, ["run", source, "--config", str(config)])
    assert first.exit_code == 0
    assert "every proposed line and what it rests on" in first.output

    written = next((tmp_path / "runs").rglob(changes.CHANGES_JSON))
    payload = json.loads(written.read_text(encoding="utf-8"))
    # A flagged line waits for an explicit decision, so the person settles each one.
    for proposal in payload["proposals"]:
        if proposal["decision"] == "review":
            proposal["decision"] = "accept"
    at(payload, "skills[0]")["decision"] = "reject"
    approval = write_manifest(tmp_path, payload)

    second = runner.invoke(
        app, ["run", source, "--config", str(config), "--approve", str(approval)]
    )
    assert second.exit_code == 0
    assert "1 proposed line(s) were rejected" in second.output


def test_the_command_line_reports_a_refused_approval_with_its_own_exit_code(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "clearscout.infrastructure.providers.base.provider_for",
        lambda name, key, timeout=None: ScriptedProvider({}),
    )

    def refuse(*_: Any, **__: Any) -> Any:
        raise ApprovalError("That approval is stale.")

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.run", refuse)
    config = tmp_path / "clearscout.toml"
    config.write_text(
        f'cv = "{master_cv}"\noutput_dir = "{tmp_path / "runs"}"\ncache = false\n',
        encoding="utf-8",
    )
    approval = write_manifest(tmp_path, {"schema": changes.SCHEMA, "proposals": []})
    result = runner.invoke(
        app,
        [
            "run",
            posting_at(tmp_path),
            "--config",
            str(config),
            "--approve",
            str(approval),
        ],
    )

    assert result.exit_code == 5
    assert "That approval is stale." in result.output
    assert "Traceback" not in result.output


def _roles(master_cv: Path) -> Any:
    from clearscout.application.master_cv.cv import parse_cv

    return parse_cv(master_cv).roles


def _no_dangling_heading(markdown: str) -> bool:
    """Return whether every role heading in ``markdown`` still has content under it."""
    lines = [line for line in markdown.splitlines() if line.strip() and line.strip() != "---"]
    return not any(
        line.startswith("### ") and (index == len(lines) - 1 or lines[index + 1].startswith("#"))
        for index, line in enumerate(lines)
    )
