"""Facts read from a posting and CV, plus disagreements with extraction."""

from tailorcv.application.tailoring.fit.facts.disagreements import disagreements
from tailorcv.application.tailoring.fit.facts.residence import resident_countries
from tailorcv.application.tailoring.fit.facts.seniority import level_in

__all__ = ["disagreements", "level_in", "resident_countries"]
