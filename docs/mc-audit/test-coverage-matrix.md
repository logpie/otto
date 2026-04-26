# Phase 1D — Test Coverage Matrix

Cross-references the 40 user flows from Phase 1C against the five test layers we run (or plan to run) against Mission Control. Each cell answers one question: *does this layer exercise this flow today?*

- **Server unit (pytest)** — pure-Python tests of route handlers, model, adapters, actions. Fast, mocked filesystem. Source: `tests/test_web_mission_control.py`, `tests/test_mission_control_*.py`, `tests/test_queue_dashboard.py`.
- **Server integration (pytest)** — multi-component tests (registry + filesystem + cross-process). Source: `tests/test_mission_control_integration.py`, multi-fixture flows in `test_web_mission_control.py`.
- **Agent-browser script (existing)** — `scripts/e2e_web_mission_control.py` scenarios mapped via `docs/mc-audit/coverage-migration.md`.
- **Playwright planned (Phase 3 t-id)** — pytest-playwright suite from plan section 3C (`t01–t40`).
- **Playwright live (Phase 5 W-id)** — `scripts/web_as_user.py` real-LLM scenarios from plan section 5B (`W1–W13`).

Cell values: `✓` covered, `✗` not covered, `n/a` not applicable to this layer, `partial` covers some sub-paths but not all.

---

## Coverage matrix

| # | Flow | Server unit (pytest) | Server integration (pytest) | agent-browser script (existing) | Playwright planned (Phase 3 t-id) | Playwright live (Phase 5 W-id) |
|---|---|---|---|---|---|---|
| 1 | Cold start, no project | ✓ `test_web_project_launcher_starts_without_selected_project` | ✓ `test_web_projects_endpoint_has_no_root_side_effect_without_launcher` | ✓ `project-launcher` | t01 | W1, W11 |
| 2 | Launch invalid path | ✗ | ✗ | partial (covers managed-root rejection only) | t02 | n/a |
| 3 | Launch duplicate project | ✗ | ✗ | ✗ | t03 | n/a |
| 4 | Launch outside repo / non-git | ✓ `test_web_project_launcher_rejects_selection_outside_managed_root` | ✗ | ✗ | t04 | n/a |
| 5 | Switch project | partial (`test_web_project_launcher_can_clear_selected_project` covers clear, not switch) | ✗ | ✗ | t05 | n/a |
| 6 | Submit build job (happy path) | ✓ `test_web_queue_build_enqueues_without_click_context` | ✓ (queue events recorded — `test_web_records_queue_events_and_exposes_operator_timeline`) | ✓ `fresh-queue` | t06 | W1, W2, W11 |
| 7 | Submit improve job | partial (queue routing covered, JobDialog matrix not) | ✗ | ✓ `job-submit-matrix` | t07 | W3, W11 |
| 8 | Submit certify job | partial (queue routing covered) | ✗ | ✓ `job-submit-matrix` | t08 | n/a (W11 enqueues only build/improve in current plan) |
| 9 | Submit on dirty target | ✓ `test_web_landing_blocks_merge_when_project_has_tracked_changes`, `test_web_review_packet_blocks_landing_when_project_has_tracked_changes`, `test_web_merge_all_rejects_dirty_project_before_launch` | ✗ | ✓ `dirty-blocked` | t09 | W5 |
| 10 | Submit with invalid input | ✓ `test_web_queue_rejects_unknown_after_dependency`, `test_web_queue_rejects_invalid_inner_command_args` | ✗ | ✗ | t10 | n/a |
| 11 | Resume paused run | ✓ `test_resume_queue_calls_queue_resume_subprocess`, `test_resume_build_uses_record_cwd` | ✗ | ✗ | t11 | W12a |
| 12 | Cancel running run | ✓ `test_cancel_appends_envelope_and_clears_banner_for_queue_atomic_and_merge`, `test_cancel_falls_back_to_sigterm_after_one_heartbeat`, `test_cancel_waits_at_least_four_seconds_before_fallback`, `test_cancel_skips_sigterm_for_stale_writer_identity`, `test_cancel_rejects_terminalized_live_record_before_append`, `test_cancel_rejects_duplicate_pending_cancel_before_append`, `test_queue_cancel_without_task_id_fails_fast`, `test_legacy_queue_cancel_uses_queue_state_without_live_record` | ✗ | ✗ | t12 | W2, W11, W12a |
| 13 | Retry failed run | ✓ `test_retry_uses_stored_source_argv`, `test_requeue_reconstructs_queue_cli_from_stored_task_definition`, `test_requeue_dedups_past_existing_retry_ids` | ✗ | ✗ | t13 | W6, W12a |
| 14 | Cleanup completed run | ✓ `test_cleanup_terminal_atomic_run_calls_cleanup_cli`, `test_atomic_and_merge_cleanup_wait_for_writer_finalization`, `test_remove_queued_task_calls_queue_rm`, `test_remove_abandoned_legacy_queue_task_calls_queue_rm` | ✗ | ✓ `control-tour` (cancel-only variant) | t14 | n/a |
| 15 | Merge run (single) | ✓ `test_merge_selected_and_all_shell_out`, `test_web_merge_action_rejects_already_merged_task`, `test_web_merge_action_reports_already_merged_before_dirty_repo`, `test_web_merge_action_uses_fast_merge_and_reports_immediate_failure`, `test_web_merge_action_records_late_background_failure` | ✓ `test_web_landed_task_uses_merge_state_diff_after_source_branch_deleted` | ✓ `ready-land` | t15 | W4, W11, W12b |
| 16 | Bulk merge-all | partial (`test_web_merge_all_rejects_dirty_project_before_launch` only) | ✗ | ✓ `bulk-land` | t16 | W11 |
| 17 | Browse run history | ✓ `test_history_pagination_and_dedup`, `test_history_merges_v1_v2_and_archived_sources_before_pagination`, `test_history_rows_default_missing_resumable_to_false`, `test_history_outcome_removed_filters_correctly`, `test_history_unknown_outcome_buckets_to_other_and_warns_once`, `test_web_history_detail_recovers_provider_from_manifest_argv`, `test_web_history_usage_reads_merge_summary_extra_artifact` | ✗ | ✗ | t18 | W11 |
| 18 | Filter / search / no-match | ✓ `test_web_run_detail_is_not_hidden_by_list_filters`, `test_history_outcome_removed_filters_correctly` | ✗ | ✓ `control-tour` (search portion) | t19 | n/a |
| 19 | Open run detail (inspector tabs) | ✓ `test_web_state_detail_logs_and_artifact_content`, `test_detail_view_uses_adapter_artifact_ordering`, `test_selection_preservation_across_live_to_history_transition` | ✓ `test_mission_control_multiprocess_registry_integration` | ✓ `long-log-layout` (logs+artifact) | t20 | W1, W11 |
| 20 | Diff viewer | ✓ `test_web_landing_and_detail_show_review_packet_changed_files`, `test_web_landing_surfaces_diff_errors`, `test_web_landed_task_uses_merge_state_diff_after_source_branch_deleted`, `test_web_landing_does_not_show_diff_errors_for_queued_future_branches`, `test_web_review_packet_does_not_diff_queued_future_branch`, `test_web_landing_ignores_unreachable_merge_commit`, `test_web_landing_ignores_merge_state_for_different_target` | ✗ | ✓ `ready-land` (inspector.diff) | t21 | W4, W12b |
| 21 | Proof drawers | ✓ `test_web_review_packet_includes_story_details_and_html_report`, `test_web_merge_run_review_packet_is_landing_audit_not_landable`, `test_web_merge_history_review_packet_uses_persisted_target` | ✗ | ✓ `long-log-layout` (inspector.proof) | t22 | W1 |
| 22 | Artifact viewer | ✓ `test_web_state_detail_logs_and_artifact_content`, `test_web_artifact_content_rejects_paths_outside_project`, `test_atomic_adapter_orders_artifacts_and_formats_summary`, `test_queue_adapter_includes_queue_manifest_and_merge_action_preview` | ✗ | ✓ `long-log-layout` (inspector.artifact.drilldown) | t23 | W1 |
| 22b | Run detail with no proof report (graceful) | ✗ | ✗ | ✗ | t24 | n/a |
| 23 | Diagnostics view (runtime + backlog + malformed) | ✓ `test_web_runtime_surfaces_state_and_command_recovery_issues`, `test_web_events_endpoint_reports_malformed_rows_without_breaking_state`, `test_web_events_tail_preserves_boundary_aligned_rows` | ✗ | ✓ `multi-state` (diagnostics.open) | t25 | W11 |
| 24 | Watcher start | ✓ `test_web_start_watcher_launches_background_process`, `test_web_start_watcher_reports_started_when_state_becomes_alive`, `test_web_start_watcher_blocks_when_runtime_is_stale` | ✗ | ✓ `command-backlog` | t26 | W2, W11 |
| 25 | Watcher stop | ✓ `test_web_can_stop_stale_but_live_watcher_process`, `test_web_allows_stop_for_supervised_live_watcher_pid`, `test_web_does_not_stop_stale_watcher_pid_without_held_lock` | ✗ | ✓ `watcher-stop-ui` (cancel + confirm variants) | t27 | n/a |
| 26 | Stale / unverified watcher PID | ✓ `test_web_state_marks_abandoned_live_runs_stale_not_active`, `test_web_state_marks_abandoned_legacy_queue_runs_stale`, `test_web_state_marks_abandoned_starting_queue_runs_stale`, `test_web_refuses_to_stop_unverified_live_watcher_pid`, `test_web_ignores_unheld_queue_lock_pid`, `test_web_reports_held_queue_lock_as_stale_runtime`, `test_stale_overlay_derivation_uses_grace_window_and_dead_writer`, `test_stale_live_runs_are_not_counted_as_active`, `test_queue_adapter_disables_cancel_without_task_id_and_cleanup_while_writer_alive` | ✗ | ✗ | t28 | n/a |
| 27 | Watcher start failure | ✓ `test_web_start_watcher_records_immediate_failure`, `test_long_running_subprocess_reports_late_failure` | ✗ | ✗ | t29 | n/a |
| 28 | Server restart mid-session | ✗ | ✗ | ✗ | t30 | W13 |
| 29 | Tab backgrounded → return | n/a (browser-only behavior) | n/a | ✗ | t31 | W9 |
| 30 | Two tabs open | n/a | n/a | ✗ | t32 | W10 |
| 31 | Long-running run + slow network | ✗ | ✗ | ✗ | t33 | W9 |
| 32 | Action error 4xx/5xx surfaced inline | partial (`test_web_landing_surfaces_diff_errors`, `test_web_merge_action_uses_fast_merge_and_reports_immediate_failure`, `test_web_merge_action_records_late_background_failure`, `test_disabled_action_reason_surfaces_without_execution`) | ✗ | ✗ | t17 | W5, W6 |
| 33 | Tasks ↔ Diagnostics + URL push/replace | n/a | n/a | ✗ | t34 | W11 |
| 34 | Deep link `?run=X&view=tasks` | n/a | n/a | ✗ | t35 | n/a |
| 35 | Invalid deep link (missing/deleted run) | partial (`test_web_keeps_failed_queue_tasks_inspectable_for_requeue`, `test_web_failed_queue_run_prefers_existing_primary_log`, `test_web_failed_queue_fallback_uses_latest_exact_task_block`, `test_web_cleaned_failed_queue_history_is_audit_only`) | ✗ | ✗ | t36 | n/a |
| 36 | Deep link to selected run that gets deleted mid-session | ✗ | ✗ | ✗ | t37 | n/a |
| 37 | Tab through whole UI (no traps) | n/a | n/a | ✗ | t38 | W8 |
| 38 | Operate critical flows keyboard-only | n/a | n/a | ✗ | t39, t40 | W8 |
| 39 | Screen-reader landmarks | n/a | n/a | ✗ | ✗ (out-of-plan; flagged as gap) | n/a |
| 40 | Reduced motion | n/a | n/a | ✗ | ✗ (CSS-only; check via tv* baseline at `prefers-reduced-motion`) | n/a |
| 41 | Resize between viewports | n/a | n/a | ✓ `responsive.mobile` (viewport sweep marker) | tv01–tv08 (visual regression) | W7 |
| 42 | Long strings (intent / error / URL) | partial (`test_mission_control_refresh_uses_live_registry_mtime_cache`, `test_mission_control_refresh_hot_path_stays_under_150ms_for_20_runs` cover scale, not overflow) | ✗ | ✓ partial via `long-log-layout` | tv08 (1000-row history density) | W11 |

> Numbered 1–40 in plan; rows 22b and 41/42 are split out so each plan flow has its own row. The plan lists 40 *flows*; the matrix has 42 *rows* because flow 20 ("Artifact viewer") split off "no proof report" as its own t-id (t24) and flow 39/40 split visual ("resize") from textual ("long strings") — both already separately addressed in tv01–tv08.

---

## Gap summary

Counting **plan flows 1–40** (treating row 22b as part of flow 20, rows 41/42 as flows 39/40 from the visual section):

| Coverage class | Flow count | Flows |
|---|---|---|
| **Zero coverage today** (no server unit + no agent-browser; only future Playwright/live) | **9** | 3 (duplicate project), 22b/24 (no-proof-report), 28 (server restart), 29 (tab backgrounded), 30 (two tabs), 31 (slow network), 36 (deep-link mid-delete), 37 (tab traps), 38 (keyboard-only) |
| **Server-only coverage** (server unit/integration ✓; agent-browser ✗) | **13** | 4 (non-git), 5 (switch project, partial), 10 (invalid input), 11 (resume), 12 (cancel), 13 (retry), 14 (cleanup), 17 (history), 23 (diagnostics), 26 (stale watcher), 27 (watcher start failure), 32 (action errors, partial), 35 (invalid deep link, partial) |
| **Agent-browser only** (no server unit) | **0** | — every agent-browser scenario also has at least partial server coverage |
| **Server + agent-browser (full existing coverage)** | **15** | 1, 6, 7, 8, 9, 15, 16, 18, 19, 20, 21, 22, 24, 25, 41 (resize) |
| **Inherently future-only** (Playwright/live; n/a at server layer) | **3** | 33 (URL push/replace), 34 (deep-link land), 39 (screen-reader — currently has no planned test at all → backlog gap) |

**The "no coverage at all" subset** — flows where neither pytest nor agent-browser exercise anything today, so the implementation backlog must build them from scratch in Phase 3 / 5:

1. **Flow 3** — Launch duplicate project (t03)
2. **Flow 20.b** — Run detail with no proof report (t24)
3. **Flow 26** — Server restart mid-session (t30, W13)
4. **Flow 27** — Tab backgrounded → return (t31, W9)
5. **Flow 28** — Two tabs same project (t32, W10)
6. **Flow 29** — Long-running run + slow network (t33, W9)
7. **Flow 34** — Deep link to deleted run mid-session (t37)
8. **Flow 35** — Tab through UI / no traps (t38, W8)
9. **Flow 36** — Keyboard-only critical flows (t39, t40, W8)
10. **Flow 37** — Screen-reader landmarks — *no planned test in this audit*; flag as separate backlog item
11. **Flow 38** — Reduced-motion preference — *only indirectly checked via tv\* baseline*; flag as separate backlog item

Plus a separately-tracked partial gap:

- **Flow 5 (switch project)**: only "clear" is tested at server layer; switching from project A to project B with state flush has zero coverage.
- **Flow 16 (bulk merge)**: only the dirty-precheck is tested at server layer; success/per-row-failure paths have agent-browser only.

---

## Priority order for closing gaps

CRITICAL — block release if missing, build first:

1. **Flow 6 (build submit happy path)** — already covered, but t06 + W1 must be the first Playwright lights-on check; everything builds on it.
2. **Flow 1 (first-run cold start)** — already covered server-side; t01 + W1 are the user's literal first impression.
3. **Flow 15 (merge happy path)** — financial/data correctness; expand thin server coverage of bulk merge per-row outcome (Flow 16).
4. **Flow 9 (dirty-target submit)** — silent footgun if regressed; t09 + W5.
5. **Flow 12 (cancel running run)** — already very strong server coverage; just needs t12 binding.
6. **Flow 28 (server restart mid-session)** — currently zero coverage; W13 is the only safety net for our most likely production failure mode.
7. **Flow 26 (stale/unverified watcher PID)** — strong server coverage but no UI verification; honesty regression here looks like a hang to users.

IMPORTANT — second wave:

8. Flow 11 (resume), Flow 13 (retry), Flow 14 (cleanup) — destructive-confirm UX bound to server logic; t11/t13/t14.
9. Flow 19 (inspector tab routing) — t20 spans Logs/Diff/Proof/Artifacts; high churn surface.
10. Flow 23 (diagnostics) — operator's only window into correctness; t25.
11. Flow 16 (bulk merge per-row outcome) — only dirty-precheck has server coverage today.
12. Flow 5 (switch project) — partial today; t05 closes it.
13. Flow 27 (tab background) + Flow 30 (slow network) — silent staleness bugs.

NOTE — third wave:

14. Flow 29 (two-tab consistency) — rare but real (operator opens duplicate tab).
15. Flow 35 (tab traps), Flow 36 (keyboard-only) — accessibility blockers per plan policy "always fix accessibility regardless of severity"; treat as IMPORTANT in practice.
16. Flow 37 (screen-reader landmarks), Flow 38 (reduced motion) — currently no planned test; surface in `findings.md` as gap items, decide whether to add or accept.
17. Flow 22b/24 (no proof report graceful) — edge polish.
18. Flow 3 (duplicate project), Flow 33/34 (deep-link variants) — robustness; quick wins once Playwright fixture lands.
19. Flow 41/42 (visual + long strings) — tv01–tv08 + W11 cover at scale; cosmetic.
