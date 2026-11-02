import type { PropsWithChildren } from "react";

import { useConfirmation } from "../confirmations";
import type { ConfirmationOptions } from "../providers/ConfirmDialog";

export function ConfirmedActionButton({
  ariaDescribedBy,
  ariaLabel,
  children,
  className,
  disabled,
  onConfirm,
  options,
}: PropsWithChildren<{
  ariaDescribedBy?: string;
  ariaLabel?: string;
  className?: string;
  disabled?: boolean;
  onConfirm: () => void;
  options: ConfirmationOptions;
}>) {
  const confirm = useConfirmation();
  return (
    <button
      aria-describedby={ariaDescribedBy}
      aria-label={ariaLabel}
      className={className}
      disabled={disabled}
      onClick={() => {
        void confirm(options).then((approved) => {
          if (approved) onConfirm();
        });
      }}
      type="button"
    >
      {children}
    </button>
  );
}
