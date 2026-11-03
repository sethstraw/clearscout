"""Say what the model proposed, line by line, and bind it to what it read.

``changes.json`` is the record a later command reads back and ``changes.md`` is
the same content laid out for a person. Both are staged and promoted with the
rest of the run package, so an interrupted run leaves neither.

The rationale is assembled here rather than asked for, and the digest covers it,
so a rationale that could not be recomputed could not be proven untampered.
"""

from clearscout.application.tailoring.changes.proposals import (
    MIN_NEW_WORD as MIN_NEW_WORD,
)
from clearscout.application.tailoring.changes.proposals import (
    STOPWORDS as STOPWORDS,
)
from clearscout.application.tailoring.changes.proposals import (
    review as review,
)
from clearscout.application.tailoring.changes.record import (
    ACCEPT as ACCEPT,
)
from clearscout.application.tailoring.changes.record import (
    CHANGES_JSON as CHANGES_JSON,
)
from clearscout.application.tailoring.changes.record import (
    CHANGES_MD as CHANGES_MD,
)
from clearscout.application.tailoring.changes.record import (
    INPUT_NAMES as INPUT_NAMES,
)
from clearscout.application.tailoring.changes.record import (
    REJECT as REJECT,
)
from clearscout.application.tailoring.changes.record import (
    REVIEW as REVIEW,
)
from clearscout.application.tailoring.changes.record import (
    SCHEMA as SCHEMA,
)
from clearscout.application.tailoring.changes.record import (
    Excerpt as Excerpt,
)
from clearscout.application.tailoring.changes.record import (
    Kind as Kind,
)
from clearscout.application.tailoring.changes.record import (
    Proposal as Proposal,
)
from clearscout.application.tailoring.changes.record import (
    Review as Review,
)
from clearscout.application.tailoring.changes.record import (
    digest as digest,
)
from clearscout.application.tailoring.changes.record import (
    digest_of as digest_of,
)
from clearscout.application.tailoring.changes.record import (
    set_digest as set_digest,
)
from clearscout.application.tailoring.changes.report import (
    render as render,
)
from clearscout.application.tailoring.changes.slots import (
    emptied_roles as emptied_roles,
)
from clearscout.application.tailoring.changes.slots import (
    rebuild as rebuild,
)
