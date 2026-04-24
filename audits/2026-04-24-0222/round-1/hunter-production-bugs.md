# Round 1 - Production Bugs

Target: `otto/web`, `otto/mission_control`, `otto/merge`.

## Candidates

1. IMPORTANT - fixed - merge run ids can collide inside one Python process.
   - Evidence: `otto/merge/state.py` generated ids as `merge-<seconds>-<pid>`.
   - Impact: two merge runs in the same process and second can write the same
     `otto_logs/merge/<id>/state.json`, hiding earlier merge evidence.
   - Fix: add random suffix to `new_merge_id()` and add a uniqueness regression
     test.

2. IMPORTANT - fixed before final audit - web merge-ready could include already
   merged queue tasks.
   - Evidence: `otto merge --all` previously selected every `done` queue task,
     regardless of prior successful merge state.
   - Impact: Mission Control could show one ready task while the merge log
     claimed multiple branches.
   - Fix: `--all` skips branches already recorded as merged and reports a clear
     no-unmerged-branches message.

3. IMPORTANT - fixed before final audit - `--fast` merge could still certify.
   - Evidence: clean merge path only checked `no_certify`.
   - Impact: web-triggered fast merges could be slow and spawn LLM certifier work.
   - Fix: treat `fast` as certification skip and make Mission Control pass
     `--no-certify` explicitly.

4. IMPORTANT - fixed before final audit - action child processes inherited the
   web server process group.
   - Evidence: `subprocess.Popen` in `otto/mission_control/actions.py` did not
     set `start_new_session=True`.
   - Impact: fallback cancellation could target the wrong process group.
   - Fix: launch action children in their own process group.

5. IMPORTANT - fixed before final audit - native browser confirms blocked
   inspectable automation.
   - Evidence: web merge/stop actions depended on `window.confirm`.
   - Impact: agent-browser E2E could click merge but not observe or reliably
     complete confirmation.
   - Fix: in-app accessible confirmation dialog.

6. IMPORTANT - fixed before final audit - overview active count used live rows
   instead of watcher state.
   - Evidence: queued/done compatibility rows could inflate `Active`.
   - Impact: users saw active work when there were no active children.
   - Fix: compute active count from watcher task state counts.

## Invalid / No Action

- `pass` blocks in merge lock cleanup and story collection are intentional
  best-effort cleanup/optional file parsing paths.
- Protocol `...` methods in Mission Control model are type declarations, not
  shipped placeholders.
- Minified static assets are generated build output and excluded from manual
  source audit.
