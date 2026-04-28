import {useCallback, useEffect, useRef, useState} from "react";
import type {Dispatch, MutableRefObject, SetStateAction} from "react";
import {api, runDetailUrl} from "../api";
import {
  LOG_BUFFER_MAX_BYTES,
  LOG_POLL_BACKOFF_MS,
  LOG_POLL_BASE_MS,
  appendToLogBuffer,
  bytesToString,
  countLines,
  initialLogState,
  type LogState,
} from "../logBuffer";
import type {ArtifactContentResponse, DiffResponse, LogsResponse, RunDetail} from "../types";
import type {InspectorMode, ToastState} from "../uiTypes";
import type {RouteState} from "../routeState";
import {writeRouteState} from "../routeState";
import {detailWasRemoved, errorMessage} from "../utils/missionControl";

type ShowToast = (message: string, severity?: ToastState["severity"]) => void;

export function useRunResources({
  selectedRunId,
  selectedRunIdRef,
  historyPageSize,
  currentRouteState,
  setSelectedRunId,
  showToast,
}: {
  selectedRunId: string | null;
  selectedRunIdRef: MutableRefObject<string | null>;
  historyPageSize: number;
  currentRouteState: () => RouteState;
  setSelectedRunId: Dispatch<SetStateAction<string | null>>;
  showToast: ShowToast;
}) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [logState, setLogState] = useState<LogState>(initialLogState);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorMode, setInspectorMode] = useState<InspectorMode>("proof");
  const [selectedArtifactIndex, setSelectedArtifactIndex] = useState<number | null>(null);
  const [artifactContent, setArtifactContent] = useState<ArtifactContentResponse | null>(null);
  const [diffContent, setDiffContent] = useState<DiffResponse | null>(null);
  const [logVisibilityTick, setLogVisibilityTick] = useState(0);

  const logOffsetRef = useRef(0);
  const logTextRef = useRef("");
  const logPollTimeoutRef = useRef<number | null>(null);
  const logPollVisibleRef = useRef(true);

  const resetRunResources = useCallback((options: {closeInspector?: boolean; resetMode?: boolean} = {}) => {
    setDetail(null);
    logOffsetRef.current = 0;
    logTextRef.current = "";
    setLogState(initialLogState);
    setArtifactContent(null);
    setDiffContent(null);
    setSelectedArtifactIndex(null);
    if (options.resetMode !== false) setInspectorMode("proof");
    if (options.closeInspector) setInspectorOpen(false);
  }, []);

  const refreshDetail = useCallback(async (runId: string) => {
    const url = runDetailUrl(runId, {historyPageSize});
    const nextDetail = await api<RunDetail>(url);
    if (selectedRunIdRef.current !== runId) return;
    setDetail(nextDetail);
  }, [historyPageSize, selectedRunIdRef]);

  const loadLogs = useCallback(async (runId: string, reset = false, force = false) => {
    if (!force && (inspectorMode !== "logs" || !inspectorOpen) && !reset) return;
    if (reset) {
      logOffsetRef.current = 0;
      logTextRef.current = "";
      setLogState({...initialLogState, status: "loading"});
    } else {
      setLogState((prev) => (prev.status === "idle" ? {...prev, status: "loading"} : prev));
    }
    const offset = reset ? 0 : logOffsetRef.current;
    try {
      const logs = await api<LogsResponse>(`/api/runs/${encodeURIComponent(runId)}/logs?offset=${offset}`);
      if (selectedRunIdRef.current !== runId) return;
      logOffsetRef.current = typeof logs.next_offset === "number" ? logs.next_offset : offset;
      const baseText = reset ? "" : logTextRef.current;
      const incoming = logs.text || "";
      const {text: nextText, droppedBytes: newlyDropped} = appendToLogBuffer(baseText, incoming, LOG_BUFFER_MAX_BYTES);
      logTextRef.current = nextText;
      const incomingLines = countLines(incoming);
      const incomingBytes = bytesToString(incoming);
      setLogState((prev) => {
        const droppedBytes = (reset ? 0 : prev.droppedBytes) + newlyDropped;
        const baseLines = reset ? 0 : prev.totalLines;
        const baseBytes = reset ? 0 : prev.totalBytes;
        const totalBytes = typeof logs.total_bytes === "number" && logs.total_bytes > 0
          ? logs.total_bytes
          : baseBytes + incomingBytes;
        return {
          text: nextText,
          totalLines: baseLines + incomingLines,
          totalBytes,
          droppedBytes,
          path: logs.path ?? null,
          status: logs.exists ? "ok" : "missing",
          error: null,
          lastUpdatedAt: Date.now(),
          pollIntervalMs: LOG_POLL_BASE_MS,
          consecutiveErrors: 0,
        };
      });
    } catch (error) {
      if (selectedRunIdRef.current !== runId) return;
      if (detailWasRemoved(error)) {
        setLogState((prev) => ({...prev, status: "missing", error: null}));
        return;
      }
      const message = errorMessage(error);
      setLogState((prev) => {
        const consecutiveErrors = prev.consecutiveErrors + 1;
        const idx = Math.min(consecutiveErrors - 1, LOG_POLL_BACKOFF_MS.length - 1);
        const backoff = LOG_POLL_BACKOFF_MS[idx] ?? LOG_POLL_BACKOFF_MS[LOG_POLL_BACKOFF_MS.length - 1] ?? LOG_POLL_BASE_MS;
        return {
          ...prev,
          status: "error",
          error: message,
          consecutiveErrors,
          pollIntervalMs: backoff,
        };
      });
    }
  }, [inspectorMode, inspectorOpen, selectedRunIdRef]);

  useEffect(() => {
    if (!selectedRunId) {
      resetRunResources({closeInspector: true});
      return;
    }
    resetRunResources();
    refreshDetail(selectedRunId).catch((error) => {
      if (detailWasRemoved(error)) {
        selectedRunIdRef.current = null;
        setSelectedRunId(null);
        resetRunResources({closeInspector: true});
        writeRouteState(currentRouteState(), "replace");
        return;
      }
      showToast(errorMessage(error), "error");
    });
  }, [currentRouteState, refreshDetail, resetRunResources, selectedRunId, selectedRunIdRef, setSelectedRunId, showToast]);

  useEffect(() => {
    const logsVisible = inspectorMode === "logs" && inspectorOpen;
    const previewActive = detail?.active === true;
    if (!selectedRunId || (!logsVisible && !previewActive)) return;
    const runIsActive = detail?.active === true;
    const shouldKeepPolling = runIsActive || logState.status === "loading" || logState.status === "idle" || logState.status === "error";
    if (!shouldKeepPolling) return;

    let cancelled = false;

    const scheduleNext = (delayMs: number) => {
      if (cancelled) return;
      logPollTimeoutRef.current = window.setTimeout(async () => {
        if (cancelled) return;
        if (!logPollVisibleRef.current) return;
        await loadLogs(selectedRunId, false, true);
        if (cancelled) return;
        scheduleNext(logState.pollIntervalMs);
      }, delayMs);
    };

    scheduleNext(logState.pollIntervalMs);

    return () => {
      cancelled = true;
      if (logPollTimeoutRef.current !== null) {
        window.clearTimeout(logPollTimeoutRef.current);
        logPollTimeoutRef.current = null;
      }
    };
  }, [inspectorMode, inspectorOpen, loadLogs, selectedRunId, detail?.active, logState.status, logState.pollIntervalMs]);

  useEffect(() => {
    const logsVisible = inspectorMode === "logs" && inspectorOpen;
    const previewActive = detail?.active === true;
    if (!selectedRunId || (!logsVisible && !previewActive)) return;
    if (logState.status !== "idle") return;
    void loadLogs(selectedRunId, true, true);
  }, [detail?.active, inspectorMode, inspectorOpen, loadLogs, selectedRunId, logState.status]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const update = () => {
      const visible = document.visibilityState !== "hidden";
      const wasVisible = logPollVisibleRef.current;
      logPollVisibleRef.current = visible;
      if (visible && !wasVisible) {
        setLogVisibilityTick((tick) => tick + 1);
      }
    };
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    if (logVisibilityTick === 0) return;
    if (!selectedRunId || inspectorMode !== "logs" || !inspectorOpen) return;
    void loadLogs(selectedRunId, false, true);
  }, [logVisibilityTick, selectedRunId, inspectorMode, inspectorOpen, loadLogs]);

  const loadArtifact = useCallback(async (index: number) => {
    const runId = selectedRunIdRef.current;
    if (!runId) return;
    setSelectedArtifactIndex(index);
    setInspectorMode("artifacts");
    setInspectorOpen(true);
    setArtifactContent(null);
    try {
      const content = await api<ArtifactContentResponse>(`/api/runs/${encodeURIComponent(runId)}/artifacts/${index}/content`);
      if (selectedRunIdRef.current !== runId) return;
      setArtifactContent(content);
    } catch (error) {
      if (detailWasRemoved(error) || selectedRunIdRef.current !== runId) return;
      showToast(errorMessage(error), "error");
    }
  }, [selectedRunIdRef, showToast]);

  const loadDiff = useCallback(async () => {
    const runId = selectedRunIdRef.current;
    if (!runId) return;
    setDiffContent(null);
    try {
      const content = await api<DiffResponse>(`/api/runs/${encodeURIComponent(runId)}/diff`);
      if (selectedRunIdRef.current !== runId) return;
      setDiffContent(content);
    } catch (error) {
      if (detailWasRemoved(error) || selectedRunIdRef.current !== runId) return;
      showToast(errorMessage(error), "error");
    }
  }, [selectedRunIdRef, showToast]);

  const showLogs = useCallback(() => {
    setInspectorOpen(true);
    setInspectorMode("logs");
    setArtifactContent(null);
    const runId = selectedRunIdRef.current;
    if (runId) void loadLogs(runId, true);
  }, [loadLogs, selectedRunIdRef]);

  const showArtifacts = useCallback(() => {
    setInspectorOpen(true);
    setInspectorMode("artifacts");
    setSelectedArtifactIndex(null);
    setArtifactContent(null);
  }, []);

  const showDiff = useCallback(() => {
    setInspectorOpen(true);
    setInspectorMode("diff");
    setArtifactContent(null);
    void loadDiff();
  }, [loadDiff]);

  const showProof = useCallback(() => {
    setInspectorOpen(true);
    setInspectorMode("proof");
  }, []);

  const showTryProduct = useCallback(() => {
    setInspectorOpen(true);
    setInspectorMode("try");
  }, []);

  return {
    detail,
    logState,
    inspectorOpen,
    inspectorMode,
    selectedArtifactIndex,
    artifactContent,
    diffContent,
    setInspectorOpen,
    setInspectorMode,
    setSelectedArtifactIndex,
    setArtifactContent,
    resetRunResources,
    refreshDetail,
    loadArtifact,
    loadDiff,
    showLogs,
    showArtifacts,
    showDiff,
    showProof,
    showTryProduct,
  };
}
