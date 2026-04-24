# Round 1 - Tests

Target tests:

- `tests/test_web_mission_control.py`
- `tests/test_mission_control_actions.py`
- `tests/test_mission_control_adapters.py`
- `tests/test_mission_control_tui.py`
- `tests/test_merge_orchestrator.py`
- `tests/test_merge_state.py`

## Candidates

1. IMPORTANT - fixed - no regression test covered same-process merge id
   collisions.
   - Evidence: a tight test creating multiple merge states in one process exposed
     state overwrite when ids used only seconds plus PID.
   - Fix: added `test_new_merge_id_is_unique_within_one_process_tick`.

2. IMPORTANT - fixed - no regression test covered `--all` skipping previously
   merged done tasks.
   - Evidence: product E2E showed Mission Control can have a mix of merged and
     ready queue rows.
   - Fix: added `test_merge_all_skips_queue_tasks_already_merged_by_prior_run`.

3. IMPORTANT - fixed before final audit - web/API tests did not cover fast merge
   certifier skip or explicit `--no-certify` action argv.
   - Fix: added focused merge orchestrator and Mission Control action tests.

## Invalid / No Action

- Monkeypatch-heavy subprocess tests are intentional: they verify argv,
  process-group, and immediate failure behavior without launching real LLM work.
- The remaining `assert proc.stdout is not None` is followed by real process
  output reads and exit-code checks; it is not a false-pass assertion.
