# Otto Prompt Content Audit Report

Date: 2026-05-20
Scope: `lead.md` (280 LOC), `lead-integration.md` (106 LOC), `setup-claude.md` (28 LOC)

---

## Summary

This audit found **27 findings** across the three prompts, ranging from magic numbers without justification, contradictory guidance, hard-coded path whitelists, project-kind assumptions, and undebuggable instructions.

Key categories:
- **A (Magic numbers)**: 4 findings
- **B (Brittle examples)**: 7 findings
- **C (Contradictions)**: 3 findings
- **D (Unreasonable/undebuggable)**: 4 findings
- **E (Mixed voice)**: 2 findings
- **F (Format-coupled output)**: 4 findings
- **G (Hard-coded path whitelists)**: 2 findings
- **H (Project-kind assumptions)**: 1 finding

---

## Detailed Findings

### A: Magic Numbers

### F-01: Decompose size heuristic (3-5 build leaves)
- File:line: `lead.md:45`
- Category: A
- Severity: MEDIUM
- The text: "one concise architect/scaffold task plus 3-5 build leaves"
- Why it's brittle: The prompt specifies 3-5 leaves as a "usual shape" for a moderate web app but provides NO justification, no conditions on when to deviate, and no coupling to actual metrics (project complexity, critical path, scope). The prompt earlier (line 39) says to "reason about wall-clock critical path, not child count" yet then prescribes a child count range. Code does not enforce or validate this range anywhere.
- Suggested fix: Needs design decision: replace the prescriptive range with decision criteria (e.g., "decompose when leaf context would exceed X tokens" or "when subsystems have zero cross-dependencies").

### F-02: JSON field version specification (dec-...)
- File:line: `lead.md:254`
- Category: A
- Severity: MEDIUM
- The text: `{"decision_id": "dec-...", "summary": "contract decision"}`
- Why it's brittle: The prompt shows a placeholder `dec-...` format for decision_id but (a) never explains how agents should generate IDs, (b) never specifies ID uniqueness or collision avoidance, and (c) code does NOT auto-generate IDs — agents are expected to invent them. The runner simply passes through whatever agents write; there's no validation of format.
- Suggested fix: Either provide an auto-generated ID via the runner (timestamp+hash) or state explicitly "agents must generate unique IDs in form `dec-<YYYYMMDD>-<scope>-<N>`" with examples.

### F-03: Task role field values (feature vs foundation)
- File:line: `lead.md:66-67`
- Category: A
- Severity: LOW
- The text: `task_role="foundation"` and `task_role="feature"`
- Why it's brittle: The prompt introduces two task role values but does NOT enumerate them, explain what other values are invalid, or show where this field is validated. Code accepts role as a string; unclear if other values (e.g., `"integration"`, `"scaffold"`) are legal.
- Suggested fix: Explicitly enumerate all valid task_role values and their semantics in the prompt section that first introduces subtasks (line 59-68).

### F-04: `check` field values (literal vs semantic)
- File:line: `lead.md:104-105`
- Category: A
- Severity: MEDIUM
- The text: `check: "semantic"` is correct for content that legitimately evolves... use `"literal"` only for exact-match files`
- Why it's brittle: The prompt specifies two check modes but provides no rationale for the distinction and no examples of what "semantic" actually means in practice. Later (line 171) says "route registries must use `literal`" but this rule is presented as an addendum, not derived from the `semantic/literal` definition. Agents cannot predict when to use which.
- Suggested fix: Define check modes upfront with concrete examples: `"literal"` = byte-exact match enforced; `"semantic"` = content may evolve if behavior-equivalent (+ list allowed changes). Cite example: `conftest.py` is `semantic` because imports may drift; `router_loader.py` is `literal` because registration patterns must not change.

---

## B: Brittle Examples / Hard-Coded References

### F-05: Framework and stack specifics pinned without fallback
- File:line: `lead.md:73-74`
- Category: B
- Severity: HIGH
- The text: "Vite/TS-strict, React, zustand, FastAPI, SQLAlchemy single-Base, ports/start.sh"
- Why it's brittle: The prompt lists a specific "pinned, version-locked framework" but (a) does NOT explain where this spec comes from, (b) does NOT say what happens if the project uses different stack (Django, Flask, Svelte, Vue), (c) assumes start.sh exists, (d) hardcodes database ORM choice (SQLAlchemy). A CLI or library project gets dangerously misguided by this FE/BE webapp assumption.
- Suggested fix: Change to: "The scaffold must use the stack specified in DECOMP_RUNTIME_CONTEXT or the project's existing stack; if neither is available, ask the user. Do NOT assume React/FastAPI/SQLAlchemy."

### F-06: Port and start.sh assumption
- File:line: `lead.md:74, 77, 79`
- Category: B
- Severity: MEDIUM
- The text: "ports/start.sh" (lines 74, 77) and "`start.sh`" (line 79)
- Why it's brittle: Assumes every project has start.sh and a ports/ directory. A CLI project or library would not. Line 211 says "Verify the scaffold with the smallest build/typecheck command" but then the prompt never tells the agent what that command is for non-webapp projects.
- Suggested fix: "If the project is a webapp, verify via start.sh. Otherwise, verify via the project's existing build/test command (read the repo to find it)."

### F-07: Hardcoded test/build config file names
- File:line: `lead.md:101-102`
- Category: B
- Severity: MEDIUM
- The text: "`conftest.py`, `tests/setup.*`, `jest.config.*`"
- Why it's brittle: Names test harness files but does NOT say "if these exist, scaffold owns them; if not, create them" or "if project uses Vitest instead of Jest, use `vitest.config.*`". A Python project without pytest is left stranded.
- Suggested fix: "Scaffold must own test bootstrap (conftest.py for pytest, or equivalent for the project's actual test runner). Discover the test runner from the project's build/package config."

### F-08: Backend database table namespace assumption
- File:line: `lead.md:149`
- Category: B
- Severity: MEDIUM
- The text: "shared ORM declarative `Base`/`MetaData`"
- Why it's brittle: Assumes SQLAlchemy ORM with a shared Base. A project using Tortoise-ORM, Django ORM, or raw SQL has no `Base` concept. The rule (one shared metadata) is sound but the example is stack-specific.
- Suggested fix: "In any project with a shared database, ensure one canonical entity definition location (the 'schema registry'). For SQLAlchemy, this is a shared `Base`. For Django, this is the shared `models` module. Define the equivalent for your stack."

### F-09: Frontend state container assumption (zustand/context)
- File:line: `lead.md:117-130`
- Category: B
- Severity: MEDIUM
- The text: "context/hook/store/client... (e.g. `frontend/src/store/uiStore.ts`, `hooks/useWebSocket.ts`)"
- Why it's brittle: Assumes React with hooks/context. A project using a different framework (Vue, Svelte, plain HTML/JS) would not have these APIs. The shell example `useToast(): {...}` is React-specific.
- Suggested fix: "If the project is a multi-feature FE app, shared state must be composition-based: define one scaffold-owned composition point (store, provider, module) that features extend via globs, not edit. Examples: [React hooks], [Vue provides], [Svelte stores]. Show the equivalent for your FE framework."

### F-10: API-specific example for proof of completion
- File:line: `lead.md:177`
- Category: B
- Severity: LOW
- The text: "`useToast(): { showToast(message: string, type?: 'success'|'error'): void }`"
- Why it's brittle: Shows React hook signature. A GraphQL backend or CLI would not have this API. The example is valuable but frame it as "FE example; show equivalent for your stack."
- Suggested fix: Prefix with "Example (React): ".

### F-11: Hard-coded path examples missing project-kind variants
- File:line: `lead.md:117, 121, 138, 140-141, 157-158`
- Category: B
- Severity: MEDIUM
- The text: All `backend/routers/`, `backend/models/`, `frontend/src/features/`, `schemas/<feature>.py` examples
- Why it's brittle: Uses webapp directory structure throughout. A CLI project (single binary, no backend) or library (src/ only, no frontend) is left without guidance.
- Suggested fix: Add conditional prose: "If the project is a webapp, follow paths like `backend/routers/<feature>/router.py`. If it's a CLI, use `src/<feature>/commands.py`. Use the existing project structure as the template."

---

## C: Contradictions

### F-12: Contradiction: "don't read codebase" vs "read journeys path and CHARTER"
- File:line: lead.md does not say "read the entire codebase" but DOES say "Read the scoped context path first when provided. Otherwise read CHARTER.md" (lines 27-29), and then lines 218+ extensively describe reading project structure
- Category: C
- Severity: LOW
- The text: Implicit contradiction between the principle "Prefer inline work... without waiting" (line 40-52, implying agents should bound their scope) and "read CHARTER, decisions.md, journeys" (lines 27-29) which could be lengthy.
- Why it's brittle: No explicit resolution of scope-bounding vs. information-gathering cost. An agent might read 50KB of context to decide to decompose, wasting budget.
- Suggested fix: Add: "Read CHARTER and decisions.md first (should be <5KB total). If they don't exist or don't clarify scope, make a decomposition call based on intent alone; do not try to infer everything from code."

### F-13: Contradiction: "no recursive decomposition" vs "architect may decompose"
- File:line: `lead.md:54` vs lines 68-77
- Category: C
- Severity: MEDIUM
- The text: "Avoid recursive decomposition" (line 54) BUT "The architect, if emitted, must build inline and must not decompose" (line 68)
- Why it's brittle: Line 54 says "avoid recursive" but line 68 states it as a hard rule. An architect child that feels too large has NO guidance—the hard rule forbids it from decomposing, but avoiding recursive decomposition is just a preference. What if the architect genuinely needs to decompose?
- Suggested fix: State clearly: "Architects MUST NOT decompose (hard rule). If architect scope is too large, emit a smaller architect + a second architect task, both with task_role='foundation', but this is a sign of bad decomposition at the parent level—ask for human help instead."

### F-14: Contradiction: Mock at boundary vs No cross-stack integration for leaves
- File:line: `lead.md:222-223` vs `lead-integration.md:45-46`
- Category: C
- Severity: MEDIUM
- The text: "Mock sibling APIs only at the contract boundary if needed" (lead.md:223) BUT "No MSW, `vi.mock`, `page.route()`, or fake backend" (lead-integration.md:46)
- Why it's brittle: The leaf agent is told mocking is okay "at the boundary". The integration agent is told mocking is forbidden and to use real services. An agent might assume that once features integrate, the leaf-written mocks are removed; but if a feature's tests rely on mocks, integration tests may differ. No explicit handoff guidance.
- Suggested fix: "Leaf agents: mock external dependencies outside your scope (other sibling APIs, external services). Integration agent: replace all mocks with real services. Leaf tests should pass with both mocked AND real versions of sibling APIs (test both paths if boundary is unclear)."

---

## D: Unreasonable / Undebuggable Instructions

### F-15: "Do not invent placeholder keys like PLACEHOLDER_*"
- File:line: `lead.md:191-192`
- Category: D
- Severity: MEDIUM
- The text: "never invent placeholder keys like `PLACEHOLDER_*`; an unknown task_id fails the contract gate"
- Why it's brittle: The architect sees `feature_partition_targets` in the decomp_runtime_context (which is JSON injected by the runner). If that JSON is missing or malformed, the architect has NO instruction on what to do. The instruction is prohibitive (don't invent) but provides no fallback.
- Suggested fix: "If `feature_partition_targets` is empty or malformed, emit one foundation task and STOP—do not decompose further until you receive valid partition targets."

### F-16: "Seeding a feature-owned stub makes the foundation a contributor"
- File:line: `lead.md:202-207`
- Category: D
- Severity: MEDIUM
- The text: "Seeding a feature-owned stub makes the foundation a contributor to a feature-owned file, which then fails the integration union guard or merge when the owning feature implements it for real"
- Why it's brittle: The prompt describes a failure mode ("fails the integration union guard") but does NOT explain what "integration union guard" is, where it's implemented, how an agent should know it will trigger, or how to fix it. An agent cannot verify compliance without knowing what the guard checks.
- Suggested fix: Add a link or inline explanation: "The runner validates that each file is owned by exactly one task; if the scaffold and a feature both touch a file, the run fails with error 'integration_union_incomplete'. To avoid this: do NOT create stubs for feature-owned files."

### F-17: "Do not run browser E2E against an empty shell"
- File:line: `lead.md:212`
- Category: D
- Severity: LOW
- The text: "Do not run browser E2E against an empty shell"
- Why it's brittle: Tells agent what NOT to do but does not define "empty shell". An agent could reasonably interpret "empty shell" as "no user-visible features", "no backend API", or "no CSS styling". Leaves room for misinterpretation.
- Suggested fix: "Do not run Playwright browser tests on a scaffold with no features. Verify the scaffold with a quick typecheck/build (no runtime E2E)."

### F-18: "Verify scaffold with smallest build/typecheck command"
- File:line: `lead.md:211-212`
- Category: D
- Severity: MEDIUM
- The text: "Verify the scaffold with the smallest build/typecheck command that proves it is usable"
- Why it's brittle: Does not tell the agent HOW to discover the smallest command. For a monorepo with 10+ build scripts, which is "smallest"? An agent must read package.json or Makefile, but the prompt does not say that. For a Python project, is it `python -m py_compile` or `pytest -q` or `mypy --ignore-missing-imports`?
- Suggested fix: "Verify scaffold usability by: (1) running the project's standard build command (read package.json / pyproject.toml for scripts), (2) running typecheck if available, (3) for webapps, run start.sh and check server starts. Do NOT run E2E browser tests."

---

## E: Mixed Voice / Inconsistent Register

### F-19: Inconsistent agent framing (you vs the architect vs scaffold)
- File:line: `lead.md:1-7` vs `lead.md:70-85`
- Category: E
- Severity: LOW
- The text: "You are an Otto build agent" (line 1) BUT "The architect, if emitted..." (line 70), "The scaffold and every feature..." (line 73)
- Why it's brittle: Switches between "you" (agent voice) and "the architect/scaffold" (third-person). Lines 70-77 describe architect behavior as if the agent is reading instructions for a sibling, but lines 1-7 frame the agent as the one building.
- Suggested fix: Restructure section to be agent-first: "If you emit an architect child, that child must... If you build the scaffold inline (not decomposing), you must..."

### F-20: Inconsistent instruction voice in verdict section
- File:line: `lead.md:232-235` vs `lead.md:237`
- Category: E
- Severity: LOW
- The text: "If you decomposed, stop after emitting children" (line 234) vs "If you built inline, write..." (line 237)
- Why it's brittle: Uses "you" here but earlier sections used imperative ("do not run cross-stack Playwright") and passive ("the integration session writes the parent verdict"). Minor consistency issue but reduces clarity.
- Suggested fix: Standardize to agent imperative: "If you decomposed... If you built inline..."

---

## F: Format-Coupled Output

### F-21: Verdict schema is described in text but not enforced at format-parse time
- File:line: `lead.md:237-256`
- Category: F
- Severity: MEDIUM
- The text: The JSON template shows `journeys`, `summary`, `intent_coverage`, `evidence`, `test_command`, `decisions_appended` but the code (lead.py:756-782) accepts ANY dict that has "verdict" and performs lossy canonicalization
- Why it's brittle: Code in lead.py silently accepts incomplete verdicts (e.g., missing `journeys` array) and fills defaults. The prompt claims these fields are required but code treats them as optional. If an agent omits `test_command`, the verdict still passes.
- Suggested fix: The prompt should say "Must include: verdict, summary, journeys (array), intent_coverage, evidence or test_command" OR code should enforce schema via jsonschema validation and reject incomplete verdicts. Currently neither is true; agents can ship partial verdicts.

### F-22: `decisions_appended` format has no validation
- File:line: `lead.md:253-255` and `lead-integration.md:79-81`
- Category: F
- Severity: MEDIUM
- The text: `{"decision_id": "dec-...", "summary": "contract decision"}`
- Why it's brittle: Shows two required keys but code (lead.py) just passes through `verdict_payload.get("decisions_appended")` with no schema validation. An agent could emit `decisions_appended: ["string"]` or `{"badly_named_key": "value"}` and the runner would accept it.
- Suggested fix: In code: validate each entry in decisions_appended has `id` and `summary`. In prompt: say "decision_id MUST be unique across sessions."

### F-23: `intent_coverage` field shape has no validation
- File:line: `lead.md:245-248`
- Category: F
- Severity: MEDIUM
- The text: JSON shows `"built": [list], "partial": [list of {feature, what_works, gap}], "skipped": [list of {feature, reason}]`
- Why it's brittle: Code in lead.py does NOT validate this nested structure. An agent could emit `intent_coverage: "incomplete"` (string) or `"partial": [strings]` and the runner accepts it.
- Suggested fix: "intent_coverage MUST be an object with keys 'built', 'partial', 'skipped'. Each partial entry MUST have 'feature' and 'gap'. Built and skipped entries MUST have 'feature'."

### F-24: Evidence path validation missing
- File:line: `lead.md:251` and `lead-integration.md:77`
- Category: F
- Severity: LOW
- The text: `"evidence": ["path/to/test.log"]`
- Why it's brittle: The prompt shows relative paths like `path/to/test.log` but does NOT specify: are these relative to session_dir, project root, or absolute? Code does NOT validate that paths exist. An agent could emit evidence that lives on their local disk and is gone after the session ends.
- Suggested fix: "Evidence paths MUST be relative to session_dir. Provide paths like `build/test-output.log`, not absolute paths. The runner validates that files exist; if not, the verdict is downgraded to 'unverified'."

---

## G: Hard-Coded Path Whitelists

### F-25: Integration agent hard-coded path whitelist (lines 95-99)
- File:line: `lead-integration.md:95-99`
- Category: G
- Severity: HIGH
- The text: "Stage only legitimate product paths such as `frontend/`, `backend/`, `api/`, `client/`, `server/`, `web/`, `src/`, `app/`, `packages/`, `lib/`, `public/`, `scripts/`, `tests/`, `docs/`, `spec/`, `CHARTER.md`, `decisions.md`, `package.json`, `package-lock.json`, `pyproject.toml`, `requirements.txt`, `uv.lock`, `start.sh`, and `.gitignore`"
- Why it's brittle: This whitelist assumes a webapp or monorepo. A project using Cargo (Rust) would need `Cargo.toml` and `Cargo.lock`, not `package.json` or `uv.lock`. A project with `go.mod` and `go.sum` is excluded. A C++ project with `CMakeLists.txt` is excluded. A compiled language project with `.o` / `.a` outputs is not mentioned.
- Suggested fix: Change to: "Stage legitimate product files: source code directories (src/, lib/, etc.), manifests (package.json, pyproject.toml, Cargo.toml, go.mod, etc.), lock files, config files (CHARTER.md, decisions.md, .gitignore, start.sh), and test files. Do NOT stage runtime artifacts (node_modules, .venv, dist/, .o, .a, *.db)."

### F-26: Integration agent exclusion list (lines 103-105)
- File:line: `lead-integration.md:103-105`
- Category: G
- Severity: MEDIUM
- The text: "Never stage runtime state: `.worktrees/`, `otto_logs/`, `uploads/`, `*.db`, `*.db.bak`, `*.sqlite`, `*.log`, `node_modules/`, `.venv/`, `dist/`, or `build/`"
- Why it's brittle: Forbids `.log` but what if the project has a `logs/` DIRECTORY of git-tracked logs (intentional artifacts)? Forbids `dist/` but some projects version dist as part of release artifacts. Does NOT mention language-specific caches (`.o`, `.pyc`, `__pycache__`, `.gradle/`, `.m2/`).
- Suggested fix: "Never stage: transient caches (node_modules, .venv, __pycache__, .o files), runtime databases (*.db, *.sqlite), logs generated during the run (logs/*.log, *.log), or otto-specific paths (otto_logs, .worktrees)."

---

## H: Project-Kind Assumptions

### F-27: Lead prompt assumes webapp with frontend/backend split
- File:line: `lead.md:87-88, 88-92, 117-130, 138-145, 150-167`
- Category: H
- Severity: MEDIUM
- The text: Multiple sections: "when this is a webapp" (line 87), "Webapp scaffolds MUST isolate route/API/screen registration" (line 88), "frontend/src/store/uiStore.ts", "backend/models.py", "backend/routers/*/router.py"
- Why it's brittle: The prompt is heavily framed around web apps with frontend+backend separation. A CLI tool, library, or backend-only API lacks clear guidance. The Information Architecture Contract (line 87) says "when this is a webapp" but provides NO guidance for other project kinds.
- Suggested fix: Add: "If the project is a CLI tool or library (not a webapp), the architecture contract differs: (1) no frontend/backend split, (2) shared contracts are at the module level (e.g., exported types, command interfaces), (3) each feature adds its own module/command/type and does NOT edit shared registries. Adapt the registration_isolation and feature_owned_paths patterns to your project kind."

---

## Additional Observations (Low Priority)

1. **Unused input variable**: `IS_ROOT` (line 12) is injected into the prompt but never referenced in the prompt text. It should either be used or removed.

2. **DECOMP_RUNTIME_CONTEXT documentation is vague**: The prompt says "Use DECOMP_RUNTIME_CONTEXT to reason about wall-clock critical path" (line 39) but the structure and fields are not explained. An agent seeing `{"scaffold_seed": {...}, "feature_partition_targets": [...]}` might not understand what to do with them without reading the code.

3. **Verdict downgrade rule is asymmetric**: The code (lead.py:1060-1061) downgrades "pass" to "unverified" if no evidence is provided, but the prompt (lead.md:259) says "Use `partial` for failed journeys or meaningful gaps." This asymmetry could surprise agents.

4. **Integration-specific context is incomplete**: The integration agent (lead-integration.md) is told to read child verdicts but NOT how to interpret contradictory child decisions. If child A says "use SQLAlchemy single-Base" and child B says "each feature owns its own ORM context", integration has no tiebreaker.

---

## Summary Table

| ID | Title | File | Severity | Category |
|---|---|---|---|---|
| F-01 | Decompose size heuristic (3-5 leaves) | lead.md:45 | MEDIUM | A |
| F-02 | JSON decision_id format (dec-...) | lead.md:254 | MEDIUM | A |
| F-03 | Task role field values undefined | lead.md:66-67 | LOW | A |
| F-04 | Check field values (literal vs semantic) | lead.md:104-105 | MEDIUM | A |
| F-05 | Framework/stack pinned without fallback | lead.md:73-74 | HIGH | B |
| F-06 | Port and start.sh assumption | lead.md:74,77,79 | MEDIUM | B |
| F-07 | Hardcoded test config file names | lead.md:101-102 | MEDIUM | B |
| F-08 | Backend database ORM assumption | lead.md:149 | MEDIUM | B |
| F-09 | Frontend state container (React-specific) | lead.md:117-130 | MEDIUM | B |
| F-10 | React hook signature example | lead.md:177 | LOW | B |
| F-11 | Path examples missing project variants | lead.md:117+ | MEDIUM | B |
| F-12 | Scope-bounding vs information-gathering | lead.md:27-52 | LOW | C |
| F-13 | No recursive decomposition vs architect hard rule | lead.md:54,68 | MEDIUM | C |
| F-14 | Mock at boundary vs no cross-stack mocks | lead.md:222,lead-integration.md:45 | MEDIUM | C |
| F-15 | No placeholder keys without fallback | lead.md:191-192 | MEDIUM | D |
| F-16 | Integration union guard unexplained | lead.md:202-207 | MEDIUM | D |
| F-17 | Empty shell undefined | lead.md:212 | LOW | D |
| F-18 | Smallest build command discovery undefined | lead.md:211-212 | MEDIUM | D |
| F-19 | Mixed voice (you vs the architect) | lead.md:1-70 | LOW | E |
| F-20 | Inconsistent instruction voice | lead.md:232-237 | LOW | E |
| F-21 | Verdict schema not enforced at parse time | lead.md:237-256 | MEDIUM | F |
| F-22 | decisions_appended format not validated | lead.md:253-255 | MEDIUM | F |
| F-23 | intent_coverage shape not validated | lead.md:245-248 | MEDIUM | F |
| F-24 | Evidence path validation missing | lead.md:251 | LOW | F |
| F-25 | Path whitelist excludes non-JS projects | lead-integration.md:95-99 | HIGH | G |
| F-26 | Exclusion list incomplete (.log, caches) | lead-integration.md:103-105 | MEDIUM | G |
| F-27 | Assumes webapp project kind | lead.md:87-145 | MEDIUM | H |

---

## Recommendations for Immediate Action

**HIGH severity (2):**
- F-05: Reframe framework assumptions as optional/discovered
- F-25: Replace hardcoded path whitelist with language-agnostic guidance

**MEDIUM severity (15):**
- Require decision_id format specification (F-02)
- Clarify check field semantics (F-04)
- Add project-kind alternatives to all britttle examples (F-06, F-07, F-08, F-09, F-11)
- Resolve contradictions on mocking and recursive decomposition (F-13, F-14)
- Add schema validation in code for verdict output (F-21, F-22, F-23)
- Improve exclusion list for language diversity (F-26)
- Extend guidance to non-webapp projects (F-27)

**LOW/immediate:** F-03, F-10, F-17, F-19, F-20, F-24 are mostly clarity issues that can be addressed with prose refinement.

