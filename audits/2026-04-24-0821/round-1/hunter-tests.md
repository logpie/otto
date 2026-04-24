# Test Bug Hunter

## Fixed / Added Coverage

- `test_requeue_deduplicates_existing_queue_task_id` verifies failed queue retries enqueue under a new id instead of colliding with the permanent original id.
- `test_remove_abandoned_legacy_queue_task_calls_queue_rm` verifies stale legacy queue rows remove through `otto queue rm`.
- `test_web_run_detail_is_not_hidden_by_list_filters` verifies detail lookup is independent of current list filters.
- `test_web_state_marks_abandoned_legacy_queue_runs_stale` verifies stale classification, watcher counts, landing labels, and enabled removal for abandoned legacy queue tasks.
- `test_web_keeps_failed_queue_tasks_inspectable_for_requeue` verifies old failed queue rows remain selectable and expose enabled requeue.

## Reviewed

- Existing monkeypatch-heavy action tests are appropriate because they verify subprocess argv and avoid launching real destructive commands.
- Focused web service tests cover API-level behavior; browser coverage is documented separately in `results.md` because it was run with `agent-browser` against live servers.

## Rejected Candidates

- No tautological assertions or broad `pytest.raises(Exception)` patterns were found in the touched Mission Control tests.
