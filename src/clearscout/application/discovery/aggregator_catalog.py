"""The aggregators ClearScout ships as a starting point, and what each one asks for.

Every entry is an aggregator, which lists many employers and names none. No entry
is an employer board, and the order carries no preference. Each condition is this
repository's wording of what the operator publishes, paired with the address it is
published at. Nothing here states whether an adapter can read an entry.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StarterAggregator:
    """One shipped aggregator and the condition its operator publishes."""

    name: str
    url: str
    terms_url: str
    condition: str


STARTER_AGGREGATORS: tuple[StarterAggregator, ...] = (
    StarterAggregator(
        name="Arbeitnow",
        url="https://www.arbeitnow.com/",
        terms_url="https://www.arbeitnow.com/terms",
        condition=(
            "Provide a link back to arbeitnow.com on the platform that uses its API. "
            "Permission to use the API can be revoked at any time."
        ),
    ),
    StarterAggregator(
        name="Himalayas",
        url="https://himalayas.app/jobs",
        terms_url="https://himalayas.app/terms",
        condition=(
            "Every Apply action uses the applicationLink the API supplies, and Himalayas "
            "is clearly identified as the source with a visible link to the job or to "
            "himalayas.app. Jobs show only in apps you operate and are not passed on to "
            "other job boards, aggregators or job search engines. Cache where possible, "
            "stay within request limits, and remove expired jobs."
        ),
    ),
    StarterAggregator(
        name="Remote OK",
        url="https://remoteok.com/remote-jobs",
        terms_url="https://remoteok.com/legal",
        condition="Link back to Remote OK on any page or screen that shows its data.",
    ),
    StarterAggregator(
        name="USAJOBS",
        url="https://www.usajobs.gov/Search/Results",
        terms_url="https://developer.usajobs.gov/guides/terms-of-service",
        condition=(
            "Applications follow the official instructions in each announcement, and "
            "nothing may claim an application reached USAJOBS or an agency. OPM "
            "encourages naming USAJOBS as the source and linking the official announcement."
        ),
    ),
)
