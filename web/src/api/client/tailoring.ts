import { requestJson as request } from "../http";
import type { ReviewDecision, TailoringPreflight, TailoringReview } from "./types";

export const tailoringApi = {
  tailoringPreflight: (
    workspaceId: string,
    jobId: string,
    approvalTailoringRunId?: string,
    signal?: AbortSignal,
  ) => {
    const approval = approvalTailoringRunId
      ? `&approval_tailoring_run_id=${encodeURIComponent(approvalTailoringRunId)}`
      : "";
    return request<TailoringPreflight>(
      `/workspaces/${encodeURIComponent(workspaceId)}/jobs/${encodeURIComponent(jobId)}/tailoring-preflight?cover_letter=true${approval}`,
      { signal },
    );
  },
  tailoringReview: (runId: string, workspaceId: string, signal?: AbortSignal) =>
    request<TailoringReview>(
      `/tailoring-runs/${encodeURIComponent(runId)}/review?workspace_id=${encodeURIComponent(workspaceId)}`,
      { signal },
    ),
  decideProposal: (
    proposalId: string,
    body: {
      workspace_id: string;
      proposal_digest: string;
      decision: "accept" | "reject" | "replace";
      replacement_text: string | null;
      replacement_source_ids: string[];
    },
  ) =>
    request<ReviewDecision>(`/review-proposals/${encodeURIComponent(proposalId)}/decisions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
