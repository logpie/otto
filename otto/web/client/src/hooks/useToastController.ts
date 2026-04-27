import {useCallback, useEffect, useRef, useState} from "react";
import type {ToastState} from "../uiTypes";

const TOAST_DURATION_MS = 3200;

export function useToastController() {
  const [toast, setToast] = useState<ToastState | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const toastTimerRef = useRef<number | null>(null);

  const dismissToast = useCallback(() => {
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current);
      toastTimerRef.current = null;
    }
    setToast(null);
  }, []);

  const scheduleToastDismiss = useCallback((duration: number) => {
    if (toastTimerRef.current !== null) window.clearTimeout(toastTimerRef.current);
    toastTimerRef.current = window.setTimeout(() => {
      toastTimerRef.current = null;
      setToast(null);
    }, duration);
  }, []);

  const pauseToastDismiss = useCallback(() => {
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current);
      toastTimerRef.current = null;
    }
  }, []);

  const resumeToastDismiss = useCallback(() => {
    scheduleToastDismiss(TOAST_DURATION_MS);
  }, [scheduleToastDismiss]);

  const showToast = useCallback((message: string, severity: ToastState["severity"] = "information") => {
    if (severity === "error") setLastError(message);
    setToast({message, severity});
    scheduleToastDismiss(TOAST_DURATION_MS);
  }, [scheduleToastDismiss]);

  useEffect(() => () => {
    if (toastTimerRef.current !== null) window.clearTimeout(toastTimerRef.current);
  }, []);

  return {
    toast,
    lastError,
    setLastError,
    dismissToast,
    pauseToastDismiss,
    resumeToastDismiss,
    showToast,
  };
}
