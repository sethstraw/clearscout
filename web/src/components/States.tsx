import type { PropsWithChildren } from "react";

import { ApiError } from "../api/client";

export function LoadingState({ label = "Loading" }: { label?: string }) {
  return (
    <div aria-live="polite" className="loading-state" role="status">
      <span className="spinner" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorState({ error }: { error: unknown }) {
  const detail = error instanceof Error ? error.message : "Something unexpected happened.";
  const correlationId = error instanceof ApiError ? error.correlationId : undefined;
  return (
    <div className="error-state" role="alert">
      <strong>TailorCV could not load this view.</strong>
      <span>{detail}</span>
      {correlationId && <small>Correlation ID: {correlationId}</small>}
    </div>
  );
}

export function EmptyState({ children }: PropsWithChildren) {
  return <div className="empty-state">{children}</div>;
}
