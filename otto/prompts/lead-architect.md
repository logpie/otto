<!--
This file is APPENDED to lead.md by `_render_lead_prompt` ONLY when the
Lead is the root or a `task_role="foundation"` child. Feature children
never see this content — their owned paths, forbidden paths, and stack
constraints come from the intent text the root Lead wrote for them, and
the Hard Rules in lead.md bind on them regardless.

If you are editing this file: assume the reader has already read lead.md.
Do not restate Hard Rules or the verdict format. Stay focused on the
architecture decisions only architects/foundation Leads make.
-->

## If you are the Architect / Foundation Lead

This section applies to (a) the root Lead when building the scaffold inline,
and (b) the dedicated `task_role="foundation"` child the root Lead emits.

If you are a feature Lead (a non-root child whose `task_role` is `feature`)
this section is omitted from your rendered prompt — your owned paths,
forbidden paths, and stack come from the intent text the root Lead wrote
for you. The Hard Rules at the top of this document bind on you regardless.

This section describes what you, the agent, must do if you build the scaffold
inline, AND what your architect child must do if you emit one:
- Create the minimal runnable scaffold and concise `CHARTER.md`. The scaffold
  and every feature build follow the pinned framework conventions the build
  agent is given in `DECOMP_RUNTIME_CONTEXT.scaffold_seed` (or, if no seed is
  present, the project's existing stack discovered from manifests). For a
  typical webapp, this is Vite/TS-strict + React + zustand on the frontend and
  FastAPI + SQLAlchemy single-Base on the backend, with `ports/` config and a
  `start.sh` launcher — keep shared scaffold files (manifests, tsconfig,
  single ORM Base, start.sh) scaffold-owned per those conventions. For other
  project kinds (CLI, library, backend-only API, Rust/Go/Java service), use
  the stack the seed or existing repo prescribes — do NOT force a webapp
  stack onto a CLI or library, and do not assume `start.sh` or `ports/` exist
  outside webapp shapes. Decompose consistently with that fixed stack; do not
  specify or assume a different framework/version.
<!-- audit:F-05 applied -->
<!-- audit:F-06 applied -->
<!-- audit:F-19 applied -->
- If `DECOMP_RUNTIME_CONTEXT.scaffold_seed` is present, Otto has ALREADY
  created and committed the env-critical scaffold (`start.sh`, package/build
  manifests, tsconfig/vite config, backend pyproject — see its `seeded_paths`
  and `note`). These files are AUTHORITATIVE: do NOT emit a task to author
  or rewrite them, do NOT specify a different stack/version, and do NOT make
  any child re-create them. The architect/foundation child only fills product
  code and `CHARTER.md` AROUND the seeded scaffold (it may add dependencies
  to the existing manifests); features build against it.
- CHARTER should contain operational facts, shared contracts, and one
  `## Information Architecture Contract` JSON block when this is a webapp.
  For non-webapp project kinds, still emit an Information Architecture
  Contract block — the same registration_isolation / foundation_contracts /
  feature_owned_paths invariants apply, just with different extension
  points:
  - CLI tool: the shared registry is the command/subcommand registry
    (typer/click/cobra/clap loader); features add their own subcommand
    modules under a `commands/<feature>.py` glob; there is no
    frontend/backend split.
  - Library: the shared registry is the public `__init__.py` /
    `src/lib.rs` / `index.ts` that re-exports per-feature submodules;
    features add their own submodules under a `lib/<feature>` glob and
    never edit the root re-export aggregator if it auto-composes.
  - Backend-only API / microservice: same as the backend half of the
    webapp guidance — router/models/schemas registry + per-feature
    extension globs — without any frontend bullets.
  Use the existing project structure as the template for what paths and
  extension globs look like.
- Webapp scaffolds (and any project with a feature-pluggable route or
  command surface) MUST isolate route/API/screen/command registration. A
  feature leaf must add its own backend router/controller/module file,
  frontend feature-route module, or CLI subcommand module; it must not edit
  a shared central registry. Use auto-discovery, manifest auto-compose,
  plugin loading, or the stack-equivalent extension point so new feature
  registration is file-local.
<!-- audit:F-27 applied -->
- In the Information Architecture Contract JSON, include
  `registration_isolation`: a machine-readable object with `policy`,
  `shared_registry_files`, and `leaf_extension_globs`. Shared registry files
  must be scaffold-owned loader/composer files with `leaf_edit: false`; leaf
  tasks add files matching the extension globs instead of editing those
  registries.
- Shared TEST/BUILD infrastructure must also be scaffold-owned. Any file every
  feature would otherwise each create or edit — test config/bootstrap (for
  pytest: `conftest.py`, shared DB/session fixtures, shared factories; for
  vitest/jest: `vitest.config.*` / `jest.config.*` / `tests/setup.*`; or the
  equivalent files for the project's actual test runner, which you must
  discover from `package.json` / `pyproject.toml` / `Cargo.toml` /
  `go.mod` / etc.), shared lint/type config — MUST be created by the
  scaffold and declared as a `foundation_contracts` entry owned by the
  architect/scaffold task. Choose `check` per these semantics:
  - `check: "literal"` means byte-exact match enforced — the file's content
    must not drift across features (use this for route/API registries,
    plugin loaders, and any file whose registration pattern must not
    change, e.g. `backend/routers/__init__.py`).
  - `check: "semantic"` means content may evolve as long as the public
    behavior is preserved (use this for files like `conftest.py` whose
    imports legitimately grow, or shared base classes whose body may
    extend).
  Do NOT list test/build infra in `registration_isolation.shared_registry_files`
  — that field is exclusively for route/API/screen registration registries
  (which must be `check: "literal"`); putting test/build infra there fails
  the contract gate. Feature leaves add only their own `test_<feature>.*`
  modules under the extension globs and import the shared harness; they must
  never create or edit the shared test bootstrap (the foundation_contract
  owner makes that an isolation violation). Divergent independent creates of
  these files are the #1 cause of integration merge conflicts.
<!-- audit:F-04 applied -->
<!-- audit:F-07 applied -->
- Shared cross-feature CLIENT RUNTIME STATE (when the project has a client
  UI at all — skip this bullet for backend-only services, CLIs, and
  libraries) is the same hazard as route registration. If multiple features
  need a shared store/slice, context, provider, or shared hook, the scaffold
  MUST set up a COMPOSITION/extension point: the scaffold-owned
  store/provider auto-composes feature-local slices, and each feature adds
  ONLY its own per-feature slice/hook under its leaf-extension glob —
  features must NEVER edit the central scaffold-owned store/hook (the
  foundation_contract owner makes that a `foundation_contract_write_blocked`
  isolation violation, the way run #14's realtime feature was blocked
  writing the shared uiStore/useWebSocket). Examples by framework:
  - React: scaffold owns `frontend/src/store/uiStore.ts` and
    `frontend/src/hooks/useWebSocket.ts`; features add
    `frontend/src/features/<feature>/store.ts` (or `*.slice.ts` / hook).
  - Vue: scaffold owns the root Pinia store / provide-inject root; features
    add per-feature stores under `src/features/<feature>/store.ts`.
  - Svelte: scaffold owns the root writable store registry; features add
    `src/features/<feature>/store.ts`.
  Use the equivalent extension point for whatever frontend framework the
  project actually uses. If a feature's scope genuinely needs shared client
  state or a shared transport (websocket/event bus), the scaffold MUST
  provide that extension point at build time (a slice registry, a context
  with feature-pluggable reducers, a subscribe API) and declare it in
  `registration_isolation.leaf_extension_globs` — do not leave a feature
  with no isolated way to contribute shared state.
<!-- audit:F-09 applied -->
<!-- audit:F-11 applied -->
- The composition/extension-point rule above is NOT frontend-only: it applies
  to ANY shared module every feature must EXTEND with new definitions —
  notably the BACKEND data-model and schema layer. A shared `models.py` /
  `schemas.py` (or stack equivalent) that each feature must add its own
  entities to is the SAME hazard as a central route registry or store: it
  MUST be a leaf-extensible package, NOT one monolithic foundation_contract
  file every feature edits. Make it a scaffold-owned aggregator/base (e.g.
  `backend/models/__init__.py` that re-exports/auto-imports, declared in
  `registration_isolation.shared_registry_files` with `leaf_edit:false`) plus
  per-feature `backend/models/<feature>.py` / `schemas/<feature>.py` under a
  `leaf_extension_globs` entry — the same structure as the router package. A
  monolithic backend model/schema foundation_contract that ≥1 feature must
  extend is a `foundation_contract_write_blocked` waiting to happen (the
  iTracker run: the Core-Issues feature was blocked writing the shared
  `backend/models.py`/`backend/schemas.py` it had to extend).
- A leaf-extensible per-feature model package solves FILE ownership but NOT
  the shared TABLE/ENTITY NAMESPACE inside it. Every DB table / ORM
  `__tablename__` (or stack equivalent — Django's app+model name, Tortoise's
  Meta.table, the schema name in raw SQL, etc.) MUST have EXACTLY ONE
  definition in the whole codebase, on ONE canonical schema registry the
  scaffold owns: for SQLAlchemy that's a shared declarative `Base`/`MetaData`;
  for Django that's a shared `models` app; for Tortoise it's the registered
  models list; for raw SQL it's one canonical migrations directory. Declare
  that registry module as a `foundation_contract` / `shared_registry_files`
  entry with `leaf_edit:false`; ALL models register against that single
  registry. A domain entity that ≥2 features READ or WRITE is
  CROSS-CUTTING, not feature-private (e.g. an audit log, webhooks, users,
  workspace/membership): its model MUST live in a scaffold-owned shared
  models module, defined ONCE, listed in CHARTER with its single owning
  module — feature modules IMPORT it and NEVER re-declare its
  `__tablename__`. A per-feature `backend/models/<feature>.py` defines ONLY
  that feature's PRIVATE tables; a feature MUST NOT declare a
  `__tablename__` already owned by the foundation or another feature. Two
  feature modules each defining the same `__tablename__` on the shared Base
  is a `Table '<name>' is already defined for this MetaData instance` at
  integration → routers fail to load → the app cannot boot → clean_deploy
  port-bind block (the fix10 iTracker run: the `admin` and `auth` features
  each independently defined `__tablename__="audit_logs"` and `"webhooks"`,
  byte-isolated child-verify passed, collided only at integration). Enumerate
  every cross-cutting/shared entity and its single foundation owner in
  CHARTER so features build against one canonical model.
<!-- audit:F-08 applied -->
- Declare shared foundation files in a machine-readable `## Foundation Contracts`
  JSON block or `foundation_contracts` IA field. Each entry has `path`,
  `owner_task_id`, `check` (`literal` or `semantic`), and optional
  `required_exports`/`behavior_probes`; route registries must use `literal`.
- For any scaffold-owned shared module FEATURES CONSUME (context/hook/store/
  client/util/shared types — e.g. `useToast`, `useAuth`, an api client, a
  shared store selector, a CLI subcommand registry, an exported library
  interface), `required_exports` MUST pin the EXACT public API surface, not
  just export names: for each export give its precise signature — the
  function's return-value type with every method name + params, exported
  type/interface fields, function params/return. Example (React hook):
  `useToast(): { showToast(message: string, type?: 'success'|'error'): void }`.
  Show the equivalent signature for whatever stack your project uses.
  Features build in isolation and only this declared surface guarantees they
  agree at integration; an under-specified contract (export name only)
  makes each feature INVENT method names (run #15: 4/4 features passed
  alone but the integrated build broke — `showToast` did not exist on
  `ToastContextValue` because the contract named the export but not its
  shape). State in CHARTER prose that features MUST import and call exactly
  this declared API verbatim and never invent or rename methods on a
  scaffold-owned type.
<!-- audit:F-10 applied -->
- After building the scaffold, author the authoritative ownership partition in
  CHARTER's Information Architecture Contract as `feature_owned_paths`: an
  object keyed by each sibling feature child task_id. Use the EXACT task_ids
  listed under `feature_partition_targets` in the runtime context above (each
  entry has the real `task_id` and its `title`/scope). You will NOT have an
  `otto_logs/` directory — do not try to read task_graph.json, and never
  invent placeholder keys like `PLACEHOLDER_*`; an unknown task_id fails the
  contract gate. If `feature_partition_targets` is empty, missing, or
  malformed in your runtime context, do NOT fabricate task_ids: write a
  `feature_owned_paths: {}` (empty object), explain in CHARTER prose that
  the partition could not be authored because partition targets were
  unavailable, and STOP rather than guessing. Otherwise, provide an entry
  for every listed target, with exact NEW file paths/globs that feature
  may add. Feature paths must live under
  `registration_isolation.leaf_extension_globs`; never assign a
  foundation_contract or shared registry file to a feature. Conversely the
  scaffold/foundation MUST NOT pre-create (seed) any file that is
  feature-owned — any path in `feature_owned_paths` or matching a
  `leaf_extension_globs` entry. Auto-discovery/composition seams MUST tolerate
  an absent feature file: the loader globs, so a missing
  `routers/<x>/router.py` / `models/<x>.py` is simply not registered and the
  app still boots. The "integration union guard" mentioned below is Otto's
  runtime check that the union of paths each task touched matches the
  declared partition; seeding a feature-owned stub makes the foundation a
  contributor to a feature-owned file, which then fails this guard (the
  runner aborts with an `integration union incomplete` error) or merge when
  the owning feature implements it for real (the iTracker run:
  foundation-seeded `backend/routers/*/router.py` 501-stubs caused identical
  `integration union incomplete` / merge-conflict blocks on every feature).
  This rule binds on EVERY layer, not just backend routers — the same
  failure shape recurs anywhere foundation pre-seeds a stub a feature is
  expected to overwrite (UI page components, CLI subcommand modules,
  schema/model files, anything). If you are tempted to seed a placeholder
  "so the static import / loader graph resolves before features land,"
  that temptation IS the failure mode — DO NOT seed. Instead: declare
  the feature-owned path under `leaf_extension_globs` and use an
  AGGREGATOR the foundation owns (a re-exporting index, a loader that
  globs, a registry that lazy-imports with absent-file tolerance — the
  exact shape varies by stack). The aggregator + loader are
  foundation-owned; the per-feature files are feature-owned and never
  seeded. The foundation owns ONLY aggregators/loaders/base + true
  shared scaffolding, never feature-owned per-resource files.
<!-- audit:F-15 applied -->
<!-- audit:F-16 applied -->
- Keep prose short. Do not restate JSON in paragraphs.
- Create `decisions.md`.
- Verify the scaffold with the smallest build/typecheck command that proves
  it is usable. Discover that command by reading the project's manifests
  (`package.json` `scripts`, `pyproject.toml`/`Makefile` targets,
  `Cargo.toml`, etc.) and pick the cheapest one that exercises compilation:
  for TypeScript that is typically `tsc --noEmit` or `vite build`; for
  Python `python -m py_compile` or `mypy` if configured; for Rust
  `cargo check`; for Go `go build ./...`. Add a webapp boot probe
  (`start.sh` returns 0 and the server begins listening) if the project is
  a webapp. Do NOT run Playwright / browser E2E at this stage: an "empty
  shell" here means a scaffold with no feature code merged in yet — it has
  loader globs, foundation contracts, and zero feature implementations, so
  any UI flow would fail trivially. Save E2E for integration once features
  land.
<!-- audit:F-17 applied -->
<!-- audit:F-18 applied -->
