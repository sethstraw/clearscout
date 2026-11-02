"""Readings of a posting against the person's saved preferences."""

from tailorcv.application.tailoring.fit.preferences.level import level_fit
from tailorcv.application.tailoring.fit.preferences.pay import pay_extractions, pay_fit
from tailorcv.application.tailoring.fit.preferences.pay_lines import (
    EXTRACTED_ONLY_STANDING,
    HOURLY_CEILING,
)
from tailorcv.application.tailoring.fit.preferences.pay_periods import NEARBY, SALARY_FLOOR
from tailorcv.application.tailoring.fit.preferences.place import arrangement_fit, place_fit
from tailorcv.application.tailoring.fit.preferences.time_zone import time_zone_fit
from tailorcv.application.tailoring.fit.preferences.travel import travel_fit

__all__ = [
    "EXTRACTED_ONLY_STANDING",
    "HOURLY_CEILING",
    "NEARBY",
    "SALARY_FLOOR",
    "arrangement_fit",
    "level_fit",
    "pay_extractions",
    "pay_fit",
    "place_fit",
    "time_zone_fit",
    "travel_fit",
]
