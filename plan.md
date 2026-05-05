# Otto redesign — implementation plan

Source of truth: [`research.md`](research.md). Conversation transcript:
[`docs/otto-redesign-conversation.md`](docs/otto-redesign-conversation.md).

This plan supersedes the V21 detail-panel framing and the prior
intent-to-product plan at `~/.claude/plans/plan-based-on-this-soft-cloud.md`.
Per CLAUDE.md, **Codex Plan Gate review is mandatory** before
implementation begins on each step. Recorded in `review.md` per step.

---

## Cost stance

User has removed cost as a constraint. Loop work runs to completion
regardless of LLM spend. Cost tracking remains for retrospective only,
not for gating. Phase advance gates do not check cost; they check
correctness, generalization, and honest-failure handling.

## Sequencing principle

Vocabulary first, code reorg second, new behavior third. The temptation
is to start with the new MC panel; resist. Renaming is cheap once and
expensive across N follow-ups. Get the words right; let everything
else fall out.

```
Phase A0 — Vocabulary refactor (rename + dedupe magic numbers)
Phase A1 — Spec / Group / Feature dataclasses + Compile output schema
Phase A2 — Audit Feature-tagging (walkthrough.jsonl, verdicts)
Phase A3 — Render — per-Feature mini-packet
Phase A4 — MC redesign — Feature-first run drawer
Phase A5 — Hybrid plan ownership + spec review screen
Phase A6 — Brownfield compile mode (deferred-spec, per §9.4)
Phase B  — Cutover: legacy CLI commands route through new stack
Phase C  — Deletion: legacy modules, dataclasses, MC widgets
```

Each step has: scope, files, verification, and a Codex review cue.

---

## Phase A0 — Vocabulary refactor + defaults.py + JSON shims

**⚠️ Estimated 5-7 days, not 1-2.** Plan reviewer grepped: ~2,353 hits
across ~110 files. `Slice` dataclass has 10 import sites + JSON
serialization in session dirs / bench results / API responses.
`certifier` is baked into user-visible `otto.yaml` config keys. This
requires backward-compat shims, not a find-replace.

**Goal:** zero hits for retired words in `otto/`, `tests/`, `docs/`,
plus JSON-key shims so old session dirs still readable.

### Existing state (read first)

The plan reviewer confirmed these modules already exist and use the
retired vocabulary internally:

- `otto/spec_compile.py` — has `Spec` + `Slice` dataclasses, `compile_spec`,
  JSON schema validation, amendment chain. (The plan's "new file:
  `otto/spec.py`" is the legacy markdown gate, not the structured spec.)
- `otto/build.py` — 1000+ lines, `build_groups` orchestration using
  `Slice`, `BuildBudget`, `SliceStatus`, progress-based retry logic.
- `otto/audit.py` — 1400+ lines, `CapabilityVerdict`/`SliceVerdict`/
  `AuditVerdict`, full audit + fix loop.
- `otto/render.py` — 500+ lines, `ProofPacket` renderer.
- `otto/checks.py` — full `Check` hierarchy with `run_checks`.
- `otto/merge_queue.py` — eligibility-gated FIFO already implemented.
- `otto/merge/` — full module: `orchestrator.py`, `conflict_agent.py`,
  `git_ops.py`, `state.py`, `edit_scope.py`, `stories.py`.
- `otto/spec_state.py` — `state.jsonl` event emitter (named
  `spec-state.jsonl`) with `run.finished` event support.
- `otto/spec_amend.py`, `otto/spec_warnings.py`, `otto/spec_schemas/` —
  exist and are wired into compile.
- `otto/cli_run.py` — `otto run` already drives the full pipeline.

**A0 is rename + shim, not greenfield.** Identify every import site
before any edit.

### Scope

- Rename in code (compatible with the inflight branch — single PR):
  - `slice` → `group` (everywhere: variables, fields, file names, JSON
    keys via versioned schema migration, prompt templates)
  - `Slice` dataclass → `Group` (in `otto/spec_compile.py` — the real
    module)
  - `capability` / `capability_verdict` → `feature` / `feature_verdict`
  - `certifier` → `audit` (module rename, function names, prompt files)
  - `story` / `stories_passed` / `stories_tested` → `feature` /
    `features_passed` / `features_tested`
  - `task` (in user-facing surfaces only): replace with "feature" or
    "todo item" depending on context. Internal agent loop variables
    can stay as `step`/`todo_item` — no user surface, no user concept.
- New file: `otto/defaults.py` — single source for retry counts,
  timeouts, budgets, default models, audit modes. Grep for embedded
  magic numbers in `otto/`, route through `defaults.get(...)`. Magic
  numbers currently live in `BuildBudget` dataclass fields (e.g.
  `per_slice_retries_hard_cap: int = 8`, `per_slice_wall_s: int = 30 * 60`),
  `otto/config.py`, and scattered callsites — find them all.

- **JSON-key backward-compat shims:**
  - `otto/spec_compile.py:load_spec` accepts both `"slices"` and
    `"groups"` keys, returning `Group` objects either way. Shim stays
    until Phase C deletion.
  - `otto/render.py` reads both `"capability_verdicts"` and `"features"`
    from old `proof-packet.json` files.
  - `otto/merge/stories.py` reads both `"stories"` and `"features"`
    from legacy proof manifests (renamed to `otto/merge/features.py`).
  - MC API response layer continues to emit legacy keys alongside new
    keys during Phase A coexistence; legacy keys removed in Phase C.
- Update prompts in `otto/prompts/*.md` to use the new vocabulary.
- Update existing tests' assertion strings to match new naming.

### Files

- All `otto/**` Python files (rename via Edit, not refactor tools, to
  ensure code review surface)
- `otto/web/client/src/**/*.{ts,tsx}` for any shared field names
- `docs/*.md`
- `tests/**/*.py`

### What's *not* renamed

- Files under `otto_logs/` (read-only history; legacy field names are
  fine in old session dirs)
- `bench-results/` (frozen)
- The `domain` field on `HistoryRow` — that goes away in Phase C, not
  here

### Verification

- `grep -rE '\b(slice|capability|capability_verdict|certifier|story|stories_passed|stories_tested|acceptance.check|\bAC\b)\b' otto/ tests/ docs/` returns zero hits (excluding intentional historical references in docs).
- `grep -rE '\b(retries|timeout|max_attempts|budget)\s*=\s*\d+' otto/ | grep -v defaults.py` returns zero hits.
- `pytest -q` green.
- `npm run web:typecheck && npm run web:build` green.
- TDD-style: change the rename target *first*, expect failures, fix
  cascading sites, watch test count climb back to baseline.

### Codex review cue

> "Review the vocabulary refactor diff. Confirm: (a) no retired words
> appear in user-facing surfaces — code, UI strings, prompts, log
> labels, file paths, JSON keys; (b) `otto/defaults.py` is the only
> place numeric retry/timeout/budget values are defined; (c) JSON
> schema changes are versioned (old `proof-packet.json` files still
> readable). Report any retired-word leaks, magic-number leaks, or
> back-compat breaks."

---

## Phase A1 — Spec / Group / Feature dataclasses (split into A1a/A1b/A1c)

**⚠️ Phase A1 was estimated 4-6 days as a single phase.** Plan reviewer
recommends splitting into three sub-phases with separate gates: each is
2 days. Total still 6 days but with internal checkpoints. **Each sub-phase
is a refactor of existing code, not net-new construction.**

**Sequencing change:** between A1a and A1b, run a 1-day "Phase A4 type
contract" sub-step that defines the `RunView` TypeScript interface
upfront. This locks the API contract before backend work continues, so
A1b/A1c/A2/A3 build against the right output shape.

### Phase A1a — Dataclasses (2 days)

Refactor `otto/spec_compile.py`:
- Rename `Slice` → `Group` dataclass + 10 import sites
- Rename `Slice.tasks` → `Group.feature_ids` (list of Feature ids)
- Add `Feature` dataclass with `id`, `name`, `description`,
  `acceptance_detail`, `evidence_kinds[]`, `group_id`, `verdict?`,
  `evidence_completeness`, `coverage_confidence`, `multi_actor_required`
- Add `Guardrail` dataclass with `id`, `text`, `applies_to`
- **Add `Component` dataclass** (research §2.6) with `id`, `name`,
  `description`, `owned_paths`, `dependencies`, `checks[]`, `consumed_by[]`
- **Add `Spec.shared_paths: list[str]`** (research §2.6)
- **Add per-`project_kind` structure schemas** (research §2.7) — webapp,
  api, library, cli variants
- **Add `audit_fixtures[]`** to Spec (research audit-honesty section)
- Add `parse_spec_md(md_text, base=None) -> Spec | ParseError`
- Add `render_spec_md(spec) -> str`
- Round-trip property test: `parse_spec_md(render_spec_md(s), base=s) == s`
- Backward-compat shim in `load_spec`: accepts both `"slices"` and
  `"groups"` keys

**Gate:** `pytest tests/test_spec.py` green; spec round-trip + JSON
back-compat tests pass.

### Phase A4 type contract (1 day, between A1a and A1b)

Define types upfront so backend implementation has a target shape:

- New file: `otto/web/client/src/types/run.ts`
- Define: `RunView`, `FeatureView`, `GroupView`, `ComponentView`,
  `GuardrailView`, `StageView`, `EvidenceRef`, `RunMeta`. Include
  `null` semantics for in-flight fields.
- Define `build_run_view(session_dir, *, live_state=None) -> RunView`
  signature in `otto/mission_control/run_view.py` with stub returning
  the correct shape.

**Gate:** typecheck passes; the stub returns valid `RunView` for the
fixture session dir; review-walkthrough reports' RunView field needs
all addressed.

### Phase A1b — Build + Checks (2 days)

Refactor `otto/build.py` and `otto/checks.py`:
- `build_groups` (already exists as `build_slices`) renamed and updated
  to dispatch by `Group` instead of `Slice`
- `BuildBudget` fields renamed (per_slice_* → per_group_*); values
  routed through `otto/defaults.py`
- **`Component` dispatch** — Components run alongside Groups in the
  same parallel build phase. Same agent model, no Feature verdict.
- **Shared-paths handling** — Group agents may edit shared_paths
  freely; merge queue serializes lands across Groups touching shared
  files (already covered by existing eligibility logic).
- Modify `otto/checks.py`: add `feature_id` field to `Evidence`;
  thread through `run_checks` signature.
- **Add new `Check` kinds:** `CLIProbe`, `ImportCheck`, `TypeCheck`
  (research §2.7).
- Updated walkthrough actions emit `action_kind` discriminator and
  per-kind fields (research §2.7).

**Gate:** `pytest tests/test_build.py tests/test_checks.py` green;
greenfield Run produces multi-Group artifacts; Component dispatch
verified.

### Phase A1c — Merge (2 days)

Refactor `otto/merge_queue.py` + `otto/merge/`:
- Eligibility logic uses `Group.dependencies` + `shared_paths` rule
- `Component` dependencies threaded into eligibility ordering
- Per-Component conflict repair (Components have agents like Groups)
- Stories module renamed: `otto/merge/stories.py` → `otto/merge/features.py`
  with backward-compat reader for legacy `"stories"` JSON

**Gate:** `pytest tests/test_merge.py` green; integration test:
two Groups + one Component land in dep order; conflict repair on
shared_paths works.

### Scope

- `otto/spec.py`:
  - `Spec` dataclass with `intent`, `features[]`, `groups[]`,
    `guardrails[]`, `structure`, `project_kind`, `schema_version`.
  - `Feature` dataclass with `id`, `name`, `description`,
    `acceptance_detail`, `evidence_kinds[]`, `group_id`, `verdict?`,
    `audit_pre_merge?`, overrides.
  - `Group` dataclass with `id`, `name`, `description`, `feature_ids[]`,
    `dispatch_plan`, `owned_paths`, `dependencies[]`, overrides.
  - `Guardrail` dataclass with `id`, `text`, `applies_to`.
  - `compile_spec(intent, project_kind, base=None) -> Spec` — LLM call.
    Reuses tightened `otto/prompts/compile.md`.
  - `compile_validator(spec) -> ValidationResult` — schema check.
- `otto/checks.py`: `Check` base + kinds (`RepoTestCheck`, `ApiProbe`,
  `StateInvariant`, `BrowserJourney`); `run_check(check, project_dir,
  *, feature_id) -> Evidence`. Evidence carries `feature_id`.
- `otto/build.py`: `build_groups(spec, session_dir)` dispatches per
  `Group` to a long-lived agent on its own worktree/branch. Internal
  Check loop bounded by `defaults.retries.check_loop.max_attempts_per_group`.
- `otto/merge.py`: eligibility-gated FIFO merge queue per Group.

### Files

- New: `otto/spec.py`, `otto/checks.py`, `otto/build.py`, `otto/merge.py`
- Modified: `otto/cli.py` — `otto run` routes through these
- New: `tests/test_spec.py`, `tests/test_checks.py`, `tests/test_build.py`,
  `tests/test_merge.py`

### Verification

- Unit tests cover: spec round-trip JSON, validator catches under-spec'd
  webapps, check kinds happy + failure paths, build dep ordering,
  merge eligibility ordering with deps + staleness.
- Integration test: a fixture intent compiles → spec.json with ≥1
  Group, ≥2 Features per Group, all Features have `id` and `group_id`.
- `pytest -q tests/test_spec.py tests/test_checks.py tests/test_build.py tests/test_merge.py` green.

### Codex review cue

> "Review `otto/spec.py`, `otto/checks.py`, `otto/build.py`,
> `otto/merge.py`. Confirm: (a) no retired words; (b) every numeric
> default routes through `otto/defaults.py`; (c) Feature ids are
> stable across compile→build→audit→render (a Feature can be
> referenced post-Run by id); (d) Groups can have multiple Features
> and a Feature has exactly one `group_id`; (e) merge eligibility
> respects `Group.dependencies` and serializes correctly on
> `owned_paths` overlap; (f) the Check loop's retry cap is
> configurable, not hardcoded."

---

## Phase A1.5 — Seed stage (1 day)

**Existing state:** does not exist. Net-new module.

For multi-user products needing pre-seeded fixtures (research audit-
fixtures section), add a Seed stage between Build and Audit:

- New file: `otto/seed.py` — `seed_fixtures(spec, session_dir) -> SeedResult`
- Reads `Spec.audit_fixtures[]`, applies fixtures to live product
- Idempotent on rerun
- Failed seed = blocked Run, not silent proceed-with-empty-state
- Per-fixture-kind handlers: `user`, `channel`, `follow`, `data` (extensible)

**Gate:** `pytest tests/test_seed.py` green; integration: Run with
`audit_fixtures` declares pre-existing test users before audit walks.

---

## Phase A2 — Audit Feature-tagging

**Goal:** every walkthrough action records which Feature(s) it
evidences. Audit verdicts are per-Feature, with evidence refs.

### Scope

- `otto/audit.py`:
  - `audit_run(spec, session_dir) -> AuditResult` — single LLM pass on
    integrated product. Walkthrough produces `walkthrough.jsonl`, each
    line `{feature_ids[], action, narrative, screenshot?, dom_snapshot?}`.
  - Audit prompt explicitly instructs the agent to tag each action
    with the Feature(s) it evidences. Prompt enforces "no untagged
    actions" — if an action evidences nothing in the Spec, log it as
    `{feature_ids: [], note: "exploration"}`.
  - `feature-verdicts.json` per attempt: per-Feature verdict +
    evidence-ref list (paths into walkthrough.jsonl).
  - `quality-findings.json` per attempt: severity-tagged informational
    findings.
- `otto/audit_loop.py`: Layer 2 retry. On failing Feature, route to its
  Group's agent for one repair pass, re-audit only affected Features.
  Bounded by `defaults.retries.audit_loop`.

### Files

- New: `otto/audit.py`, `otto/audit_loop.py`
- New: `otto/prompts/audit.md` (replaces legacy certifier prompts)
- New: `tests/test_audit.py`

### Verification

- Unit test injects a fixture spec + a fixture walkthrough, asserts
  Feature verdicts derived correctly, evidence refs link to actual
  walkthrough lines.
- Audit loop test: failing Feature → repair attempt → re-audit affects
  only that Feature, not others.
- Real-cost integration test (gated `OTTO_ALLOW_REAL_COST`): a fixture
  greenfield Run produces walkthrough.jsonl where every line has
  populated `feature_ids[]` (no untagged-other-than-exploration lines).
- **Coverage threshold: ≥ 90% of non-exploration walkthrough actions
  have non-empty `feature_ids[]`.** Below 90%: rewrite audit prompt
  before proceeding. **Backup plan** if 3 real-cost runs are below 90%:
  post-process via a cheap LLM pass mapping action narrative text to
  spec feature ids; if still below 90% after post-processing, fall
  back to per-Group verdicts only with proof packet stating "per-
  Feature attribution unavailable for this Run."
- **Validation step:** every emitted `feature_ids[]` element matches a
  known Feature id in the spec; unknown ids logged as warnings;
  unrecognized-id rate > 5% blocks the Run.
- **Severity ladder** test: inject a Feature whose audit produces a
  `critical` quality finding; assert verdict flips to `partial` and
  Layer 2 repair triggers.
- **Audit honesty test:** inject a `multi_actor_required` Feature
  (e.g. cross-user notification); assert Feature verdict has
  `evidence_completeness=proxy_only` and narrative explains why.

### Codex review cue

> "Review `otto/audit.py` and `otto/audit_loop.py`. Confirm: (a) every
> walkthrough action carries `feature_ids[]`; (b) Feature verdicts are
> derived only from tagged actions, never from agent narrative alone;
> (c) audit loop retries are configurable; (d) audit prompt explicitly
> requires Feature tagging and the parser rejects untagged actions
> outside the 'exploration' allowlist; (e) the audit pass is honest
> about partial verdicts — a Feature with 0 evidence refs returns
> `verdict: missing` not `passed`."

---

## Phase A3 — Render — per-Feature mini-packet

**Goal:** Render produces whole-product Proof + per-Feature pages,
deterministic and re-runnable.

### Scope

- `otto/render.py`:
  - `render_proof(session_dir) -> RenderResult` — pure function.
  - Reads: `spec.json`, `audit/attempt-NN/walkthrough.jsonl`,
    `audit/attempt-NN/feature-verdicts.json`, `groups/<id>/*`,
    `state.jsonl`.
  - Writes: `proof/proof-packet.html`, `proof/proof-packet.json`,
    `proof/features/<feature-id>/proof.html`,
    `proof/features/<feature-id>/proof.json`, `proof/assets/`.
  - HTML template: feature list primary, group expander secondary,
    stage timeline tertiary, run metadata collapsed.
  - Anchors: whole-product packet has `#feature-<id>` for each
    Feature.
  - Multi-Feature evidence: walkthrough segments that evidence N
    Features render under each, with `data-shared-with="..."` markup.
- `otto/cli.py`: `otto render <session-id>` re-runs Render with no LLM
  cost.

### Files

- New: `otto/render.py`
- New: `otto/web/templates/proof-packet.html.j2` (Jinja or similar)
- New: `otto/web/templates/feature-proof.html.j2`
- New: `tests/test_render.py`

### Verification

- Unit test: fixture session dir → run `render_proof` → assert all
  files exist, all per-Feature pages contain at least one evidence
  ref.
- Re-run test: same session, run Render twice. Output is byte-stable
  **only when `rendered_at` is pinned to a sentinel** (set via
  `OTTO_RENDER_TIMESTAMP` env var in tests). Asserts: same Feature
  count, same verdict, same evidence-link hrefs, same Feature ids,
  same Group ids. **Raw HTML byte equality is NOT asserted** — it
  false-negatives on template-whitespace changes. Use semantic
  equivalence: parse HTML, count `<div class="feature">` elements,
  assert count equals spec feature count.
- **Determinism guards in code:** `time.time()` calls behind
  `now_or_sentinel()`; all asset paths normalized session-relative;
  floats formatted via `f"{cost:.4f}"` (no `.4g`); dict iteration via
  explicit sort.
- **Per-`project_kind` proof template branch:** assert
  `feature-proof.html.j2` renders correctly for webapp/api/library/cli
  fixture sessions. Non-visual proofs render terminal-style transcripts
  / API request-response tables / import status tables — not empty
  screenshot grids.
- Snapshot test: golden HTML for a fixture spec — review changes
  manually before merging.
- Edge cases: Feature with 0 evidence refs renders as "missing"
  honestly, not as empty page. Multi-Feature segment cross-links.

### Codex review cue

> "Review `otto/render.py`. Confirm: (a) Render is a pure deterministic
> function (no LLM calls, no time-dependent output, no random); (b)
> per-Feature pages are first-class, not afterthought sections of the
> whole-product packet; (c) anchor scheme is consistent (`#feature-<id>`
> matches `<a href>` references); (d) multi-Feature evidence is
> cross-linked, not duplicated; (e) `proof-packet.json` schema is
> versioned and stable; (f) re-running Render on an old session
> doesn't break (forward-compatible)."

---

## Phase A4 — MC redesign — Feature-first run drawer

**Goal:** click any Run, see Features as primary surface, Groups as
secondary, stage timeline as tertiary. Legacy WARN noise gone.

### Scope

- Backend:
  - `otto/mission_control/run_view.py` — pure function
    `build_run_view(session_dir, *, live_state=None) -> RunView`. Reads
    Proof + state, returns shape ready for frontend.
  - Mount `/api/runs/<id>` returning RunView. (i2p_routes can become a
    thin wrapper or be merged in.)
- Frontend:
  - New dir `otto/web/client/src/components/run/`:
    - `RunsView.tsx` — landing list (replaces today's Tasks panel)
    - `RunDrawer.tsx` — single drawer; dispatches by run state
    - `VerdictHeader.tsx`, `FeatureList.tsx`, `GroupList.tsx`,
      `StageTimeline.tsx`, `Guardrails.tsx`, `RunMetadata.tsx`
    - `MetricChip.tsx`, `EvidenceLink.tsx` (primitives)
  - Types: `RunView`, `Feature`, `Group`, `Guardrail`, `Stage`,
    `EvidenceRef` in a fresh `otto/web/client/src/types/run.ts`. Old
    `types.ts` kept until Phase C.
  - Routing: if `run.domain === "i2p"` (or post-rename, if Run has the
    new shape) → `<RunDrawer />`. Else legacy panel unchanged.

### Files

- New: `otto/mission_control/run_view.py`
- New: `otto/web/client/src/components/run/*.tsx`
- New: `otto/web/client/src/types/run.ts`
- Modified: `otto/web/client/src/App.tsx` (or equivalent entry) for
  routing
- New: `tests/test_run_view.py`, `tests/browser/test_run_drawer.py`

### Verification

- Backend: `tests/test_run_view.py` — fixture session → `RunView` with
  correct Feature counts, Group expansion data, stage durations.
- Frontend: typecheck + build green.
- Browser: chrome-devtools RUA pass on `/tmp/otto-e2e/p7-shortener`,
  `/tmp/otto-e2e/p9-kanban`, and the in-flight `/tmp/otto-e2e/p10-docflow`
  if it lands. Screenshot every panel state.
- Regression: legacy `otto build` runs still render the unchanged old
  panel (snapshot test).

### Codex review cue

> "Review the MC redesign — `otto/mission_control/run_view.py` and
> `otto/web/client/src/components/run/`. Confirm: (a) FeatureList is
> primary surface, GroupList is one click below, stages are tertiary;
> (b) every UI string uses unified vocabulary (no slice/capability/
> certifier/story); (c) values displayed in MC trace to a real field
> in `proof-packet.json` or `state.jsonl` — no MC-side derivations
> beyond formatting; (d) per-Feature drilldown link routes to
> `/api/sessions/<id>/features/<feature-id>` correctly; (e) live runs
> render via the same drawer with fields appropriately partial; (f)
> legacy panel components are not modified."

---

## Phase A5 — Hybrid plan ownership + spec review screen

**Goal:** user can edit Features, Groups, Guardrails before Build.
Approve / regenerate / abort.

### Scope

- Backend:
  - Spec review gate state in `state.jsonl`: `spec.review.opened`,
    `spec.edited`, `spec.approved`, `spec.regenerated`.
  - API: `POST /api/runs/<id>/spec/edit` (apply user edits + checkpoint),
    `POST /api/runs/<id>/spec/approve`, `POST /api/runs/<id>/spec/recompile`.
- Frontend:
  - New: `otto/web/client/src/components/run/SpecReview.tsx` —
    feature checkboxes, Group expander, Guardrail pill input,
    add/remove operations, approve button, recompile button.
  - When a Run pauses at the spec gate, `RunDrawer` renders
    `<SpecReview />` instead of `<FeatureList />`.

### Files

- Modified: `otto/spec.py` for in-place edit operations
- New: `otto/mission_control/spec_review_routes.py`
- New: `otto/web/client/src/components/run/SpecReview.tsx`
- New: `tests/test_spec_review.py`

### Verification

- Unit: edit operations preserve `feature.id` stability across edits.
- Integration: launch a Run, pause at spec gate, edit via API, approve,
  Build proceeds with edited Spec.
- Browser RUA: spec review flow end-to-end.

### Codex review cue

> "Review spec review. Confirm: (a) Feature ids are stable across
> edit operations (so post-Run audit-by-feature-id works); (b) edits
> are versioned (`spec-v1.json`, `spec-v2.json`); (c) approve flow
> doesn't lose user edits; (d) recompile preserves user-added Features
> if compatible with new structure."

---

## Phase A6 — Brownfield compile mode

**Goal:** `otto run` works against an existing project with code, only
emitting new/changed Features.

### Scope (deferred — see research §9.4 and §11.4)

- Compile reads working tree + existing `spec/spec.json`.
- Diff against existing Spec; only emit deltas.
- File-level "preserve" markers (mechanism TBD; recommend `.otto/preserve.json`).
- Brownfield Compile prompt variant.

This step is sketched, not implemented yet — gated on Phase A0–A5
proving stable on greenfield.

---

## Phase B — Cutover

**Goal:** legacy CLI commands route through the new stack.

- `otto build` → maps Intent + project_kind → calls `otto run` under
  the hood. Same session dir, same Proof packet.
- `otto certify` → `otto run --rerun-audit <existing-session>` with
  appropriate flags.
- `otto improve` → `otto run` with brownfield Compile mode + intent
  describing the improvement.
- Legacy Code paths emit `DeprecationWarning`.

### Phase B parity gate (concrete criteria, inlined)

**2 of 3 runs must pass all criteria** (single-run variance is too
high to gate on a single pass):

- 0 Features blocked (note: was "0 slices blocked" pre-rename)
- Wall time ≤ 36 min (1.5× mono baseline of ≈ 24 min)
- Cost ≤ 1.2× mono baseline
- Audit verdict = `passed`
- Browser private evaluator passes
- Hidden evaluator passes
- **Human check:** landing page loads, ≥ 2 Features visible in UI,
  no console errors in browser DevTools (reviewer captures
  screenshot for `review.md` evidence)

### Verification

- Existing bench scripts (Microfeed, etc.) point at `otto build` —
  run them via legacy aliases, confirm equivalent output via the new
  stack.
- Run Microfeed bench 3 times; require 2-of-3 pass per criteria above.

---

## Phase C — Deletion

**Goal:** delete everything in research §13.

Single PR. Big diff. Codex implementation gate is mandatory.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Vocabulary refactor breaks running benches | Phase A0 lands before any new behavior; full bench rerun confirms baseline |
| Audit Feature-tagging rate is < 100% (some actions untagged) | Audit prompt enforces tagging; parser rejects untagged-non-exploration; audit loop fails-fast on parser violation |
| Per-Feature audit cost balloons | Default to whole-product audit with Feature anchors; per-Feature only on demand |
| Phase B cutover breaks user automation | Keep `otto build` as alias for one minor version with `DeprecationWarning`, delete in Phase C |
| Legacy MC users see broken panels post-rename | Phase A is purely additive; legacy panel components untouched |
| Multi-Group runs with file overlap deadlock | Merge queue test coverage for owned_paths overlap; eligibility ordering with deps |
| User edits to Spec break Feature id stability | Edit operations use stable id generator; renaming a Feature changes `name`, not `id` |

---

## Codex Plan Gate

Per CLAUDE.md mandatory protocol:

1. Before starting Phase A0 implementation, dispatch
   `mcp__codex__codex` with:
   - This plan + research.md + conversation transcript
   - Mode: `read-only`, `approval-policy: "never"`
   - Prompt: "Adversarial Plan Gate review. Find: missing steps,
     ordering bugs, risks not in the register, vocabulary gaps,
     'verification' steps that don't actually verify."
2. Address every Codex finding before opening implementation work.
3. Append review trail to this file as `## Codex Plan Gate review N`
   sections.
4. Up to 4 rounds. Do not begin Phase A0 implementation until Codex
   returns APPROVED.

---

## What's not in this plan

- Concrete UI design (visual styling, color palettes, animation):
  use the wireframes in `docs/otto-wireframes.md` as scaffolding;
  detailed visual design is downstream of structural agreement.
- Bench parity criteria for Phase B: inherit from
  `~/.claude/plans/plan-based-on-this-soft-cloud.md` step 11.
- Concurrent-Run support (research §11.6): out of scope; single
  project lock retained.
