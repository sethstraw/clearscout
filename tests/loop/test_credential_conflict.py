"""The paid-path credential conflict: which extracted gates may refuse a posting.

Only a verified blocking gate counts, and its verbatim posting span is matched,
so a refusal never rests on an unplaced model gate or a paraphrase that dropped a
disjunction the person could satisfy. Synthetic credentials stand in for the real
workspace conditions.
"""

from __future__ import annotations

from clearscout.application.tailoring.stages.jobspec import HardGate
from clearscout.application.tailoring.stages.spans import Span
from clearscout.application.tailoring.workflows.evaluations import _credential_conflict
from clearscout.domain import SpanStatus

TRADE = ("Requires an active locksmith or master electrician license", "not held")
_VERIFIED = Span(status=SpanStatus.VERIFIED, start=0, end=1)


def _gate(text: str, *, blocking: bool, verified: bool, quote: str = "") -> HardGate:
    return HardGate(
        kind="credential",
        text=text,
        blocking=blocking,
        quote_or_span=quote,
        source_span=_VERIFIED if verified else Span(),
    )


def test_a_verified_blocking_gate_matching_a_condition_is_a_conflict() -> None:
    gate = _gate("Active locksmith licensure is required.", blocking=True, verified=True)
    assert _credential_conflict([TRADE], (gate,)) == "Active locksmith licensure is required."


def test_the_conflict_names_the_posting_span_of_the_gate_that_refuses() -> None:
    """The verbatim span is what the reason quotes, and only the refusing gate is named."""
    unrelated = _gate("Must lift 20 pounds.", blocking=True, verified=True)
    refusing = _gate(
        "locksmith license required",
        blocking=True,
        verified=True,
        quote="Active locksmith license required at hire",
    )
    assert (
        _credential_conflict([TRADE], (unrelated, refusing))
        == "Active locksmith license required at hire"
    )


def test_an_unverified_blocking_gate_is_not_a_conflict() -> None:
    """An unplaced gate could be a model invention, so it must not force a refusal."""
    gate = _gate("Active locksmith licensure is required.", blocking=True, verified=False)
    assert _credential_conflict([TRADE], (gate,)) == ""


def test_a_non_blocking_gate_is_not_a_conflict() -> None:
    gate = _gate("Active locksmith licensure is required.", blocking=False, verified=True)
    assert _credential_conflict([TRADE], (gate,)) == ""


def test_a_disjunction_in_the_verbatim_span_abstains_even_if_the_paraphrase_lost_it() -> None:
    """The verbatim span, not the model's paraphrase, decides; its "or" abstains."""
    gate = _gate(
        "locksmith license required",
        blocking=True,
        verified=True,
        quote="locksmith or apprentice permit required",
    )
    assert _credential_conflict([TRADE], (gate,)) == ""


def test_no_conditions_never_conflicts() -> None:
    gate = _gate("Active locksmith licensure is required.", blocking=True, verified=True)
    assert _credential_conflict([], (gate,)) == ""
