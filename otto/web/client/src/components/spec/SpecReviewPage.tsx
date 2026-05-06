// SpecReviewPage — spec-review surface (wireframes 4a/4b/4c/4d).
//
// Tick 34: skeleton (read-only + edit toggle, console-log stubs).
// Tick 35: backend GET markdown / POST edit / POST approve landed.
// Tick 56: wire Save / Approve buttons to live backend, surface
//          parse_spec_md warnings + 409 stale-edit errors.
// Post-RUA round 1: B9–B14, B26/B27, B30–B32. HTML-comment strip in
// render path, header reformat with relative time, top-right action
// group, markdown<->form view toggle (placeholder), confirm dialogs
// for Approve / discard-on-cancel, success banner, read-only banner
// for approved specs, and history sidebar that shows v1+ rows.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import type {
  SpecApproveResult,
  SpecEditResult,
  SpecMdView,
} from "../../types/spec";
import { ConfirmDialog, type ConfirmState } from "../ConfirmDialog";
import { Pill } from "../Pill";
import { AddFeatureModal } from "./AddFeatureModal";
import { useSpecMd } from "./useSpecMd";

// R2-B22: the rendered spec markdown often starts with its own `# Title`,
// which would emit a second <h1> on a page that already has a "Spec
// review" h1. Demote every heading by one level on the render path so
// the page has exactly one h1 and the document outline stays valid.
// `h1 → h2`, `h2 → h3`, ..., `h5 → h6`, `h6` stays h6 (HTML caps at h6).
const HEADING_DEMOTION_COMPONENTS: Components = {
  h1: ({ children, ...props }) => <h2 {...props}>{children}</h2>,
  h2: ({ children, ...props }) => <h3 {...props}>{children}</h3>,
  h3: ({ children, ...props }) => <h4 {...props}>{children}</h4>,
  h4: ({ children, ...props }) => <h5 {...props}>{children}</h5>,
  h5: ({ children, ...props }) => <h6 {...props}>{children}</h6>,
  // h6 has no lower level — leave it as h6 to avoid losing structure.
};

// Pull existing Group + Feature ids out of the rendered markdown so the
// Add Feature modal can populate the Group dropdown and check id collisions.
const GROUP_COMMENT_RE = /<!--\s*group:\s*([\w-]+)\s*-->/g;
const FEATURE_COMMENT_RE = /<!--\s*feature:\s*([\w-]+)/g;

// B9: strip raw HTML comments from the markdown before rendering. We
// only do this on the render path — the underlying markdown source
// (and the edit-mode textarea) keeps the comments intact so they can
// be edited and round-tripped.
const HTML_COMMENT_RE = /<!--[\s\S]*?-->/g;
function stripHtmlComments(markdown: string): string {
  return markdown.replace(HTML_COMMENT_RE, "");
}

function extractGroupIds(markdown: string): string[] {
  const seen = new Set<string>();
  let match: RegExpExecArray | null;
  GROUP_COMMENT_RE.lastIndex = 0;
  while ((match = GROUP_COMMENT_RE.exec(markdown)) !== null) {
    if (match[1]) seen.add(match[1]);
  }
  return Array.from(seen);
}

function extractFeatureIds(markdown: string): string[] {
  const seen = new Set<string>();
  let match: RegExpExecArray | null;
  FEATURE_COMMENT_RE.lastIndex = 0;
  while ((match = FEATURE_COMMENT_RE.exec(markdown)) !== null) {
    if (match[1]) seen.add(match[1]);
  }
  return Array.from(seen);
}

// B10: relative-time formatter using Intl.RelativeTimeFormat (built-in).
// Returns short forms like "4m ago", "2h ago", "3d ago", or "just now".
const RTF = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
function formatRelative(iso: string): string {
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return iso;
  const diffMs = ts - Date.now();
  const absSec = Math.abs(diffMs) / 1000;
  if (absSec < 45) return "just now";
  if (absSec < 60 * 60) {
    return RTF.format(Math.round(diffMs / (60 * 1000)), "minute");
  }
  if (absSec < 60 * 60 * 24) {
    return RTF.format(Math.round(diffMs / (60 * 60 * 1000)), "hour");
  }
  if (absSec < 60 * 60 * 24 * 30) {
    return RTF.format(Math.round(diffMs / (60 * 60 * 24 * 1000)), "day");
  }
  if (absSec < 60 * 60 * 24 * 365) {
    return RTF.format(Math.round(diffMs / (60 * 60 * 24 * 30 * 1000)), "month");
  }
  return RTF.format(
    Math.round(diffMs / (60 * 60 * 24 * 365 * 1000)),
    "year",
  );
}

// B12: parse the markdown into a flat group->features outline for the
// "Form view" placeholder. This is intentionally simple — the wireframe
// 4b form editor is a larger feature; this just shows the structured
// list so users can sanity-check what otto extracted.
interface ParsedFeature {
  id: string;
  name: string;
}
interface ParsedGroup {
  id: string;
  features: ParsedFeature[];
}
function parseStructure(markdown: string): ParsedGroup[] {
  const groups: ParsedGroup[] = [];
  let current: ParsedGroup | null = null;
  let lastFeatureName: string | null = null;
  const lines = markdown.split("\n");
  for (const raw of lines) {
    const line = raw.trim();
    const groupMatch = line.match(/^<!--\s*group:\s*([\w-]+)\s*-->$/);
    if (groupMatch) {
      const id = groupMatch[1] ?? "";
      current = { id, features: [] };
      groups.push(current);
      continue;
    }
    const headingMatch = raw.match(/^####\s+(.+?)\s*$/);
    if (headingMatch) {
      lastFeatureName = headingMatch[1] ?? null;
      continue;
    }
    const featureMatch = line.match(/^<!--\s*feature:\s*([\w-]+)/);
    if (featureMatch) {
      const id = featureMatch[1] ?? "";
      const name = lastFeatureName ?? id;
      if (!current) {
        current = { id: "ungrouped", features: [] };
        groups.push(current);
      }
      current.features.push({ id, name });
      lastFeatureName = null;
    }
  }
  return groups;
}

interface Props {
  specId: string;
  onApproved?: (view: SpecMdView) => void;
}

interface SubmitState {
  inFlight: boolean;
  error: string | null;
  warnings: string[];
}

const INITIAL_SUBMIT_STATE: SubmitState = {
  inFlight: false,
  error: null,
  warnings: [],
};

type SpecViewMode = "markdown" | "form";

export function SpecReviewPage({ specId, onApproved }: Props) {
  const { data, loading, error, reload } = useSpecMd(specId);
  const [editing, setEditing] = useState<boolean>(false);
  const [draft, setDraft] = useState<string>("");
  const [submit, setSubmit] = useState<SubmitState>(INITIAL_SUBMIT_STATE);
  const [showAddFeature, setShowAddFeature] = useState<boolean>(false);
  const [viewMode, setViewMode] = useState<SpecViewMode>("markdown");
  // B30: success-banner state with auto-clear timer.
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const successTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // B30/B31: confirm-dialog plumbing (mirrors RunDrawer).
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [confirmPending, setConfirmPending] = useState<boolean>(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  // B27: track the latest successful save so the versions effect refires.
  const [savedTick, setSavedTick] = useState<number>(0);
  // Spec-version history (wireframe A5.2). The `specId` URL param is the
  // session id (see spec_review_routes.py: routes are mounted under
  // /api/specs/<session_id>/...). Endpoint returns archived prior
  // versions, sorted ascending.
  const [versions, setVersions] = useState<number[] | null>(null);
  const [versionsError, setVersionsError] = useState<string | null>(null);

  const draftGroupIds = useMemo(() => extractGroupIds(draft), [draft]);
  const draftFeatureIds = useMemo(() => extractFeatureIds(draft), [draft]);

  // Sync draft when fresh data lands.
  useEffect(() => {
    if (data) {
      setDraft(data.markdown);
    }
  }, [data]);

  // B27: refetch the version list whenever the session changes, the
  // server-reported updated_at moves, OR a save just succeeded. The
  // savedTick covers the case where the server returns the same
  // updated_at after a no-op save (which shouldn't happen, but the
  // explicit dependency keeps us honest).
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/specs/${encodeURIComponent(specId)}/versions`)
      .then((resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        return resp.json() as Promise<{ versions: number[] }>;
      })
      .then((payload) => {
        if (cancelled) return;
        const list = [...(payload.versions || [])].sort((a, b) => a - b);
        setVersions(list);
        setVersionsError(null);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setVersionsError(err.message || String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [specId, data?.updated_at, savedTick]);

  // Cleanup success-banner timer on unmount.
  useEffect(() => {
    return () => {
      if (successTimerRef.current !== null) {
        clearTimeout(successTimerRef.current);
        successTimerRef.current = null;
      }
    };
  }, []);

  const flashSuccess = useCallback((message: string) => {
    if (successTimerRef.current !== null) {
      clearTimeout(successTimerRef.current);
    }
    setSuccessMessage(message);
    successTimerRef.current = setTimeout(() => {
      setSuccessMessage(null);
      successTimerRef.current = null;
    }, 3000);
  }, []);

  const closeConfirm = useCallback(() => {
    if (confirmPending) return;
    setConfirm(null);
    setConfirmError(null);
  }, [confirmPending]);

  const runConfirm = useCallback(async () => {
    if (!confirm) return;
    setConfirmPending(true);
    setConfirmError(null);
    try {
      await confirm.onConfirm();
      setConfirm(null);
    } catch (err) {
      setConfirmError(err instanceof Error ? err.message : String(err));
    } finally {
      setConfirmPending(false);
    }
  }, [confirm]);

  // Hooks below MUST be called unconditionally — they were below the
  // early returns, which violates React's Rules of Hooks (renders an
  // inconsistent hook count and trips React error #310).
  const renderedMarkdown = useMemo(
    () => (data ? stripHtmlComments(data.markdown) : ""),
    [data],
  );
  const structure = useMemo(
    () => (data ? parseStructure(data.markdown) : []),
    [data],
  );

  if (loading && !data) {
    return (
      <div className="spec-review-loading" data-testid="spec-review-loading">
        <p>Loading spec {specId}…</p>
      </div>
    );
  }
  if (error) {
    return (
      <div className="spec-review-error" data-testid="spec-review-error">
        <p>Failed to load spec: {error}</p>
        <button type="button" onClick={reload}>
          Retry
        </button>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="spec-review-empty" data-testid="spec-review-empty">
        <p>No spec selected.</p>
      </div>
    );
  }

  const isApproved = data.lifecycle === "approved";
  const isDirty = editing && draft !== data.markdown;

  async function performSave(): Promise<void> {
    if (!data) return;
    setSubmit({ inFlight: true, error: null, warnings: [] });
    try {
      const resp = await fetch(
        `/api/specs/${encodeURIComponent(specId)}/edit`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            intent_hash: data.intent_hash,
            markdown: draft,
          }),
        },
      );
      if (resp.status === 409) {
        const body = await resp.json().catch(() => ({}));
        setSubmit({
          inFlight: false,
          error:
            (body as { detail?: string }).detail ??
            "Spec was edited by someone else; reload and reapply.",
          warnings: [],
        });
        return;
      }
      if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        setSubmit({
          inFlight: false,
          error: `HTTP ${resp.status}: ${body || resp.statusText}`,
          warnings: [],
        });
        return;
      }
      const result = (await resp.json()) as SpecEditResult;
      setSubmit({
        inFlight: false,
        error: null,
        warnings: result.warnings,
      });
      setEditing(false);
      setSavedTick((tick) => tick + 1);
      flashSuccess("Spec saved");
      reload();
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e);
      setSubmit({ inFlight: false, error: message, warnings: [] });
    }
  }

  function handleSave() {
    void performSave();
  }

  // B30: Approve goes through ConfirmDialog and surfaces a success
  // banner once the lifecycle flips.
  function requestApprove() {
    setConfirm({
      title: "Approve this spec?",
      body: "Once approved, edits are blocked unless you re-run with --force.",
      confirmLabel: "Approve",
      tone: "primary",
      onConfirm: async () => {
        if (!data) return;
        setSubmit({ inFlight: true, error: null, warnings: [] });
        try {
          const resp = await fetch(
            `/api/specs/${encodeURIComponent(specId)}/approve`,
            { method: "POST" },
          );
          if (!resp.ok) {
            const body = await resp.text().catch(() => "");
            const message = `HTTP ${resp.status}: ${body || resp.statusText}`;
            setSubmit({ inFlight: false, error: message, warnings: [] });
            throw new Error(message);
          }
          const result = (await resp.json()) as SpecApproveResult;
          setSubmit({ inFlight: false, error: null, warnings: [] });
          flashSuccess("Spec approved");
          if (onApproved) onApproved(result.view);
          reload();
        } catch (e: unknown) {
          if (e instanceof Error) throw e;
          throw new Error(String(e));
        }
      },
    });
  }

  // B31: discard-confirmation when there are unsaved edits.
  function requestCancelEdit() {
    if (!isDirty) {
      setEditing(false);
      setSubmit(INITIAL_SUBMIT_STATE);
      return;
    }
    setConfirm({
      title: "Discard unsaved changes?",
      body: "Your edits will be lost.",
      confirmLabel: "Discard",
      tone: "danger",
      onConfirm: async () => {
        if (data) setDraft(data.markdown);
        setEditing(false);
        setSubmit(INITIAL_SUBMIT_STATE);
      },
    });
  }

  return (
    <div className="spec-review" data-testid="spec-review">
      <header className="spec-review-header spec-review-header-grid">
        <div className="spec-review-title-block">
          <div className="spec-review-title-row">
            <h1>Spec review</h1>
            {/* R3-B38: lifecycle pill uses a neutral grey tone for the
                draft state so it isn't confused with the primary blue
                Approve button. The `success` tone (green) is reserved
                for the terminal "approved" state. */}
            <Pill
              tone={isApproved ? "success" : "neutral"}
              className="spec-review-lifecycle"
            >
              <span data-testid="spec-lifecycle">{data.lifecycle}</span>
            </Pill>
            {/* R3-B39: surface the working version number near the
                lifecycle pill. `versions` lists ARCHIVED prior versions;
                the live working draft is one ahead, so we display
                `versions.length + 1` as the current version. We render
                only after the version list has resolved (versions !==
                null) to avoid a flash of "v1". */}
            {versions !== null && (
              <span
                className="spec-review-version"
                data-testid="spec-review-version"
              >
                Version v{versions.length + 1}
              </span>
            )}
            {/* R2-B21: explicit bullet separator between the lifecycle
                pill and the relative timestamp so the two visually
                distinct items don't run together. */}
            <span
              className="spec-review-meta-sep"
              aria-hidden="true"
            >
              ·
            </span>
            <span className="spec-review-meta">
              Updated{" "}
              <time dateTime={data.updated_at} title={data.updated_at}>
                {formatRelative(data.updated_at)}
              </time>
            </span>
          </div>
          {/* R2-B24: small attribution subline. The SpecMdView envelope
              currently lacks `created_at` / `created_by` metadata, so we
              fall back to the file mtime (updated_at) and the static
              author "compile-agent". When the backend grows real spec
              metadata, swap data.updated_at for data.created_at and
              read the author from the metadata block. */}
          <div
            className="spec-review-attribution"
            data-testid="spec-review-attribution"
          >
            Created{" "}
            <time
              dateTime={data.updated_at}
              title={data.updated_at}
            >
              {formatRelative(data.updated_at)}
            </time>{" "}
            <span className="spec-review-meta-sep" aria-hidden="true">
              ·
            </span>{" "}
            by compile-agent
          </div>
        </div>

        {/* B11: action buttons live in the header (top-right). */}
        <div
          className="spec-review-header-actions"
          data-testid="spec-review-header-actions"
        >
          {!editing && !isApproved && (
            <button
              type="button"
              data-testid="spec-review-edit"
              onClick={() => setEditing(true)}
              disabled={submit.inFlight}
            >
              Edit
            </button>
          )}
          {editing && (
            <>
              <button
                type="button"
                data-testid="spec-review-add-feature"
                onClick={() => setShowAddFeature(true)}
                disabled={submit.inFlight}
              >
                Add Feature
              </button>
              <button
                type="button"
                data-testid="spec-review-cancel"
                onClick={requestCancelEdit}
                disabled={submit.inFlight}
              >
                Cancel
              </button>
              <button
                type="button"
                data-testid="spec-review-save"
                onClick={handleSave}
                disabled={submit.inFlight || draft === data.markdown}
              >
                {submit.inFlight ? "Saving…" : "Save"}
              </button>
            </>
          )}
          {!editing && !isApproved && (
            <button
              type="button"
              className="primary"
              data-testid="spec-review-approve"
              onClick={requestApprove}
              disabled={submit.inFlight}
            >
              {submit.inFlight ? "Approving…" : "Approve"}
            </button>
          )}
        </div>
      </header>

      {/* B30: aria-live success banner. */}
      <div
        className="spec-review-success"
        data-testid="spec-review-success"
        role="status"
        aria-live="polite"
      >
        {successMessage ?? ""}
      </div>

      {/* B32: read-only banner when the spec is approved. */}
      {isApproved && (
        <div
          className="spec-review-readonly-banner"
          data-testid="spec-review-readonly-banner"
          role="status"
        >
          <span aria-hidden="true">⚠</span> Spec is approved (read-only). Re-run
          with <code>otto build --resume --force</code> to edit again.
        </div>
      )}

      {submit.error && (
        <div
          className="spec-review-submit-error"
          data-testid="spec-review-submit-error"
          role="alert"
        >
          {submit.error}
        </div>
      )}

      {submit.warnings.length > 0 && (
        <div
          className="spec-review-warnings"
          data-testid="spec-review-warnings"
        >
          <h3>Parse warnings</h3>
          <ul>
            {submit.warnings.map((w, i) => (
              <li key={`${i}-${w}`}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {/* B12: Markdown <-> Form view toggle (placeholder form view). */}
      {!editing && (
        <div
          className="spec-review-view-toggle"
          data-testid="spec-review-view-toggle"
          role="tablist"
          aria-label="Spec view mode"
        >
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === "markdown"}
            data-testid="spec-review-view-markdown"
            className={
              viewMode === "markdown" ? "view-toggle-active" : undefined
            }
            onClick={() => setViewMode("markdown")}
          >
            Markdown
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === "form"}
            data-testid="spec-review-view-form"
            className={viewMode === "form" ? "view-toggle-active" : undefined}
            onClick={() => setViewMode("form")}
          >
            Form
          </button>
        </div>
      )}

      <div className="spec-review-body">
        {editing ? (
          <textarea
            className="spec-review-editor"
            data-testid="spec-review-editor"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            rows={32}
            disabled={submit.inFlight}
          />
        ) : viewMode === "form" ? (
          // B12: placeholder form view — read-only structured outline of
          // groups + features. The full wireframe-4b form editor is a
          // separate, larger feature; this gives users a structural
          // summary in the meantime.
          <div
            className="spec-review-form spec-review-form-placeholder"
            data-testid="spec-review-form"
          >
            <p className="spec-review-form-note">
              Form view is a placeholder — the full editor lands later.
              Below is the parsed group/feature outline.
            </p>
            {structure.length === 0 ? (
              <p className="spec-review-form-empty">
                No groups or features detected in this spec.
              </p>
            ) : (
              <ul className="spec-review-form-groups">
                {structure.map((g, gi) => (
                  <li key={`${gi}-${g.id}`}>
                    <strong>{g.id}</strong>
                    {g.features.length === 0 ? (
                      <p className="spec-review-form-empty">
                        No features in this group.
                      </p>
                    ) : (
                      <ul>
                        {g.features.map((f) => (
                          <li key={f.id}>
                            <code>{f.id}</code> — {f.name}
                          </li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <div
            className="spec-review-markdown spec-markdown"
            data-testid="spec-review-markdown"
          >
            <ReactMarkdown components={HEADING_DEMOTION_COMPONENTS}>
              {renderedMarkdown}
            </ReactMarkdown>
          </div>
        )}

        <aside
          className="spec-review-history"
          data-testid="spec-review-history"
          aria-label="Spec history"
        >
          <h3>Spec history</h3>
          {versionsError ? (
            <p className="spec-review-history-error" role="alert">
              Failed to load versions: {versionsError}
            </p>
          ) : versions === null ? (
            <p className="spec-review-history-empty">Loading…</p>
          ) : versions.length < 1 ? (
            // B26: empty state only when truly empty.
            <p className="spec-review-history-empty">
              No prior versions yet
            </p>
          ) : (
            <ul>
              {versions.map((v) => {
                const latest = versions[versions.length - 1] as number;
                const hasMultiple = versions.length >= 2;
                const isLatest = v === latest;
                const href =
                  `?view=spec-diff&session=${encodeURIComponent(specId)}` +
                  `&from=${v}&to=${latest}`;
                return (
                  <li key={v}>
                    <span className="spec-review-history-version">v{v}</span>
                    {/* B26: only show the compare/current dropdown when
                        there are at least 2 versions to compare. */}
                    {hasMultiple ? (
                      isLatest ? (
                        <span className="spec-review-history-current">
                          current
                        </span>
                      ) : (
                        <a
                          href={href}
                          data-testid={`spec-review-history-link-${v}`}
                        >
                          Compare to v{latest}
                        </a>
                      )
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </aside>
      </div>

      {showAddFeature && (
        <AddFeatureModal
          groupIds={draftGroupIds}
          existingFeatureIds={draftFeatureIds}
          onAppend={(block) => setDraft((d) => d + block)}
          onClose={() => setShowAddFeature(false)}
        />
      )}

      {confirm && (
        <ConfirmDialog
          confirm={confirm}
          pending={confirmPending}
          error={confirmError}
          checkboxAck={false}
          onChangeCheckboxAck={() => {}}
          onCancel={closeConfirm}
          onConfirm={runConfirm}
        />
      )}
    </div>
  );
}

export default SpecReviewPage;
