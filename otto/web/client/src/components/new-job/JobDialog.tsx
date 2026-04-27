import {FormEvent, useEffect, useRef, useState} from "react";
import type {MouseEvent as ReactMouseEvent} from "react";
import {api, buildQueuePayload} from "../../api";
import {Spinner} from "../Spinner";
import {useDialogFocus} from "../../hooks/useDialogFocus";
import type {
  CertificationPolicy,
  ExecutionMode,
  HistoryItem,
  ImproveSubcommand,
  JobCommand,
  LandingItem,
  PlanningMode,
  QueueResult,
  StateResponse,
} from "../../types";
import {titleCase} from "../../utils/format";
import {errorMessage} from "../../utils/missionControl";

interface PriorRunOption {
  run_id: string;
  branch: string;
  label: string;
}

export function collectPriorRunOptions(
  landingItems: LandingItem[],
  historyItems: HistoryItem[],
): PriorRunOption[] {
  const seen = new Set<string>();
  const options: PriorRunOption[] = [];

  // Landing items first — they're the freshest and explicitly "ready to
  // land", which means the branch is on disk and uncollided.
  for (const item of landingItems) {
    if (item.landing_state !== "ready") continue;
    const runId = (item.run_id || "").trim();
    const branch = (item.branch || "").trim();
    if (!runId || !branch || seen.has(runId)) continue;
    seen.add(runId);
    options.push({
      run_id: runId,
      branch,
      label: priorRunLabel({
        summary: item.summary,
        branch,
        task_id: item.task_id,
        run_id: runId,
        when: null,
      }),
    });
  }

  // History items: terminal-success build/improve only.
  for (const row of historyItems) {
    if (row.terminal_outcome !== "success") continue;
    const familyOk = row.command === "build"
      || row.command === "improve"
      || row.command?.startsWith("improve.");
    if (!familyOk) continue;
    const runId = (row.run_id || "").trim();
    const branch = (row.branch || "").trim();
    if (!runId || !branch || seen.has(runId)) continue;
    seen.add(runId);
    options.push({
      run_id: runId,
      branch,
      label: priorRunLabel({
        summary: row.summary || row.intent || "",
        branch,
        task_id: row.queue_task_id,
        run_id: runId,
        when: row.completed_at_display,
      }),
    });
    if (options.length >= 25) break;
  }

  return options;
}

export function priorRunLabel(args: {
  summary: string | null;
  branch: string;
  task_id: string | null;
  run_id: string;
  when: string | null;
}): string {
  const summary = (args.summary || "").trim();
  const trimmed = summary.length > 60 ? summary.slice(0, 57) + "…" : summary;
  const headline = trimmed || args.task_id || args.branch || args.run_id;
  const suffix = args.when ? ` · ${args.when}` : "";
  return `${headline} (${args.branch})${suffix}`;
}

export function JobDialog({project, dirtyFiles, priorRunOptions, onClose, onQueued, onError}: {
  project: StateResponse["project"] | undefined;
  dirtyFiles: string[];
  // W3-CRITICAL-1: list of prior runs the operator can iterate on. Sourced
  // from the parent (landing.items + history.items, filtered to terminal
  // success runs with a recorded branch). When empty, the dialog tells the
  // operator there's nothing to improve and disables Submit for command=
  // "improve" so the silent-fork-from-main bug cannot recur.
  priorRunOptions: PriorRunOption[];
  onClose: () => void;
  onQueued: (message?: string) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [command, setCommand] = useState<JobCommand>("build");
  const [subcommand, setSubcommand] = useState<"bugs" | "feature" | "target">("bugs");
  const [intent, setIntent] = useState("");
  const [taskId, setTaskId] = useState("");
  const [after, setAfter] = useState("");
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("split");
  const [planning, setPlanning] = useState<PlanningMode>("direct");
  const [specFilePath, setSpecFilePath] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [buildProvider, setBuildProvider] = useState("");
  const [buildModel, setBuildModel] = useState("");
  const [buildEffort, setBuildEffort] = useState("");
  const [certifierProvider, setCertifierProvider] = useState("");
  const [certifierModel, setCertifierModel] = useState("");
  const [certifierEffort, setCertifierEffort] = useState("");
  const [fixProvider, setFixProvider] = useState("");
  const [fixModel, setFixModel] = useState("");
  const [fixEffort, setFixEffort] = useState("");
  const [certification, setCertification] = useState<CertificationPolicy>("");
  const [rounds, setRounds] = useState("");
  const [targetConfirmed, setTargetConfirmed] = useState(false);
  const [status, setStatus] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // W3-CRITICAL-1: which prior run the improve job should iterate on.
  // Auto-selects the freshest option when the dropdown becomes available
  // so the most-likely choice is one click away. Empty string means "no
  // selection" — Submit stays disabled so the server never silently falls
  // back to main.
  const [priorRunId, setPriorRunId] = useState<string>("");
  const priorRunOptionsAvailable = priorRunOptions.length > 0;
  // For non-improve commands the dropdown isn't rendered — treat it as
  // satisfied so it doesn't block the submit button.
  const priorRunMissing =
    command === "improve" && (!priorRunOptionsAvailable || !priorRunId.trim());
  // Whether the Advanced section should be programmatically opened. The
  // pre-submit summary "Edit" link sets this so users get one-click access
  // to the provider/model/effort fields without scrolling through Otto
  // jargon. mc-audit codex-first-time-user.md #2.
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const advancedRef = useRef<HTMLDetailsElement | null>(null);
  // mc-audit codex-destructive-action-safety #7: provider-spend jobs queue
  // immediately on Submit and the watcher dispatches with no cancel window.
  // Add a 3-second grace banner with a [Cancel] button between submit and
  // the actual POST. The form/dialog stay editable during the grace period.
  const [pendingSeconds, setPendingSeconds] = useState<number | null>(null);
  const pendingTimerRef = useRef<number | null>(null);
  const pendingTickRef = useRef<number | null>(null);
  const pendingCancelledRef = useRef<boolean>(false);
  const dialogRef = useDialogFocus<HTMLFormElement>(onClose, submitting);
  const targetNeedsConfirmation = Boolean(project?.dirty);
  // ALL commands now require a non-empty intent (or focus). Codex flagged
  // that improve/certify could queue blank — equivalent to "do something
  // unspecified", which is never what a user means. mc-audit
  // codex-first-time-user.md #8.
  const intentRequired = !intent.trim();
  const specFileRequired = command === "build" && planning === "spec-file" && !specFilePath.trim();
  const submitDisabled =
    submitting
    || intentRequired
    || specFileRequired
    || (targetNeedsConfirmation && !targetConfirmed)
    || priorRunMissing;

  // Pre-submit summary fields. We resolve the visible "will run with" line
  // by combining the user's selection with the project's defaults. mc-audit
  // codex-first-time-user.md #2.
  const summary = jobRunSummary({command, subcommand, project, provider, model, effort, certification, rounds});
  const effectiveRounds = effectiveRoundLimit(project, rounds);
  const improveOneRoundWarning =
    command === "improve"
    && executionMode === "split"
    && effectiveRounds !== null
    && effectiveRounds <= 1;
  const intentLabelMap: Record<JobCommand, string> = {
    build: "Intent",
    improve: "Focus",
    certify: "Focus",
  };
  const intentPlaceholderMap: Record<JobCommand, string> = {
    build: "Describe what you want Otto to build.",
    improve: "Describe what to refine, fix, or extend in the existing run.",
    certify: "Describe what to verify in the existing run.",
  };
  const commandHelpMap: Record<JobCommand, string> = {
    build: "Build new work from your description.",
    improve: "Iterate on an existing run (refine, fix bugs, extend feature).",
    certify: "Verify an existing run against acceptance criteria.",
  };

  useEffect(() => {
    setTargetConfirmed(false);
  }, [project?.path]);

  useEffect(() => {
    if (!certificationPolicyAllowed(command, subcommand, certification)) {
      setCertification("");
    }
    if (command !== "build") {
      setPlanning("direct");
      setSpecFilePath("");
    }
  }, [certification, command, subcommand]);

  // W3-CRITICAL-1: when the operator switches to "improve" and there is
  // exactly one obvious prior run (or the previously-picked id is no
  // longer in the list), auto-select the freshest. The list is sorted
  // most-recent-first by collectPriorRunOptions, so options[0] is the
  // last terminal-success build/improve.
  useEffect(() => {
    if (command !== "improve") return;
    const first = priorRunOptions[0];
    if (!first) {
      if (priorRunId) setPriorRunId("");
      return;
    }
    const stillValid = priorRunOptions.some((option) => option.run_id === priorRunId);
    if (!stillValid) {
      setPriorRunId(first.run_id);
    }
  }, [command, priorRunOptions, priorRunId]);

  // Sync the <details> open state when the user clicks the "Edit" link in
  // the summary. The native attribute change has to land on the DOM node so
  // the disclosure widget actually toggles open without a re-render race.
  useEffect(() => {
    const el = advancedRef.current;
    if (!el) return;
    if (advancedOpen && !el.open) el.open = true;
  }, [advancedOpen]);

  // mc-audit codex-destructive-action-safety #7: clear timers on unmount so a
  // half-finished grace countdown can't fire after the dialog closes.
  useEffect(() => {
    return () => {
      if (pendingTimerRef.current !== null) {
        window.clearTimeout(pendingTimerRef.current);
        pendingTimerRef.current = null;
      }
      if (pendingTickRef.current !== null) {
        window.clearInterval(pendingTickRef.current);
        pendingTickRef.current = null;
      }
    };
  }, []);

  async function performQueue(): Promise<void> {
    setStatus("queueing");
    try {
      const priorRunForPayload = command === "improve" ? priorRunId.trim() : "";
      const payloadArgs: Parameters<typeof buildQueuePayload>[0] = {
        command,
        subcommand,
        intent: intent.trim(),
        taskId: taskId.trim(),
        after,
        executionMode,
        provider,
        model,
        effort,
        buildProvider,
        buildModel,
        buildEffort,
        certifierProvider,
        certifierModel,
        certifierEffort,
        fixProvider,
        fixModel,
        fixEffort,
        certification,
        rounds,
        planning,
        specFilePath: specFilePath.trim(),
      };
      if (priorRunForPayload) payloadArgs.priorRunId = priorRunForPayload;
      const payload = buildQueuePayload(payloadArgs);
      const result = await api<QueueResult>(`/api/queue/${command}`, {method: "POST", body: JSON.stringify(payload)});
      await onQueued(result.message);
    } catch (error) {
      const message = errorMessage(error);
      setStatus(message);
      onError(message);
    } finally {
      setSubmitting(false);
    }
  }

  function cancelGraceWindow(): void {
    pendingCancelledRef.current = true;
    if (pendingTimerRef.current !== null) {
      window.clearTimeout(pendingTimerRef.current);
      pendingTimerRef.current = null;
    }
    if (pendingTickRef.current !== null) {
      window.clearInterval(pendingTickRef.current);
      pendingTickRef.current = null;
    }
    setPendingSeconds(null);
    setSubmitting(false);
    setStatus("Queueing cancelled.");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (intentRequired) {
      setStatus(`${intentLabelMap[command]} is required.`);
      return;
    }
    if (targetNeedsConfirmation && !targetConfirmed) {
      setStatus("Confirm the dirty target project above.");
      return;
    }
    if (priorRunMissing) {
      setStatus(
        priorRunOptionsAvailable
          ? "Select a prior run to improve."
          : "No prior runs. Run a build first."
      );
      return;
    }
    // mc-audit codex-destructive-action-safety #7: 3-second grace window.
    // Show a banner with countdown; user can hit [Cancel] to abort before
    // the POST fires. Form fields stay editable so the user can fix a typo
    // and resubmit. After the grace expires (and no cancel), POST fires.
    setSubmitting(true);
    pendingCancelledRef.current = false;
    setPendingSeconds(3);
    setStatus("Queueing in 3s. Cancel to edit.");
    pendingTickRef.current = window.setInterval(() => {
      setPendingSeconds((prev) => {
        if (prev === null) return null;
        const next = Math.max(0, prev - 1);
        if (next > 0) {
          setStatus(`Queueing in ${next}s. Cancel to edit.`);
        }
        return next;
      });
    }, 1000);
    pendingTimerRef.current = window.setTimeout(() => {
      if (pendingTickRef.current !== null) {
        window.clearInterval(pendingTickRef.current);
        pendingTickRef.current = null;
      }
      pendingTimerRef.current = null;
      setPendingSeconds(null);
      if (pendingCancelledRef.current) return;
      void performQueue();
    }, 3000);
  }

  const dirtyPreview = dirtyFiles.slice(0, 5);
  const dirtyOverflow = Math.max(0, dirtyFiles.length - dirtyPreview.length);

  // mc-audit live W11-CRITICAL-2: clicking the backdrop dismisses the dialog
  // (standard modal UX). Without this, a dialog whose Submit was silently
  // rejected (e.g. dirty-target guard) appears closed-but-stuck — the
  // backdrop intercepts every subsequent page click. Skip dismissal while a
  // POST is mid-flight or a grace window is counting down so the user can
  // never lose an in-flight job by missing the dialog and hitting backdrop.
  const onBackdropClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    if (submitting) return;
    onClose();
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onBackdropClick}>
      <form
        ref={dialogRef}
        className="job-dialog job-palette"
        role="dialog"
        aria-modal="true"
        aria-labelledby="jobDialogHeading"
        aria-describedby={status ? "jobDialogStatus" : undefined}
        tabIndex={-1}
        onSubmit={(event) => void submit(event)}
      >
        <header className="job-palette-head">
          <h2 id="jobDialogHeading">{command === "improve" ? "Improve" : command === "certify" ? "Certify" : "New job"}</h2>
          <button
            type="button"
            className="job-palette-close"
            data-testid="job-dialog-close-button"
            aria-label="Close"
            onClick={onClose}
          ><span aria-hidden="true">×</span><span className="sr-only">Close</span></button>
        </header>
        {/* Intent textarea is THE primary field — front and center, autofocused. */}
        <label className="job-palette-intent" aria-label={intentLabelMap[command]}>
          <textarea
            value={intent}
            data-testid="job-dialog-intent"
            rows={4}
            autoFocus
            placeholder={intentPlaceholderMap[command]}
            aria-describedby={submitDisabled && !submitting && pendingSeconds === null ? "jobDialogValidationHint" : undefined}
            aria-invalid={intentRequired ? true : undefined}
            onChange={(event) => setIntent(event.target.value)}
            onKeyDown={(event) => {
              // W8-IMPORTANT-1: documented power-user shortcut. Cmd+Enter
              // (mac) / Ctrl+Enter (linux/windows) submits the dialog from
              // the textarea — universal in code-gen tools and in MC's own
              // accelerator catalogue. The default `<textarea>` swallows
              // Enter as a newline, so we intercept here. The submit goes
              // through the form's onSubmit so the validation gating (grace
              // window, dirty-target confirm, prior-run requirement) stays
              // in one place.
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                if (submitDisabled) return;
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
          />
        </label>
        {/* Command pills — Build / Improve / Certify, sentence-style not select. */}
        <div className="job-palette-commands" role="radiogroup" aria-label="Command">
          {(["build", "improve", "certify"] as const).map((cmd) => (
            <button
              key={cmd}
              type="button"
              role="radio"
              aria-checked={command === cmd}
              data-testid={cmd === "build" ? "job-command-select" : `job-command-${cmd}`}
              className={`job-palette-pill ${command === cmd ? "active" : ""}`}
              onClick={() => setCommand(cmd)}
            >
              {cmd === "build" ? "Build" : cmd === "improve" ? "Improve" : "Certify"}
            </button>
          ))}
          {/* Improve sub-mode pills, only shown when Improve is selected. */}
          {command === "improve" && (
            <span className="job-palette-submodes">
              {(["bugs", "feature", "target"] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  role="radio"
                  aria-checked={subcommand === mode}
                  className={`job-palette-pill job-palette-pill-sub ${subcommand === mode ? "active" : ""}`}
                  onClick={() => setSubcommand(mode)}
                  data-testid={`job-improve-mode-${mode}`}
                >
                  {mode === "bugs" ? "Bugs" : mode === "feature" ? "Feature" : "Target"}
                </button>
              ))}
            </span>
          )}
        </div>
        <p className="field-hint job-command-help" data-testid="job-command-help">
          {commandHelpMap[command]}
        </p>
        {/* Compact target line — shown subtly. Dirty confirm flow stays. */}
        <div className={`job-palette-target ${project?.dirty ? "is-dirty" : ""}`} data-testid="job-dialog-summary">
          <span>Target</span>
          <code title={project?.path || ""}>{project?.name || project?.path || "loading"}</code>
          <span>on</span>
          <code>{project?.branch || "-"}</code>
          {project?.dirty ? <em>· dirty</em> : null}
          <span className="job-palette-target-summary" data-testid="job-dialog-summary-text">{summary}</span>
          <button
            type="button"
            className="job-palette-summary-edit"
            data-testid="job-dialog-summary-edit"
            onClick={() => {
              setAdvancedOpen(true);
              window.requestAnimationFrame(() => advancedRef.current?.scrollIntoView({block: "nearest"}));
            }}
          >
            Edit options
          </button>
        </div>
        {targetNeedsConfirmation && (
          <div className="job-palette-dirty">
            {dirtyPreview.length ? (
              <div className="target-dirty-files" data-testid="job-dialog-dirty-files" aria-label="Uncommitted files">
                <strong>Uncommitted changes ({dirtyFiles.length})</strong>
                <ul>
                  {dirtyPreview.map((path) => <li key={path}>{path}</li>)}
                  {dirtyOverflow > 0 && <li>+{dirtyOverflow} more</li>}
                </ul>
              </div>
            ) : null}
            <label className="check-label target-confirm">
              <input
                checked={targetConfirmed}
                data-testid="target-project-confirm"
                type="checkbox"
                onChange={(event) => setTargetConfirmed(event.target.checked)}
              />
              I understand this dirty project may affect the queued work
            </label>
          </div>
        )}
        {/* Prior run selector — shown only for Improve. */}
        {command === "improve" && priorRunOptionsAvailable && (
          <label className="job-palette-prior">
            <span>Prior run</span>
            <select
              data-testid="job-prior-run-select"
              value={priorRunId}
              onChange={(event) => setPriorRunId(event.target.value)}
            >
              {priorRunOptions.map((option) => (
                <option key={option.run_id} value={option.run_id}>{option.label}</option>
              ))}
            </select>
          </label>
        )}
        {command === "improve" && !priorRunOptionsAvailable && (
          <span className="field-hint" data-testid="job-prior-run-empty">No prior runs. Run a build first.</span>
        )}
        {submitDisabled && !submitting && pendingSeconds === null && (
          <p id="jobDialogValidationHint" className="job-dialog-validation" data-testid="job-dialog-validation-hint" aria-live="polite">
            {intentRequired
              ? command === "build"
                ? "Describe the requested outcome to queue."
                : `Describe the ${intentLabelMap[command].toLowerCase()} to queue.`
              : specFileRequired
              ? "Enter the spec file path."
              : targetNeedsConfirmation && !targetConfirmed
              ? "Confirm the dirty target project above."
              : priorRunMissing
              ? (priorRunOptionsAvailable
                  ? "Select a prior run to improve."
                  : "No prior runs. Run a build first.")
              : null}
          </p>
        )}
        <details
          className="job-advanced"
          ref={advancedRef}
          open={advancedOpen}
          onToggle={(event) => setAdvancedOpen((event.target as HTMLDetailsElement).open)}
        >
          <summary>Advanced options</summary>
          {command !== "certify" && (
            <label>Execution mode
              <select data-testid="job-execution-mode-select" value={executionMode} onChange={(event) => setExecutionMode(event.target.value as ExecutionMode)}>
                <option value="split">Reliable split mode</option>
                <option value="agentic">Agentic single session</option>
              </select>
              <span className="field-hint">{executionModeHelp(executionMode, command)}</span>
            </label>
          )}
          {command === "build" && (
            <label>Planning
              <select data-testid="job-planning-select" value={planning} onChange={(event) => setPlanning(event.target.value as PlanningMode)}>
                <option value="direct">Direct build</option>
                <option value="spec-review">Generate spec for review</option>
                <option value="spec-auto">Generate spec and approve automatically</option>
                <option value="spec-file">Use spec file</option>
              </select>
              <span className="field-hint">{planningHelp(planning)}</span>
            </label>
          )}
          {command === "build" && planning === "spec-file" && (
            <label>Spec file path
              <input
                data-testid="job-spec-file-input"
                value={specFilePath}
                type="text"
                placeholder="/path/to/spec.md"
                onChange={(event) => setSpecFilePath(event.target.value)}
              />
            </label>
          )}
          <div className="field-grid">
            <label>Task id
              <input value={taskId} type="text" placeholder="auto-generated" onChange={(event) => setTaskId(event.target.value)} />
            </label>
            <label>After
              <input value={after} type="text" placeholder="optional dependencies" onChange={(event) => setAfter(event.target.value)} />
            </label>
          </div>
          <label>Max rounds
            <input
              data-testid="job-rounds-input"
              value={rounds}
              type="number"
              min={1}
              max={50}
              placeholder={project?.defaults?.max_certify_rounds ? `inherit: ${project.defaults.max_certify_rounds}` : "inherit"}
              onChange={(event) => setRounds(event.target.value)}
            />
            <span className={`field-hint ${improveOneRoundWarning ? "field-warning" : ""}`} data-testid="job-rounds-help">
              {improveOneRoundWarning
                ? "One split improve round only evaluates existing work. Use 2+ rounds to let Otto fix/improve and re-check."
                : "Maximum certify/evaluate rounds for this queued job."}
            </span>
          </label>
          <div className="field-grid">
            <label>Provider
              <select data-testid="job-provider-select" value={provider} onChange={(event) => setProvider(event.target.value)}>
                <option value="">{providerDefaultLabel(project)}</option>
                <option value="codex">Codex</option>
                <option value="claude">Claude</option>
              </select>
            </label>
            <label>Reasoning effort
              <select data-testid="job-effort-select" value={effort} onChange={(event) => setEffort(event.target.value)}>
                <option value="">{effortDefaultLabel(project)}</option>
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="max">Max</option>
              </select>
            </label>
          </div>
          <label>Model
            <input value={model} type="text" placeholder={modelDefaultPlaceholder(project)} onChange={(event) => setModel(event.target.value)} />
          </label>
          <details className="job-agent-routing">
            <summary>{executionMode === "agentic" && command !== "certify" ? "Agent session" : "Phase routing"}</summary>
            {command !== "certify" && executionMode === "agentic" && (
              <div className="static-field">
                <span>Routing model</span>
                <strong>Single session</strong>
                <p className="field-hint">Use Provider, Model, and Reasoning above for the main agent. Split-only phase overrides are hidden because agentic mode does not run separate build/certify/fix calls.</p>
              </div>
            )}
            {command === "build" && executionMode === "split" && (
              <PhaseRoutingFields
                label="Build"
                testKey="build"
                provider={buildProvider}
                model={buildModel}
                effort={buildEffort}
                onProvider={setBuildProvider}
                onModel={setBuildModel}
                onEffort={setBuildEffort}
              />
            )}
            {(command === "certify" || executionMode === "split") && (
              <PhaseRoutingFields
                label={command === "improve" ? "Certifier / evaluator" : "Certifier"}
                testKey="certifier"
                provider={certifierProvider}
                model={certifierModel}
                effort={certifierEffort}
                onProvider={setCertifierProvider}
                onModel={setCertifierModel}
                onEffort={setCertifierEffort}
              />
            )}
            {command !== "certify" && executionMode === "split" && (
              <PhaseRoutingFields
                label={command === "improve" ? "Improver / fixer" : "Fix"}
                testKey="fix"
                provider={fixProvider}
                model={fixModel}
                effort={fixEffort}
                onProvider={setFixProvider}
                onModel={setFixModel}
                onEffort={setFixEffort}
              />
            )}
          </details>
          {certificationOptions(command, subcommand, project).length > 0 ? (
            <label>Certification
              <select
                data-testid="job-certification-select"
                value={certification}
                onChange={(event) => setCertification(event.target.value as CertificationPolicy)}
              >
                {certificationOptions(command, subcommand, project).map((option) => (
                  <option key={option.value || "inherit"} value={option.value}>{option.label}</option>
                ))}
              </select>
              <span className="field-hint">{certificationHelp(command, subcommand, certification, project)}</span>
            </label>
          ) : (
            <div className="static-field" data-testid="job-certification-static">
              <span>Evaluation policy</span>
              <strong>{staticCertificationLabel(command, subcommand)}</strong>
            </div>
          )}
        </details>
        {pendingSeconds !== null && (
          <div
            className="job-grace-banner"
            data-testid="job-grace-banner"
            role="status"
            aria-live="polite"
          >
            <span>
              Queueing in <strong data-testid="job-grace-countdown">{pendingSeconds}s</strong>… edit fields above or cancel to abort.
            </span>
            <button
              type="button"
              className="job-grace-cancel"
              data-testid="job-grace-cancel-button"
              onClick={cancelGraceWindow}
            >
              Cancel
            </button>
          </div>
        )}
        <footer>
          <span id="jobDialogStatus" className="muted" aria-live="polite">{status}</span>
          <button
            className="primary"
            type="submit"
            data-testid="job-dialog-submit-button"
            disabled={submitDisabled}
            aria-busy={submitting}
            title={!submitting && submitDisabled ? (
              intentRequired
                ? `Describe the ${intentLabelMap[command].toLowerCase()} to queue.`
                : specFileRequired
                ? "Enter the spec file path."
                : priorRunMissing
                ? (priorRunOptionsAvailable
                    ? "Select a prior run to improve."
                    : "No prior runs. Run a build first.")
                : "Confirm the dirty target project above."
            ) : undefined}
          >
            {submitting ? <><Spinner /> Queueing…</> : "Queue job"}
          </button>
        </footer>
      </form>
    </div>
  );
}

export function PhaseRoutingFields({label, testKey, provider, model, effort, onProvider, onModel, onEffort}: {
  label: string;
  testKey: string;
  provider: string;
  model: string;
  effort: string;
  onProvider: (value: string) => void;
  onModel: (value: string) => void;
  onEffort: (value: string) => void;
}) {
  return (
    <section className="phase-routing-group" aria-label={`${label} routing`}>
      <h3>{label}</h3>
      <div className="field-grid">
        <label>Provider
          <select data-testid={`job-${testKey}-provider-select`} value={provider} onChange={(event) => onProvider(event.target.value)}>
            <option value="">Inherit</option>
            <option value="codex">Codex</option>
            <option value="claude">Claude</option>
          </select>
        </label>
        <label>Reasoning
          <select data-testid={`job-${testKey}-effort-select`} value={effort} onChange={(event) => onEffort(event.target.value)}>
            <option value="">Inherit</option>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
            <option value="max">Max</option>
          </select>
        </label>
      </div>
      <label>Model
        <input value={model} type="text" placeholder="inherit" onChange={(event) => onModel(event.target.value)} />
      </label>
    </section>
  );
}

export function executionModeHelp(mode: ExecutionMode, _command: JobCommand): string {
  if (mode !== "split") {
    return "Single session. Faster, less reliable.";
  }
  return "Phases run separately. Default.";
}

export function planningHelp(planning: PlanningMode): string {
  if (planning === "spec-review") return "Generate a spec, then wait for approval.";
  if (planning === "spec-auto") return "Generate a spec and use it without review.";
  if (planning === "spec-file") return "Use an existing spec file.";
  return "Build directly from the intent.";
}

export function jobRunSummary({command, subcommand, project, provider, model, effort, certification, rounds}: {
  command: JobCommand;
  subcommand: ImproveSubcommand;
  project: StateResponse["project"] | undefined;
  provider: string;
  model: string;
  effort: string;
  certification: CertificationPolicy;
  rounds: string;
}): string {
  const defaults = project?.defaults;
  const providerLabel = provider || defaults?.provider || "default";
  const modelLabel = model.trim() || defaults?.model || "default";
  const effortLabel = effort || defaults?.reasoning_effort || "default";
  const verificationLabel = describeVerificationPolicy(command, subcommand, certification, project);
  const roundLimit = effectiveRoundLimit(project, rounds);
  const roundLabel = roundLimit ? ` · rounds ${roundLimit}` : "";
  return `${providerLabel} · model ${modelLabel} · effort=${effortLabel} · verification=${verificationLabel}${roundLabel}`;
}

export function effectiveRoundLimit(project: StateResponse["project"] | undefined, rounds: string): number | null {
  const requested = Number.parseInt(rounds, 10);
  if (Number.isFinite(requested) && requested > 0) return requested;
  const inherited = project?.defaults?.max_certify_rounds;
  return typeof inherited === "number" && inherited > 0 ? inherited : null;
}

export function describeVerificationPolicy(
  command: JobCommand,
  subcommand: ImproveSubcommand,
  certification: CertificationPolicy,
  project: StateResponse["project"] | undefined,
): string {
  if (certification === "skip") return "skipped";
  if (certification) return certification;
  if (command === "improve" && subcommand === "feature") return "hillclimb";
  if (command === "improve" && subcommand === "target") return "target";
  if (command === "improve" && subcommand === "bugs") return "thorough (improve default)";
  const defaults = project?.defaults;
  if (defaults?.skip_product_qa) return "skipped (project default)";
  return defaults?.certifier_mode || "fast";
}

export function certificationOptions(
  command: JobCommand,
  subcommand: ImproveSubcommand,
  project: StateResponse["project"] | undefined,
): Array<{value: CertificationPolicy; label: string}> {
  if (command === "improve" && subcommand !== "bugs") return [];
  const inherited = command === "improve" && subcommand === "bugs"
    ? "Inherit: thorough"
    : certificationDefaultLabel(project);
  const options: Array<{value: CertificationPolicy; label: string}> = [
    {value: "", label: inherited},
    {value: "fast", label: "Fast"},
    {value: "standard", label: "Standard"},
    {value: "thorough", label: "Thorough"},
  ];
  if (command === "build") {
    options.push({value: "skip", label: "Skip"});
  }
  return options;
}

export function certificationPolicyAllowed(command: JobCommand, subcommand: ImproveSubcommand, policy: CertificationPolicy): boolean {
  if (!policy) return true;
  if (policy === "skip") return command === "build";
  return command === "build" || command === "certify" || (command === "improve" && subcommand === "bugs");
}

export function providerDefaultLabel(project: StateResponse["project"] | undefined): string {
  const defaults = project?.defaults;
  if (!defaults) return "Inherit default";
  return `Inherit: ${titleCase(defaults.provider || "claude")}`;
}

export function effortDefaultLabel(project: StateResponse["project"] | undefined): string {
  const defaults = project?.defaults;
  if (!defaults) return "Inherit default";
  const effort = defaults.reasoning_effort ? titleCase(defaults.reasoning_effort) : "Provider default";
  return `Inherit: ${effort}`;
}

export function modelDefaultPlaceholder(project: StateResponse["project"] | undefined): string {
  const model = project?.defaults?.model;
  return model ? `default: ${model}` : "provider default";
}

export function certificationDefaultLabel(project: StateResponse["project"] | undefined): string {
  const defaults = project?.defaults;
  if (!defaults) return "Inherit default";
  const policy = defaults.skip_product_qa ? "skip" : (defaults.certifier_mode || "fast");
  return `Inherit: ${policy}`;
}

export function certificationHelp(
  command: JobCommand,
  subcommand: ImproveSubcommand,
  certification: CertificationPolicy,
  project: StateResponse["project"] | undefined,
): string {
  if (certification === "skip") return "Skips verification.";
  if (certification) return "Applies to the verify phase only.";
  const defaults = project?.defaults;
  if (defaults?.config_error) return `Using built-in defaults — otto.yaml could not be read: ${defaults.config_error}`;
  if (command === "improve" && subcommand === "bugs") return "Defaults to thorough for bug improvements.";
  return "Uses otto.yaml defaults.";
}

export function staticCertificationLabel(command: JobCommand, subcommand: ImproveSubcommand): string {
  if (command === "improve" && subcommand === "feature") return "Feature improvement uses hillclimb evaluation";
  if (command === "improve" && subcommand === "target") return "Target improvement uses target evaluation";
  return "Managed by this command";
}
