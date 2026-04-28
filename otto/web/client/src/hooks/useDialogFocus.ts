import {useEffect, useRef} from "react";

export function useDialogFocus<T extends HTMLElement>(onCancel: () => void, disabled: boolean) {
  const dialogRef = useRef<T | null>(null);
  const onCancelRef = useRef(onCancel);
  const disabledRef = useRef(disabled);

  useEffect(() => {
    onCancelRef.current = onCancel;
  }, [onCancel]);

  useEffect(() => {
    disabledRef.current = disabled;
  }, [disabled]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    window.setTimeout(() => {
      const explicitTarget = dialog.querySelector<HTMLElement>("[data-dialog-initial-focus], [autofocus]");
      const selectedTab = dialog.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]:not([disabled])');
      const target = explicitTarget || selectedTab || focusableDialogElements(dialog)[0] || dialog;
      target.focus();
    }, 0);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !disabledRef.current) {
        event.preventDefault();
        onCancelRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = focusableDialogElements(dialog);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      const active = document.activeElement;
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    dialog.addEventListener("keydown", onKeyDown);
    return () => {
      dialog.removeEventListener("keydown", onKeyDown);
      previousFocus?.focus();
    };
  }, []);

  return dialogRef;
}

export function focusableDialogElements(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(
    [
      "a[href]",
      "button:not([disabled])",
      "textarea:not([disabled])",
      "input:not([disabled])",
      "select:not([disabled])",
      "[tabindex]:not([tabindex='-1'])",
    ].join(",")
  )).filter((node) => {
    const style = window.getComputedStyle(node);
    return style.visibility !== "hidden" && style.display !== "none";
  });
}
