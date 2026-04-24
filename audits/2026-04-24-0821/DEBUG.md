## Observations

- Reproduced in `/tmp/otto-failure-lab-0821`: clicking `Requeue` for `failed-feature` did not add a visible new queue task.
- CLI parity showed `failed-feature` remained `failed`, with no retry task in `otto queue ls --all`.
- The failed task definition was still present in `.otto-queue.yml`, which is normal because queue ids are permanent.
- `otto/mission_control/actions.py` reconstructed the original queue command with `--as <original-id>` and returned a collision warning when that id already existed.

## Hypotheses

### H1: Requeue collides with permanent task ids (ROOT HYPOTHESIS)
- Supports: failed queue tasks remain in `.otto-queue.yml`; `_reconstruct_queue_command` used `--as task.id`; code returned `None` when `task.id` was in existing ids.
- Conflicts: none.
- Test: change retry reconstruction to use a deduplicated retry id and assert subprocess receives the new `--as` value.

### H2: Web action name mapping sends the wrong action
- Supports: the UI maps action key `R` through `actionName`.
- Conflicts: API route maps both `retry` and `requeue` to `R`, and the confirmation dialog reached the backend path.
- Test: post directly to `/api/runs/<id>/actions/retry` and inspect the returned action message.

### H3: Queue state refresh hides the newly-created retry task
- Supports: detail refresh had already exposed stale-selection issues.
- Conflicts: direct CLI `otto queue ls --all` also showed no new task.
- Test: inspect `.otto-queue.yml` after requeue.

## Experiments

- Confirmed H1 by reading `_execute_retry`: `existing_task_ids` always includes the current failed task, so normal failed queue tasks hit the collision branch.

## Root Cause

Queue requeue tried to enqueue the retry under the original permanent task id, making normal failed queue tasks unretryable from Mission Control.

## Fix

- Requeue now derives a retry task id from the original id, such as `failed-feature-2`, before reconstructing the queue CLI command.
- Added regression coverage that verifies the reconstructed command uses the deduplicated id and reports the new id in the action message.
