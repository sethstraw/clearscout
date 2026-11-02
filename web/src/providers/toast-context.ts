import { createContext } from "react";

import type { ToastTone } from "./Toast";

/**
 * The toast surface. Separate from the provider and the hook because a module
 * that mixes them is not a component boundary fast refresh can reason about.
 */
export interface ToastContextValue {
  showToast: (message: string, tone: ToastTone) => void;
}

export const ToastContext = createContext<ToastContextValue | null>(null);
