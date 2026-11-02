"""Public application API for master-CV intake and immutable evidence versions."""

from tailorcv.application.master_cv.commands import register_master_cv
from tailorcv.application.master_cv.intake import (
    MAX_CV_BYTES,
    SUPPORTED_SUFFIXES,
    canonical_problem,
    parse_canonical_source,
)
from tailorcv.application.master_cv.service import MasterCvService

__all__ = [
    "MAX_CV_BYTES",
    "SUPPORTED_SUFFIXES",
    "MasterCvService",
    "canonical_problem",
    "parse_canonical_source",
    "register_master_cv",
]
