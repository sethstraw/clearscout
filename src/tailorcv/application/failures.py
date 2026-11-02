"""Use-case failures translated independently by CLI and HTTP adapters."""

from __future__ import annotations

from tailorcv.shared.errors import TailorcvError


class ApplicationError(TailorcvError):
    """Base class for an expected application-service refusal."""


class NotFoundError(ApplicationError):
    """A requested durable entity does not exist in the selected workspace."""


class ConflictError(ApplicationError):
    """A write conflicts with immutable history or an idempotency key."""


FREE_MODE_REASON = "No provider API key is set, so paid actions are off."


class FreeModeError(ConflictError):
    """Paid work was requested while the configured provider has no API key."""


class ValidationError(ApplicationError):
    """A request violates a domain rule after structural validation."""


class UnsupportedPublisherRefreshError(ValidationError):
    """No free reader can check this row's publisher; distinct from a read failure."""


class PublisherUnverifiedRefreshError(ValidationError):
    """The publisher answered but would not confirm this row; distinct from a dead link."""


class EvaluationContentUnavailableError(ValidationError):
    """A full evaluation cannot acquire a verified publisher body without spending."""


class EvaluationIdentityMissingError(ValidationError):
    """The ledger records only a name the program copied, so an unnamed row is not evaluated."""


class CareerPageRefusedError(ValidationError):
    """Published policy or the site's access control refuses reading a career page."""

    def __init__(self, url: str, cause: str) -> None:
        """Name the page and the refusal."""
        super().__init__(f"The career page at {url} could not be read: {cause}.")
        self.url = url
        self.cause = cause


class UnavailableError(ApplicationError):
    """Required local infrastructure is unavailable until the user repairs it."""


class CareerPageUnreadableError(ApplicationError):
    """A career page the request depends on failed to load or may not be read yet."""

    def __init__(self, url: str, cause: str) -> None:
        """Name the page and the reason it could not be read."""
        super().__init__(f"The career page at {url} could not be read: {cause}.")
        self.url = url
        self.cause = cause


class AlreadyEvaluatedError(ApplicationError):
    """A pending discovery names a durable identity already carried by an evaluated posting."""

    def __init__(self, job_posting_id: str, evaluation_id: str, existing_decision: str) -> None:
        """Bind the collision to the job, evaluation, and decision the SPA can display."""
        super().__init__(
            f"Job {job_posting_id!r} already carries evaluation {evaluation_id!r} "
            f"({existing_decision})."
        )
        self.job_posting_id = job_posting_id
        self.evaluation_id = evaluation_id
        self.existing_decision = existing_decision
