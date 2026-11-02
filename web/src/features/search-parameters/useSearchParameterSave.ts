import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useSaveSearchParameters, useSearchParameters } from "../../api/hooks";
import type { JobSearchParameters } from "../../api/client";
import type { SearchParametersState } from "../../api/client/types";
import { ApiError } from "../../api/http";

export type ConflictRecovery = {
  workspaceId: string;
  reading: boolean;
  latest: SearchParametersState | null;
  error: string | null;
};

/** Save a search-parameter version against an expected version, with 409 recovery state. */
export function useSearchParameterSave(workspaceId: string) {
  const client = useQueryClient();
  const stored = useSearchParameters(workspaceId);
  const save = useSaveSearchParameters(workspaceId);
  const [recovery, setRecovery] = useState<ConflictRecovery | null>(null);

  const conflicted = save.error instanceof ApiError && save.error.status === 409;
  const activeRecovery = recovery?.workspaceId === workspaceId ? recovery : null;

  // Which scoring policy is in force follows the saved parameters.
  const refresh = async () => {
    const [result] = await Promise.all([
      stored.refetch(),
      client.invalidateQueries({ queryKey: ["score-weight-sets", workspaceId] }),
    ]);
    return result;
  };

  /** Resolves to the latest saved version, or null with the failure kept in recovery. */
  const readLatest = async () => {
    setRecovery({ workspaceId, reading: true, latest: null, error: null });
    const result = await refresh();
    const latest = result.isSuccess ? (result.data ?? null) : null;
    setRecovery((current) =>
      current?.workspaceId === workspaceId
        ? {
            workspaceId,
            reading: false,
            latest,
            error: result.isError
              ? result.error.message
              : latest
                ? null
                : "No saved preferences were returned.",
          }
        : current,
    );
    return latest;
  };

  const submit = (
    parameters: JobSearchParameters,
    expectedVersion: number,
    onSuccess?: () => void,
  ) => {
    setRecovery(null);
    save.mutate(
      { workspace_id: workspaceId, parameters, expected_version: expectedVersion },
      { onSuccess: () => onSuccess?.() },
    );
  };

  const reset = () => {
    setRecovery(null);
    save.reset();
  };

  /** Drop the unsaved choice only once the latest saved version is on screen. */
  const keepLatest = async () => {
    const latest = await readLatest();
    if (latest) reset();
    return Boolean(latest);
  };

  return {
    stored,
    save,
    conflicted,
    recovery: activeRecovery,
    readLatest,
    submit,
    reset,
    keepLatest,
  };
}
