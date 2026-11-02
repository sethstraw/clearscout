import { useId } from "react";
import type { PropsWithChildren } from "react";

/**
 * A disabled action exposes its reason through the control's accessible label.
 */
export function ActionWithReason({
  children,
  className = "button button--primary",
  disabledReason,
  onClick,
  pending = false,
  type = "button",
}: PropsWithChildren<{
  className?: string;
  disabledReason?: string;
  onClick?: () => void;
  pending?: boolean;
  type?: "button" | "submit";
}>) {
  const reasonId = useId();
  const blocked = Boolean(disabledReason);
  return (
    <div className="action-with-reason">
      <button
        aria-busy={pending || undefined}
        aria-describedby={blocked ? reasonId : undefined}
        className={className}
        disabled={blocked || pending}
        onClick={onClick}
        type={type}
      >
        {children}
      </button>
      {blocked && (
        <small className="field-note" id={reasonId}>
          {disabledReason}
        </small>
      )}
    </div>
  );
}
