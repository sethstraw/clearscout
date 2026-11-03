import { useEffect } from "react";
import { Link } from "react-router-dom";

import { Icons } from "../components/Icons";

export type ToastTone = "error" | "success";
export type ToastAction = { label: string; to: string };

export interface ToastProps {
  id: string;
  message: string;
  type: ToastTone;
  action?: ToastAction;
  onDismiss: (id: string) => void;
}

export function Toast({ id, message, type, action, onDismiss }: ToastProps) {
  useEffect(() => {
    const timer = setTimeout(() => onDismiss(id), 5000);
    return () => clearTimeout(timer);
  }, [id, onDismiss]);

  return (
    <div className={`toast toast--${type}`} role="alert">
      <div className="toast-content">
        <p>{message}</p>
        {action && (
          <Link onClick={() => onDismiss(id)} to={action.to}>
            {action.label}
          </Link>
        )}
      </div>
      <button
        aria-label="Dismiss notification"
        className="toast-dismiss"
        onClick={() => onDismiss(id)}
        type="button"
      >
        <Icons.close height={14} width={14} />
      </button>
    </div>
  );
}

export function ToastContainer({ children }: { children: React.ReactNode }) {
  return <div className="toast-container">{children}</div>;
}
