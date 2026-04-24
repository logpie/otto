# Code Health Findings Fixed

1. IMPORTANT - merge run ids could collide inside one process.
   - Fixed by adding random suffix entropy to `new_merge_id()`.
   - Added `test_new_merge_id_is_unique_within_one_process_tick`.

2. IMPORTANT - merge-ready web workflow could include previously merged tasks.
   - Fixed by making `--all` skip branches already recorded as merged.
   - Added `test_merge_all_skips_queue_tasks_already_merged_by_prior_run`.

3. IMPORTANT - fast Mission Control merge could run certification.
   - Fixed by treating `--fast` as no-certification in the orchestrator and
     passing `--no-certify` from Mission Control actions.

4. IMPORTANT - action-launched child processes could share the web server
   process group.
   - Fixed by launching action subprocesses with `start_new_session=True`.

5. IMPORTANT - native browser confirmations made web merge automation unreliable.
   - Fixed with an in-app confirmation dialog.

6. IMPORTANT - overview active count could show done queue rows as active work.
   - Fixed by deriving active count from watcher task state.
