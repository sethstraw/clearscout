import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../client";
import type { ReconciliationWindow } from "../client/types";
import { queryKeys } from "./keys";

export function useHealth() {
  return useQuery({ queryKey: queryKeys.health, queryFn: api.health, refetchInterval: 10_000 });
}

export function useProviders() {
  return useQuery({ queryKey: queryKeys.providers, queryFn: api.providers });
}

export function useStoreProviderKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ provider, key }: { provider: string; key: string }) =>
      api.storeProviderKey(provider, key),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.providers }),
  });
}

export function useForgetProviderKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (provider: string) => api.forgetProviderKey(provider),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.providers }),
  });
}

// A free billing read, not a paid call, so the result is shown inline and no
// cached query is invalidated.
export function useReconcileOpenAiSpend() {
  return useMutation({
    mutationFn: (window: ReconciliationWindow) => api.reconcileOpenAiSpend(window),
  });
}

// A refresh changes what later calls are priced at, not the amounts already
// recorded, so the result is shown inline and no cached query is invalidated.
export function useRefreshPrices() {
  return useMutation({ mutationFn: () => api.refreshPrices() });
}
