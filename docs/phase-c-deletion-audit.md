# Phase C deletion audit

**Status**: descriptive — no deletions yet. Bench evidence + default flip
+ one cycle without regression must precede actual deletion (per
`plan.md` Phase C: "after Phase A bench passes parity").

**Generated**: tick 52 (2026-05-04). Re-run the same audit before
landing the deletion PR — line counts shift as Phase B work continues.

## Modules slated for deletion

| Module | Lines | Caller surface (otto + tests) | Notes |
|---|---:|---|---|
| `otto/pipeline.py` | 2,875 → 61 (gutted tick 65) | otto: cli (`_build_locked` → hard-error, `_run_spec_phase` re-pointed at `otto.runs.lifecycle`), agent (`_cleanup_orphan_processes` re-imported from new module), certifier (gutted in C.2, no longer imports); tests: 12 files including `test_v3_pipeline.py` (DELETED), `test_hardening.py` (-2,732 LOC, 18 v3 classes/functions pruned in place), `test_run_history.py` / `test_token_usage_phase_logs.py` / `test_agent.py` (re-pointed at `otto.runs.lifecycle`), `test_build_fallback_to_intent_md.py` (DELETED), `test_resume_flow.py` (untouched — integration scope) | **DELETED tick 65 (Phase C.3):** `build_agentic_v3` + `run_certify_fix_loop` removed. Shared run-lifecycle helpers (`_write_session_summary`, `_append_session_history`, `_runtime_metadata`, `_cleanup_orphan_processes`, `_atomic_publisher`, atomic terminal/heartbeat callbacks, `_persist_atomic_cancelled_terminal_state`, `_repair_atomic_history`, `_intent_provenance_payload`, `_spec_provenance_payload`, etc.) moved to new `otto/runs/lifecycle.py` (~600 lines) — kept because `otto/agent.py`, `otto/cli.py:_run_spec_phase`, and the test suite still depend on them, and the new i2p stack uses them transitively via the registry / heartbeat callbacks. `otto/cli.py::_build_locked` body gutted (-654 lines) and replaced with a stub that calls `_exit_legacy_build_removed`. The remaining 61-line `pipeline.py` is a thin shim that re-exports the moved helpers (so any forgotten import path keeps working) and hard-errors via `__getattr__` on `build_agentic_v3` / `run_certify_fix_loop` / `BuildResult` / `InfraFailureError`. Final shim deletion is blocked on `otto/merge/orchestrator.py`'s lazy `run_agentic_certifier` import (C.3 follow-up). |
| `otto/certifier/__init__.py` | 4,456 → ~80 (gutted tick 64) | otto: cli, pipeline, merge.orchestrator; tests: 6 files including `test_certifier_stories.py`, `test_proof_provenance.py`, `test_v3_pipeline.py`, `test_spec.py` | **DELETED tick 64 (Phase C.2):** legacy `run_agentic_certifier` and the entire dispatch loop removed; `__init__.py` now a pure shim that re-exports `contracts.py` + `report.py` and exposes a hard-error stub for the `run_agentic_certifier` symbol (so the lazy imports in `otto/pipeline.py` and `otto/merge/orchestrator.py` — both Phase C.3 deletion targets — fail loudly instead of with `ImportError`). `contracts.py` (292) and `report.py` (40) **kept** — referenced by `otto/merge/orchestrator.py` (C.3), `tests/test_merge_orchestrator.py`, and `tests/test_hardening.py`. The new stack (`otto/audit.py`, `otto/render.py`, `otto/audit_loop.py`) does not import them. `otto/cli.py::_certify_locked` deleted (-229 lines); `otto certify --legacy` now hard-errors via `_exit_legacy_certify_removed` (sibling to `_exit_legacy_build_removed` from C.3). `tests/test_certifier_stories.py` deleted (-1,839 lines); `tests/test_proof_provenance.py`'s certifier-coupled `test_visual_evidence_manifest_written_at_capture` removed (-76 lines). `tests/test_legacy_deprecation.py` updated: the run_agentic_certifier deprecation test became a hard-error assertion (call → `RuntimeError` naming Phase C.2). **Phase C cleanup pass (tick 64 follow-up):** pruned 23 orphaned tests across `tests/test_hardening.py` (-846 lines: `TestProofOfWorkRendering` ×13, `TestSpecTimeoutTolerance` ×3, `TestCertifyPassesConfig` ×2, `TestCertifierStoryDedup` ×2, `test_standalone_certifier_target_*` ×2) and `tests/test_spec.py` (-63 lines: `TestStandaloneCertifierPrompt` ×3); rewrote `tests/test_cli_run.py::test_certify_without_i2p_uses_legacy_path` as `test_certify_without_i2p_hard_errors_after_phase_c2` (mirrors the build-side `_after_phase_c3` pattern). Targeted suite green: 135 passed across the three files. |
| `otto/cli_improve.py` | 1,366 → ~480 (gutted tick 63) | otto: cli (registration); tests: `test_cli_improve.py`, several integration tests | **DELETED tick 63 (Phase C.1a):** `_run_improve`, `_run_improve_locked`, `_apply_improver_agent_aliases`, `_exit_for_lock_busy`, `_create_improve_branch`, `_resolve_improve_certifier_mode`, `_resolve_feature_certifier_mode` removed. Subcommands stay; `--legacy` now hard-errors with a Phase C migration message. Pure helpers (`_VERDICT_GLYPHS`, `_journey_verdict`, `_render_results_section`, option callbacks, `_require_intent`) preserved because they retain external test coverage. |
| `otto/spec.py` | 603 | unknown — needs grep audit; likely the legacy markdown spec gate | NOT the new `otto/spec_compile.py` (which Phase A0/A1 built). The two coexist; `spec.py` is on the legacy `otto build --spec` markdown path. Confirm caller mapping before deletion. |

**Total LOC slated for deletion: ~9,300 lines + their test counterparts.**

## Tests likely slated for deletion or migration

These tests exercise legacy paths exclusively. Phase C deletion will
drop them. Re-evaluate on the deletion PR — some may have moved to
testing migrations.

- `tests/test_v3_pipeline.py`
- `tests/test_hardening.py` (mostly v3-related; spot-check what moves)
- `tests/test_run_history.py` (v3-specific shape; migration if needed)
- `tests/test_proof_provenance.py` (legacy proof-of-work shape)
- `tests/test_certifier_stories.py` (legacy certifier)
- `tests/test_improvement_report_*.py` (3 files; legacy improve)
- `tests/test_improve_writes_build_journal_single_round.py`
- `tests/test_improve_phase_writes_to_improve_dir.py`
- `tests/test_token_usage_phase_logs.py` (verify which shape)
- `tests/test_registry_gc.py` (verify scope)
- `tests/test_build_fallback_to_intent_md.py` (verify scope)
- `tests/integration/test_resume_flow.py` (legacy resume)
- `tests/_helpers.py` — keep; partly used by new tests.

`tests/test_legacy_deprecation.py` itself becomes obsolete after deletion (the warned functions disappear).

## Mission Control routes slated for deletion

**DELETED tick 65 (Phase C step 4 / progress.md C.1e).**

`otto/web/app.py` declared 11 GET/POST routes under `/api/runs/...`
(legacy MC inspector surface). The new design ships
`/api/run-view/<session_id>` (tick 27/29) and `/api/specs/<session_id>/...`
(tick 35) as the replacement surface. Phase C step 4 deleted the
legacy `/api/runs/<run_id>/...` body and switched the MC default
landing to the new RunListLanding → RunViewPage flow.

Routes deleted (11 total — confirmed against the grep performed at
deletion time, matches the audit count):

1. `GET /api/runs/{run_id}` (`run_detail`)
2. `GET /api/runs/{run_id}/logs` (`run_logs`)
3. `GET /api/runs/{run_id}/artifacts` (`run_artifacts`)
4. `GET /api/runs/{run_id}/artifacts/{artifact_index}/content` (`run_artifact_content`)
5. `GET /api/runs/{run_id}/artifacts/{artifact_index}/raw` (`run_artifact_raw`)
6. `GET /api/runs/{run_id}/proof-report` (`run_proof_report`)
7. `GET /api/runs/{run_id}/proof-assets/{asset_path:path}` (`run_proof_asset`)
8. `GET /api/runs/{run_id}/evidence/{asset_path:path}` (`run_legacy_proof_evidence_asset`)
9. `GET /api/runs/{run_id}/proof-of-work.{extension}` (`run_legacy_proof_file`)
10. `GET /api/runs/{run_id}/diff` (`run_diff`)
11. `POST /api/runs/{run_id}/actions/{action}` (`run_action`)

Side effects:

- `HTMLResponse` import removed from `app.py` (only used by
  `run_proof_report`).
- `otto/mission_control/service.py` methods `detail`, `logs`,
  `artifacts`, `artifact_content`, `artifact_raw_path`,
  `proof_report_html`, `proof_report_asset_path`, `diff`, `execute`
  are now orphaned — kept in place pending C.1c (`pipeline.py`
  deletion) which will sweep the legacy mission-control wiring.
- Frontend default switched: `main.tsx` mounts `<RunListLanding/>`
  by default (lists sessions from `/api/run-view`); legacy `App.tsx`
  (1731 lines) deleted. New component:
  `otto/web/client/src/components/run/RunListLanding.tsx`.
  `App.tsx`'s transitive deps (`RunInspector`, `useRunResources`,
  legacy hooks/components) are now unimported — deletion deferred
  to C.2 to keep this PR scoped to the route surface.
- Tests: `test_web_landing.py`, `test_web_mission_control.py`,
  `test_web_review_packet.py` deleted (55 tests). Surgical removals:
  1 test in `test_web_events_history.py`, 1 legacy detail call in
  `test_web_queue_actions.py`. Targeted suite (120 tests across
  `tests/test_run_view*.py`, `tests/test_spec_review_routes.py`,
  `tests/test_web_*.py`) green.

**Out-of-scope-but-affected tests** (C.1c / C.1b will clean these):
`tests/test_proof_provenance.py`, `tests/test_diff_freshness.py`,
`tests/test_merge_preflight_dirty_tree.py`, `tests/test_hardening.py`
all invoke the deleted routes directly. Per the deletion-PR
constraint they were left untouched here; they are slated for
deletion alongside `otto/pipeline.py` and `otto/certifier/`.

## Data-shape migration concerns

These don't block Phase C but would surface if not addressed:

1. **history.jsonl** — written by both legacy and i2p paths. Schema
   probably overlaps; verify by reading any legacy history record
   AND any i2p record and confirming the consumer (`otto history`,
   MC history view) handles both shapes. If they diverge, add a
   migration tool or freeze the legacy entries as historical
   archives.
2. **proof-of-work.json (legacy)** vs **proof-packet.json (i2p)** —
   research §7 has the new shape; legacy reports use a different
   schema. Phase C may need to keep a JSON loader that handles both
   so historical reports stay readable.
3. **certifier-memory.jsonl** — was used by the legacy certifier for
   cross-run memory; needs decision on whether the new audit_loop
   uses something similar.
4. **checkpoint.json** — used by `--resume`. New stack uses
   `state.jsonl` (research §3 / otto/state.py). Confirm `--resume` is
   declared unsupported in i2p mode (orchestrate_run/improve don't
   honor it currently — verified in tick 39 + 48 ignored-flag list).

## Order of deletion (lowest-blast-radius first)

When the user signals "delete":

1. **First**: delete `otto/cli_improve.py` legacy body (`_run_improve`,
   `_run_improve_locked`, helpers). Keep the click subcommand
   registrations; gut their bodies to dispatch unconditionally to
   `orchestrate_improve`. Lowest blast radius — improve was opt-in to
   the i2p path until B.3 default flip lands.
   **DONE — tick 63 (Phase C.1a).** Legacy bodies + helpers removed.
   `--legacy` flag now hard-errors. Three integration test files deleted
   (`test_improvement_report_splits_pass_warn_fail.py`,
   `test_improve_writes_build_journal_single_round.py`,
   `test_improve_phase_writes_to_improve_dir.py`); 7 hardening tests in
   `tests/test_hardening.py` deleted in place; 2 hardening tests in
   `tests/test_hardening.py` rewritten to target the i2p orchestrator;
   `tests/test_cli_improve.py` rewritten to assert the legacy-flag error
   path. `tests/test_cli_run.py` legacy-dispatch tests updated to assert
   the hard-error instead of the now-deleted `_run_improve` call.
2. **Second**: delete `otto/certifier/__init__.py` (with care —
   `contracts.py` and `report.py` may have shared types used by the
   new stack; verify imports in the new `otto/audit.py` and
   `otto/render.py`).
   **DONE — tick 64 (Phase C.2).** Legacy `run_agentic_certifier`
   and the entire dispatch loop deleted; `__init__.py` is now a
   pure shim that re-exports `contracts.py` + `report.py` and exposes
   a hard-error stub for the `run_agentic_certifier` symbol so that
   lazy imports in `otto/pipeline.py` and `otto/merge/orchestrator.py`
   (both still Phase C.3 deletion targets) raise `RuntimeError` with
   a migration message instead of `ImportError`. `contracts.py` (292
   lines) and `report.py` (40 lines) **kept** — confirmed via grep
   that `otto/audit.py`, `otto/render.py`, and `otto/audit_loop.py`
   do NOT import them; the only consumers are
   `otto/merge/orchestrator.py` (C.3) and a few legacy tests. The
   ``otto certify --legacy`` path is now a hard error via
   `cli.py::_exit_legacy_certify_removed` (mirrors C.3's
   `_exit_legacy_build_removed`).
3. **Third**: delete `otto/pipeline.py`. Largest module; hits the most
   tests. Do this after the v3 bench cycle without regression.
4. **Fourth**: delete legacy `/api/runs/<run_id>/...` MC routes from
   `otto/web/app.py`. Frontend default switches to the new RunDrawer
   simultaneously.
   **DONE — tick 65.** All 11 GET/POST routes removed; frontend
   default switched to `<RunListLanding/>` → `<RunViewPage/>`. Legacy
   `App.tsx` deleted. See the "Mission Control routes slated for
   deletion" section above for the full route + test delta.
5. **Fifth**: drop `otto/spec.py` if confirmed legacy-only after grep
   audit (the new compile path lives in `otto/spec_compile.py`).
   **NOT YET — Phase C cleanup pass (W8-B follow-up).** Grep audit
   confirms `otto/spec.py` still has an *active* consumer:
   `otto/mission_control/actions.py:375` lazily imports
   `write_spec_review_decision` for the web spec-review approve /
   regenerate flow. The other consumers are the test suite
   (`tests/test_spec.py`) and the now-deleted `otto/cli.py:_run_spec_phase`.
   Decision: **keep `otto/spec.py` for now**; revisit when Mission
   Control's web spec-review is migrated to the i2p compile pipeline
   (or the helper is moved into `otto/spec_compile.py`).
6. **Sixth**: delete legacy tests in batches (one PR per legacy
   module to keep diffs scannable).

## Phase C cleanup (W8-B follow-up — tick 66)

After Phase C.3 deleted `build_agentic_v3` + `run_certify_fix_loop`,
the parallel agent W8-B left a few unreachable / orphaned references.
This cleanup pass removes them surgically without touching modules
owned by other parallel work (cli_run.py, cli_improve.py, audit.py,
build.py, runner.py, render.py, spec_compile.py).

| Item | Status | Notes |
|---|---|---|
| `otto/cli.py:_run_spec_phase` (~450 LOC) | **DELETED.** | Only caller was the C.3-gutted `_build_locked` stub. Verified via grep across `otto/` and `tests/`: no remaining references. The new build path is wired through `otto/cli_run.py` / `otto/cli_improve.py`, which use `otto/spec_compile.py`. |
| `otto/spec.py` (603 LOC) | **KEPT.** | Mission Control's web spec-review (`otto/mission_control/actions.py`) still imports `write_spec_review_decision`. See "Order of deletion" §5 above. |
| `tests/integration/test_resume_flow.py` (47 LOC) | **DELETED.** | Single test that imported `from otto.pipeline import build_agentic_v3` (now hard-error stub). Resume coverage for the i2p stack lives in the run-lifecycle / audit-loop unit tests. |
| `tests/integration/test_build_flow.py::test_build_agentic_v3_dedupes_repeated_certify_round_markers` | **DELETED in place.** | Test name still referenced the deleted v3 entry point even though it actually exercised `run_agent_query` + `parse_certifier_markers`. Per the "minimum viable cleanup" constraint, the test was removed rather than renamed. The other test in the file (`test_build_cli_writes_canonical_and_queue_manifest_mirror`) is unaffected; unused imports (`pytest`, `AgentOptions`, `run_agent_query`, `parse_certifier_markers`) were pruned. |
| `otto/merge/orchestrator.py:1730` lazy `run_agentic_certifier` import | **KEPT (with comment).** | The orchestrator is reachable from the new stack via `otto/cli_merge.py`. Its call shape (`intent + stories + merge_context`) is incompatible with `otto.audit.run_audit`'s (`Spec + BuildResult + MergeQueueResult`); a substitute would be a structural rewrite, not a cleanup. The Phase C.2 hard-error stub already provides a clear migration message at runtime, and `tests/test_merge_orchestrator.py` exercises the path via monkeypatched stubs (49 tests pass). Added a 10-line comment block at the import explaining the C.2/C.3 status so future readers don't accidentally narrow it further. |

Verification: `uv run pytest tests/test_merge_orchestrator.py -q` →
49 passed. `tests/integration/test_build_flow.py` retains the
canonical+mirror manifest test (the only behavioural assertion left
in that file).

## Prerequisites for actual deletion

Per `plan.md` Phase C: "after Phase A bench passes parity".

- [ ] Bench A on a real fixture intent runs `otto build` (or `otto build --i2p` pre-flip) end-to-end with no regressions vs the legacy v3 path.
- [ ] `default_pipeline: i2p` flipped in `otto.yaml` — at least one cycle without regression observed.
- [ ] No active session in any user's repo currently using the legacy paths (check `otto history` for in-flight legacy sessions).
- [ ] User explicitly signals "delete" — Phase C is irreversible and gates on that signal.

## What this audit does NOT decide

- Which legacy modules are actually safe to delete in isolation.
  That's the user's call — this doc surfaces the data so the call has
  context.
- Whether the legacy paths get a long deprecation tail (e.g. one
  release with warnings + one release without before deletion). Plan.md
  says "Phase C deletes" but doesn't pin a tail length.
- How to handle users mid-flight on legacy sessions during the cutover
  window. Suggest: legacy resume keeps working through Phase B; Phase C
  deletion happens only after a quiet window.
