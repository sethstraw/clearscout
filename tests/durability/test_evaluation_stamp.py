"""What the evaluation stamp is allowed to move for."""

from __future__ import annotations

import json

from tailorcv.application.tailoring.stages.match import Match, response_schema
from tailorcv.infrastructure.providers.base import json_schema


def test_the_matching_stamp_describes_what_a_provider_is_sent() -> None:
    """A field the program keeps for its own use does not move the stamp.

    The stamp is taken from the schema sent to the provider, not from
    ``EvidenceMap``, which the model never receives.
    """
    sent = json.dumps(json_schema(response_schema(("entry-1",), ("requirement-1",))))

    assert "requirement_id" in sent
    for internal in ("evidence_status", "gap_class"):
        assert internal in Match.model_fields
        assert internal not in sent
