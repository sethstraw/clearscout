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

/** True once health reports free mode; loading or a failed read is not free mode. */
export function useFreeMode(): boolean {
  return useHealth().data?.free_mode === true;
}

export function useStoreProviderKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ provider, key }: { provider: string; key: string }) =>
      api.storeProviderKey(provider, key),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.providers }),
        client.invalidateQueries({ queryKey: queryKeys.health }),
      ]),
  });
}

export function useForgetProviderKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (provider: string) => api.forgetProviderKey(provider),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.providers }),
        client.invalidateQueries({ queryKey: queryKeys.health }),
      ]),
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
