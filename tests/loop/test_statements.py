"""Which identity a statement keeps when its document is written again."""

from __future__ import annotations

from tailorcv.domain.statements import Known, Stated, carried_identities

ROLE = "acme-data-lead"
RXNORM = "Led RxNorm normalization, raising coverage from 8 per cent to 25 per cent."


def _stated(statement_id: str, text: str, scope: str = ROLE) -> Stated:
    return Stated(id=statement_id, scope=scope, text=text)


def _known(statement_id: str, key: str, text: str, scope: str = ROLE) -> Known:
    return Known(id=statement_id, key=key, scope=scope, text=text)


def test_an_unchanged_statement_keeps_its_identity() -> None:
    previous = [_known("b-1111", "key-rxnorm", RXNORM)]

    assert carried_identities(previous, [_stated("b-1111", RXNORM)]) == {"b-1111": "key-rxnorm"}


def test_a_reworded_statement_keeps_its_identity() -> None:
    reworded = "Led RxNorm normalisation, raising coverage from 8 per cent to 25 per cent."
    previous = [_known("b-1111", "key-rxnorm", RXNORM)]

    assert carried_identities(previous, [_stated("b-2222", reworded)]) == {"b-2222": "key-rxnorm"}


def test_a_different_claim_under_the_same_scope_starts_its_own_identity() -> None:
    previous = [_known("b-1111", "key-rxnorm", RXNORM)]
    other = "Maintained production Python named-entity-recognition services for document parsing."

    assert carried_identities(previous, [_stated("b-3333", other)]) == {}


def test_a_statement_moved_to_another_scope_starts_its_own_identity() -> None:
    previous = [_known("b-1111", "key-rxnorm", RXNORM)]

    assert carried_identities(previous, [_stated("b-4444", RXNORM, scope="elsewhere")]) == {}


def test_one_identity_is_claimed_once() -> None:
    previous = [_known("b-1111", "key-rxnorm", RXNORM)]
    near = RXNORM.replace("25 per cent", "26 per cent")
    nearer = RXNORM.replace("Led", "Drove")

    carried = carried_identities(previous, [_stated("b-5555", near), _stated("b-6666", nearer)])

    assert len(carried) == 1
    assert set(carried.values()) == {"key-rxnorm"}


def test_a_statement_the_previous_version_did_not_make_inherits_nothing() -> None:
    assert carried_identities([], [_stated("b-7777", RXNORM)]) == {}
