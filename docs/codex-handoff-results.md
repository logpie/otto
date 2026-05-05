# Codex i2p Handoff Results

Started: 2026-05-04 21:03 PDT
Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2`
Branch: `codex-i2p-v2`

## Intake + Merge

- Read the required handoff/design/progress/resume/smoke docs from `cc-i2p-2` before merging because the current branch did not yet contain them.
- Read `/Users/yuxuan/work/cc-autonomous/codex-learnings.md` before non-trivial Otto work.
- Merged `cc-i2p-2` into `codex-i2p-v2` with `git merge --ff-only cc-i2p-2`.
- Result: fast-forward merge, no conflicts. Current HEAD: `692ed0420`.
- Constraint decision: `docs/HANDOFF-codex.md` was written for a `cc-i2p-2` to `main` handoff, but the active user instruction is to stay in this worktree and merge `cc-i2p-2` into this branch. Followed the active instruction.

## Design Notes To Verify, Not Assume

- The load-bearing architecture bet is that a concrete compiled spec prevents structural drift, while slice/group-owned merge prevents context-free flattening. This is plausible, but brownfield and CLI runs need real evidence because most prior validation was webapp-heavy.
- `docs/i2p-resume-design.md` has stale internal prose: the status line and progress notes say `otto certify --resume` is implemented, while an older v1 table says certify resume is unsupported. Treat implementation and direct CLI behavior as source of truth.
- Phase C deleted substantial legacy surface, including `otto merge`. The requested merge-ordering bug hunt must target `otto/merge_queue.py`, not the removed legacy merge orchestrator.

## Known Live Areas Tracking

- Brownfield compile: passed on a real Codex-provider brownfield bugfix run.
- `--resume` on real interrupted runs: passed after fixing the Node-check and blocked-dependency repair issues it exposed.
- Multi-Group merge ordering with shared scaffolds: tiny webapp and TODO-CLI both landed two groups in dependency order; TODO-CLI exposed a design risk where a downstream slice changed a dep-owned single-file CLI entrypoint.
- Mission Control live polling under high event volume: synthetic backend stress read completed; no code change made.
- Layer 2 audit to repair loop: fixed and covered by targeted runner tests plus real brownfield repair pass.
- Spec-edit during in-flight run: current product policy blocks post-approval edits with HTTP 409; surgical in-flight amendment is not implemented.

## End-to-End Project Shapes

Provider for paid E2E runs: `codex` (Claude quota is constrained).

- Tiny webapp: passed.
  - Root: `/tmp/otto-i2p-codex-e2e-20260505-041750/tiny-webapp`
  - Session: `2026-05-05-041751-ec18da`
  - Command: `uv --project /Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2 run --extra dev python -m otto.cli build ... --i2p --provider codex --budget 1200 --yes`
  - Result: exit 0; `summary.json` says `status=completed`, `verdict=passed`, `stories_passed=5/5`, duration `475.1s`.
  - Evidence: final `spec-state.jsonl` has both group branches landed (`home_shell`, `counter_behavior`), final audit `verdict=passed`, and `proof-packet.json/html` were generated.
  - External verification: `python3 tests/acceptance.py` passed; `uv --project /Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2 run --extra dev python tests/run_counter_browser_journey.py` passed. Plain system `python` was not available outside Otto's uv environment, so the browser journey must be run with the project environment.
  - Design warning observed: compile emitted `multi-slice spec declares no cross_slice_checks`, so the tiny webapp succeeded despite missing explicit cross-slice integration checks. This is a real product-design gap for multi-slice confidence, not a blocker for this run.
- Small CLI: passed.
  - Artifact: `bench-results/todo-cli-i2p-20260504-213103`
  - Run root: `/var/folders/xg/dk8wgfy119z44797kyz7w0380000gn/T/todo-cli-i2p-20260504-213103-83fq66my/i2p`
  - Session: `2026-05-05-043104-517040`
  - Provider: `codex` via seeded `otto.yaml`.
  - Result: benchmark `verdict=passed`; `cli_exit_code=0`; wall `951.6s`; slices landed `cli_scaffold`, `task_lifecycle`; final audit `verdict=passed`; evaluator aggregate `passed`.
  - External verification: sequential `.venv/bin/python3 tests/run_acceptance.py && .venv/bin/python3 tests/user_journey.py` passed. Running those two scripts in parallel is invalid because both mutate `tasks.json`.
  - Quality findings from audit: add/delete are silent, empty list prints nothing, corrupt-store error lacks full path/recovery hint. Benchmark still passed.
  - Design challenge: the compiled spec listed `task_lifecycle.owned_paths=["tasks.json"]`, but every task in that slice required `todo.py` and the agent modified `todo.py` through the transitive-dep allowance. This made the run succeed, but it means `owned_paths` is not a complete summary of what a slice may change when dependencies own shared single-file surfaces. The proof packet did not surface that ownership expansion as a warning.
- Brownfield: passed.
  - Root: `/tmp/otto-i2p-codex-brownfield-20260505-051900/counter-bug`
  - First interrupted diagnostic session: `2026-05-05-045239-97dea1`
  - Passing rerun session: `2026-05-05-050516-c62505`
  - Provider: `codex` via `otto.yaml`.
  - Seed verification: `python3 tests/acceptance.py` failed before Otto with `acceptance:reset:FAIL ... expected 0, got 1`.
  - Result: `otto improve bugs "fix reset behavior so Reset returns the counter to 0 after increments" --i2p` exited 0; final `spec-state.jsonl` has `audit.finished` + `run.finished` with `verdict=passed`; proof packet JSON/HTML generated.
  - External verification: `python3 tests/acceptance.py` passed after repair with `acceptance:PASS reset returns to 0`.
  - Git verification in the brownfield project: repair commit `29ee426` changed only `app.js`; `.playwright-cli/` remained untracked and was not committed.
- Interrupted resume smoke: passed after fixes.
  - Root: `/tmp/otto-i2p-codex-resume-20260504-225456/resume-webapp`
  - Session: `2026-05-05-055504-fa7827`
  - Provider: `codex`.
  - Procedure: started `otto build ... --i2p --provider codex`, interrupted after `slice.started`, verified `otto_logs/paused -> sessions/2026-05-05-055504-fa7827` and checkpoint `status=in_progress`, then ran `otto build --i2p --resume --provider codex`.
  - Result: exit 0; final output `Build: 2/2 slices passing`, `Merge: 2 landed, 0 blocked`, `Audit verdict: passed`.
  - Evidence: final `spec-state.jsonl` has `slice.merge.landed` for `counter_app` (`1b3420f`) and `local_tests` (`cfd1b92`), `audit.finished verdict=passed`, and `run.finished verdict=passed`; proof packet JSON/HTML generated.
  - External verification: `npm test` passed with `Acceptance contract passed.` Proof JSON validated with `python3 -m json.tool`.
  - Git verification: `main` contains separate merge commits for `counter_app` and `local_tests`; no blocked dependency was smuggled through a downstream merge in the passing run.

## Findings + Fixes

### Resume Checkpoint + Spec Hash Drift

Finding: i2p `--resume` expected `otto_logs/paused` plus a per-session `checkpoint.json`, but `runner.run_pipeline` never wrote them. Fresh i2p runs therefore had no normal resume entry point. The same area had a spec-hash safety bug: `plan_resume()` hashed the current `spec/spec.json`, then the CLI compared that same file to the just-computed hash, so edits made while the run was paused were accepted as the new baseline.

Fix: `runner.run_pipeline` now writes an i2p checkpoint and paused pointer once a spec is available, storing the original `spec_hash`, `spec_path`, command, intent, and i2p phase. Terminal completion marks the checkpoint completed and clears the paused pointer. `plan_resume()` now prefers the checkpoint's stored `spec_hash` and only falls back to current bytes for older sessions.

Verification: `uv run --extra dev pytest tests/test_resume.py tests/test_runner.py -q --maxfail=3` passed, including new regressions for checkpoint writing and paused-spec drift refusal.

### Resume Merge Replays Already-Landed Work

Finding: resume passed landed ids to `run_build(skip_components=...)`, but `run_merge_queue` started with an empty `landed_ids` set. A resumed run could try to merge an already-landed group again, and downstream dependencies would not be considered satisfied until that redundant merge path ran.

Fix: `run_merge_queue` now accepts the same `skip_components` ids and seeds its landed set from the spec order. `runner.run_pipeline` forwards the resume landed set into merge.

Verification: `uv run --extra dev pytest tests/test_resume.py tests/test_runner.py tests/test_merge_queue.py -q --maxfail=3` passed, including a regression where an already-landed dependency unlocks a downstream group without reprocessing the skipped branch.

### Layer 2 Feature Repair Did Not Close The Loop

Finding: runner-level Layer 2 repair dispatched the build agent with `feature_id`, but then passed `re_audit=None`. That meant the repair attempt could not change the final audit verdict, and its cost was not reflected in the final run/proof totals. In git projects, a successful Layer 2 edit also had no integrated commit step.

Fix: Layer 2 now supplies a one-pass whole-product re-audit callback to `repair_failing_features`, updates the final `AuditResult` from that re-audit, records repair cost in the final run cost, charges the shared budget for repair-agent spend, and commits successful integrated repairs when the project is a git repo.

Verification: `uv run --extra dev pytest tests/test_runner.py tests/test_runner_layer2_fix.py tests/test_audit_loop_repair.py -q --maxfail=3` passed, including a regression where a partial feature audit is repaired, re-audited to passed, and rendered with the passed final verdict.

### TODO-CLI Bench Provider Flag Was Cosmetic

Finding: `scripts/bench_todo_cli_i2p.py --provider codex` recorded `PROVIDER=codex` in `paths.env`, but the seeded project `otto.yaml` did not include that provider and `otto run` has no `--provider` option. A paid run through this harness could silently use the default provider instead of the requested one.

Fix: the TODO-CLI bench now writes the selected provider into the seeded `otto.yaml` and validates provider choices.

Verification: `uv run --extra dev pytest tests/test_e2e_scripts.py::test_todo_cli_i2p_bench_writes_requested_provider -q` passed. `uv run --extra dev ruff check scripts/bench_todo_cli_i2p.py tests/test_e2e_scripts.py` passed.

### Ownership Is Broader Than `owned_paths` Suggests

Finding: the TODO-CLI compile produced `task_lifecycle.owned_paths=["tasks.json"]` while all lifecycle tasks named `todo.py`. The downstream lifecycle agent changed `todo.py` because the current build prompt and scope checker permit modifying transitive dependency owned files. That may be the right escape hatch for foundations, but the operator-facing spec/proof reads as if `task_lifecycle` only owns `tasks.json`; no warning or audit note explains that it also edited the dependency-owned CLI entrypoint.

Fix: build scope tracking now emits a non-blocking `scope.warning` when a slice modifies a dependency-owned path outside its own `owned_paths`. Dep-owned extension remains allowed, but the proof packet/run evidence no longer hides the extra write surface.

Verification: `uv run --extra dev pytest tests/test_build.py::test_dep_owned_modifications_are_reported_as_extensions tests/test_build.py::test_run_build_warns_on_dep_owned_extension -q` passed. `uv run --extra dev ruff check otto/build.py tests/test_build.py scripts/bench_todo_cli_i2p.py tests/test_e2e_scripts.py` passed.

### Brownfield Repair Looked For Nonexistent Slice Branches

Finding: a real `otto improve bugs --i2p` brownfield run against `/tmp/otto-i2p-codex-brownfield-20260505-051900/counter-bug` correctly audited the reset bug, then emitted `slice.attempt.failed` with `could not checkout slice branch i2p/2026-05-05-045239-97dea1/static-counter-app for fix`. Brownfield mode skips build/merge, so no per-slice branch exists for audit-routed repair.

Fix: `run_audit` now distinguishes greenfield build-slice repairs from brownfield integrated repairs. If a build-phase slice result/branch exists, it keeps branch-isolated repair and merge. If not, it runs the fix agent against the integrated worktree and commits the repair directly on the current base branch. `_commit_slice_work` also excludes `.playwright-cli` from repair commits so browser audit artifacts do not get committed into the user project.

Verification: `uv run --extra dev pytest tests/test_audit.py::test_run_audit_brownfield_fix_repairs_integrated_worktree tests/test_audit.py::test_run_audit_routes_to_fix_loop_for_failing_slice -q` passed. `uv run --extra dev ruff check otto/audit.py otto/build.py tests/test_audit.py` passed.

Real-cost verification: reran the brownfield counter fixture with Codex provider after the fix. Session `2026-05-05-050516-c62505` first failed audit on reset behavior, landed integrated repair commit `29ee426`, then passed the second audit pass. Final proof packet paths:
`/tmp/otto-i2p-codex-brownfield-20260505-051900/counter-bug/otto_logs/sessions/2026-05-05-050516-c62505/proof-packet.html`
and
`/tmp/otto-i2p-codex-brownfield-20260505-051900/counter-bug/otto_logs/sessions/2026-05-05-050516-c62505/proof-packet.json`.

### Clean Merge Checks Could Not Run Node Commands

Finding: the real interrupted-resume smoke initially failed merge verification for a generated Vite app. The build slice created `package.json` and `package-lock.json`; after `git clean -fdx`, merge verification ran `npm run build` on the integrated branch without `node_modules`, producing `vite: command not found` and blocking the app slice even though the branch had valid locked dependencies.

Fix: `RepoTestCheck` and `BrowserJourney` now do a narrow locked-dependency bootstrap for npm commands: if `package.json` + `package-lock.json` exist and `node_modules/` is absent, Otto runs `npm ci --prefer-offline --no-audit --no-fund` before the declared npm check. The declared check remains the source of pass/fail evidence; bootstrap output is included in raw evidence.

Verification: `uv run --extra dev pytest tests/test_checks.py::test_repo_test_npm_run_bootstraps_locked_dependencies -q` passed. `uv run --extra dev ruff check otto/checks.py tests/test_checks.py` passed.

### Layer 2 Could Land A Downstream Slice While Its Dependency Was Blocked

Finding: the first interrupted-resume smoke exposed a more serious merge-ordering bug. `counter_app` was blocked at merge time, but audit-routed Layer 2 repair still repaired and landed the downstream `counter_tests` branch. Because that branch was based on `counter_app`, the blocked app commit was pulled into `main` through the downstream merge while the proof still reported `counter_app` blocked.

Fix: audit-routed repair now tracks unavailable/blocked slices during the repair pass and skips a failing slice when any direct dependency is still blocked and not landed. This prevents downstream branches from smuggling blocked dependency commits into the integration branch.

Verification: `uv run --extra dev pytest tests/test_audit.py::test_run_audit_skips_downstream_repair_when_dependency_blocked -q` passed. The second real interrupted-resume smoke (`2026-05-05-055504-fa7827`) then completed with both slices landed in order and final audit passed.

### Mission Control High-Event Polling Stress

Finding: `build_run_view` currently reads `spec-state.jsonl` with full-file `read_text().splitlines()` on every `/api/run-view/<session>` request. A synthetic 60,000-event, 12.6 MB session took `0.112s` to build a RunView on this machine. That is acceptable for the current polling cadence, but it is not a long-run architecture; a multi-hour high-event session will make the backend do growing full-file reads every 3 seconds.

Fix: no code change in this pass. Design challenge: RunView needs an indexed/tail-aware state reader or a compacted stage summary before this is safe for genuinely high-event Mission Control sessions.

Verification: synthetic command used `build_run_view` against 60k events and returned `elapsed=0.112s status=building`.

### Spec Edit During In-Flight Runs Is A Product Policy Gap

Finding: research prose mentions in-flight spec edits and dependent-slice invalidation, but the implemented API intentionally blocks edits after approval: `POST /api/specs/<session>/edit` returns HTTP 409 once lifecycle is `approved`, and the UI hides editing in that state. This means there is no live in-flight amendment behavior to smoke; the design doc is ahead of the product.

Fix: no code change in this pass. I would not implement surgical invalidation as a hidden endpoint behavior. It should be a loud product workflow: pause run, show impacted slices/features, require explicit replan/continue.

Verification: existing `tests/test_spec_review_routes.py::test_post_edit_blocked_after_approval` covers the current 409 policy.

### Targeted Non-Cost Gate

`uv run --extra dev pytest tests/test_resume.py tests/test_runner.py tests/test_cli_run.py tests/test_merge_queue.py tests/test_merge_eligibility.py tests/test_merge_component_repair.py tests/test_build_shared_paths.py tests/test_brownfield_compile.py tests/integration/test_brownfield_compile_real.py tests/test_audit_loop_repair.py tests/test_runner_layer2_fix.py tests/test_run_view.py tests/test_run_view_routes.py tests/test_spec_review_routes.py tests/integration/test_spec_review_e2e.py -q --maxfail=5` passed: 167 tests.

`uv run --extra dev ruff check otto/runner.py otto/resume.py otto/merge_queue.py tests/test_resume.py tests/test_runner.py tests/test_merge_queue.py` passed.

After the resume-smoke fixes, the expanded targeted gate passed:
`uv run --extra dev pytest tests/test_resume.py tests/test_runner.py tests/test_cli_run.py tests/test_merge_queue.py tests/test_merge_eligibility.py tests/test_merge_component_repair.py tests/test_build_shared_paths.py tests/test_brownfield_compile.py tests/integration/test_brownfield_compile_real.py tests/test_audit_loop_repair.py tests/test_runner_layer2_fix.py tests/test_run_view.py tests/test_run_view_routes.py tests/test_spec_review_routes.py tests/integration/test_spec_review_e2e.py tests/test_checks.py tests/test_audit.py -q --maxfail=5` → 237 passed.

Final touched-file lint passed:
`uv run --extra dev ruff check otto/audit.py otto/build.py otto/checks.py otto/cli_run.py otto/merge_queue.py otto/resume.py otto/runner.py scripts/bench_todo_cli_i2p.py tests/test_audit.py tests/test_build.py tests/test_checks.py tests/test_e2e_scripts.py tests/test_merge_queue.py tests/test_resume.py tests/test_runner.py`.

Final broad gate passed:
`uv run python scripts/test_tiers.py fast` → 1304 passed, 530 deselected.

## 2026-05-05 Claude Round-2 Merge Checkpoint

Status: paused after user-requested steps 1 and 2, before broader bug hunt / real-world E2E.

Merge:
- Fast-forwarded `codex-i2p-v2` from `692ed0420` to `cc-i2p-2` HEAD `0ee14f018`.
- Preserved the local first-wave fixes via stash, reapplied them, and resolved conflicts in `otto/audit.py`, `otto/build.py`, `otto/cli_run.py`, `otto/merge_queue.py`, `otto/runner.py`, and `tests/test_runner.py`.
- Ported the local fixes across Claude's Slice->Group runtime rename:
  - i2p checkpoint / paused pointer writes in `runner.run_pipeline`, while keeping Claude's review gate, pause/resume, and spec-edit invalidation.
  - resume skip propagation into `run_merge_queue`, while keeping Claude's operator-aborted-group filtering.
  - brownfield integrated audit repair plus dependency-blocked repair skipping in `run_audit`, using canonical `group_*` events/fields.
  - dependency-owned scope extension warnings in `run_build`, using canonical Group naming.
  - TODO CLI bench provider config and summary fields, using canonical Group journal events.

Verification:
- `uv run python -m compileall -q otto/audit.py otto/build.py otto/checks.py otto/cli_run.py otto/merge_queue.py otto/resume.py otto/runner.py scripts/bench_todo_cli_i2p.py tests/test_audit.py tests/test_build.py tests/test_merge_queue.py tests/test_runner.py` passed.
- `uv run --extra dev pytest -q tests/test_audit.py tests/test_build.py tests/test_checks.py tests/test_merge_queue.py tests/test_resume.py tests/test_runner.py tests/test_e2e_scripts.py tests/test_cli_run.py tests/test_spec_amend.py tests/test_spec_review_routes.py tests/test_mission_control_actions.py` -> 324 passed.
- `npm run web:typecheck && npm run web:build` passed after `npm ci --prefer-offline --no-audit --no-fund` refreshed stale local `node_modules` for the newly declared `react-markdown` dependency.
- `uv run python scripts/test_tiers.py fast` -> 1359 passed, 531 deselected.

Completion assessment:
- The single-worktree v1 pipeline is substantially shipped: compile/spec, Group build, merge queue, audit, proof render, Mission Control run/spec views, review gate, pause/resume/abort, in-flight spec edit invalidation, and `--resume` all have code and focused tests.
- It is not the whole architectural design yet. The remaining design-level gaps are not just cosmetics: A8 default video/screenshots are BYO, A9 is persistent worktree/branch rather than SDK session-pinned long-lived agents, A10 still has layered audit retry/fix loops, A11/A12 multi-worktree stale-base/superseded eligibility and post-repair scope enforcement are deferred, and B3/B5 remain open.
- The docs are also internally mixed: `docs/intent-to-product-design.md` still starts with `Status: Design — not yet implemented` and early sections still use old Slice/process/video language before later deferral notes correct it. Treat "architectural delivery complete" as "Phase-A single-worktree v1 delivered with documented v2 deferrals", not as end-state completion.

## 2026-05-05 Claude Round-3 Merge Checkpoint

Status: accepted Claude's round-3 hardening after verification, with one additional Codex composition fix.

Merge:
- Fast-forwarded `codex-i2p-v2` to `cc-i2p-2` HEAD `0c20fcd5b`.
- Reapplied the local Codex first-wave fixes on top. The only merge conflict was generated `otto/web/static/build-stamp.json`; the bundle was rebuilt after resolution.
- Reviewed Claude's round-3 changes and kept them:
  - run-view replay now maps aborted groups correctly,
  - spec edit/approve routes refuse paused or invalid lifecycle states,
  - resume surfaces paused-by-user and prior spec-edit invalidation warnings,
  - spec-state schema moved to v2 with legacy-key warnings,
  - `Event.feature_id` is plumbed for feature-scoped journal evidence.

Additional finding:
- `_invalidated_group_ids` in `otto/runner.py` still treated `group.merge.landed` and `group.blocked` as terminal after `group.invalidated_by_spec_edit`, but not `group.aborted_by_user`. A spec-edit invalidated Group that the operator aborted could therefore be redispatched on the next runner pass, even though round-3 had already fixed the equivalent resume scanner.

Fix:
- `otto/runner.py` now treats `group.aborted_by_user` as terminal for live invalidation scanning.
- Added `tests/test_runner.py::test_spec_edit_invalidation_clears_after_user_abort`.

Verification:
- `uv run python -m compileall -q otto/audit.py otto/build.py otto/checks.py otto/cli_run.py otto/merge_queue.py otto/resume.py otto/runner.py otto/spec_compile.py otto/spec_state.py otto/web/spec_review_routes.py tests/test_audit.py tests/test_build.py tests/test_checks.py tests/test_e2e_scripts.py tests/test_merge_queue.py tests/test_resume.py tests/test_runner.py tests/test_spec_compile.py tests/test_spec_review_routes.py tests/test_spec_state.py` passed.
- `uv run --extra dev pytest -q tests/test_audit.py tests/test_build.py tests/test_checks.py tests/test_merge_queue.py tests/test_resume.py tests/test_runner.py tests/test_e2e_scripts.py tests/test_cli_run.py tests/test_spec_amend.py tests/test_spec_compile.py tests/test_spec_state.py tests/test_spec_review_routes.py tests/test_mission_control_actions.py` -> 387 passed.
- `npm run web:typecheck && npm run web:build` passed.
- `uv run --extra dev ruff check otto/audit.py otto/build.py otto/checks.py otto/cli_run.py otto/merge_queue.py otto/resume.py otto/runner.py scripts/bench_todo_cli_i2p.py tests/test_audit.py tests/test_build.py tests/test_checks.py tests/test_e2e_scripts.py tests/test_merge_queue.py tests/test_resume.py tests/test_runner.py tests/test_spec_compile.py tests/test_spec_review_routes.py tests/test_spec_state.py` passed.
- `git diff --check --cached` passed.
- `uv run python scripts/test_tiers.py fast` -> 1376 passed, 531 deselected.

## 2026-05-05 Codex Design-Comparison Fix Pass

Scope: compared the shipped code against `plan.md`, `progress.md`, and the
redesign docs after the round-3 merge. This pass fixed implementation gaps
that were still real, and left only larger v2/design deferrals documented.

Findings fixed:
- Feature audits were still effectively name-keyed in several paths. Added
  `FeatureAudit.feature_id`, updated the audit prompt/parser, proof rendering,
  and runner Layer 2 routing to prefer stable Feature ids with one-cycle
  display-name fallback.
- `walkthrough.jsonl` strict parsing was not actually load-bearing enough.
  `_validate_walkthrough_jsonl` now keeps permissive coverage stats but only
  strict entries become proof evidence; parse errors force
  `walkthrough_coverage.meets_threshold=false`. `run_audit` validates both
  before and after the audit agent because the prompt allows the agent to
  write the JSONL artifact.
- Layer 2 re-audit accepted a requested failing-Feature set but re-audited the
  whole product. `run_pipeline` now passes `feature_scope_ids` into
  `run_audit`, the audit prompt names the scoped ids, and returned
  `feature_audits` are filtered to the requested Feature ids.
- A12 was documented but not enforced. Merge repair now checks the repair diff
  with `detect_scope_violations` before committing; peer-owned overreach emits
  `scope.warning`, discards the uncommitted repair, and blocks the Group.
- A3.3 was genuinely missing. Added `otto render <session-id>` / direct session
  path support to regenerate `proof-packet.html` from `proof-packet.json`
  without LLM cost. JSON is not rewritten unless `--rewrite-json` is passed.
- Proof and history surfaces still leaked Slice-era vocabulary. Proof HTML now
  renders "Groups" for dispatch details, and i2p history synthesis reads
  canonical `landed_group_ids` / `blocked_group_ids` / `groups` while keeping
  `i2p_slice_count` as a temporary compatibility alias.
- B3 is closed: screenshot thumbnails now use a responsive CSS grid instead of
  inline-block thumbnails.

Remaining not-yet-complete redesign items:
- A8: default video/screenshot capture is still BYO via project journeys.
- A9: "long-lived agent" is still persistent worktree/branch plus fresh
  subprocess calls, not SDK session-pinned continuity.
- A10: audit retry layering is still multi-layered; this pass did not collapse
  the retry architecture.
- A11: stale-base/superseded eligibility remains a multi-worktree extension.
- A3.2: Jinja templates from the original plan are still not present; the
  shipped proof renderer is the pure dependency-free `render.py` path.
- B5: lifecycle render tests are still split rather than combined; cosmetic.
- `build_run_view` still does full-file event reads per poll; acceptable for
  current sessions but not the long-run Mission Control architecture.

Verification:
- `uv run --extra dev pytest -q tests/test_render.py tests/test_render_per_feature.py tests/test_cli_render.py tests/test_run_history.py tests/test_cli_smoke.py tests/test_audit_walkthrough_coverage.py tests/test_audit_coverage_cap.py tests/test_audit_walkthrough_entries.py tests/test_walkthrough_strict_parsing.py tests/test_audit_prompt_feature_tagging.py tests/test_audit.py tests/test_a0_4_propagation.py tests/test_runner.py tests/test_merge_queue.py --maxfail=10` -> 170 passed.
- `uv run ruff check otto/audit.py otto/render.py otto/runner.py otto/merge_queue.py otto/cli.py otto/runs/history.py otto/mission_control/serializers.py tests/test_audit.py tests/test_audit_walkthrough_coverage.py tests/test_audit_coverage_cap.py tests/test_audit_walkthrough_entries.py tests/test_audit_prompt_feature_tagging.py tests/test_a0_4_propagation.py tests/test_render.py tests/test_render_per_feature.py tests/test_cli_render.py tests/test_runner.py tests/test_merge_queue.py tests/test_run_history.py` -> passed.
- `uv run python scripts/test_tiers.py fast` -> 1385 passed, 531 deselected.
- `git diff --check` -> passed.

## 2026-05-05 Codex Remaining-Gap Fix Pass

Scope: user requested committing the existing worktree state first, then
finishing the remaining gaps, specifically calling out that video/screenshot
capture can use agent browser or Playwright. Existing worktree changes were
committed as `6021640a3 Harden i2p design completion gaps` before this pass.

Findings fixed:
- A8 was still real: the synthesized webapp walkthrough only wrote a log and
  HTML body. It now attempts Playwright capture against `base_url` or the
  generated body artifact and records screenshot, DOM, video when available,
  browser log, and `walkthrough.jsonl`. Missing browser support is logged as
  fallback evidence, not hidden.
- A9 was still real: provider session ids were returned by
  `run_agent_with_timeout` but discarded. Build/fix paths now preserve
  `session_id`, pass it back as `agent_session_id`, and set
  `AgentOptions.resume` in `default_build_agent`.
- A10 was still real in the live runner: `run_pipeline` passed the same
  fix-agent into `run_audit` and Layer 2. The runner now calls
  `run_audit(..., fix_agent=None)` and reserves repair for
  `repair_failing_features`; direct `run_audit` callers keep the compatibility
  loop.
- A11 was partially real: merge eligibility did not account for superseded
  BuildResult entries and did not use the branch/worktree from the actual
  latest passing result. Merge queue now keys eligibility off the latest
  Group/Component result per id and uses that result's branch/worktree.
- A3.2 was still open in `progress.md`: added the proof-packet and
  feature-proof template files and wired the renderers to load them without
  adding a Jinja dependency.
- B5 was cosmetic but easy to close: added a combined render-run lifecycle
  fixture with one landed Group and one blocked Group.
- While testing, an indentation error in the new session-reuse audit patch
  caused no-scope audits to loop forever. Reproduced with a direct
  `run_audit` smoke, fixed, and re-ran the affected suite.

Verification:
- `uv run python -m py_compile otto/audit.py otto/build.py otto/merge_queue.py otto/runner.py` passed.
- Direct `run_audit` smoke returned `AuditVerdict.PASSED 0.1`.
- Focused regressions:
  `uv run pytest -q tests/test_audit.py::test_synthesized_walkthrough_static_site_branch tests/test_audit.py::test_default_walkthrough_no_browser_journey_webapp_synthesizes tests/test_build.py::test_run_build_reuses_agent_session_between_retries tests/test_build.py::test_default_build_agent_passes_resume_session_to_provider tests/test_runner.py::test_repair_called_on_non_pass_with_fix_agent tests/test_merge_queue.py::test_passing_group_ids_latest_result_supersedes_older_pass tests/test_merge_queue.py::test_run_merge_queue_uses_latest_passing_branch_for_superseded_group`
  -> 7 passed.
- Expanded affected suite:
  `uv run pytest -q tests/test_audit.py tests/test_build.py tests/test_merge_queue.py tests/test_runner.py tests/test_runner_layer2_fix.py tests/test_audit_loop_repair.py tests/test_render.py`
  -> 160 passed.
- Template/render expansion:
  `uv run pytest -q tests/test_render.py tests/test_render_per_feature.py tests/test_a1a_dataclasses.py -k 'feature_proof_block_to_html'`
  -> 10 passed, 137 deselected.
- Expanded affected suite after templates/B5:
  `uv run pytest -q tests/test_audit.py tests/test_build.py tests/test_merge_queue.py tests/test_runner.py tests/test_runner_layer2_fix.py tests/test_audit_loop_repair.py tests/test_render.py tests/test_render_per_feature.py tests/test_a1a_dataclasses.py -k 'not test_autopilot_full_executes_safe_recovery_once'`
  -> 294 passed.
- Integration E2E collection:
  `uv run python -m py_compile tests/integration/test_intent_to_proof.py && uv run pytest -q tests/integration/test_intent_to_proof.py --collect-only`
  -> collected `test_intent_to_proof_real_codex`.
- Lint:
  `uv run ruff check otto/audit.py otto/build.py otto/cli_run.py otto/merge_queue.py otto/runner.py otto/render.py otto/spec_compile.py tests/test_audit.py tests/test_build.py tests/test_merge_queue.py tests/test_runner.py tests/test_render.py tests/integration/test_intent_to_proof.py`
  -> passed.
- Fast gate:
  `uv run python scripts/test_tiers.py fast`
  -> 1391 passed, 531 deselected.
- `git diff --check` -> passed.

Superseded pending notes:
- Full fast gate after documentation updates.
- Real-cost Codex-provider E2E on tiny webapp, small CLI, and brownfield
  projects after this implementation pass is committed/validated.

## 2026-05-05 A8 Browser Capture Closure

Scope: close the remaining A8 environment and static-webapp blind spots after
the user explicitly pointed out that screenshot/video capture should use
Playwright or agent-browser.

This supersedes the A8/browser-binary pending note above. The paid
Codex-provider tiny webapp, small CLI, brownfield, and resume runs are recorded
earlier in this file.

Findings fixed:
- Playwright Chromium is installed and launchable in this worktree. A direct
  `sync_playwright()` smoke opened a page and read `body=ok`.
- The synthesized static-webapp detector skipped plain root-level
  `index.html`. That would miss many tiny vanilla webapps and would leave
  screenshot/video capture unused even though the product was web-shaped.

Fix:
- `_synthesized_webapp_walkthrough` now detects root `index.html` after
  generated output directories.
- Added a regression proving the root static index path emits screenshot and
  video artifacts through the default walkthrough.
- Fixed `tests/integration/test_intent_to_proof.py` to run the subprocess via
  `uv --project <repo> run --extra dev python -m otto.cli ...`; the old
  `uv run otto` invocation happened from inside the throwaway project and was
  not pinned to this checkout.
- Updated handoff/progress docs so A8 no longer claims browser binaries are
  unconfirmed.

Verification:
- Direct Playwright root-index smoke returned `succeeded=True` with
  `screenshot-home.png` (9344 bytes), `dom-home.html` (96 bytes),
  `walkthrough.webm` (5648 bytes), `walkthrough.jsonl`, and
  `browser-capture.log`.
- `uv run pytest -q tests/test_audit.py::test_synthesized_walkthrough_static_site_branch tests/test_audit.py::test_synthesized_walkthrough_root_index_static_site tests/test_audit.py::test_synthesized_walkthrough_not_applicable_returns_succeeded tests/test_audit.py::test_default_walkthrough_no_browser_journey_webapp_synthesizes`
  -> 4 passed.

## 2026-05-05 Real-Codex Paid E2E Follow-up

Scope: reran the gated real-Codex intent-to-proof test after the A8/harness
changes. This exposed three deeper bugs and one over-narrow test assertion.

Paid run 1:
- Session: `2026-05-05-082246-ebd913`
- Wall/cost/verdict: `302.7s`, `$0.7640907`, `partial`
- Finding: i2p wrote a valid session but no `otto_logs/latest`, so the harness
  could not resolve the session. The audit also capped to partial because a
  group-only spec had `features: []` while walkthrough lines used Group ids.

Paid run 2:
- Session: `2026-05-05-083348-196420`
- Wall/cost/verdict: `706.3s`, `$1.27068095`, `partial`
- Finding: `otto_logs/latest` was fixed and pointed to the session. The run
  still capped to partial because the group-only spec's walkthrough used
  Group `feature_ids` prose (`"create Counter component"`, etc.), not Group ids.

Paid run 3:
- Session: `2026-05-05-084813-e3a2d8`
- Wall/cost/verdict: `516.6s`, `$1.0733032`, `passed`
- Shape: tiny real webapp, one Group, real retry. Attempt 1 failed because the
  BrowserJourney file did not exist; attempt 2 reused context, created the
  journey, passed checks, merged, audited, and rendered.
- Evidence: `otto_logs/latest -> sessions/2026-05-05-084813-e3a2d8`;
  `summary.json verdict=passed`; `proof-packet.json verdict=passed`;
  6 screenshot artifacts under `tests/evidence/`.
- Harness note: the pytest command still failed at the old screenshot assertion
  because it only counted `audit/` + `otto_artifacts/`, while the generated
  BrowserJourney wrote screenshots under `tests/evidence/`. The assertion is
  now broadened, and the saved session passes the updated checks.

Fixes from these runs:
- i2p runner now writes `otto_logs/latest` when it marks a managed session
  active; completion still clears only the `paused` pointer.
- Audit coverage validation now builds an audit-only fallback Feature map from
  observed Group ids and Group `feature_ids` for legacy/group-only specs. It
  does not mutate or repersist the spec, so spec hashes remain stable.
- BrowserJourney check execution now recovers existing screenshot/video paths
  printed by the harness when `evidence_globs` are wrong or stale.
- The real-Codex integration test now pins `uv --project <repo>`, accepts
  screenshots from actual BrowserJourney locations, and still requires a
  strict product verdict of `passed`.

Verification after fixes:
- Saved-session assertion for `2026-05-05-084813-e3a2d8`: latest pointer valid,
  `summary_verdict=passed`, `proof_verdict=passed`, screenshot count `6`.
- `uv run pytest -q tests/test_checks.py::test_browser_journey_subprocess_and_globs_collect_artifacts tests/test_checks.py::test_browser_journey_collects_printed_artifacts_when_glob_misses tests/test_audit_walkthrough_coverage.py tests/test_audit_coverage_cap.py tests/test_runner.py::test_run_pipeline_writes_resume_checkpoint_and_clears_pointer`
  -> 19 passed.
- `uv run ruff check otto/checks.py otto/audit.py otto/runner.py tests/test_checks.py tests/test_audit_walkthrough_coverage.py tests/integration/test_intent_to_proof.py tests/test_runner.py`
  -> passed.
- `uv run python scripts/test_tiers.py fast`
  -> 1395 passed, 531 deselected.
- `git diff --check`
  -> passed.

## 2026-05-05 Pressure Test Tier 1 - Brownfield CLI/Library

Project:
- Tier: 1, medium existing CLI/library project.
- Source/path: fresh clone of `python-humanize/humanize` at
  `/tmp/otto-i2p-pressure-20260505/humanize-rerun15`.
- Why this is harder than prior tiny webapp runs: real public library with
  mature tests, tox env matrix, docs/lint/mypy gates, locale-sensitive parsing,
  and multiple public APIs that needed consistent behavior.

Otto run:
- Exact command:
  `/usr/bin/time -p uv --project /Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2 run --extra dev python -m otto.cli improve feature "Support comma-separated numeric strings consistently for existing numeric string APIs: humanize.intword('1,200,000') should return '1.2 million', humanize.naturalsize('1,024', binary=True) should return '1.0 KiB', and invalid strings should still be returned unchanged. Preserve existing public behavior and existing test suite." --i2p --provider codex --budget 1800 --max-turns 120 --break-lock --verbose`
- Provider/model: Codex provider requested and verified by child process tree;
  concrete model name was not surfaced in Otto artifacts.
- Session id: `2026-05-05-183958-4c7fea`.
- Wall time: `/usr/bin/time` `real 1332.28s`; proof packet `wall_s=1063.99s`.
- Cost: `proof-packet.json cost_usd=0.0`; Codex token usage was recorded but no
  USD cost was surfaced by the provider adapter.
- Final Otto verdict: `passed`.
- Proof packet:
  `/tmp/otto-i2p-pressure-20260505/humanize-rerun15/otto_logs/sessions/2026-05-05-183958-4c7fea/proof-packet.html`
  and
  `/tmp/otto-i2p-pressure-20260505/humanize-rerun15/otto_logs/sessions/2026-05-05-183958-4c7fea/proof-packet.json`.
- Browser/video/screenshot artifacts: not applicable for this library tier.

Run behavior and evidence:
- Compile produced 5 Groups/Features: number, filesize, time, list, and i18n.
- Initial audit correctly blocked: `intword('1,200,000')` returned the original
  string and `naturalsize('1,024', binary=True)` raised `ValueError`.
- First repair fixed valid comma parsing for `intword`; second repair fixed
  `naturalsize` with grouping validation.
- Product-wide re-audit correctly found a remaining invalid-string bug:
  `intword('1,20,000')` normalized to `120.0 thousand` instead of remaining
  unchanged.
- The repair loop retried the still-partial number Feature and landed
  `e22d1e8`, adding structural grouping validation and a native regression.
- Final audit ran exact API probes, focused tests, full pytest, and native
  `uvx --with tox-uv tox`; all passed.

External verifier:
- `uv run --extra tests pytest -q` from the target repo:
  `693 passed, 69 skipped in 0.51s`.
- Independent API oracle:
  `intword('1,200,000') == '1.2 million'`,
  malformed/alpha comma strings remain unchanged, and
  `naturalsize('1,024', binary=True) == '1.0 KiB'`; all assertions passed.

Bugs found and classification:
- Otto bug fixed: scoped or stale re-audit results could previously mask still
  failing Features and allow a false pass. The real run exercised the fix by
  retrying `number-formatting-and-wording` after a partial re-audit.
- Otto bug fixed: default repair cap of 3 was too low for a real two-feature
  brownfield repair. Raised default Layer 2 cap to 6 with tests.
- Otto bug fixed: i2p CLI/provider/config propagation previously allowed deeper
  build/audit/repair agents to drift from the requested provider. This run kept
  Codex subprocesses under the live Otto process.
- Project bugs fixed by Otto in target repo: `intword` and `naturalsize`
  comma-separated numeric string behavior and invalid-string preservation.

Root cause:
- Otto needed product-wide final re-audit semantics plus retry-loop state that
  preserves unresolved/unreturned failures across scoped repair attempts.
- Brownfield spec/CLI/provider plumbing had several weak seams: group aliases,
  raw string checks, placeholder fixtures, project kind inference, and explicit
  intent/provider overrides were not robust enough for real library repos.

Generic fixes made in this worktree:
- Brownfield spec parser/routing hardening in `otto/spec_compile.py`.
- Project-kind inference and i2p CLI override propagation in
  `otto/config.py`, `otto/cli.py`, `otto/cli_run.py`, and
  `otto/cli_improve.py`.
- Build/audit config propagation into spawned provider agents.
- Audit repair loop state merge, product-wide final re-audit, unactionable
  failure filtering, and default repair cap increase.
- Build/audit prompt hardening requiring exact acceptance and invalid-path
  executable evidence.
- Target-provider environment propagation for project `.venv/bin` and `src/`.
- `otto-as-user` skill refreshed for the redesigned i2p flow.

Regression tests added:
- Focused unit coverage across
  `tests/test_spec_compile.py`, `tests/test_brownfield_compile.py`,
  `tests/test_config.py`, `tests/test_cli_run.py`, `tests/test_agent.py`,
  `tests/test_build.py`, `tests/test_audit.py`,
  `tests/test_audit_prompt_feature_tagging.py`,
  `tests/test_audit_loop_repair.py`, `tests/test_runner.py`,
  `tests/test_merge_queue.py`, `tests/test_defaults.py`, and
  `tests/test_a1a_dataclasses.py`.

Gates run for this entry:
- Target repo external verifier:
  `uv run --extra tests pytest -q` -> `693 passed, 69 skipped`.
- Target repo external API oracle -> passed.
- Current worktree focused gates were run before the paid rerun; final expanded
  worktree gates still pending after this documentation update.

Decision:
- Escalate to Tier 2. Tier 1 passed after generic Otto fixes and a real retry
  path; no Tier 1 beyond-current-capability finding.

Follow-up fix from Tier 1 evidence inspection:
- Finding: separate audit calls in the same i2p session reused
  `audit/attempt-00`, overwriting walkthrough and feature-verdict artifacts.
  Tier 1 still had enough final proof evidence, but historical blocked/partial
  artifacts were not durable.
- Generic fix: `run_audit` now allocates the next available
  `audit/attempt-NN` directory at call start and uses absolute attempt indexes
  for log dirs and journal events.
- Regression:
  `uv run pytest -q tests/test_audit.py::test_run_audit_allocates_new_attempt_dir_across_calls`
  -> 1 passed.
- Additional checks:
  `uv run python -m py_compile otto/audit.py tests/test_audit.py` -> passed;
  `uv run ruff check otto/audit.py tests/test_audit.py` -> passed.

Checkpoint gate before commit:
- Focused affected suite:
  `uv run pytest -q tests/test_spec_compile.py tests/test_brownfield_compile.py tests/test_runner.py tests/test_build.py tests/test_audit_loop_repair.py tests/test_a1a_dataclasses.py::test_features_to_repair_caps_at_default tests/test_a1a_dataclasses.py::test_can_run_another_audit_pass_within_cap tests/test_defaults.py tests/test_audit.py tests/test_config.py tests/test_cli_run.py tests/test_agent.py tests/test_merge_queue.py tests/test_audit_prompt_feature_tagging.py`
  -> 375 passed.
- Touched-file lint:
  `uv run ruff check otto/agent.py otto/audit.py otto/audit_loop.py otto/build.py otto/cli.py otto/cli_improve.py otto/cli_run.py otto/config.py otto/defaults.py otto/merge_queue.py otto/runner.py otto/spec_compile.py tests/test_a1a_dataclasses.py tests/test_agent.py tests/test_audit.py tests/test_audit_loop_repair.py tests/test_audit_prompt_feature_tagging.py tests/test_brownfield_compile.py tests/test_build.py tests/test_cli_run.py tests/test_config.py tests/test_defaults.py tests/test_merge_queue.py tests/test_runner.py tests/test_spec_compile.py`
  -> passed.
- `git diff --check` -> passed.
- Note: full `uv run ruff check otto tests` currently reports unrelated
  pre-existing unused imports in files outside this patch set
  (`otto/logstream.py`, `otto/spec_amend.py`, and several untouched tests).

## 2026-05-05 Pressure Test Tier 2 - Brownfield Webapp

Project:
- Tier: 2, brownfield webapp.
- Source/path: official Flask tutorial app copied from a fresh
  `pallets/flask` clone into
  `/tmp/otto-i2p-pressure-20260505/flaskr-tier2`.
- Why this is harder than Tier 1: the task required preserving an existing
  server-rendered app, authentication, SQLite-backed blog CRUD, native tests,
  and browser-visible behavior while adding a new query-driven user workflow.

Otto run:
- Exact command:
  `/usr/bin/time -p uv --project /Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2 run --extra dev python -m otto.cli improve feature "Add search to the existing Flaskr blog index: the home page should include a search form using query parameter q, filter posts case-insensitively by title or body when q is non-empty, preserve existing login/register/auth behavior, show all posts when q is blank, and show a clear no-results message when no posts match. Add focused tests and keep the existing tutorial test suite passing." --i2p --provider codex --budget 2200 --max-turns 140 --break-lock --verbose`
- Provider/model: Codex provider requested and verified by live child process
  tree; concrete model name was not surfaced in Otto artifacts.
- Session id: `2026-05-05-190842-6c7b0d`.
- Wall time: `/usr/bin/time` `real 1019.39s`; proof packet
  `wall_s=799.997081999667`.
- Cost: `proof-packet.json cost_usd=0.0`; Codex token usage was recorded but no
  USD cost was surfaced by the provider adapter.
- Final Otto verdict: `passed`.
- Proof packet:
  `/tmp/otto-i2p-pressure-20260505/flaskr-tier2/otto_logs/sessions/2026-05-05-190842-6c7b0d/proof-packet.html`
  and
  `/tmp/otto-i2p-pressure-20260505/flaskr-tier2/otto_logs/sessions/2026-05-05-190842-6c7b0d/proof-packet.json`.

Run behavior and evidence:
- Baseline external test before Otto: `uv run --extra test pytest -q` ->
  `24 passed`.
- Initial audit correctly blocked: native tests passed, but direct Flask
  test-client probes showed `/?q=ALPHA`, `/?q=banana`, and `/?q=nomatch` still
  returned all posts, no search form existed, and no no-results message existed.
- Layer 2 repair changed only `flaskr/blog.py`,
  `flaskr/templates/blog/index.html`, and `tests/test_blog.py`, then passed
  `python -m pytest tests/test_blog.py` with `13 passed`.
- Final audit corrected a bad intermediate CLI artifact, ran the full suite,
  exercised title/body/blank/no-result/wildcard search cases, and passed.
- Target repo repair commit: `63f591c i2p(blog): build slice on
  layer2/blog-index-list-posts`.

External verifier:
- `uv run --extra test pytest -q` from the target repo:
  `25 passed in 0.25s`.
- HTTP verifier:
  `curl http://127.0.0.1:5123/?q=ALPHA` returned only `Alpha Release`;
  `curl http://127.0.0.1:5123/?q=nomatch` returned the search form and
  `No posts found.` with no posts.
- Browser evidence:
  Playwright snapshot at
  `/tmp/otto-i2p-pressure-20260505/flaskr-tier2/otto_logs/external-browser/.playwright-cli/page-2026-05-05T19-27-01-247Z.yml`
  showed textbox `Search posts` with value `banana` and only `Lunch Plans`.
  Screenshot:
  `/tmp/otto-i2p-pressure-20260505/flaskr-tier2/otto_logs/external-browser/.playwright-cli/page-2026-05-05T19-27-17-371Z.png`.

Bugs found and classification:
- Otto bug fixed: `otto improve --i2p` reused the baseline brownfield compile
  prompt, which told the spec agent to document only current behavior and not
  include missing requested features. In this run, the requested search feature
  appeared in `non_goals` as `search-not-currently-implemented`. Audit still
  blocked because it used the user intent, but the spec contract was wrong.
- Otto bug fixed: Flask apps with templates/static were inferred as `api`,
  degrading project-kind-specific proof rendering and default browser evidence.

Root cause:
- Brownfield compile had only one mode. `certify` needs a current-state
  baseline contract, but `improve` needs a desired post-run target contract.
  Treating both the same contradicts the redesign's "intent becomes product
  contract" promise.
- Python project-kind inference treated any Flask/Django dependency as API
  without checking whether the repo had server-rendered templates/static assets.

Generic fixes made in this worktree:
- `compile_spec(..., brownfield_mode="baseline"|"target")` now renders
  mode-specific guidance.
- `otto improve --i2p` routes brownfield compile through target mode, while
  `otto certify --i2p` keeps baseline mode.
- `detect_project_kind` now classifies Flask/Django projects with top-level or
  package-level `templates/` or `static/` directories as `webapp`; FastAPI
  remains `api`.

Regression tests added:
- `tests/test_brownfield_compile.py::test_brownfield_target_mode_treats_intent_as_future_contract`
- `tests/test_cli_run.py::test_orchestrate_improve_uses_target_brownfield_compile`
- `tests/test_config.py::TestDetectProjectKind::test_detects_flask_template_app_as_webapp`

Gates run for this entry:
- Target repo external verifier: `uv run --extra test pytest -q` ->
  `25 passed`.
- Target repo HTTP and Playwright browser checks -> passed.
- Focused worktree regressions:
  `uv run pytest -q tests/test_brownfield_compile.py::test_brownfield_compile_uses_brownfield_prompt tests/test_brownfield_compile.py::test_brownfield_target_mode_treats_intent_as_future_contract tests/test_config.py::TestDetectProjectKind tests/test_cli_run.py::test_orchestrate_improve_uses_target_brownfield_compile`
  -> `9 passed`.
- Touched-file lint:
  `uv run ruff check otto/spec_compile.py otto/config.py otto/cli_run.py otto/prompts/__init__.py tests/test_brownfield_compile.py tests/test_config.py tests/test_cli_run.py`
  -> passed.
- Prompt smoke on the Flaskr repo confirmed `detect_project_kind(...) ==
  "webapp"` and target-mode prompt includes
  `A missing requested behavior is a target Feature, not a non_goal`.

Decision:
- Escalate to Tier 3. Tier 2 passed after one real repair cycle and two
  generic Otto fixes; no Tier 2 beyond-current-capability finding.

## 2026-05-05 Pressure Test Tier 3 - Full-Stack/Persistence OSS App

Project:
- Tier: 3, full-stack/persistence-backed real OSS app.
- Source/path: fresh Datasette clone at
  `/tmp/otto-i2p-pressure-20260505/tier3/datasette`, starting from
  `0dc7bb1 Table headers and column options visible for 0 rows`.
- Why this is harder than Tier 2: the task touched a larger mature codebase
  with CLI output, SQLite persistence, immutable inspect-file cache semantics,
  JSON APIs, native tests, and durable state verification rather than a single
  rendered page workflow.

Otto run:
- Exact command:
  `/usr/bin/time -p uv --project /Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2 run --extra dev python -m otto.cli improve feature "Enhance Datasette's inspect-file workflow so datasette inspect includes a columns list for each table in database order, and when Datasette is served with --inspect-file the database JSON for immutable databases exposes those column lists without losing existing cached counts, hash, size, table rows, or table API behavior. Preserve existing inspect output fields, add focused tests, and keep the native targeted tests passing." --i2p --provider codex --budget 2600 --max-turns 160 --break-lock --verbose`
- Provider/model: Codex provider requested and verified by child process tree;
  concrete model name was not surfaced in Otto artifacts.
- Session id: `2026-05-05-193804-887f13`.
- Wall time: `/usr/bin/time` `real 1309.65s`; `summary.json`
  `duration_s=1029.324188041035`.
- Cost: `summary.json cost_usd=0.0`; Codex token usage was recorded in logs
  but no USD cost was surfaced by the provider adapter.
- Final Otto verdict: `passed`; `summary.json` reports `stories_passed=6`,
  `stories_tested=6`, `rounds=1`.
- Proof packet:
  `/tmp/otto-i2p-pressure-20260505/tier3/datasette/otto_logs/sessions/2026-05-05-193804-887f13/proof-packet.html`
  and
  `/tmp/otto-i2p-pressure-20260505/tier3/datasette/otto_logs/sessions/2026-05-05-193804-887f13/proof-packet.json`.
- Browser/video/screenshot artifacts: not applicable for this CLI/API tier.

Run behavior and evidence:
- Baseline targeted native tests before Otto:
  `uv run --group dev pytest -q tests/test_cli.py::test_inspect_cli tests/test_cli.py::test_serve_with_inspect_file_prepopulates_table_counts_cache tests/test_api.py::test_inspect_file_used_for_count`
  -> `3 passed`.
- Compile produced a 4-group target contract for database introspection,
  CLI inspect workflow, database JSON API, and table JSON API. The rendered
  compile prompt correctly used target repair guidance, not baseline guidance.
- Initial audit correctly blocked `datasette inspect`: output had counts only,
  omitted `columns`, and returned tables alphabetically instead of SQLite
  creation order for an audit DB created as `z_first`, then `a_second`.
- Repair focused on `datasette/cli.py` and `tests/test_cli.py`, then committed
  `e530a85 i2p(cli-inspect-workflow): build slice on layer2/inspect-json-summary-with-columns`.
- Second audit ran focused CLI/API checks, created an independent SQLite DB,
  mutated generated inspect-file count/hash/size to sentinel values, served it
  immutable, and verified CLI output, `/audit-columns.json`,
  `/-/databases.json`, and table JSON behavior. Audit verdict passed.

External verifier:
- Targeted native suite after Otto:
  `uv run --group dev pytest -q tests/test_cli.py::test_inspect_cli tests/test_cli.py::test_inspect_cli_writes_to_file tests/test_cli.py::test_serve_with_inspect_file_prepopulates_table_counts_cache tests/test_api.py::test_inspect_file_used_for_count tests/test_api.py::test_database_page tests/test_table_api.py::test_table_json`
  -> `6 passed in 1.16s`.
- Independent durable-state verifier outside Otto:
  created a SQLite DB with `z_first` then `a_second`, asserted
  `datasette inspect` stdout and `--inspect-file` output preserve creation
  order and per-table column lists, mutated inspect-file count/hash/size to
  sentinel values, and verified `serve -i --inspect-file --get` responses for
  `/audit-columns.json`, `/-/databases.json`, and
  `/audit-columns/z_first.json?_size=1&_extra=count`.
  Result: passed; verifier temp dir
  `/var/folders/xg/dk8wgfy119z44797kyz7w0380000gn/T/otto-datasette-tier3-verify-v6jzufr7`.
- Verifier correction: the first outside script expected the wrong table JSON
  field (`filtered_table_rows_count`); live Datasette returns `count` for this
  endpoint. Classified as brittle oracle fixed generically, not an Otto or
  product bug.

Bugs found and classification:
- Otto bug fixed before the run: mixed Python/Node manifests were classified as
  `library` because `package.json` could override `pyproject.toml`; Datasette
  has both. Generic fix committed in `03abc4370` so Python manifests win over
  auxiliary package manifests.
- Otto bug fixed after the run: target-mode brownfield improve printed
  `brownfield baseline spec` even though it sent the correct target prompt.
  This was a misleading log/triage bug, not a behavior blocker.
- Skill/doc bug fixed: `otto-as-user` still described `--resume` as a generic
  i2p flag. Current CLI supports i2p resume for `build` and `certify`; `improve
  --resume` is legacy-only/ignored. The skill now says that explicitly.
- Project bug fixed by Otto in target repo: Datasette inspect JSON did not
  include table `columns` and did not preserve SQLite creation order for the
  new desired contract.

Root cause:
- Otto project-kind inference needed to respect primary language manifests
  before auxiliary frontend/tooling manifests in mixed repositories.
- Brownfield target/baseline mode existed in prompt plumbing but the CLI
  progress label was hard-coded, which undercut logs-first debugging.
- The user-level dogfood skill needed to track current CLI semantics after the
  redesign cutover.

Generic fixes made in this worktree:
- `otto/config.py` project-kind detection now checks Python manifests before
  JavaScript manifests.
- `otto/cli_run.py` now prints `brownfield target spec` for improve-mode target
  compile and `brownfield baseline spec` for baseline compile.
- `.codex/skills/otto-as-user/SKILL.md` now documents build/certify resume
  support and warns that improve resume is currently legacy-only/ignored.

Regression tests added:
- `tests/test_config.py::TestDetectProjectKind::test_python_manifest_beats_auxiliary_package_json`
- `tests/test_cli_run.py::test_orchestrate_improve_uses_target_brownfield_compile`
  now also asserts the target-mode compile heading.

Gates run for this entry:
- Worktree mixed-manifest regression:
  `uv run pytest -q tests/test_config.py::TestDetectProjectKind` -> `7 passed`.
- Worktree compile-heading regression:
  `uv run pytest -q tests/test_cli_run.py::test_orchestrate_improve_uses_target_brownfield_compile`
  -> `1 passed`.
- Touched-file lint:
  `uv run ruff check otto/config.py otto/cli_run.py tests/test_config.py tests/test_cli_run.py`
  -> passed.
- `git diff --check` -> passed.
- Target repo external native and durable-state verifiers -> passed as above.

Decision:
- Escalate to Tier 4. Tier 3 passed after one real repair cycle; no Tier 3
  beyond-current-capability finding.

## 2026-05-05 Pressure Test Tier 4 - Nontrivial Open-Source Repo

Project:
- Tier: 4, nontrivial open-source repo.
- Source/path: fresh Rich clone at
  `/tmp/otto-i2p-pressure-20260505/tier4/rich`, starting from
  `46cebbb fix changelog`.
- Why this is harder than Tier 3: the task changed a mature terminal rendering
  library with a broad native test suite, docs, CLI entrypoint behavior, inline
  Markdown rendering semantics, nested layout behavior, and a tox-based CI
  matrix that is broader than a local product contract gate.

Otto run:
- Exact command:
  `/usr/bin/time -p uv --project /Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2 run --extra dev python -m otto.cli improve feature "Add GitHub-style Markdown task list rendering to Rich: unordered list items that begin with [x] or [X] should render as checked task items, items that begin with [ ] should render as unchecked task items, the raw marker should not appear in rendered output, nested task lists should still indent correctly, ordinary list items and inline Markdown styling should keep existing behavior, and focused tests plus docs should cover the behavior." --i2p --provider codex --budget 3000 --max-turns 160 --break-lock --verbose`
- Provider/model: Codex provider requested and verified by child process tree;
  concrete model name was not surfaced in Otto artifacts.
- Session id: `2026-05-05-200518-e12466`.
- Wall time: `/usr/bin/time` `real 1374.44s`; `summary.json`
  `duration_s=1118.111467416864`.
- Cost: `summary.json cost_usd=0.0`; Codex token usage was recorded in logs
  but no USD cost was surfaced by the provider adapter.
- Final Otto verdict: `partial`; product/features passed, but the contract gate
  selected Rich's broad tox matrix and failed locally.
- Proof packet:
  `/tmp/otto-i2p-pressure-20260505/tier4/rich/otto_logs/sessions/2026-05-05-200518-e12466/proof-packet.html`
  and
  `/tmp/otto-i2p-pressure-20260505/tier4/rich/otto_logs/sessions/2026-05-05-200518-e12466/proof-packet.json`.
- Browser/video/screenshot artifacts: not applicable for this terminal
  library/CLI tier.

Run behavior and evidence:
- Baseline prep installed Rich editable into a project `.venv`, installed
  missing native test deps (`attrs`, `pytest-cov`, `typing-extensions`), and
  removed a generated `uv.lock` so the target repo state stayed focused.
- Baseline focused Markdown tests before Otto:
  `.venv/bin/python -m pytest -q tests/test_markdown.py tests/test_markdown_no_hyperlinks.py`
  -> `9 passed`.
- Baseline full native suite before Otto:
  `.venv/bin/python -m pytest -q` -> `957 passed, 24 skipped`.
- Initial audit correctly blocked the missing feature: task markers remained
  visible in both API and `python -m rich.markdown` probes.
- Otto repaired the target repo with two commits:
  `3155903 i2p(markdown-rendering): build slice on layer2/markdown-task-list-rendering`
  and
  `da9eba0 i2p(markdown-rendering): build slice on layer2/markdown-module-cli`.
- Attempt-01 audit marked all 11 features passed and repeatedly showed the full
  direct pytest suite passing: `961 passed, 24 skipped`.
- The final `partial` verdict came from the integrated contract gate, not the
  requested product behavior. The proof packet shows:
  `test_command='uvx --with tox-uv tox' exit=1`, while the same packet includes
  `961 passed, 24 skipped` for direct pytest.

Recovery/certify run:
- Exact command:
  `/usr/bin/time -p uv --project /Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2 run --extra dev python -m otto.cli certify "Verify Rich Markdown renders GitHub-style task lists: unordered items beginning [x] or [X] render checked tasks, [ ] renders unchecked tasks, raw markers are removed, nested indentation and ordinary/ordered lists are preserved, inline Markdown styling still works, and the module CLI renders the same behavior." --i2p --provider codex --budget 1600 --max-turns 120 --break-lock`
- Session id: `2026-05-05-203108-49d4d9`.
- Wall time: `/usr/bin/time` `real 523.90s`; `summary.json`
  `duration_s=320.643324916251`.
- Cost: `summary.json cost_usd=0.0`.
- Final Otto verdict: `passed`; `summary.json` reports `stories_passed=3`,
  `stories_tested=3`, `rounds=1`.
- Proof packet:
  `/tmp/otto-i2p-pressure-20260505/tier4/rich/otto_logs/sessions/2026-05-05-203108-49d4d9/proof-packet.html`
  and
  `/tmp/otto-i2p-pressure-20260505/tier4/rich/otto_logs/sessions/2026-05-05-203108-49d4d9/proof-packet.json`.
- Recovery evidence after the detector fix: audit selected the project venv's
  pytest (`/private/tmp/otto-i2p-pressure-20260505/tier4/rich/.venv/bin/pytest`)
  as the contract command and passed with `961 passed, 24 skipped`.

External verifier:
- Full native suite outside Otto:
  `.venv/bin/python -m pytest -q` -> `961 passed, 24 skipped in 3.51s`.
- Independent render oracle outside Otto:
  rendered checked, uppercase checked, unchecked, nested, ordinary bullet,
  ordered-list literal, and inline-styled Markdown through `rich.markdown.Markdown`
  and through `python -m rich.markdown --width 44`; asserted task glyphs are
  present, raw task markers are stripped, ordinary/ordered content is preserved,
  and CLI behavior matches. Result: `rich task-list oracle passed`.

Bugs found and classification:
- Otto bug fixed: contract-command detection preferred `tox` whenever
  `tox.ini` existed, even after the project had a prepared `.venv/bin/pytest`
  that represented the native local product contract. Rich's tox envlist is a
  broad CI matrix with lint/docs/multiple Python versions, so this produced a
  false `partial` despite all requested product checks passing.
- Otto bug fixed: `otto certify --i2p --budget ... --max-turns ...` warned that
  budget and max-turns were ignored even though the CLI forwarded them to
  `orchestrate_certify`. This made logs-first triage misleading.
- Design gap documented: the Rich compile emitted a spec-validator warning for
  a group dependency that was not present in the final spec. The run still
  completed correctly, but unknown dependency handling should be hardened in a
  later spec-normalization pass instead of hidden by this pressure test.

Root cause:
- `detect_test_command` treated tox/nox as a better signal than a ready project
  venv pytest command. For real projects, tox/nox often encode CI matrix
  concerns that are not the same as Otto's local user-facing contract gate.
- The i2p certify ignored-flag list was stale after budget/max-turn forwarding
  was added.

Generic fixes made in this worktree:
- `otto/config.py` now lets a prepared project `.venv/bin/pytest` beat tox/nox
  orchestration for the default test command.
- `otto/cli.py` no longer reports `--budget` or `--max-turns` as ignored for
  `certify --i2p`.

Regression tests added:
- `tests/test_config.py::TestDetectTestCommand::test_project_venv_pytest_beats_tox_matrix`
- `tests/test_cli_run.py::test_certify_i2p_budget_and_max_turns_are_not_ignored`

Gates run for this entry:
- Target repo full native suite and independent render oracle -> passed as
  listed above.
- Recovery certify run through Otto -> passed.
- Focused worktree regressions:
  `uv run pytest -q tests/test_cli_run.py::test_certify_i2p_warns_about_ignored_legacy_flags tests/test_cli_run.py::test_certify_i2p_budget_and_max_turns_are_not_ignored tests/test_config.py::TestDetectTestCommand`
  -> `20 passed`.
- Touched-file lint:
  `uv run ruff check otto/config.py otto/cli.py tests/test_config.py tests/test_cli_run.py`
  -> passed.
- `git diff --check` -> passed.

Decision:
- Fix and escalate to Tier 5. Tier 4 found two generic Otto defects and one
  documented spec-normalization gap; the target product change itself passed
  native, external, and recovery certify checks.

## 2026-05-05 Pressure Test Tier 5 - Complex Product Workflow

Project:
- Tier: 5, complex product workflow.
- Source/path: fresh Healthchecks clone at
  `/tmp/otto-i2p-pressure-20260505/tier5/healthchecks`, starting from
  `39a2fb7 Fix double escaping in "Monthly Call Limit Reached" message`.
- Why this is harder than Tier 4: the task crossed Django model persistence,
  migrations, authenticated web UI, readonly team authorization, API
  create/update/list/get semantics, query-state preservation, native test
  discovery, and durable database state.

Otto run:
- Exact command:
  `/usr/bin/time -p uv --project /Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2 run --extra dev python -m otto.cli improve feature "Add a favorite-check workflow to Healthchecks: authenticated read-write users can mark and unmark individual checks as favorites from both the My Checks list and the check details page; the favorite state persists on the Check model; read-only team members can see favorite state but cannot change it; My Checks supports a favorite=1 query filter that composes with existing tag, search, status, and sort filters and preserves that query state in sort/filter URLs; API create, update, list, and get responses include a boolean favorite field with validation, and invalid non-boolean favorite input returns 400 without saving. Controls must have accessible labels and aria-pressed state. Add focused tests and keep the native Django test suite passing." --i2p --provider codex --budget 4500 --max-turns 180 --break-lock --verbose`
- Provider/model: Codex provider requested and verified by child process tree;
  concrete model name was not surfaced in Otto artifacts.
- Session id: `2026-05-05-204653-da65ea`.
- Wall time: `/usr/bin/time` `real 3010.59s`; `summary.json`
  `duration_s=2623.3898103339598`.
- Cost: `summary.json cost_usd=0.0`; Codex token usage was recorded in logs
  but no USD cost was surfaced by the provider adapter.
- Final Otto verdict: `passed`; `summary.json` reports `stories_passed=12`,
  `stories_tested=12`, `rounds=1`.
- Proof packet:
  `/tmp/otto-i2p-pressure-20260505/tier5/healthchecks/otto_logs/sessions/2026-05-05-204653-da65ea/proof-packet.html`
  and
  `/tmp/otto-i2p-pressure-20260505/tier5/healthchecks/otto_logs/sessions/2026-05-05-204653-da65ea/proof-packet.json`.
- Browser/video/screenshot artifacts: no screenshot/video artifact for this
  tier. The synthesized webapp walkthrough logged `shape=not-applicable`
  because it only recognizes Flask/static entrypoints, then the audit judge
  compensated with direct Django test-client HTML and API walkthrough artifacts
  under `audit/attempt-01/walkthrough/`.

Run behavior and evidence:
- Baseline setup installed Healthchecks in a project `.venv` on Python 3.12.
  Full `requirements-dev.txt` initially failed on optional `mysqlclient`
  system headers, so the local SQLite pressure environment installed dev
  requirements excluding MySQL-only support.
- Baseline focused native tests before Otto:
  `.venv/bin/python manage.py test hc.front.tests.test_my_checks hc.front.tests.test_add_check hc.front.tests.test_details hc.api.tests.test_create_check hc.api.tests.test_update_check --verbosity 1`
  -> `Ran 151 tests`, OK.
- Baseline full native suite before Otto:
  `.venv/bin/python manage.py test --verbosity 1`
  -> `Ran 1701 tests in 5.518s`, OK.
- Initial audit correctly blocked the missing favorite workflow while preserving
  existing My Checks, details, ping/status, readonly, and API behavior.
- Otto repaired the target repo with five commits:
  `5366616 i2p(front-checks-ui): build slice on layer2/favorite-web-toggle`,
  `111550b i2p(front-checks-ui): build slice on layer2/favorite-filter-url-state`,
  `de1e459 i2p(front-checks-ui): build slice on layer2/favorite-accessible-controls`,
  `40a4693 i2p(api-check-core): build slice on layer2/check-model-persistence-preserved`,
  and
  `18e93f1 i2p(api-check-core): build slice on layer2/checks-api-favorite-field-validation`.
- Final audit marked all 12 features passed and selected the project
  `.venv/bin/python manage.py test` command, which ran `1716 tests` OK.
- Walkthrough evidence includes direct HTML/API artifacts for My Checks,
  details, readonly views, favorite filter URLs, API CRUD, invalid input
  denial, readonly key denial, and status polling.

External verifier:
- Full native suite outside Otto:
  `.venv/bin/python manage.py test --verbosity 1`
  -> `Ran 1716 tests in 5.556s`, OK.
- Independent durable-state verifier outside Otto:
  used a fresh SQLite database and Django test client, migrated from scratch,
  created read-write and read-only users/API keys, toggled favorite state from
  My Checks and Details, asserted query-state preservation and accessible
  labels/`aria-pressed`, validated API create/update/list/get favorite
  behavior, asserted invalid values return 400 without mutation, asserted
  read-only team writes are denied, and asserted read-only API write attempts
  are denied without mutation. Result:
  `healthchecks favorite workflow oracle passed db=/var/folders/xg/dk8wgfy119z44797kyz7w0380000gn/T/otto-healthchecks-tier5-jearrren/verify.sqlite3`.

Bugs found and classification:
- Otto bug fixed before the run: `detect_test_command` did not recognize
  Django `manage.py` projects, so a large Django app could fall back to generic
  or missing test commands. Generic fix committed in `c4adcd50e`.
- Design gap documented: the Healthchecks spec compiled into multiple groups
  with `cross_group_checks: []`; the run still passed because audit artifacts
  checked the integrated product, but spec generation should require meaningful
  cross-group checks for multi-group product workflows.
- Design gap documented: synthesized webapp capture currently handles static
  and Flask-style app shapes but skipped this Django app. The audit judge made
  a correct Django client walkthrough, so product verification passed, but A8
  browser/video capture remains incomplete for non-Flask dynamic webapps.
- Project setup issue: optional MySQL client headers were unavailable locally;
  SQLite-focused Healthchecks native tests still provided valid pressure
  coverage.
- Oracle brittleness fixed during external verification: the outside checker
  needed Django setup before importing Healthchecks models, needed to preserve
  query state through `Referer` rather than assuming URL-param ordering, and
  needed to accept read-only API write denial as either 401 or 403 while
  asserting no mutation. Classified as verifier brittleness, not an Otto bug.

Root cause:
- Otto's default test-command detector needed framework-aware handling for
  Django projects with a local project venv.
- The audit browser/screenshot surface is still entrypoint-shape limited; for
  Django, it falls back to judge-authored direct client evidence instead of
  launching the app for browser artifacts.

Generic fixes made in this worktree:
- `otto/config.py` now detects `manage.py` and uses the project venv Python to
  run `manage.py test` when no stronger pytest signal exists.

Regression tests added:
- `tests/test_config.py::TestDetectTestCommand::test_detects_django_manage_py_test_with_project_venv`

Gates run for this entry:
- Worktree Django detector regression:
  `uv run pytest -q tests/test_config.py::TestDetectTestCommand` -> `19 passed`.
- Touched-file lint:
  `uv run ruff check otto/config.py tests/test_config.py` -> passed.
- Target repo full native suite and independent durable-state oracle -> passed
  as listed above.

Decision:
- Escalate to Tier 6. Tier 5 passed after one generic Otto detector fix and
  exposed two documented design gaps: multi-group cross-check weakness and
  incomplete dynamic webapp screenshot/video capture.

## 2026-05-05 Pressure Test Tier 6 - Adversarial Beyond-Capability Probe

Project:
- Tier: 6, adversarial/beyond-capability probe.
- Source/path: fresh Saleor clone at
  `/tmp/otto-i2p-pressure-20260505/tier6/saleor`, starting from
  `b205b2b bump: Django v5.2.14 (#19186)`.
- Why this is harder than Tier 5: Saleor is a large production GraphQL/Django
  commerce codebase with PostgreSQL-only migrations, broad native tests,
  schema generation, checkout domain invariants, event/webhook side effects,
  migrations, and concurrency-sensitive mutation semantics. The task required
  durable idempotency across API, domain, persistence, tests, and generated
  schema without regressing existing checkout behavior.

Setup and retry:
- Local setup used `uv sync --group dev`, Homebrew `libmagic`, and an
  ephemeral Homebrew PostgreSQL 16 cluster on port `55432`
  (`DATABASE_URL=postgres://saleor:saleor@localhost:55432/saleor`).
- Baseline focused native checkout mutation tests before Otto:
  `DATABASE_URL=postgres://saleor:saleor@localhost:55432/saleor DATABASE_URL_REPLICA=postgres://saleor:saleor@localhost:55432/saleor uv run pytest -q saleor/graphql/checkout/tests/mutations/test_checkout_lines_add.py saleor/graphql/checkout/tests/mutations/test_checkout_lines_update.py -n0 --reuse-db`
  -> `108 passed, 2 warnings in 59.09s`.
- An initial Otto command with `--max-turns 220` failed before creating a
  session because the CLI caps `--max-turns` at 200. The real run below is the
  retried command.

Otto run:
- Exact command:
  `/usr/bin/time -p env DATABASE_URL=postgres://saleor:saleor@localhost:55432/saleor DATABASE_URL_REPLICA=postgres://saleor:saleor@localhost:55432/saleor uv --project /Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2 run --extra dev python -m otto.cli improve feature "Add durable idempotency to Saleor checkout line mutations: checkoutLinesAdd and checkoutLinesUpdate accept an optional idempotencyKey string. For the same checkout and mutation type, the first successful call records a durable key, normalized input fingerprint, and resulting checkout state. Retrying the same mutation with the same key and semantically identical line payload must be a no-op that returns the current checkout without increasing quantities, duplicating lines, or dispatching duplicate checkout events. Reusing the same key for the same checkout and mutation type with a different line payload must return a GraphQL checkout error and leave checkout lines unchanged. The key must be scoped by checkout and mutation type so the same key can be used on a different checkout or on the other line mutation. Invalid empty or over-128-character keys must return validation errors without mutation. Preserve existing checkout line behavior, stock validation, permissions, and webhook/event behavior for calls without an idempotencyKey. Add migrations, focused GraphQL tests for add/update/retry/conflict/scope/validation, and keep the native focused checkout tests passing." --i2p --provider codex --budget 6000 --max-turns 200 --break-lock --verbose`
- Provider/model: Codex provider requested; concrete model name was not
  surfaced in Otto artifacts.
- Session id: `2026-05-05-215145-0e3585`.
- Wall time: `/usr/bin/time` `real 4186.09s`; `summary.json`
  `duration_s=3860.4688793332316`.
- Cost: `summary.json cost_usd=0.0`; Codex token usage was recorded in logs
  but no USD cost was surfaced by the provider adapter.
- Final Otto verdict: `partial`; `summary.json` reports `passed=false`,
  `stories_passed=1`, `stories_tested=9`, `rounds=1`.
- Proof packet:
  `/tmp/otto-i2p-pressure-20260505/tier6/saleor/otto_logs/sessions/2026-05-05-215145-0e3585/proof-packet.html`
  and
  `/tmp/otto-i2p-pressure-20260505/tier6/saleor/otto_logs/sessions/2026-05-05-215145-0e3585/proof-packet.json`.
- Browser/video/screenshot artifacts: none. This tier was a backend GraphQL
  probe; synthesized webapp capture again logged non-applicable for the project
  shape.

Run behavior and evidence:
- Attempt 00 correctly blocked the missing feature. The integrated contract
  command ran the large native pytest suite and found a pre-existing/runtime
  AVIF MIME failure before implementation:
  `1 failed, 17227 passed, 1 skipped`.
- Layer 2 repair produced six target commits ending at
  `792afe8292598ca64bc4c3b742169a42fb365a36`; target diff stat was
  `12 files changed, 1336 insertions(+), 44 deletions(-)`.
- Attempt 01 correctly refused to call the result passed. It found the public
  `idempotencyKey` API and exact-retry/conflict branches, but reported:
  durable records were not the runtime source of truth, input fingerprints were
  raw line-list JSON rather than normalized checkout-line semantics, generated
  schema had trailing whitespace, and DB-backed focused tests inside the agent
  could not connect to PostgreSQL.
- Attempt 01 contract result:
  `13 failed, 17227 passed, 1 skipped, 34 warnings in 265.09s`.

External verifier:
- Focused add/update tests outside Otto, using the working PostgreSQL URL:
  `DATABASE_URL=postgres://saleor:saleor@localhost:55432/saleor DATABASE_URL_REPLICA=postgres://saleor:saleor@localhost:55432/saleor uv run pytest -q saleor/graphql/checkout/tests/mutations/test_checkout_lines_add.py saleor/graphql/checkout/tests/mutations/test_checkout_lines_update.py -n0 --reuse-db --maxfail=20`
  -> `119 passed, 2 warnings in 8.52s`.
- Generated diff hygiene outside Otto:
  `git diff --check origin/main...HEAD` -> failed with trailing whitespace at
  `saleor/graphql/schema.graphql:20868` and `saleor/graphql/schema.graphql:20897`.
- Clean GraphQL context test outside Otto after dropping reused test DBs:
  `DATABASE_URL=postgres://saleor:saleor@localhost:55432/saleor DATABASE_URL_REPLICA=postgres://saleor:saleor@localhost:55432/saleor uv run pytest -q saleor/graphql/tests/test_context.py -n0 --create-db --maxfail=10`
  -> `1 failed, 4 passed`; the Otto-added test mixed introspection and normal
  GraphQL fields, which Saleor rejects.
- Independent semantic fingerprint oracle outside Otto:
  imported `saleor.graphql.checkout.mutations.utils._fingerprint_checkout_line_idempotency_payload`
  after `django.setup()` and compared grouped-equivalent and order-equivalent
  line payloads. Result:
  `grouped_equals_split False` and `order_a_equals_order_b False`, confirming
  semantically equivalent retries can be classified as conflicts.
- Static oracle outside Otto:
  `saleor/graphql/checkout/mutations/utils.py` reads
  `CheckoutMetadata.private_metadata` as the idempotency lookup source;
  `CheckoutLineIdempotencyRecord` exists in `saleor/checkout/models.py`, but
  `saleor/checkout/utils.py` mirrors metadata into durable rows instead of
  making those rows authoritative.

Bugs found and classification:
- Otto bug fixed: provider agents stripped project runtime environment
  variables even though deterministic checks inherited them. The parent Otto
  process and external verifier used PostgreSQL on port `55432`, while inner
  agent audit/repair commands tried Saleor's default localhost `5432`. This
  made logs misleading and blocked agent-side DB execution on real projects.
- Beyond-current-capability: the Saleor product change did not satisfy the
  requested durable idempotency contract. It requires target-specific domain
  design work around durable authoritative records, normalized line semantics,
  event dispatch, schema hygiene, and full checkout regression behavior. That
  is not an Otto generic root-cause fix in this pass.
- Provider/runtime flake or local setup issue: the AVIF/libmagic MIME test
  failure appeared in attempt 00 before implementation and was not caused by
  Otto's target patch.
- Target implementation bugs: raw-list idempotency fingerprints, durable table
  used only as a mirror, bad mixed-introspection context test, generated schema
  trailing whitespace, and full-suite checkout regressions.
- Design gap documented: the Saleor spec again compiled multiple groups with
  `cross_group_checks: []`, so integration coverage depended on the final audit
  instead of an explicit compiled cross-group contract.

Root cause:
- Otto's agent env allowlist was too narrow for real projects. It kept provider
  credentials and shell basics, but excluded common app/test runtime handles
  such as `DATABASE_URL`, `DATABASE_URL_REPLICA`, `DJANGO_SETTINGS_MODULE`, and
  project API URL variables. Deterministic checks and provider agents therefore
  reasoned about different environments.
- The Saleor beyond-capability result was primarily a product/domain complexity
  limit, not a single orchestration failure: Otto made plausible partial
  edits, then its audit correctly identified important semantic gaps instead of
  claiming success.

Generic fixes made in this worktree:
- `otto/testing.py` now passes through common project runtime env variables to
  provider agents while still excluding arbitrary secrets such as
  `CUSTOM_PASSWORD`.
- `.codex/skills/otto-as-user/SKILL.md` was refreshed because the skill was
  mildly stale after the redesign: it now identifies `otto run` as the direct
  i2p surface, notes that provider-specific Codex pressure evidence still needs
  provider-capable entrypoints, records the `--max-turns` cap, and calls out
  runtime env verification.

Regression tests added:
- `tests/test_hardening.py::TestSubprocessEnv::test_project_runtime_env_is_allowlisted`

Gates run for this entry:
- Worktree env/provider regression:
  `uv run pytest -q tests/test_hardening.py::TestSubprocessEnv tests/test_agent.py::test_codex_resume_command_uses_resume_subcommand_shape tests/test_agent.py::test_make_agent_options_cli_overrides_beat_per_agent_yaml tests/test_agent.py::test_make_agent_options_phase_cli_overrides_beat_global_cli`
  -> `6 passed`.
- Touched-file lint:
  `uv run ruff check otto/testing.py tests/test_hardening.py` -> passed.
- Target repo focused tests and external oracles -> results listed above.

Decision:
- Stop after Tier 6 as beyond-current-capability. The run satisfies the
  breaking-point criteria: real session id, failed logs, external verifier
  evidence, repair/audit retry path, and a written reason why the target product
  implementation is not a generic Otto fix in this pass.

## 2026-05-05 Final Pressure Campaign Summary

Fixed Otto bugs:
- Brownfield improve target specs now preserve user intent instead of compiling
  a generic certification prompt.
- Mixed-manifest project kind detection no longer misclassifies substantial
  existing repos from stale generated manifests.
- Contract-command detection now prefers a prepared project venv pytest over a
  broad tox/nox matrix when that is the usable local native contract.
- `certify --i2p` no longer warns that forwarded `--budget` and `--max-turns`
  flags are ignored.
- Django `manage.py test` is detected for project-venv Django apps.
- Provider agents now inherit common project runtime env vars needed to run the
  same app/test environment as deterministic checks.

Remaining design gaps:
- Multi-group specs repeatedly emitted `cross_group_checks: []` on complex
  brownfield tasks. Final audit caught integration issues, but compile should
  produce explicit cross-group checks for multi-group plans.
- A8 screenshot/video capture is present for static/Flask-like shapes but not
  yet broad enough for Django or backend GraphQL projects.
- `otto run` is the direct i2p surface but does not expose provider/budget/turn
  overrides in CLI help, so Codex-provider pressure tests still use
  `build/improve/certify --i2p --provider codex`.
- Large-repo contract planning still defaults toward very broad native suites
  in some repos. That is honest but expensive and can surface unrelated
  environment flakes; future spec compilation should choose focused native
  contract commands more deliberately.

Deferred non-blockers:
- Optional MySQL headers for Healthchecks were unavailable locally; SQLite
  native tests and an independent durable-state oracle covered the requested
  workflow.
- Saleor's pre-existing AVIF MIME test failure is local runtime/tooling noise,
  not an Otto target implementation regression.

Beyond-current-capability finding:
- Saleor checkout-line durable idempotency remained partial. Otto produced API
  and model scaffolding plus tests, but did not produce an authoritative durable
  runtime design or normalized semantic fingerprinting, and full-suite
  regressions remained. The audit verdict was honest and externally confirmed.

Fast-gate addendum:
- The final fast gate initially found
  `tests/test_brownfield_preamble.py::test_brownfield_prompt_renders_with_preamble`
  failing because `compile-spec-brownfield.md` had lost the explicit
  `do not invent` / `never invent` anti-derivation wording. This was fixed in
  the prompt, preserving the existing test because the guidance matters for
  brownfield correctness.
