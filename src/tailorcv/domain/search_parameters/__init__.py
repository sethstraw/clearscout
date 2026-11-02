"""Typed search preferences and their deterministic matching vocabulary.

One person's answers about where they will work, what a role has to pay, which
titles are worth reading, and what the work is about. Nothing here reads a
posting or a CV: it is the vocabulary the readers elsewhere are written in.
"""

from tailorcv.domain.search_parameters.compensation import (
    ANNUAL_EQUIVALENT as ANNUAL_EQUIVALENT,
)
from tailorcv.domain.search_parameters.compensation import (
    BASES as BASES,
)
from tailorcv.domain.search_parameters.compensation import (
    MINIMUM_STANDING as MINIMUM_STANDING,
)
from tailorcv.domain.search_parameters.compensation import (
    SCALE_MARGIN as SCALE_MARGIN,
)
from tailorcv.domain.search_parameters.compensation import (
    TARGET_STANDING as TARGET_STANDING,
)
from tailorcv.domain.search_parameters.compensation import (
    CompensationBand as CompensationBand,
)
from tailorcv.domain.search_parameters.compensation import (
    CompensationBasis as CompensationBasis,
)
from tailorcv.domain.search_parameters.compensation import (
    CurrencyCode as CurrencyCode,
)
from tailorcv.domain.search_parameters.compensation import (
    basis_for as basis_for,
)
from tailorcv.domain.search_parameters.focus import (
    CLUSTER_KINDS as CLUSTER_KINDS,
)
from tailorcv.domain.search_parameters.focus import (
    MAX_CLUSTER_TERMS as MAX_CLUSTER_TERMS,
)
from tailorcv.domain.search_parameters.focus import (
    MAX_CLUSTERS as MAX_CLUSTERS,
)
from tailorcv.domain.search_parameters.focus import (
    Cluster as Cluster,
)
from tailorcv.domain.search_parameters.focus import (
    ClusterKind as ClusterKind,
)
from tailorcv.domain.search_parameters.focus import (
    Focus as Focus,
)
from tailorcv.domain.search_parameters.parameters import (
    JobSearchParameters as JobSearchParameters,
)
from tailorcv.domain.search_parameters.places import (
    ARRANGEMENTS as ARRANGEMENTS,
)
from tailorcv.domain.search_parameters.places import (
    OUTSIDE_DECLARED as OUTSIDE_DECLARED,
)
from tailorcv.domain.search_parameters.places import (
    Acceptance as Acceptance,
)
from tailorcv.domain.search_parameters.places import (
    Arrangement as Arrangement,
)
from tailorcv.domain.search_parameters.places import (
    CountryNames as CountryNames,
)
from tailorcv.domain.search_parameters.places import (
    Location as Location,
)
from tailorcv.domain.search_parameters.places import (
    WorkEligibility as WorkEligibility,
)
from tailorcv.domain.search_parameters.places import (
    WorkingTimeZone as WorkingTimeZone,
)
from tailorcv.domain.search_parameters.roles import (
    EMPLOYMENT_TYPES as EMPLOYMENT_TYPES,
)
from tailorcv.domain.search_parameters.roles import (
    LANES as LANES,
)
from tailorcv.domain.search_parameters.roles import (
    LEVELS as LEVELS,
)
from tailorcv.domain.search_parameters.roles import (
    MAX_ARCHETYPES as MAX_ARCHETYPES,
)
from tailorcv.domain.search_parameters.roles import (
    MAX_TITLES as MAX_TITLES,
)
from tailorcv.domain.search_parameters.roles import (
    EmployerArchetype as EmployerArchetype,
)
from tailorcv.domain.search_parameters.roles import (
    Employers as Employers,
)
from tailorcv.domain.search_parameters.roles import (
    Employment as Employment,
)
from tailorcv.domain.search_parameters.roles import (
    EmploymentType as EmploymentType,
)
from tailorcv.domain.search_parameters.roles import (
    Lane as Lane,
)
from tailorcv.domain.search_parameters.roles import (
    Seniority as Seniority,
)
from tailorcv.domain.search_parameters.roles import (
    TargetTitle as TargetTitle,
)
