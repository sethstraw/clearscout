import { ApiError, FREE_MODE_PROBLEM } from "../api/http";
import type { ToastAction } from "../providers/Toast";

export const FREE_MODE_REASON = "Add a provider key to use paid actions.";
export const PROVIDER_SETTINGS_PATH = "/settings?tab=provider";
export const PROVIDER_SETTINGS_ACTION = {
  label: "Open provider settings",
  to: PROVIDER_SETTINGS_PATH,
};

export type ErrorNoticeContent = { message: string; action?: ToastAction };

/** The words and optional route a failed request shows the user. */
export function errorNotice(error: Error): ErrorNoticeContent {
  if (error instanceof ApiError && error.problemType === FREE_MODE_PROBLEM) {
    return { message: FREE_MODE_REASON, action: PROVIDER_SETTINGS_ACTION };
  }
  return { message: error.message };
}
