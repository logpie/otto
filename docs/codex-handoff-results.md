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

Pending:
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
