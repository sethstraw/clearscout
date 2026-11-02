import {
  FloatingFocusManager,
  FloatingPortal,
  autoUpdate,
  flip,
  offset,
  shift,
  useClick,
  useDismiss,
  useFloating,
  useInteractions,
  useRole,
} from "@floating-ui/react";
import { useState } from "react";
import type { ReactNode } from "react";

/**
 * A click-opened explanation beside a control. `label` names the button for
 * screen readers; Escape, an outside click, or a second click closes it.
 */
export function Toggletip({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: setOpen,
    placement: "bottom-start",
    middleware: [offset(6), flip(), shift({ padding: 8 })],
    whileElementsMounted: autoUpdate,
  });
  const { getReferenceProps, getFloatingProps } = useInteractions([
    useClick(context),
    useDismiss(context),
    useRole(context, { role: "dialog" }),
  ]);
  const setReference = (node: HTMLElement | null) => refs.setReference(node);
  const setFloating = (node: HTMLElement | null) => refs.setFloating(node);
  return (
    <>
      <button
        aria-label={label}
        className="toggletip-button"
        ref={setReference}
        type="button"
        {...getReferenceProps()}
      >
        ?
      </button>
      {open && (
        <FloatingPortal>
          <FloatingFocusManager context={context} modal={false}>
            <div
              aria-label={label}
              className="toggletip"
              ref={setFloating}
              style={floatingStyles}
              {...getFloatingProps()}
            >
              {children}
            </div>
          </FloatingFocusManager>
        </FloatingPortal>
      )}
    </>
  );
}
