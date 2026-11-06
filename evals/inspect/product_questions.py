"""Closed product questions measured by evaluation tasks."""

from __future__ import annotations

from enum import StrEnum


class ProductQuestion(StrEnum):
    """The product questions the evaluation harness may measure."""

    SEARCH_VOCABULARY_PROPOSAL_USEFULNESS = "search_vocabulary_proposal_usefulness"
    POSTING_EXTRACTION_FIDELITY = "posting_extraction_fidelity"
    POSTING_SOURCE_RECOVERY = "posting_source_recovery"
    CAMPAIGN_SEARCH_COVERAGE = "campaign_search_coverage"
    REQUIREMENT_CLASSIFICATION = "requirement_classification"
    EVIDENCE_SELECTION = "evidence_selection"
    FIT_RECOMMENDATION = "fit_recommendation"
    FACTUAL_DRAFTING = "factual_drafting"
    DRAFT_CRITIQUE = "draft_critique"
    CV_IMPORT_FIDELITY = "cv_import_fidelity"
    SCREENSHOT_TRANSCRIPTION = "screenshot_transcription"
