import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { errorNotice } from "../../components/freeMode";
import { useToast } from "../../providers/useToast";
import { api } from "../client";
import type { components } from "../schema";
import { queryKeys } from "./keys";

type NoteEntityType = components["schemas"]["NoteBody"]["entity_type"];

export function useNotes(workspaceId: string, entityType = "", entityId = "", search = "") {
  return useQuery({
    queryKey: queryKeys.notes(workspaceId, entityType, entityId, search),
    queryFn: ({ signal }) => api.notes(workspaceId, entityType, entityId, search, signal),
    enabled: Boolean(workspaceId),
  });
}

export function useCreateNote(
  workspaceId: string,
  scope: { entityType: NoteEntityType; entityId: string | null },
  options: { successMessage?: string } = {},
) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const mutation = useWorkspaceMutation(
    workspaceId,
    () =>
      api.createNote({
        workspace_id: workspaceId,
        entity_type: scope.entityType,
        entity_id: scope.entityId,
        title,
        body,
        pinned: false,
      }),
    options,
  );
  const submit = () =>
    mutation.mutate(undefined, {
      onSuccess: () => {
        setTitle("");
        setBody("");
      },
    });
  return { title, setTitle, body, setBody, mutation, submit };
}

export function useTags(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.tags(workspaceId),
    queryFn: ({ signal }) => api.tags(workspaceId, signal),
    enabled: Boolean(workspaceId),
  });
}

export function useWorkspaceMutation<TVariables, TResult>(
  workspaceId: string,
  mutationFn: (variables: TVariables) => Promise<TResult>,
  options: {
    onSuccess?: (result: TResult) => void | Promise<void>;
    successMessage?: string | ((result: TResult) => string | undefined);
  } = {},
) {
  const client = useQueryClient();
  const { showToast } = useToast();
  return useMutation({
    mutationFn,
    // Matched on the workspace rather than on a list of key prefixes, so a
    // query added later is refetched without being named here as well. Finished-task billing is
    // immutable and stays cached rather than joining every workspace mutation refetch.
    onSuccess: async (result) => {
      await client.invalidateQueries({
        predicate: (query) =>
          query.queryKey[1] === workspaceId && query.queryKey[0] !== "task-billing",
      });
      await options.onSuccess?.(result);
      const message =
        typeof options.successMessage === "function"
          ? options.successMessage(result)
          : options.successMessage;
      if (message) showToast(message, "success");
    },
    onError: (error: Error) => {
      const { message, action } = errorNotice(error);
      showToast(message, "error", action);
    },
  });
}
