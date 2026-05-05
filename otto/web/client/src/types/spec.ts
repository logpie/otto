// Type contracts for the spec-review surface (research §2.1).
//
// `SpecMdView` is a tiny envelope around the user-facing markdown
// representation produced by `otto.spec_compile.render_spec_md`.
// Backend endpoint: GET /api/specs/<spec_id>/markdown (lands tick 35).
//
// The frontend treats the markdown as the single source of truth for
// editing prose. Mechanical fields (intent_hash, owned_paths, deps,
// audit/coverage state, etc.) round-trip via `<!-- group: id -->` and
// `<!-- feature: id | evidence: ... -->` HTML comments inside the
// markdown — see otto/spec_compile.py:render_spec_md / parse_spec_md.
//
// We deliberately do NOT model the Spec dataclass surface as a
// TypeScript interface here: the frontend's only contract is the
// markdown text. Keeping that boundary narrow lets the spec model
// evolve without dragging the UI through schema migrations.

export type SpecLifecycle =
  | "draft" // returned from compile_spec, not yet approved
  | "approved" // user approved the spec; build can start
  | "amended"; // post-approval amendment for in-flight runs

export interface SpecMdView {
  // Spec id this snapshot belongs to (stable across edits).
  spec_id: string;

  // Session id the spec was compiled within.
  session_id: string;

  // User-facing markdown. Round-trips through render_spec_md / parse_spec_md.
  markdown: string;

  // Stable hash of the canonical Spec — frontend echoes back on POST /edit
  // so the backend can detect concurrent edits.
  intent_hash: string;

  // Lifecycle state — drives which actions the UI exposes.
  lifecycle: SpecLifecycle;

  // ISO-8601 wall-clock timestamp the spec was last written.
  updated_at: string;
}

export interface SpecEditRequest {
  // The intent_hash from the SpecMdView this edit was based on.
  // Backend rejects stale edits where this no longer matches.
  intent_hash: string;
  // Edited markdown to persist.
  markdown: string;
}

export interface SpecEditResult {
  // Updated SpecMdView (lifecycle stays draft; intent_hash advances).
  view: SpecMdView;
  // Warnings emitted by parse_spec_md — non-fatal but surfaced in UI.
  warnings: string[];
}

export interface SpecApproveResult {
  // Updated SpecMdView (lifecycle = approved).
  view: SpecMdView;
}
