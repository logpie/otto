import type {MouseEvent as ReactMouseEvent, ReactNode} from "react";

import {Spinner} from "./Spinner";
import {useDialogFocus} from "../hooks/useDialogFocus";

export interface ConfirmState {
  title: string;
  body: string;
  bodyContent?: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "danger";
  requireCheckbox?: {label: string} | undefined;
  onConfirm: () => Promise<void>;
}

export function ConfirmDialog({confirm, pending, error, checkboxAck, onChangeCheckboxAck, onCancel, onConfirm}: {
  confirm: ConfirmState;
  pending: boolean;
  error: string | null;
  checkboxAck: boolean;
  onChangeCheckboxAck: (next: boolean) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const isDanger = confirm.tone === "danger";
  const confirmClass = isDanger ? "danger-button" : "primary";
  const dialogRef = useDialogFocus<HTMLDivElement>(onCancel, pending);
  const blockedByCheckbox = Boolean(confirm.requireCheckbox) && !checkboxAck;
  const submitDisabled = pending || blockedByCheckbox;

  const onBackdropClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    if (pending) return;
    onCancel();
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onBackdropClick}>
      <div
        ref={dialogRef}
        className={`confirm-dialog${isDanger ? " confirm-dialog-danger" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirmHeading"
        aria-describedby="confirmBody"
        data-tone={isDanger ? "danger" : "primary"}
        tabIndex={-1}
      >
        <header>
          <h2 id="confirmHeading">{confirm.title}</h2>
          {!isDanger && (
            <button
              type="button"
              data-testid="confirm-dialog-header-close"
              disabled={pending}
              onClick={onCancel}
            >Close</button>
          )}
        </header>
        <div id="confirmBody" className="confirm-body">
          {confirm.body && <p className="confirm-body-text">{confirm.body}</p>}
          {confirm.bodyContent}
        </div>
        {confirm.requireCheckbox && (
          <label className="confirm-ack" data-testid="confirm-dialog-ack">
            <input
              type="checkbox"
              data-testid="confirm-dialog-ack-checkbox"
              checked={checkboxAck}
              disabled={pending}
              onChange={(event) => onChangeCheckboxAck(event.target.checked)}
            />
            <span>{confirm.requireCheckbox.label}</span>
          </label>
        )}
        {error && (
          <div
            className="confirm-error"
            data-testid="confirm-dialog-error"
            role="alert"
            aria-live="assertive"
          >
            <strong>Action did not complete</strong>
            <span>{error}</span>
          </div>
        )}
        <footer>
          <button
            type="button"
            className={isDanger ? "confirm-dialog-cancel cancel-emphasis" : "confirm-dialog-cancel"}
            data-testid="confirm-dialog-cancel-button"
            disabled={pending}
            onClick={onCancel}
          >Cancel</button>
          <button
            className={confirmClass}
            type="button"
            data-testid="confirm-dialog-confirm-button"
            disabled={submitDisabled}
            aria-busy={pending}
            title={blockedByCheckbox ? "Tick the acknowledgement above to enable this action." : undefined}
            onClick={onConfirm}
          >
            {pending ? <><Spinner /> Working...</> : confirm.confirmLabel}
          </button>
        </footer>
      </div>
    </div>
  );
}
