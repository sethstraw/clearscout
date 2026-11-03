"""Facts read from a posting and CV, plus disagreements with extraction."""

from clearscout.application.tailoring.fit.facts.disagreements import disagreements
from clearscout.application.tailoring.fit.facts.residence import resident_countries
from clearscout.application.tailoring.fit.facts.seniority import level_in

__all__ = ["disagreements", "level_in", "resident_countries"]
