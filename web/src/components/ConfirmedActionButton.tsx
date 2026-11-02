import type { PropsWithChildren } from "react";

import { useConfirmation } from "../confirmations";
import type { ConfirmationOptions } from "../providers/ConfirmDialog";

export function ConfirmedActionButton({
  ariaLabel,
  children,
  className,
  disabled,
  onConfirm,
  options,
}: PropsWithChildren<{
  ariaLabel?: string;
  className?: string;
  disabled?: boolean;
  onConfirm: () => void;
  options: ConfirmationOptions;
}>) {
  const confirm = useConfirmation();
  return (
    <button
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
