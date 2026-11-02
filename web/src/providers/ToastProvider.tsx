import { useState, useCallback } from "react";
import { Toast, ToastContainer } from "./Toast";
import type { ToastProps, ToastTone } from "./Toast";
import { ToastContext } from "./toast-context";

type ToastItem = Omit<ToastProps, "onDismiss">;

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const showToast = useCallback((message: string, tone: ToastTone) => {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts((current) => [...current, { id, message, type: tone }]);
  }, []);

  const dismissToast = useCallback((id: string) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ showToast }}>
      {children}
      {toasts.length > 0 && (
        <ToastContainer>
          {toasts.map((toast) => (
            <Toast key={toast.id} {...toast} onDismiss={dismissToast} />
          ))}
        </ToastContainer>
      )}
    </ToastContext.Provider>
  );
}
