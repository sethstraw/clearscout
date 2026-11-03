"""The documentation workspace follows the current durable schema."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.seed_demo_workspace import seed

from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.relevance_review import ALLOWED_VERDICTS
from clearscout.application.workspaces.spend import SpendService
from clearscout.domain import ARTIFACT_LOGICAL_TYPES
from clearscout.infrastructure.persistence.database import Database
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher


def test_demo_workspace_populates_every_durable_surface(tmp_path: Path) -> None:
    """A schema addition must be seeded or named as intentionally transient."""
    data_root = tmp_path / "demo"

    counts = seed(data_root, force=False)

    assert counts == {
        "jobs": 8,
        "evaluations": 10,
        "application_events": 11,
        "tags": 3,
        "tag_assignments": 5,
        "notes": 2,
        "master_cv_versions": 3,
        "sources": 3,
        "source_scans": 3,
        "source_scan_outcomes": 29,
        "source_scan_continuations": 1,
        "source_scan_frontier_items": 3,
        "pending_discoveries": 22,
        "pending_discovery_disposition_events": 6,
        "missed_requirement_texts": 9,
        "search_proposals": 8,
        "requirement_rulings": 1,
        "posting_observation_diffs": 1,
        "posting_refusals": 4,
        "refusal_pattern_proposals": 1,
        "saved_views": 1,
        "guardrail_versions": 2,
        "learning_loop_policy_versions": 1,
        "prompt_versions": 2,
        "model_calls": 13,
        "tasks": 14,
        "guided_campaign_attempts": 2,
        "guided_campaign_branches": 3,
        "guided_campaign_captured_urls": 6,
        "pending_posting_contents": 8,
        "relevance_reviews": 5,
        "relevance_review_disputes": 1,
        "relevance_review_rejections": 1,
        "tailoring_runs": 1,
        "batch_runs": 1,
        "review_decisions": 3,
        "artifact_types": len(ARTIFACT_LOGICAL_TYPES),
    }
    with sqlite3.connect(data_root / "clearscout.sqlite3") as connection:
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        empty = {
            table
            for table in table_names
            # Identifiers come from this SQLite database's own schema catalog.
            if connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'  # noqa: S608
            ).fetchone()[0]
            == 0
        }
        artifact_types = {
            row[0] for row in connection.execute("SELECT DISTINCT logical_type FROM artifacts")
        }
        feed_items = [
            (Path(uri).name, mime_type, json.loads(provenance))
            for uri, mime_type, provenance in connection.execute(
                "SELECT storage_uri, mime_type, provenance FROM artifacts "
                "WHERE logical_type = 'posting.source.xml'"
            )
        ]
        page_provenance = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT provenance FROM artifacts WHERE logical_type = 'posting.source.html'"
            )
        ]
        task_statuses = {
            row[0] for row in connection.execute("SELECT DISTINCT status FROM background_tasks")
        }
        proposal_states = {
            row[0]
            for row in connection.execute(
                "SELECT state FROM derived_search_titles "
                "UNION SELECT state FROM derived_employer_archetypes "
                "UNION SELECT state FROM derived_clusters"
            )
        }
        resume_versions = [
            row[0]
            for row in connection.execute(
                "SELECT version_number FROM artifacts "
                "WHERE logical_type = 'resume.markdown' ORDER BY version_number"
            )
        ]
        review_decisions = {
            row[0] for row in connection.execute("SELECT DISTINCT decision FROM approval_decisions")
        }
        relevance_verdicts = set(
            connection.execute(
                "SELECT core_work_alignment, core_qualification_alignment, hard_condition, "
                "decisive_basis, recommendation FROM relevance_reviews"
            )
        )
        review_origins = {
            row[0] for row in connection.execute("SELECT DISTINCT origin FROM relevance_reviews")
        }
        relevance_review_task_types = {
            row[0]
            for row in connection.execute(
                "SELECT task.task_type FROM background_tasks AS task "
                "JOIN relevance_reviews AS review ON review.task_id = task.id"
            )
        }
        disposition_actions = [
            row[0]
            for row in connection.execute(
                "SELECT action FROM pending_discovery_disposition_events ORDER BY action"
            )
        ]
        current_dismissals = connection.execute(
            "SELECT COUNT(*) FROM pending_discovery_disposition_events AS event "
            "WHERE event.action = 'dismiss' AND NOT EXISTS ("
            "SELECT 1 FROM pending_discovery_disposition_events AS successor "
            "WHERE successor.supersedes_event_id = event.id)"
        ).fetchone()[0]
        restores_with_predecessors = connection.execute(
            "SELECT COUNT(*) FROM pending_discovery_disposition_events "
            "WHERE action = 'restore' AND supersedes_event_id IS NOT NULL"
        ).fetchone()[0]
        staged_fact_documents = [
            json.loads(row[0])
            for row in connection.execute("SELECT staged_facts FROM pending_discoveries")
        ]
        screened_reasons = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT reason FROM source_scan_outcomes WHERE terminal_stage = 'screened'"
            )
        }
        excluded_title_findings = connection.execute(
            "SELECT COUNT(*) FROM source_scan_outcomes "
            "WHERE title = 'Account Executive' AND terminal_stage = 'screened' "
            "AND screening_findings LIKE '%title_excluded%'"
        ).fetchone()[0]
        frontier_statuses = {
            row[0] for row in connection.execute("SELECT status FROM source_scan_frontier_items")
        }
        campaign_statuses = {
            row[0] for row in connection.execute("SELECT status FROM guided_campaign_states")
        }
        captured_outcomes = {
            row[0]
            for row in connection.execute("SELECT outcome FROM guided_campaign_captured_urls")
        }
        retained_content_states = set(
            connection.execute(
                "SELECT content_status, identity_status FROM pending_posting_contents"
            )
        )
        spend_stages = {row[0] for row in connection.execute("SELECT stage FROM model_calls")}
        cached_calls = {
            row[0] for row in connection.execute("SELECT DISTINCT cache_hit FROM model_calls")
        }
        [workspace_id] = [row[0] for row in connection.execute("SELECT id FROM workspaces")]
        pending_rows = connection.execute("SELECT COUNT(*) FROM pending_discoveries").fetchone()[0]

    assert empty == {
        "worker_presence",
        "master_cv_proposals",
        "evaluation_currentness_reviews",
        # The seed writes calls without provider bodies, and the cache archive
        # is a one-shot rescue nothing in a demo run performs.
        "model_call_envelopes",
        "response_cache_archive",
        # Only a multi-listing board detail read stores a membership; the demo reads none.
        "source_listing_memberships",
    }
    assert artifact_types == set(ARTIFACT_LOGICAL_TYPES)
    [(feed_item_name, feed_item_mime, feed_item_provenance)] = feed_items
    assert (feed_item_name, feed_item_mime) == ("item.xml", "application/xml")
    assert feed_item_provenance["reserialized"] is True
    assert all("reserialized" not in provenance for provenance in page_provenance)
    assert task_statuses == {"completed", "failed", "cancelled", "queued", "running"}
    assert proposal_states == {"proposed", "accepted", "rejected"}
    assert resume_versions == [1, 2]
    assert review_decisions == {"accept", "replace", "reject"}
    # Every verdict is a combination the current class definitions allow, and the
    # unjudged row carries no class at all.
    assert relevance_verdicts == {
        ("direct", "established", "passes", "core_match", "review"),
        ("direct", "partially_established", "passes", "transferable_match", "investigate"),
        ("absent", "absent", "passes", "core_gap", "unlikely"),
        ("direct", "established", "unresolved", "logistics_unknown", "investigate"),
        (None, None, None, None, None),
    }
    assert relevance_verdicts - {(None, None, None, None, None)} <= ALLOWED_VERDICTS
    assert review_origins == {"discovery.relevance_review"}
    assert relevance_review_task_types == {"discovery.relevance_review"}
    assert disposition_actions == ["dismiss"] * 5 + ["restore"]
    assert current_dismissals == 4
    assert restores_with_predecessors == 1
    assert frontier_statuses == {"completed", "failed", "pending"}
    assert campaign_statuses == {"completed"}
    assert captured_outcomes == {
        "staged",
        "known_posting",
        "registered_board",
        "unreadable",
        "unresolved",
    }
    assert retained_content_states == {
        ("readable", "verified"),
        ("session_required", "not_yet_verified"),
    }
    assert spend_stages >= {"match", "draft", "critic", "cover", "relevance_review", "vocabulary"}
    assert cached_calls == {0, 1}
    assert any(
        document["locations"]["complete"]
        and document["arrangements"]["complete"]
        and document["employment_types"]["complete"]
        and document["compensation"]["complete"]
        for document in staged_fact_documents
    )
    database = Database(f"sqlite+pysqlite:///{data_root / 'clearscout.sqlite3'}")
    pending = SourceScanService(
        database,
        storage=runtime_storage(database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_pending_discoveries(workspace_id)
    # Four rows left the queue through a dismissal; the rest are still selectable.
    assert len(pending) == counts["pending_discoveries"] - 4
    assert pending_rows == counts["pending_discoveries"]
    assert screened_reasons == {
        "insufficient_title_evidence",
        "location_outside_saved_places",
    }
    assert excluded_title_findings == 2
    # A seeded price the ledger cannot match leaves the summary's totals null, so
    # the spend page reads as blank however many cost records exist.
    spend = SpendService(database).spend_summary(workspace_id)
    assert spend.call_count == counts["model_calls"]
    assert spend.unpriced_calls == 0
    assert spend.total_usd is not None
    assert spend.total_usd > 0
    assert spend.by_stage and all(
        group.amount_usd is not None and group.amount_usd > 0 for group in spend.by_stage
    )
