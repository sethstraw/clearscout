import { useEffect, useRef } from "react";
import type { ReactNode } from "react";

import { captureFocusOrigin, returnFocus } from "./focusReturn";

export type ConfirmationOptions = {
  title: string;
  description: string;
  details?: ReactNode;
  confirmLabel: string;
  danger?: boolean;
};

export function ConfirmDialog({
  options,
  onCancel,
  onConfirm,
}: {
  options: ConfirmationOptions;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const opener = captureFocusOrigin();
    cancelRef.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
      if (event.key !== "Tab") return;
      const focusable = [
        ...(dialogRef.current?.querySelectorAll<HTMLElement>(
          "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary",
        ) ?? []),
      ].filter((element) => {
        let details = element.closest("details");
        while (details) {
          if (!details.open && element !== details.querySelector(":scope > summary")) return false;
          details = details.parentElement?.closest("details") ?? null;
        }
        return !element.closest("[hidden], [inert]");
      });
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => {
      window.removeEventListener("keydown", handleKey);
      returnFocus(opener);
    };
  }, [onCancel]);
  return (
    <div className="dialog-backdrop" role="presentation">
      <section
        aria-describedby="confirm-description"
        aria-labelledby="confirm-title"
        aria-modal="true"
        className="confirm-dialog"
        ref={dialogRef}
        role="dialog"
      >
        <h2 id="confirm-title">{options.title}</h2>
        <p id="confirm-description">{options.description}</p>
        {options.details}
        <div className="button-row">
          <button className="button button--quiet" onClick={onCancel} ref={cancelRef} type="button">
            Cancel
          </button>
          <button
            className={`button ${options.danger ? "button--danger-solid" : "button--primary"}`}
            onClick={onConfirm}
            type="button"
          >
            {options.confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}
