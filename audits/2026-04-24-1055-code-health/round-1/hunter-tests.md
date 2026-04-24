# Test Bug Hunter

Scope: changed tests.

## Findings

1. fixed - Web detail regression did not prove filtered detail lookup.
   - Severity: important.
   - Location: `tests/test_web_mission_control.py`.
   - Evidence: the original assertion fetched `/api/runs/failed-task-run`
     without unrelated filters.
   - Fix: fetch `/api/runs/failed-task-run?type=merge&query=unmatched` and
     assert the same failed run remains inspectable with enabled requeue.

2. fixed - Default branch detection lacked slash-branch coverage.
   - Severity: important.
   - Location: `tests/test_config.py`.
   - Evidence: existing tests only covered `main` and no-remote feature branch
     behavior.
   - Fix: add a remote HEAD regression for
     `refs/remotes/origin/fix/codex-provider-i2p`.

3. fixed - Web landing target lacked no-config auto-detection coverage.
   - Severity: important.
   - Location: `tests/test_web_mission_control.py`.
   - Fix: assert Mission Control shows the detected slash branch target when
     `otto.yaml` is absent.

## Rejected Candidates

- The Codex subprocess limit test asserts both the concrete subprocess kwarg
  and the lower-bound value. This is not a tautology: it guards the adapter call
  site that crashed in the real run.
