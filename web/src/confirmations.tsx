/* eslint-disable react-refresh/only-export-components -- provider and hook form one public seam */
import { createContext, useCallback, useContext, useRef, useState } from "react";
import type { PropsWithChildren } from "react";

import { ConfirmDialog } from "./providers/ConfirmDialog";
import type { ConfirmationOptions } from "./providers/ConfirmDialog";

type Confirm = (options: ConfirmationOptions) => Promise<boolean>;

const ConfirmationContext = createContext<Confirm | null>(null);

export function ConfirmationProvider({ children }: PropsWithChildren) {
  const [requestId, setRequestId] = useState(0);
  const [options, setOptions] = useState<ConfirmationOptions | null>(null);
  const resolver = useRef<((confirmed: boolean) => void) | null>(null);
  const confirm = useCallback<Confirm>((next) => {
    resolver.current?.(false);
    setRequestId((current) => current + 1);
    setOptions(next);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);
  const settle = useCallback((confirmed: boolean) => {
    resolver.current?.(confirmed);
    resolver.current = null;
    setOptions(null);
  }, []);
  return (
    <ConfirmationContext.Provider value={confirm}>
      {children}
      {options && (
        <ConfirmDialog
          key={requestId}
          onCancel={() => settle(false)}
          onConfirm={() => settle(true)}
          options={options}
        />
      )}
    </ConfirmationContext.Provider>
  );
}

export function useConfirmation() {
  const confirm = useContext(ConfirmationContext);
  if (!confirm) throw new Error("Confirmation context is unavailable.");
  return confirm;
}
