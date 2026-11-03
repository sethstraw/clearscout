"""Content-address identity for a proposed name."""

from __future__ import annotations

from clearscout.domain.statements import derived_proposal_identity


def test_same_normalized_text_shares_identity() -> None:
    """Two proposals whose normalized text matches carry the same identity."""
    assert derived_proposal_identity("Product Manager") == derived_proposal_identity(
        "product manager"
    )
    assert derived_proposal_identity("Product-Manager") == derived_proposal_identity(
        "  product   manager  "
    )


def test_different_text_yields_different_identity() -> None:
    """A distinct name carries a distinct identity."""
    assert derived_proposal_identity("engineer") != derived_proposal_identity("designer")


def test_identity_is_short_and_stable() -> None:
    """A stored identity is 16 hex characters, matching the reference schema."""
    identity = derived_proposal_identity("Backend Engineer")
    assert len(identity) == 16
    assert all(character in "0123456789abcdef" for character in identity)
