import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { formatUsd } from "../../components/text";
import { api } from "../client";
import type { RequirementImportance, RequirementRulingBody } from "../client/types";
import { useWorkspaceMutation } from "./records";
import type { TaskCosts } from "./tasks";

export function useMasterCv(workspaceId: string, versionId = "") {
  return useQuery({
    queryKey: ["master-cv", workspaceId, versionId] as const,
    queryFn: () => api.masterCv(workspaceId, versionId),
    enabled: Boolean(workspaceId),
    // A workspace with no CV yet is an expected state, not a failure to retry.
    retry: false,
  });
}

export function useMasterCvVersions(workspaceId: string) {
  return useQuery({
    queryKey: ["master-cv-versions", workspaceId] as const,
    queryFn: () => api.masterCvVersions(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useMasterCvRequirementGaps(
  workspaceId: string,
  query: { minRaised?: number; importance?: readonly RequirementImportance[] } = {},
) {
  const minRaised = query.minRaised;
  const importance = query.importance ?? [];
  return useQuery({
    queryKey: [
      "master-cv-requirement-gaps",
      workspaceId,
      minRaised ?? null,
      [...importance].sort(),
    ] as const,
    queryFn: () => api.masterCvRequirementGaps(workspaceId, { minRaised, importance }),
    enabled: Boolean(workspaceId),
    placeholderData: (previous) => previous,
  });
}

export function useMasterCvDiff(workspaceId: string, fromVersion: string, toVersion: string) {
  return useQuery({
    queryKey: ["master-cv-diff", workspaceId, fromVersion, toVersion] as const,
    queryFn: () => api.masterCvDiff(workspaceId, fromVersion, toVersion),
    enabled: Boolean(workspaceId && fromVersion && toVersion && fromVersion !== toVersion),
  });
}

/** Ask what an unsaved draft would contradict. A mutation because it is a POST,
 *  not because it changes anything: the endpoint writes nothing. */
export function useMasterCvImpact(workspaceId: string) {
  return useMutation({
    mutationFn: (content: string) => api.masterCvImpact(workspaceId, content),
  });
}

export function useSaveMasterCv(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    ({ content, confirmReread = false }: { content: string; confirmReread?: boolean }) =>
      api.saveMasterCv(workspaceId, content, confirmReread),
  );
}

/** The key PaidAction prices a model re-read of a CV under. */
export const MASTER_CV_REREAD = "master_cv.reread";

/**
 * A model re-read of a CV is not a background task, so its estimate comes from
 * the master-CV route and is shaped the way PaidAction reads task costs.
 */
export function useMasterCvRereadCosts(workspaceId: string): TaskCosts {
  const estimate = useQuery({
    queryKey: ["master-cv-reread-estimate", workspaceId] as const,
    queryFn: () => api.masterCvRereadEstimate(workspaceId),
    enabled: Boolean(workspaceId),
  });
  return useMemo(() => {
    const data = estimate.data;
    const average = data?.average_usd;
    const runs = data?.past_rereads ?? 0;
    const each =
      average === null || average === undefined
        ? "no past re-read to estimate from"
        : `about ${formatUsd(average, { places: 2 })} per re-read`;
    const measured =
      average === null || average === undefined
        ? "No past re-read in this workspace to estimate from."
        : `About ${formatUsd(average, { places: 2 })} per re-read, from ${runs} past re-read${runs === 1 ? "" : "s"}.`;
    const cost = "One model re-read of the CV.";
    return {
      loaded: data !== undefined,
      isError: estimate.isError,
      isPaid: () => true,
      cost: () => cost,
      inline: () => each,
      summary: () => `${cost} ${measured}`,
      confirmation: () => `${measured} Charges may be higher.`,
    };
  }, [estimate.data, estimate.isError]);
}

export function useImportMasterCv(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    ({ file, confirmReread }: { file: File; confirmReread: boolean }) =>
      api.importMasterCv(workspaceId, file, true, confirmReread),
  );
}

export function useMasterCvRequirementRulings(workspaceId: string) {
  return useQuery({
    queryKey: ["requirement-rulings", workspaceId] as const,
    queryFn: () => api.listRequirementRulings(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

function useInvalidateRulingViews(workspaceId: string) {
  const client = useQueryClient();
  return async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["master-cv-requirement-gaps", workspaceId] }),
      client.invalidateQueries({ queryKey: ["requirement-rulings", workspaceId] }),
    ]);
  };
}

export function useSetRequirementRuling(workspaceId: string) {
  const invalidate = useInvalidateRulingViews(workspaceId);
  return useMutation({
    mutationFn: (variables: { requirementKey: string; body: RequirementRulingBody }) =>
      api.setRequirementRuling(workspaceId, variables.requirementKey, variables.body),
    onSuccess: invalidate,
  });
}

export function useClearRequirementRuling(workspaceId: string) {
  const invalidate = useInvalidateRulingViews(workspaceId);
  return useMutation({
    mutationFn: (requirementKey: string) => api.clearRequirementRuling(workspaceId, requirementKey),
    onSuccess: invalidate,
  });
}

export function useMasterCvProposals(workspaceId: string) {
  return useQuery({
    queryKey: ["master-cv-proposals", workspaceId] as const,
    queryFn: () => api.masterCvProposals(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useRefreshMasterCvProposals(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.refreshMasterCvProposals(workspaceId),
    onSuccess: () => client.invalidateQueries({ queryKey: ["master-cv-proposals", workspaceId] }),
  });
}

export function useRuleOnMasterCvProposal(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (variables: { proposalId: string; ruling: "accept" | "reject" }) =>
      api.ruleOnMasterCvProposal(workspaceId, variables.proposalId, variables.ruling),
    onSuccess: () => client.invalidateQueries({ queryKey: ["master-cv-proposals", workspaceId] }),
  });
}

export function useRefusalPatternProposals(workspaceId: string) {
  return useQuery({
    queryKey: ["refusal-pattern-proposals", workspaceId] as const,
    queryFn: () => api.refusalPatternProposals(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useRuleOnRefusalPattern(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (variables: { proposalId: string; ruling: "accept" | "reject" }) =>
      api.ruleOnRefusalPattern(workspaceId, variables.proposalId, variables.ruling),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["refusal-pattern-proposals", workspaceId] }),
  });
}
