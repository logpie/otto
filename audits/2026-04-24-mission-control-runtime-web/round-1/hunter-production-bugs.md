# Production Bug Hunter

## Fixed

- **High: stale watcher PID could still be kill-targeted without verified runtime ownership.**
  - Files: `otto/mission_control/runtime.py`, `otto/mission_control/service.py`, `tests/test_web_mission_control.py`
  - Root cause: Mission Control treated a stale `state.json` watcher PID as a blocking process even when the queue flock was not held. Because runner state does not include PID start-time identity, that PID could be reused by an unrelated process.
  - Fix: `watcher_health()` now exposes a killable `blocking_pid` only for a fresh live watcher or for a verified held `.otto-queue.lock` flock. `stop_watcher()` no longer falls back to the stale watcher PID when health is present but no blocking PID exists.
  - Regression tests: `test_web_does_not_stop_stale_watcher_pid_without_held_lock`, `test_web_can_stop_stale_but_live_watcher_process`, `test_web_ignores_unheld_queue_lock_pid`.

## Validated

- Unheld `.otto-queue.lock` files left behind after normal runner exit no longer mark Mission Control as stale.
- Held queue locks still show stale runtime and can be stopped.
- Ready queue tasks remain inspectable even when only queue-compatible live records exist.
- Review packets expose branch diff, story proof, evidence count, and next action for ready and failed queue tasks.
