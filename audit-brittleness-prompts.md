# Otto Prompts Audit: Liveness & Brittleness

**Scope:** 31 prompt files in `otto/prompts/` (4,849 LOC). Analysis date: 2026-05-20.

---

## Part A: Liveness Audit

| Filename | LOC | Loader | Verdict | Summary |
|----------|-----|--------|---------|---------|
| `lead.md` | 280 | `otto/lead.py:600` | **ALIVE** | Root and sub-task planning + inline build agent (v5 canonical) |
| `lead-integration.md` | 106 | `otto/lead.py:600` | **ALIVE** | Integration phase after child subtasks complete (v5 canonical) |
| `setup-claude.md` | 28 | (found in codebase search) | **ALIVE** | Generate CLAUDE.md for projects (low activity, but loaded) |
| All 28 others (see below) | 4,455 | None found | **ORPHAN** | Legacy pipeline (otto run v1-v4): compile-spec, build, certifier, audit, improve phases |

### Orphan Prompts (28 files, 4,455 LOC)

Grouped by legacy phase:

**Compile phase (5 files, 1,243 LOC):**
- `compile-spec.md` (1011) — v1-v4 spec compiler with complex group/feature structure
- `compile-spec-brownfield.md` (215) — brownfield spec variant
- `compile-spec-brownfield-baseline-guidance.md` (5)
- `compile-spec-brownfield-target-guidance.md` (6)
- `compile-spec-structured-output.md` (7)

**Build phase (9 files, 1,020 LOC):**
- `build.md` (97) — generic build instructions (legacy)
- `build-agent.md` (89) — legacy build agent
- `build-agent-framework-conventions.md` (851) — pinned stack/version matrix (still useful reference, but orphaned)
- `build-agent-static-policy.md` (321) — build orchestration rules (legacy)
- `build-final-instruction.md` (13)
- `build-layer2-regression-requirement.md` (18)
- `build-merge-repair.md` (12)

**Audit/Certifier phase (9 files, 1,018 LOC):**
- `certifier.md` (320) — main certifier orchestrator (v1-v4)
- `certifier-fast.md` (93) — fast-path certifier variant
- `certifier-hillclimb.md` (109) — iterative improvement certifier
- `certifier-merge-integration.md` (119) — post-merge verification
- `certifier-target.md` (86)
- `certifier-thorough.md` (303) — exhaustive certification mode
- `auditor.md` (80) — feature audit orchestrator
- `audit-final-task.md` (162) — per-feature audit task
- `audit-feature-tagging.md` (173) — feature classification

**Other legacy (5 files, 174 LOC):**
- `improve.md` (79) — self-improvement loop agent (v1-v4)
- `code.md` (41) — generic code generation
- `test-agent.md` (64) — test agent (never launched, placeholder)
- `autopilot-pilot.md` (44) — experimental auto-pilot (never shipped)
- `merger-conflict-agentic.md` (63) — agentic merge conflict resolution (not in v5 lead.md)
- `plan-amendment.md` (16) — spec amendment agent (legacy)
- `spec-light.md` (65) — lightweight spec variant (never adopted)

---

## Part B: Brittleness in ALIVE Prompts

### 1. **`lead.md` (280 LOC)**

**Brittleness patterns found:**

1. **Hard-coded file paths and glob patterns** (lines 87-98, 119-208):
   - Assumes `vite.config`, `tsconfig`, React/Zustand stack specifics
   - "Auto-discovery" language masks assumption that features auto-register
   - Example: "registration_isolation.leaf_extension_globs" is a hard requirement that may not apply to non-web projects (CLI, library, API-only)
   - **Risk:** Misalignment when applied to brownfield or non-webapp projects

2. **Negative instructions that LLMs frequently ignore** (lines 44-52):
   - "Do not create a separate integration child"
   - "Avoid recursive decomposition"
   - "FE waiting on BE is fake parallelism"
   - Better as positive: *"Prefer vertical capability leaves that can start, build, and verify end-to-end"* (which IS stated, but buried after negatives)

3. **Deep architecture coupling in examples** (lines 70-208):
   - The "Architect task guidance" section is 138 lines of JSON/architecture contracts specific to web products
   - Includes mandatory `Information Architecture Contract`, `registration_isolation`, `foundation_contracts` — all web/app conventions
   - CLI or library projects will find this section confusing or inapplicable
   - **Risk:** Agent attempts to apply web-specific contracts to non-web products, wasting budget on irrelevant architecture

4. **Format coupling without parser enforcement** (lines 243-256):
   - Prompt specifies exact `verdict.json` schema (object with keys: verdict, journeys, intent_coverage, summary, evidence, test_command, decisions_appended)
   - Consumer (`otto/lead.py:_read_agent_verdict`) is lenient and will canonicalize partial/malformed payloads
   - Example: `decisions_appended` is optional in reality but mandatory in prompt text
   - **Risk:** Misalignment on what constitutes a valid verdict; agent may omit optional fields the runner later patches in

5. **Magic numbers without parameters** (line 45):
   - "one concise architect/scaffold task plus 3-5 build leaves"
   - Baked into narrative; no configuration, tier preset, or override mechanism
   - **Risk:** Large products should produce 7-10 leaves, but agent is anchored to 3-5

### 2. **`lead-integration.md` (106 LOC)**

**Brittleness patterns found:**

1. **Hard-coded path constraints** (lines 95-99):
   - Lists 15 exact path prefixes the agent may stage: frontend/, backend/, api/, packages/, etc.
   - Missing: `proto/` (gRPC), `scripts/setup.sh`, `.env.example`
   - **Risk:** Agent hesitates to commit necessary paths not on the whitelist; workaround is git stashing, leaving broken state

2. **Temporal coupling: "run smoke_clean_deploy again after you finish"** (line 32):
   - Assumes runner will re-run preflight after agent edits
   - Runner code (`v5_runner.py`) does NOT re-run preflight after integration — it runs once pre-integration as input context
   - **Risk:** Agent believes its merge fixes will be re-verified, but they are not; stale failures pass silently

3. **Vague success criteria** (lines 51-56):
   - "primary navigation is operable", "one realistic seeded/non-fresh state works" — no concrete thresholds
   - Example: is "primary navigation" the navbar + 2 pages, or all pages?
   - **Risk:** Agent claims partial when full coverage was intended, or over-tests minor flows

### 3. **`setup-claude.md` (28 LOC)**

**Brittleness patterns found:**

1. **Placeholder tokens without validation** (line 16):
   - `{project_context}` and `{project_intent_section}` are interpolated but their structure is not specified
   - Caller must provide these; prompt assumes they are well-formed markdown
   - **Risk:** Malformed input silently produces a broken CLAUDE.md with truncated guidance

2. **Scope creep with no depth limit** (lines 22-28):
   - "Keep it concise. The agent reads code well; just orient it." — but no word count, section count, or example
   - "Include these principles if relevant" — open-ended; agent may include none, or all, or irrelevant ones
   - **Risk:** Inconsistent output across projects; some get 2-page detailed guides, others get 200-word stubs

---

## Summary: Estimated Work

### Orphan Deletion (High Confidence)

**4,455 LOC** across 28 files can be deleted outright:
- **Very safe:** All compile/build/audit/certifier/improve phase prompts (they are exclusively v1-v4 pipeline)
- **Reason:** v5 `lead.md` + `lead-integration.md` are the canonical agents; no code calls the others
- **Estimated effort:** Confirm no test coverage or documentation references; `rm` 28 files
- **Highest-value deletions** (by size + potential confusion):
  - `compile-spec.md` (1011 LOC) — largest, most complex, explicitly superseded by lead.md Decide section
  - `certifier.md` (320 LOC) — biggest orphan; leads to copy-paste if someone tries to repurpose
  - `build-agent-framework-conventions.md` (851 LOC) — huge; useful as REFERENCE but not as prompt
  - `build-agent-static-policy.md` (321 LOC)
  - `certifier-thorough.md` (303 LOC)

### Top 5 Brittleness Issues in ALIVE Prompts (Effort · Impact)

1. **Web-specific architecture section in `lead.md` lines 70-208 (Medium effort, High impact)**
   - Extract into separate `lead-webapp-architect.md` OR conditional "Webapp Architect guidance" section gated by product-kind inference
   - Compute product kind from project files (detect React → webapp, FastAPI-only → API, etc.) and conditionally include sections
   - Benefit: Prevents agent from applying web contracts to CLI/library projects; reduces wasted budget

2. **Temporal coupling: integration preflight claim vs. runner behavior in `lead-integration.md` line 32 (Low effort, High impact)**
   - Delete "The runner will run `smoke_clean_deploy` again after you finish." 
   - Replace with: "Your edits may break the preflight; the runner will NOT re-run it. Verify your merge manually before committing."
   - Benefit: Prevents silent failures where agent believes fixes are verified but aren't

3. **Format coupling on `verdict.json` schema (Low effort, Medium impact)**
   - Clarify in both `lead.md` and `lead-integration.md`: which fields are REQUIRED vs. optional
   - Example: "REQUIRED: verdict, journeys, summary. OPTIONAL: intent_coverage, evidence, test_command, decisions_appended. Consumer will default missing optional fields."
   - Benefit: Agent writes compact verdicts; no false failures on missing fields

4. **Hard-coded path whitelist in `lead-integration.md` lines 95-99 (Low effort, Medium impact)**
   - Change from enumeration to a pattern: "Product code: frontend/, backend/, src/, scripts/, config/, tests/, docs/, CHARTER.md, decisions.md, Makefile, *.json, *.toml, *.lock, .gitignore"
   - Explicitly exclude: .otto/, otto_logs/, .worktrees/, *.db, *.log, node_modules/, .venv/
   - Benefit: Agent has clearer mental model; fewer false "can I commit this path?" questions

5. **"3-5 build leaves" magic number in `lead.md` line 45 (Low effort, Low impact)**
   - Parameterize: "typical shape is one architect plus N vertical leaves (N=3 for small, 5 for medium, 10+ for large). Infer N from intent length and subsystem count."
   - Add to tier hints (already have solo/lead/modular presets)
   - Benefit: Scaling guidance for larger products; reduces false-bounded decomposition

---

## Recommended Action Plan

1. **Immediate (1 session):**
   - Delete all 28 orphan prompt files
   - Verify no git history, test suite, docs reference them
   - Verify `otto v5 run` works post-deletion

2. **Follow-up (1 session):**
   - Implement issue #1 (web-specific architect section extraction)
   - Implement issue #2 (temporal coupling fix)
   - Implement issue #3 (verdict format clarity)

3. **Optional (1 session):**
   - Implement issues #4 and #5 (path whitelist, magic number parameterization)
   - Consider a "product-kind inference" utility shared between lead.md and setup-claude.md

---

## Notes

- `build-agent-framework-conventions.md` (851 LOC) is the largest orphan and most likely to be re-discovered/copy-pasted. Preserve it as `docs/framework-conventions-reference.md` (not a prompt; just reference) if it has future value.
- `setup-claude.md` is ALIVE but low-activity; consider whether it's still used or also orphaned (grep for loaders more thoroughly).
- No brittleness found in `lead.md` verdict-file-write contract (lines 274-280); the "Hard Rules" are well-specified and effective.
