# Web Mission Control Audit - 2026-04-24

Worktree: `.worktrees/codex-provider-i2p`

## Scope

Audited the web Mission Control task manager as a real user against a temporary git project. The run used the Codex provider from the web queue form, started the watcher from the web portal, monitored build/certify progress, inspected logs and artifacts, and exercised merge actions.

## Real E2E Scenario

Project: `/tmp/otto-web-real-GeYpBd/repo`

Queued from the web portal:

```text
Create hello.py CLI that prints hello web when run. Include pytest test_hello.py covering the output.
```

Submitted options:

```text
provider=codex
effort=medium
fast=true
task_id=hello-web
```

Observed result:

- Web portal queued `hello-web`.
- Web portal started the queue watcher.
- Codex built `hello.py` and `test_hello.py`.
- Codex ran `python hello.py` and `pytest -q`.
- Build committed `c3145a6 Add hello web CLI` on `build/hello-web-2026-04-23`.
- Certifier passed 2/2 stories.
- Web portal showed live and history rows, logs, artifacts, provider/effort, and token usage.
- Web merge action merged into `main` and post-merge certification passed after parser hardening.

Claude smoke:

- Project: `/tmp/otto-claude-smoke-5cAskP`
- Command: `otto build "...hello_claude.py..." --provider claude --effort medium --fast`
- Result: build committed `5e115dd Add hello_claude.py CLI and pytest coverage`; certifier passed 2/2 stories.
- Reported cost: total `$0.30`.

## Bugs Fixed

1. Live search did not filter live queue rows.
   - Root cause: query filtering only applied to history rows.
   - Fix: apply query matching to live `RunRecord` fields and row labels.

2. Completed history detail lost provider and effort.
   - Root cause: history rows did not carry queue argv; provider extraction only looked at explicit metadata.
   - Fix: recover argv from history raw data or queue manifest, then parse `--provider`, `--model`, and `--effort`.

3. Codex usage displayed as `$0.00`.
   - Root cause: Codex reports token usage without provider cost; Mission Control only rendered `cost_usd`.
   - Fix: render Usage as token counts when cost is zero/missing and token usage exists. Queue runner now persists token usage from summaries into queue state/history where available.

4. Artifact content was overwritten by auto-refresh.
   - Root cause: `loadDetail()` re-rendered artifact buttons on each refresh, replacing opened artifact content.
   - Fix: preserve selected artifact content across refreshes and add a `Back to artifacts` control.

5. Web merge action used agent merge in Codex projects.
   - Root cause: Mission Control launched `otto merge ...`; Codex-configured projects require Claude for conflict-resolution merge unless `--fast` is used.
   - Fix: Mission Control merge actions now launch `otto merge --fast --no-certify ...` and `otto merge --fast --no-certify --all`.

6. Immediate merge failures could be hidden behind “launched”.
   - Root cause: action process launch only waited briefly before returning success.
   - Fix: merge actions wait longer for immediate failures and return the failure details.

7. Codex certifier marker parsing rejected descriptive story IDs.
   - Root cause: `STORY_RESULT` parser only accepted non-whitespace story IDs.
   - Fix: parser now accepts story IDs with spaces. This fixed a false post-merge certification failure where the evidence and verdict were PASS.

8. Merge usage was missing from history/live rows.
   - Root cause: merge certifier summaries are attached through extra artifact paths or proof-of-work paths, not always `summary_path`.
   - Fix: usage extraction now scans summary extra artifacts and proof-of-work paths.

9. Successful README-driven projects emitted a noisy `git add intent.md` warning.
   - Root cause: artifact staging tried to add `intent.md` even when it did not exist.
   - Fix: `_commit_artifacts()` only stages existing bookkeeping files.

## Verification

Focused tests:

```text
uv run --extra dev python -m pytest tests/test_mission_control_actions.py::test_merge_selected_and_all_shell_out tests/test_mission_control_adapters.py::test_queue_adapter_includes_queue_manifest_and_merge_action_preview tests/test_web_mission_control.py tests/test_hardening.py::TestMarkerParsingHardening::test_story_result_ids_may_contain_spaces -q
11 passed in 1.61s
```

Full suite:

```text
uv run --extra dev python -m pytest -q
905 passed in 151.43s
```

Agent-browser checks:

- Opened `http://127.0.0.1:8767/`.
- Queued a Codex build job through the New Job modal.
- Started and stopped the watcher through the portal.
- Verified live queue state transitioned queued -> running -> done.
- Verified history rows remained inspectable after live retention.
- Verified search clears nonmatching rows and selection.
- Verified artifact summary content stays open across refresh.
- Verified `Usage` shows token counts instead of misleading `$0.00`.
- Verified merge action can merge into `main`; rerun after marker parser fix produced a successful merge record.

## Remaining Risks

- Existing historical rows created before the parser fix can still show the old false failure; new runs parse correctly.
- Merge duration currently shows `-` in some history rows because merge history does not always persist `duration_s` in the normalized field.
- The web portal still uses simple polling. It is usable now, but high-volume task lists may need pagination or virtualized rows later.
