import { useContext } from "react";

import { ToastContext } from "./toast-context";

/**
 * Read the toast surface. Throws outside the provider rather than returning a
 * no-op, so a mutation cannot silently drop its error report.
 */
export function useToast() {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error("useToast must be used within ToastProvider");
  }
  return context;
}
