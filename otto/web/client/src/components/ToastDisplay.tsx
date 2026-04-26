type ToastView = {
  message: string;
  severity: "information" | "warning" | "error";
};

/**
 * Shared toast renderer with hover-to-pause and manual dismiss.
 */
export function ToastDisplay({toast, onMouseEnter, onMouseLeave, onDismiss}: {
  toast: ToastView | null;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  onDismiss: () => void;
}) {
  if (!toast) return null;
  return (
    <div
      id="toast"
      className={`visible toast-${toast.severity}`}
      role="status"
      aria-live="polite"
      data-testid="toast"
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      <span className="toast-message">{toast.message}</span>
      <button
        type="button"
        className="toast-close"
        data-testid="toast-close"
        aria-label="Dismiss notification"
        onClick={onDismiss}
      >×</button>
    </div>
  );
}
