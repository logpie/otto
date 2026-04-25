# Ready Land E2E Debug

## Observations

- Reproduction command:
  `uv run --extra dev python scripts/e2e_web_mission_control.py --scenario all --artifacts /tmp/otto-web-e2e-mission-control-all --viewport 1440x900`
- Failure:
  `ready-land` timed out waiting for `state["landing"]["counts"]["merged"] >= 1`.
- The UI action endpoint returned HTTP 200 for `POST /api/runs/run-saved-views/actions/merge`.
- The harness then polled `/api/state` until its 20 second timeout and never saw the merged count update.
- The scenario failed before taking a screenshot. Only `server.log` remained after the harness cleaned the per-scenario artifact directory.
- The same `ready-land` path had passed in the previous full-suite run before the artifact cleanup change, so the likely boundary is timing or post-action failure visibility rather than static setup.

## Hypotheses

### H1: Merge action can validly take longer than the scenario timeout

- Supports: the endpoint returned 200 and the failure was a timeout, not an immediate 4xx/5xx.
- Conflicts: earlier runs usually completed within the existing timeout.
- Test: rerun `ready-land` alone with `--keep` and inspect whether it passes or eventually merges after 20 seconds.

### H2: Merge action failed asynchronously after a successful launch

- Supports: the UI route returns after launching a process, so a launched action can later fail.
- Conflicts: no failure details were captured because the scenario only waited for `merged`.
- Test: after reproducing with `--keep`, inspect `/api/state` and merge artifacts for failed landing state or error output.

### H3: The confirmation click landed on the wrong control or left the confirm modal open

- Supports: this path uses generic role/name button lookup after clicking the review next action.
- Conflicts: the server log shows a merge action POST, so at least one landing command was launched.
- Test: take an intermediate screenshot before waiting, or assert the selected detail shows an in-progress/merged/failed landing state immediately after the click.

## Experiments

### E1: Rerun `ready-land` alone with kept project

- Command:
  `uv run --extra dev python scripts/e2e_web_mission_control.py --scenario ready-land --artifacts /tmp/otto-web-e2e-ready-land-debug --viewport 1440x900 --keep`
- Result: passed quickly.
- Conclusion: the failed full-suite run did not reproduce as a deterministic merge logic failure. H1 remains plausible as a transient/timing issue, while H2 could not be confirmed from the failed run because the harness did not preserve enough state detail.

### E2: Rerun full model suite after evidence-capture changes

- Command:
  `uv run --extra dev python scripts/e2e_web_mission_control.py --scenario all --artifacts /tmp/otto-web-e2e-mission-control-all --viewport 1440x900`
- Result: passed all 11 scenarios.
- Coverage report: 12/12 states covered, 17/17 actions covered, no model errors.
- Artifact cleanup check: the artifacts directory contained only the current numbered scenario folders.

## Root Cause

The immediate root cause of the debugging gap was weak E2E failure evidence: a landing wait could time out with no landing item summary, state dump, or failure screenshot.

## Fix

- The E2E harness now captures `failure-state.json` and a failure screenshot before closing the browser.
- `wait_for_api_state()` now includes a compact state summary in timeout errors, including landing counts and landing item states.
- The scenario artifact directory is cleaned before each scenario so stale screenshots from older scenario orders cannot mask the current run.
