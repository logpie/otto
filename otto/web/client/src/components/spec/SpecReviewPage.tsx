// SpecReviewPage — spec-review surface (wireframes 4a/4b/4c/4d).
//
// Tick 34: skeleton (read-only + edit toggle, console-log stubs).
// Tick 35: backend GET markdown / POST edit / POST approve landed.
// Tick 56 (this): wire Save / Approve buttons to live backend, surface
//                 parse_spec_md warnings + 409 stale-edit errors.
//
// Lifecycle:
//   - load via useSpecMd
//   - "Edit" toggles a textarea swap; user mutates `draft` locally
//   - "Cancel" reverts to the loaded markdown
//   - "Save" POSTs /edit; on 409 stale, surface the error and force reload
//   - "Approve" POSTs /approve; lifecycle flips to "approved"

import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import type {
  SpecApproveResult,
  SpecEditResult,
  SpecMdView,
} from "../../types/spec";
import { AddFeatureModal } from "./AddFeatureModal";
import { useSpecMd } from "./useSpecMd";

// Pull existing Group + Feature ids out of the rendered markdown so the
// Add Feature modal can populate the Group dropdown and check id collisions.
const GROUP_COMMENT_RE = /<!--\s*group:\s*([\w-]+)\s*-->/g;
const FEATURE_COMMENT_RE = /<!--\s*feature:\s*([\w-]+)/g;

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

export function SpecReviewPage({ specId, onApproved }: Props) {
  const { data, loading, error, reload } = useSpecMd(specId);
  const [editing, setEditing] = useState<boolean>(false);
  const [draft, setDraft] = useState<string>("");
  const [submit, setSubmit] = useState<SubmitState>(INITIAL_SUBMIT_STATE);
  const [showAddFeature, setShowAddFeature] = useState<boolean>(false);
  // Spec-version history (wireframe A5.2). The `specId` URL param is the
  // session id (see spec_review_routes.py: routes are mounted under
  // /api/specs/<session_id>/...). Endpoint returns archived prior
  // versions, sorted ascending — empty list means no edits yet.
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

  // Fetch the version history on mount (and whenever specId changes).
  // Re-fires after a Save by depending on `data.updated_at` so newly
  // archived versions appear without a manual reload.
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
  }, [specId, data?.updated_at]);

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

  async function handleSave() {
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
      reload();
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e);
      setSubmit({ inFlight: false, error: message, warnings: [] });
    }
  }

  async function handleApprove() {
    if (!data) return;
    setSubmit({ inFlight: true, error: null, warnings: [] });
    try {
      const resp = await fetch(
        `/api/specs/${encodeURIComponent(specId)}/approve`,
        { method: "POST" },
      );
      if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        setSubmit({
          inFlight: false,
          error: `HTTP ${resp.status}: ${body || resp.statusText}`,
          warnings: [],
        });
        return;
      }
      const result = (await resp.json()) as SpecApproveResult;
      setSubmit({ inFlight: false, error: null, warnings: [] });
      if (onApproved) {
        onApproved(result.view);
      }
      reload();
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e);
      setSubmit({ inFlight: false, error: message, warnings: [] });
    }
  }

  return (
    <div className="spec-review" data-testid="spec-review">
      <header className="spec-review-header">
        <h1>Spec review</h1>
        <span className="spec-review-lifecycle" data-testid="spec-lifecycle">
          {data.lifecycle}
        </span>
        <span className="spec-review-meta">updated {data.updated_at}</span>
      </header>

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
        ) : (
          <div
            className="spec-review-markdown spec-markdown"
            data-testid="spec-review-markdown"
          >
            <ReactMarkdown>{data.markdown}</ReactMarkdown>
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
          ) : versions.length < 2 ? (
            <p className="spec-review-history-empty">
              No prior versions yet
            </p>
          ) : (
            <ul>
              {versions.map((v) => {
                const latest = versions[versions.length - 1] as number;
                const isLatest = v === latest;
                const href =
                  `?view=spec-diff&session=${encodeURIComponent(specId)}` +
                  `&from=${v}&to=${latest}`;
                return (
                  <li key={v}>
                    <span className="spec-review-history-version">v{v}</span>
                    {isLatest ? (
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
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </aside>
      </div>

      <footer className="spec-review-actions">
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
              onClick={() => {
                setDraft(data.markdown);
                setEditing(false);
                setSubmit(INITIAL_SUBMIT_STATE);
              }}
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
            data-testid="spec-review-approve"
            onClick={handleApprove}
            disabled={submit.inFlight}
          >
            {submit.inFlight ? "Approving…" : "Approve"}
          </button>
        )}
      </footer>

      {showAddFeature && (
        <AddFeatureModal
          groupIds={draftGroupIds}
          existingFeatureIds={draftFeatureIds}
          onAppend={(block) => setDraft((d) => d + block)}
          onClose={() => setShowAddFeature(false)}
        />
      )}
    </div>
  );
}

export default SpecReviewPage;
