# Web Mission Control Test Model

Mission Control E2E coverage is now model-driven instead of a loose smoke suite. The model lives in `scripts/e2e_web_mission_control.py` as `COVERAGE_MODEL` and maps expected user-visible states and actions to scenario owners. A full `--scenario all` run writes `coverage-model.json` and fails if any model entry references a missing scenario or remains uncovered.

## Covered States

- Project launcher and managed project creation.
- Clean empty project.
- Queued work waiting for a watcher.
- Command backlog while the watcher is stopped.
- Running watcher with cancel and confirm stop paths.
- One ready task.
- Multiple ready tasks.
- Failed task needing recovery.
- Landed task review packet.
- Dirty repository blocking landing.
- Large proof/log/artifact review.
- Filtered board with no matching tasks.

## Covered Actions

- Create a managed project from the launcher.
- Submit build jobs.
- Submit improve jobs with advanced options.
- Submit certify jobs with advanced options.
- Start the watcher from the UI.
- Cancel watcher stop confirmation.
- Confirm watcher stop.
- Land one selected task.
- Cancel bulk landing.
- Confirm bulk landing.
- Open and cancel advanced cleanup.
- Open proof, logs, and artifact drill-downs.
- Open diagnostics.
- Search and clear task filters.
- Run responsive viewport checks.

## Why This Exists

The earlier bug pattern was whack-a-mole: fixing the visible issue from a screenshot did not prove adjacent UI paths still worked. This model forces the test harness to answer three questions before a change is considered done:

1. Which product states must a user be able to understand?
2. Which buttons and flows must be exercised?
3. Which scenarios own those checks?

If a future UI change adds a new major workflow, it should add a `COVERAGE_MODEL` entry and either map it to an existing scenario or add a new scenario.

## Verification Commands

Focused backend regression:

```text
uv run pytest tests/test_web_mission_control.py::test_web_queue_rejects_unknown_after_dependency -q
```

New browser-user scenarios:

```text
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario job-submit-matrix --artifacts /tmp/otto-web-e2e-job-submit-matrix --viewport 1440x900
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario bulk-land --artifacts /tmp/otto-web-e2e-bulk-land --viewport 1440x900
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario watcher-stop-ui --artifacts /tmp/otto-web-e2e-watcher-stop-ui --viewport 1440x900
```

Full release check:

```text
uv run pytest tests/test_web_mission_control.py tests/test_mission_control_model.py tests/test_mission_control_polish.py -q
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario all --artifacts /tmp/otto-web-e2e-mission-control-all --viewport 1440x900
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario control-tour --artifacts /tmp/otto-web-e2e-control-tour-mobile --viewport 390x844
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario long-log-layout --artifacts /tmp/otto-web-e2e-long-log-layout-mobile --viewport 390x844
```

The harness removes old numbered scenario folders before each run. On failure it writes `failure-state.json` and a failure screenshot before closing the browser, so a later investigation can inspect the actual UI and API state instead of relying on a timeout message.

## Bug Found In This Round

Submitting an `improve` job from the web UI with an unknown `After` dependency produced an internal server error. The service now converts queue validation `ValueError`s into user-facing HTTP 400 responses, and the regression test verifies the queue remains unchanged.
