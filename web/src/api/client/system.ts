import { requestJson as request } from "../http";
import type {
  Health,
  OpenAiActualSpend,
  PriceRefresh,
  Provider,
  ReconciliationWindow,
} from "./types";

export const systemApi = {
  health: () => request<Health>("/health"),
  providers: () => request<Provider[]>("/providers"),
  storeProviderKey: (provider: string, key: string) =>
    request<Provider>(`/providers/${encodeURIComponent(provider)}/key`, {
      method: "PUT",
      body: JSON.stringify({ key }),
    }),
  forgetProviderKey: (provider: string) =>
    request<Provider>(`/providers/${encodeURIComponent(provider)}/key`, { method: "DELETE" }),
  refreshPrices: () => request<PriceRefresh>("/billing/prices/refresh", { method: "POST" }),
  reconcileOpenAiSpend: (window: ReconciliationWindow) =>
    request<OpenAiActualSpend>("/billing/openai/reconciliation", {
      method: "POST",
      body: JSON.stringify(window),
    }),
};
