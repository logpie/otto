// Type contracts for the new design's MC drawer (research §2 vocabulary).
//
// `RunView` is what the backend `otto/mission_control/run_view.py:build_run_view`
// returns and what the frontend `<RunDrawer/>` consumes. These types lock
// the API contract before A1b (backend implementation) and A4 (frontend
// implementation) — locking the shape early prevents the round-trip churn
// flagged by the plan reviewer.
//
// Field semantics align with otto/spec_compile.py dataclasses:
//   FeatureView ↔ Feature
//   GroupView ↔ Group (renamed from Slice)
//   ComponentView ↔ Component
//   GuardrailView ↔ Guardrail
//   StageView ↔ pipeline stage entries (Compile/Build/Audit/Render/Land)
//
// Null semantics:
//   - In-flight runs have `verdict: null`, `finished_at: null`, etc.
//   - Stage entries have `duration_s: null` until that stage finishes.
//   - Per-Feature `verdict: null` means audit hasn't run for that Feature.

// ---------------------------------------------------------------------------
// Top-level RunView
// ---------------------------------------------------------------------------

export interface RunView {
  run_id: string;
  status: RunStatus;
  intent: string;
  project_kind: ProjectKind;

  // Verdict — null while in flight; set after Render writes proof packet.
  verdict: RunVerdict | null;

  // Atomic units of value (research §3): user's primary surface.
  features: FeatureView[];

  // Atomic units of dispatch (research §3): collapsible "Built in N groups".
  groups: GroupView[];

  // Shared infrastructure dispatch units (research §2.6): collapsible
  // "Components" section in the drawer.
  components: ComponentView[];

  // Pinned negative scope (research §2 vocab): rendered as ⊘ pills.
  guardrails: GuardrailView[];

  // Stage timeline: Compile → Build → Audit → Render → Land.
  stages: StageView[];

  // Run-level cost + wall.
  cost_usd: number;
  wall_s: number;

  // Run metadata: collapsed dl in drawer.
  meta: RunMeta;

  // Quality findings with severity (research §4 severity ladder).
  // `critical` flips Feature verdict to partial and triggers Audit loop.
  // `important` surfaces under the Feature; verdict unchanged.
  // `polish` lives in a "Polish suggestions" section.
  findings: FindingView[];
}

// ---------------------------------------------------------------------------
// Sub-views
// ---------------------------------------------------------------------------

export interface FeatureView {
  id: string;          // stable across renames
  name: string;
  description: string;
  acceptance_detail: string;
  evidence_kinds: string[];
  group_id: string;

  // Verdict — null pre-Audit, populated post-Render.
  verdict: FeatureVerdict | null;

  // Audit honesty contract (research §4):
  evidence_completeness: EvidenceCompleteness;
  coverage_confidence: CoverageConfidence;
  multi_actor_required: boolean;
  audit_pre_merge: boolean;

  // Evidence references collected by Audit; empty pre-Audit.
  evidence_refs: EvidenceRef[];
}

export interface GroupView {
  id: string;
  name: string;        // mapped from legacy Group.title for transition
  description: string;
  feature_ids: string[];

  // Per-Group dispatch state.
  status: GroupStatus;
  branch: string;      // git branch name
  owned_paths: string[];
  dependencies: string[];

  // Per-Group cost + wall (subset of Run total).
  cost_usd: number;
  wall_s: number;

  // Repair attempts during Check loop (Layer 1 retries).
  repair_attempts: number;
}

export interface ComponentView {
  id: string;
  name: string;
  description: string;

  // Components are dispatched like Groups but have NO Feature verdict.
  // Status reflects build-only outcome.
  status: ComponentStatus;
  owned_paths: string[];
  dependencies: string[];
  consumed_by: string[];  // Feature ids that depend on this Component

  cost_usd: number;
  wall_s: number;
}

export interface GuardrailView {
  id: string;
  text: string;
  applies_to: string;   // "*" for whole-product, else group_id or feature_id

  // Audit asserts no violation; null pre-Audit, true/false post-Audit.
  verified: boolean | null;
}

export interface StageView {
  name: StageName;
  status: StageStatus;
  duration_s: number | null;   // null until stage finishes
  cost_usd: number | null;     // null until stage finishes
  started_at: string | null;   // ISO 8601, null if not yet started
  finished_at: string | null;  // ISO 8601, null if not yet finished
}

export interface EvidenceRef {
  kind: EvidenceKind;
  path: string;        // relative to session_dir
  summary: string;     // one-line human description
}

export interface RunMeta {
  session_id: string;
  spec_path: string;
  spec_version: number;
  proof_packet_html: string | null;  // path; null if Render hasn't run
  proof_packet_json: string | null;
  started_at: string;                 // ISO 8601
  finished_at: string | null;         // null while in flight
  intent_hash: string;                // SHA-256 of original intent
}

export interface FindingView {
  severity: FindingSeverity;
  text: string;
  feature_id: string;  // empty string for whole-product findings
}

// ---------------------------------------------------------------------------
// Enums (string literals)
// ---------------------------------------------------------------------------

// Top-level Run status — the lifecycle states a Run progresses through.
export type RunStatus =
  | "queued"
  | "compiling"
  | "awaiting_spec_review"
  | "building"
  | "auditing"
  | "rendering"
  | "landing"
  | "landed"
  | "blocked"
  | "partial"
  | "passed"
  | "aborted"
  | "failed";

export type RunVerdict = "passed" | "partial" | "blocked";

export type FeatureVerdict =
  | "passed"
  | "partial"
  | "blocked"
  | "failed"
  | "missing";

export type EvidenceCompleteness = "full" | "proxy_only" | "partial";

export type CoverageConfidence = "high" | "medium" | "low";

export type GroupStatus =
  | "pending"
  | "in_progress"
  | "passing"
  | "blocked"
  | "landed"
  | "failed_scope";

export type ComponentStatus =
  | "pending"
  | "in_progress"
  | "passing"
  | "blocked"
  | "landed";

export type StageName =
  | "compile"
  | "spec_review"
  | "build"
  | "seed"
  | "audit"
  | "render"
  | "land";

export type StageStatus =
  | "pending"
  | "active"
  | "done"
  | "skipped"
  | "failed";

// Evidence kinds — matches otto/checks.py Check kinds + audit walkthrough kinds.
// Some are A1b additions (CLIProbe, ImportCheck, TypeCheck).
export type EvidenceKind =
  | "BrowserJourney"
  | "ApiProbe"
  | "StateInvariant"
  | "RepoTestCheck"
  | "CLIProbe"
  | "ImportCheck"
  | "TypeCheck"
  | "WalkthroughSegment";

export type FindingSeverity = "critical" | "important" | "polish";

export type ProjectKind = "webapp" | "api" | "library" | "cli";

// ---------------------------------------------------------------------------
// Helpers (not used by data shape; convenience predicates for components)
// ---------------------------------------------------------------------------

export function isInFlight(view: RunView): boolean {
  return view.verdict === null;
}

export function isFeatureBlocking(feature: FeatureView): boolean {
  return feature.verdict === "blocked" || feature.verdict === "failed";
}

export function isFeatureHonest(feature: FeatureView): boolean {
  // A Feature is "honest" if its evidence_completeness is full and
  // coverage_confidence is high. Lower combinations require explicit
  // narrative in the Proof.
  return (
    feature.evidence_completeness === "full" &&
    feature.coverage_confidence === "high"
  );
}
