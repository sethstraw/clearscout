"""Fetch one posting through the shared outbound policy."""

from __future__ import annotations

import time
from collections.abc import Callable

from clearscout.application.discovery.posting_fetch import FetchedPosting
from clearscout.application.discovery.posting_text import (
    closure_signal,
    publisher_closing_date,
)
from clearscout.application.failures import ValidationError
from clearscout.domain import utc_now
from clearscout.domain.records import PostingReadIntent
from clearscout.infrastructure.web_fetch.ingest import url as url_ingest
from clearscout.infrastructure.web_fetch.policy import (
    UNRESOLVED_DESTINATION_NOTE,
    DestinationUnresolvableError,
    InvalidFetchUrlError,
    PoliteHttpClient,
    RobotsDeniedError,
    RobotsUnavailableError,
)
from clearscout.shared.errors import IngestError

PUBLISHER_REFUSED_METHOD = "publisher_refused"
UNRESOLVED_DESTINATION_METHOD = "destination_unresolvable"


def _unresolved_destination(url: str) -> FetchedPosting:
    """Report a host that answered no public address as a retryable bodiless read."""
    return FetchedPosting(
        text=None,
        posting_status="unverified",
        method=UNRESOLVED_DESTINATION_METHOD,
        fetched_at=utc_now(),
        final_url=url,
        verification_note=UNRESOLVED_DESTINATION_NOTE,
    )


def _closure_signal_or_refusal(
    exc: IngestError,
    *,
    url: str,
    robots_denial: RobotsDeniedError | None,
) -> FetchedPosting:
    """Return a closed posting for a closure signal, or raise the right refusal.

    A robots refusal the read itself hit stays a policy refusal under any intent.
    """
    if closure_signal(str(exc)):
        return FetchedPosting(
            text=None,
            posting_status="closed",
            method="closure_signal",
            fetched_at=utc_now(),
            http_status=404 if "404" in str(exc) else None,
            final_url=url,
        )
    if isinstance(exc.__cause__, RobotsDeniedError):
        raise ValidationError(
            "The posting source disallows this refresh in robots.txt."
        ) from exc.__cause__
    if robots_denial is not None:
        raise ValidationError(
            "The posting source disallows this refresh in robots.txt."
        ) from robots_denial
    raise exc


class PolitePostingFetcher:
    """Production fetcher with robots, per-host serialization, and jitter."""

    def __init__(
        self,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] | None = None,
        client: PoliteHttpClient | None = None,
    ) -> None:
        """Configure the shared request policy or its timing seams."""
        self.client = client or PoliteHttpClient(
            user_agent=url_ingest.USER_AGENT,
            sleeper=sleeper,
            jitter=jitter,
        )

    def fetch(
        self,
        url: str,
        *,
        max_elapsed_seconds: float = url_ingest.MAX_POSTING_READ_SECONDS,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> FetchedPosting:
        """Respect robots and serialize work for the source host.

        The precheck applies robots under ``intent`` with the same rule the read does.
        """
        if max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds must be positive.")
        deadline_at = time.monotonic() + max_elapsed_seconds
        # robots.txt governs the human posting URL, but a board answers on a separate
        # permitted API path, so treat the precheck as a signal, not a veto; the HTML
        # fallback is still bound by the per-URL robots check inside the client.
        robots_denial: RobotsDeniedError | None = None
        try:
            self.client.ensure_allowed(url, max_elapsed_seconds=max_elapsed_seconds, intent=intent)
        except DestinationUnresolvableError:
            return _unresolved_destination(url)
        except InvalidFetchUrlError as exc:
            raise ValidationError(
                "Posting refresh requires a resolvable public HTTP(S) source URL."
            ) from exc
        except RobotsDeniedError as exc:
            robots_denial = exc
        except RobotsUnavailableError as exc:
            raise IngestError(str(exc)) from exc
        try:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise IngestError("The posting exceeded its total read deadline.")
            posting = url_ingest.from_url(
                url,
                client=self.client,
                max_elapsed_seconds=remaining,
                intent=intent,
                board_url=board_url,
            )
        except DestinationUnresolvableError:
            return _unresolved_destination(url)
        except url_ingest.PublisherPostingUnverifiedError as exc:
            return FetchedPosting(
                text=None,
                posting_status="unverified",
                method="publisher_unverified",
                fetched_at=utc_now(),
                final_url=url,
                verification_note=str(exc),
            )
        except url_ingest.PublisherReadRefusedError as exc:
            return FetchedPosting(
                text=None,
                posting_status="unverified",
                method=PUBLISHER_REFUSED_METHOD,
                fetched_at=utc_now(),
                http_status=exc.http_status,
                final_url=url,
                verification_note=str(exc),
            )
        except RobotsDeniedError as exc:
            raise ValidationError(
                "The posting source disallows this refresh in robots.txt."
            ) from exc
        except IngestError as exc:
            return _closure_signal_or_refusal(exc, url=url, robots_denial=robots_denial)
        fetched_at = utc_now()
        valid_through = getattr(posting, "valid_through", "")
        publisher_markup_present = getattr(posting, "publisher_markup_present", None)
        publisher_check = getattr(posting, "publisher_check", None)
        robots_basis = posting.robots_basis
        page_classification = posting.page_classification
        closing_date = publisher_closing_date(
            posting.text,
            valid_through=valid_through,
            source_url=posting.origin,
            observed_at=fetched_at,
        )
        return FetchedPosting(
            text=posting.text,
            posting_status="active",
            method=posting.method,
            fetched_at=fetched_at,
            publisher_markup_present=publisher_markup_present,
            title=posting.title,
            employer=posting.employer,
            final_url=posting.origin,
            publisher_check=publisher_check,
            robots_basis=robots_basis,
            page_classification=page_classification,
            source=posting.source,
            publisher_closing_date=closing_date if closing_date.statements else None,
            verification_note=(
                "Closing date passed; verify availability."
                if closing_date.has_passed(fetched_at) is True
                else ""
            ),
        )
