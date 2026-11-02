import type { Artifact, TailoringReview } from "../../api/client/types";

type ReviewProposal = TailoringReview["proposals"][number];

/** One stored document, the row the job file list renders. */
export function documentArtifact(overrides: Partial<Artifact> = {}): Artifact {
  return {
    id: "artifact-a",
    workspace_id: "workspace-1",
    logical_type: "resume.markdown",
    mime_type: "text/markdown",
    byte_size: 100,
    sha256: "a".repeat(16),
    integrity_status: "verified",
    last_verified_at: "2026-08-07T12:00:00Z",
    created_at: "2026-08-07T12:00:00Z",
    created_by: "system",
    provenance: { line_citations: [] },
    related_entity_type: "job_posting",
    related_entity_id: "job-1",
    tailoring_run_id: null,
    artifact_family_id: "family-a",
    version_number: 1,
    supersedes_artifact_id: null,
    author_type: "model",
    archived_at: null,
    inline_viewable: false,
    editable: true,
    ...overrides,
  };
}

/** One drafted line awaiting a decision. */
export function reviewProposal(overrides: Partial<ReviewProposal> = {}): ReviewProposal {
  return {
    id: "proposal-1",
    proposal_key: "k1",
    slot: "summary",
    text: "Ran the platform team at Globex",
    digest: "c".repeat(64),
    source_ids: ["cv-1"],
    payload: { immutable: false, rationale: "" },
    decisions: [],
    ...overrides,
  };
}

/** One recorded decision on a drafted line. */
export function reviewDecision(
  proposal: ReviewProposal,
  decision: ReviewProposal["decisions"][number]["decision"],
): ReviewProposal["decisions"][number] {
  return {
    id: `decision-${proposal.id}`,
    review_proposal_id: proposal.id,
    decision,
    decided_at: "2026-08-07T12:00:00Z",
    manifest_sha256: "d".repeat(64),
    replacement_text: null,
    replacement_source_ids: [],
  };
}
