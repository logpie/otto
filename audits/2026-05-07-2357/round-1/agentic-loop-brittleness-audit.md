# Agentic Loop Brittleness Audit

Date: 2026-05-07
Repo: `/Users/yuxuan/work/cc-autonomous`
Branch: `main`

## Scope

This pass audited the same brittleness class exposed by the finance true-web
run: over-specified path contracts, false blockers, false greens, missing
feedback to the responsible agent, and weak true-web forensics.

Four read-only agents cross-checked independent surfaces:

- Wegener: path contracts, shared paths, cross-group checks.
- Russell: build/repair/audit feedback loops and provider failure handling.
- Mencius: `scripts/web_as_user.py` true-web evidence and retention.
- Carson: browser runner robustness and test-tier wiring.

## Fixed In This Pass

1. Browser journeys no longer pass when declared evidence is missing.
   A `BrowserJourney` with `evidence_globs` must now produce at least one
   artifact, including paths printed by the command. This removes a false-green
   path where `python -c "print('ok')"` plus invented screenshot globs could
   certify a web behavior check.

2. Cross-group output evidence globs are no longer treated as required input
   files.
   Merge preflight now checks command/path references, not output
   `evidence_globs`. It also ignores whitespace-bearing inline command snippets
   so `python -c` code is not misread as a missing file path.

3. Failed true-web runs keep forensic evidence by default.
   Failed or infra terminal outcomes now keep heavy trace/HAR bundles, preserve
   project snapshots, and include `test-results` in the failed snapshot while
   still excluding `node_modules`, git data, and caches.

4. Build-agent prompts now expose `shared_paths`.
   Runtime scope checks already allowed `shared_paths`, but the prompt only
   listed owned paths, dependency-owned paths, and `shared_scaffold`. The prompt
   now names shared paths as compatible shared write surfaces to avoid
   over-constraining the agent.

5. Provider failure continuity is preserved.
   `default_build_agent` now carries `AgentCallError` session id, cost, and
   crash-artifact path into `BuildAgentOutput`. Retried attempts can resume the
   same provider conversation instead of appearing as disconnected fresh runs.

6. Web tier rebuilds before browser smoke.
   `scripts/test_tiers.py web` now runs `npm run web:build` before using
   `OTTO_BROWSER_SKIP_BUILD=1` for browser smoke, avoiding stale-bundle passes.

## Still Open

1. Malformed required checks still pass as informational evidence in
   `otto/checks.py`.
   This is intentionally documented in the current code, but it is still a
   correctness risk for autonomous testing. The likely root fix is a typed
   required/optional check contract rather than a global flip.

2. Layer 2 repair still needs a stronger evidence loop.
   Provider-reported success should be followed by Otto-owned deterministic
   checks before commit/re-audit. Provider failure with in-scope edits should be
   salvaged into a checkable candidate consistently.

3. Queue cleanup can still be brittle if queue manifest preservation returns
   `None` while session artifacts exist in the worktree.
   The cleanup path should refuse destructive worktree removal or use a
   fallback artifact-preservation path.

4. Browser infra classification is still too mixed with product failure.
   Known browser install, macOS/TCC, bind, sandbox, and launcher failures should
   become structured infra evidence that blocks product repair dispatch.

5. Scenario product verification should become a typed contract.
   Today `needs_product_verification` is mostly enforced by checking an artifact
   directory. Scenarios should return a structured product verification result
   so the harness can prove the generated app was actually inspected.

## Validation

Focused regression suite:

```bash
uv run pytest -q \
  tests/test_checks.py \
  tests/test_audit.py::test_default_walkthrough_picks_cross_slice_journey_first \
  tests/test_audit.py::test_default_walkthrough_falls_back_to_slice_journey \
  tests/test_merge_queue.py::test_run_merge_queue_does_not_treat_evidence_globs_as_missing_inputs \
  tests/test_merge_queue.py::test_run_merge_queue_defers_missing_unowned_cross_group_runner_until_complete \
  tests/test_merge_queue.py::test_run_merge_queue_reselects_cross_group_checks_after_merge \
  tests/test_web_as_user_scaffolding.py \
  tests/test_build.py::test_default_build_agent_passes_resume_session_to_provider \
  tests/test_build.py::test_default_build_agent_preserves_provider_failure_continuity \
  tests/test_build.py::test_build_agent_prompt_writeable_paths_only \
  tests/test_build.py::test_build_agent_prompt_allows_explicit_shared_entrypoint_edits \
  tests/test_build.py::test_build_agent_prompt_lists_shared_paths_as_writable_contracts \
  tests/test_test_tiers.py
```

Result: `120 passed in 6.11s`

Lint:

```bash
uv run ruff check \
  otto/checks.py otto/merge_queue.py otto/build.py \
  scripts/web_as_user.py scripts/test_tiers.py \
  tests/test_checks.py tests/test_audit.py tests/test_merge_queue.py \
  tests/test_web_as_user_scaffolding.py tests/test_build.py tests/test_test_tiers.py
```

Result: `All checks passed!`
