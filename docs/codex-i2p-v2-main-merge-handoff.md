# codex-i2p-v2 -> main merge handoff

Date: 2026-05-06

This document is for a future Codex session merging `codex-i2p-v2` into local
`main`. The merge is not a blind "take branch" operation: local `main` has its
own new web-as-user and Mission Control evidence commits. Some fixes overlap
semantically, some touch the same files for different reasons, and the static
web bundle must be regenerated after source conflict resolution.

## Current refs at handoff time

Commands were run from:

```text
/Users/yuxuan/work/cc-autonomous/.worktrees/codex-i2p-v2
```

State after `git fetch origin`:

```text
source branch: codex-i2p-v2
functional source tip before this doc: d5a0ca417de83bdc745973ea0e6d43dec0dcdb1f
local main: 9f46283ef731e9f76fd3b6752a89f3ca90823140
origin/main: 8d248568b1036374bdde77a6fbc453c294856615
merge-base(codex-i2p-v2, local main): cf4654f0ebd351d817d788459c464c4856005750
merge-base(codex-i2p-v2, origin/main): 8d248568b1036374bdde77a6fbc453c294856615
```

Interpretation:

- `origin/main` is already an ancestor of `codex-i2p-v2`.
- local `main` has 6 commits that are not in `codex-i2p-v2`.
- `codex-i2p-v2` has 16 functional commits that are not in local `main`, plus
  this handoff doc commit after it is committed.
- `git cherry -v` reported every local-main-only and branch-only commit as
  unique patch content. There are no clean patch-id duplicates to drop without
  reading.

## What this branch is trying to preserve

The branch is the i2p redesign hardening line after real Mission Control use,
pressure tests, and product-level audits. The user-visible goal is:

- Mission Control Web is the primary way to launch and inspect Otto work.
- The visible UI must show project, spec, group, feature, queue, proof, logs,
  diffs, cost, tokens, and concurrency truthfully.
- The CLI remains available for power users and agents, but real dogfood should
  queue work, watch the queue runner, inspect proof, and land through Mission
  Control.
- Audit and proof should use the target project's runtime, not Otto's own
  virtualenv or incidental local state.
- Context slimming and telemetry must reduce cost/latency without hiding the
  product contract or degrading agent capability.

## Branch-only commits to merge

These 16 commits are in `codex-i2p-v2` but not in local `main`.

| Commit | Motivation and findings | Main areas |
| --- | --- | --- |
| `6fd9d7460` `fix: surface i2p group progress and merge repair` | RunView did not make group progress, merge eligibility, repair evidence, logs, or diffs visible enough for real users. Merge repair also needed better traceability. | `otto/mission_control/run_view.py`, `otto/web/run_view_routes.py`, RunView React components, `otto/merge_queue.py`, `otto/cli_run.py`, tests for CLI run, merge queue, RunView. |
| `995018e5a` `fix: harden mission control product route audit gaps` | Product-level route audit found the app shell, launcher, feature drilldown, spec diff, and workspace paths could be technically clickable while still confusing or visually broken. | `AppShell`, `ProjectLauncher`, `FeatureDrilldown`, `SpecDiffPage`, `ProjectWorkspace`, browser gate. |
| `aafac08cf` `fix: harden mission control product audit gaps` | Follow-up audit found recovery/autopilot copy and spec-review/launcher layout gaps that screenshots alone had missed. | `autopilot.py`, launcher/spec review CSS, browser gate, `tests/test_autopilot.py`. |
| `3d19e95c1` `fix: align mission control with live i2p state` | Live dogfood showed Mission Control defaults, provider/model selection, dirty-state handling, and config serialization were misaligned with real i2p runs. | Otto skills, `checks.py`, `config.py`, serializers/service, `JobDialog`, browser first-run tests. |
| `ce41d7d1c` `fix: isolate target runtime from otto venv` | External checks could accidentally run inside Otto's venv instead of the target project runtime, creating false results. | `otto/checks.py`, `otto/testing.py`, tests in `test_checks.py` and `test_hardening.py`. |
| `8b36eff0a` `fix: discover project venv from queue worktrees` | Queue worktrees need to discover the target project's venv/test command rather than assuming the runner cwd. | `otto/checks.py`, `otto/testing.py`, runtime tests. |
| `1f39410d1` `fix: align i2p prompts and merge base` | Branch/worktree builds needed the right integration base and prompt guidance so slice branches merge into the task branch, not an unrelated default branch. | `otto/build.py`, `otto/merge_queue.py`, `otto/runner.py`, CLI run tests, build and merge queue tests. |
| `168533c99` `fix: detect packaged webapps for i2p audit` | Webapp audit needed to detect packaged app start/build conventions and avoid narrow project-specific assumptions. | `otto/audit.py`, `otto/config.py`, audit/config tests. |
| `4f83a623f` `fix: surface spec logs in run view` | Spec generation logs were not discoverable from RunView. | `otto/web/run_view_routes.py`, route tests. |
| `cfe2a612d` `fix: run webapp walkthroughs in project runtime` | Browser walkthroughs for generated apps needed to run using target project runtime setup. | `otto/audit.py`, audit tests. |
| `afd08a912` `fix: harden audit and queue verdicts` | Audit verdicts and queue task outcomes could be false green or underspecified after failures. | `otto/audit.py`, `otto/queue/runner.py`, `.gitignore` setup, queue/audit tests. |
| `7334f524a` `fix: restore project landing flow` | Landing through Mission Control regressed: ready work and modal/backdrop behavior were confusing or blocked. | `ProjectWorkspace`, `ConfirmDetails`, queue runner, modal browser test. |
| `375638b07` `fix: make i2p run views truthful` | RunView could overstate progress or hide lifecycle nuance. It now derives clearer status from proof/state events. | `run_view.py`, `spec_compile.py`, `spec_state.py`, run view tests. |
| `9403f42ab` `fix: compact i2p context and usage telemetry` | Pressure tests showed hot cost/token paths. Context should pass file paths and compact summaries where possible, while preserving contract accessibility and token accounting. | `audit.py`, `build.py`, `render.py`, `token_usage.py`, lifecycle, tests. |
| `f8ce0404e` `fix: expose i2p telemetry and compact evidence` | UI and proof needed to surface phase telemetry and compact evidence instead of dumping huge logs into prompts. | Otto skills, prompts, `spec_compile.py`, `run_view.py`, StageTimeline/VerdictHeader/types, tests. |
| `d5a0ca417` `fix: expose launch controls and group dispatch` | Mission Control launch still hid provider/budget/max-turns, and concurrency status was not visible. Added visible controls and RunView dispatch state. | `JobDialog`, `api.ts`, `run_view.py`, `run_view_routes.py`, `GroupList`, `VerdictHeader`, browser smoke in `scripts/test_tiers.py web`. |

Validation on `d5a0ca417` before this doc:

```text
uv run ruff check <touched python/tests>
npm run web:typecheck
npm run web:build
uv run pytest -q tests/test_run_view.py tests/test_run_view_routes.py tests/test_web_queue_actions.py tests/test_test_tiers.py
OTTO_BROWSER_SKIP_BUILD=1 uv run pytest -q tests/browser/test_launcher_run_view_gate.py::test_mission_control_product_smoke_launch_and_group_run_view -m browser -p playwright
uv run python scripts/test_tiers.py web
uv run python scripts/test_tiers.py smoke
```

The live server was restarted and verified on port 9000. The project launcher
loaded through `/api/projects`; a stale pre-restart browser tab was the source
of repeated `/api/state` 409s.

## Local-main-only commits to preserve

These 6 commits are in local `main` but not in `codex-i2p-v2`.

| Commit | Motivation and findings | Main areas |
| --- | --- | --- |
| `a4f3fa042` `test: add true Otto web e2e coverage` | Adds true Mission Control web E2E coverage and likely overlaps with branch RunView/queue launch work. | `otto/runner.py`, `otto/cli_run.py`, `run_view.py`, `run_view_routes.py`, `scripts/web_as_user.py`, browser tests. |
| `5a346cb0d` `fix: harden true Otto web repair flow` | Strengthens repair flow behavior for true web runs. Should be integrated with branch merge repair/base-branch fixes, not overwritten. | `otto/runner.py`, `otto/merge_queue.py`, `tests/test_runner.py`. |
| `be8c96f02` `Fix Mission Control live run evidence UX` | Improves live run evidence UX, resource panels, and stage display. This directly overlaps branch RunView group/log/diff work. | `RunDrawer`, `GroupList`, `StageTimeline`, `run_view.py`, `run_view_routes.py`, browser tests. |
| `f56c6c0be` `Harden true web as-user oracles` | Strengthens the scripted true-web as-user oracle. Keep it, then update for any merged UI labels/paths. | `scripts/web_as_user.py`, `tests/test_web_as_user_scaffolding.py`, `.codex/skills/otto-as-user/SKILL.md`. |
| `395bc41c0` `Harden true web E2E evidence gates` | Makes the web-as-user evidence gate stricter. It may need updated expectations after branch RunView/dispatch UI lands. | `scripts/web_as_user.py`, browser gate, scaffolding tests. |
| `9f46283ef` `Update Otto as-user skill false-green rules` | Adds false-green rules to the as-user skill. Combine with the branch's skill updates instead of choosing one side. | `.codex/skills/otto-as-user/SKILL.md`. |

## Predicted merge conflicts

Non-destructive check:

```text
git merge-tree $(git merge-base HEAD main) HEAD main
```

Predicted `changed in both` files:

```text
.codex/skills/otto-as-user/SKILL.md
otto/build.py
otto/cli_run.py
otto/config.py
otto/merge_queue.py
otto/mission_control/run_view.py
otto/mission_control/serializers.py
otto/queue/runner.py
otto/runner.py
otto/spec_compile.py
otto/spec_state.py
otto/web/client/src/components/new-job/JobDialog.tsx
otto/web/client/src/components/run/GroupList.tsx
otto/web/client/src/components/run/RunDrawer.tsx
otto/web/client/src/components/run/StageTimeline.tsx
otto/web/client/src/components/workspace/ProjectWorkspace.tsx
otto/web/client/src/styles.css
otto/web/client/src/types/run.ts
otto/web/run_view_routes.py
otto/web/static/build-stamp.json
otto/web/static/index.html
tests/browser/test_launcher_run_view_gate.py
tests/test_build.py
tests/test_cli_run.py
tests/test_queue_runner.py
tests/test_run_view.py
tests/test_run_view_routes.py
tests/test_web_queue_actions.py
```

The same high-level areas are touched by both branches:

- RunView/resource evidence UX: combine local main's true-web resource panel
  improvements with branch group dispatch, spec logs, truthful status, and
  telemetry fields.
- Runner/repair/merge flow: combine local main's true-web repair path with
  branch integration-base, branch/worktree, failure-summary, and queue verdict
  fixes.
- Job launch: preserve branch's visible provider/budget/max-turns controls and
  local main's direct `otto run`/true-web coverage changes.
- Web-as-user skill and harness: preserve local main's false-green/oracle rules
  and branch's real-user Mission Control simulation protocol. Do not resolve by
  taking either file wholesale.
- Static assets: ignore manual static-asset conflict resolution except to get
  the merge unstuck. After all source conflicts are resolved, run
  `npm run web:build` and commit the regenerated `otto/web/static/` bundle.

## Recommended merge procedure

Use the actual main worktree only when explicitly asked to merge main. Do not
switch this worktree off `codex-i2p-v2` just to perform the merge.

Suggested flow for the merge session:

```bash
cd /Users/yuxuan/work/cc-autonomous
git status --short --branch
git fetch origin
git rev-parse HEAD main origin/main codex-i2p-v2
git merge --no-ff codex-i2p-v2 -m "merge: codex-i2p-v2 i2p redesign hardening"
```

If conflicts occur, resolve semantically. Do not blindly accept "ours" or
"theirs" for the conflict families above.

After resolving source conflicts:

```bash
npm run web:typecheck
npm run web:build
uv run python scripts/test_tiers.py web
uv run python scripts/test_tiers.py smoke
```

If runner/merge/repair conflicts were nontrivial, also run:

```bash
uv run pytest -q tests/test_runner.py tests/test_merge_queue.py tests/test_queue_runner.py tests/test_build.py tests/test_cli_run.py
```

If web-as-user conflicts were nontrivial, also run:

```bash
uv run pytest -q tests/test_web_as_user_scaffolding.py
OTTO_BROWSER_SKIP_BUILD=1 uv run pytest -q tests/browser/test_launcher_run_view_gate.py -m browser -p playwright
```

Before telling the user to test live:

```bash
.venv/bin/python3 -m otto.cli web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher --projects-root /Users/yuxuan/otto-projects --no-open
curl -sS -o /tmp/otto-projects.json -w '%{http_code}\n' http://127.0.0.1:9000/api/projects
```

Then load Mission Control in a real browser and verify:

- initial launcher loads from `/api/projects` with no selected project
- no repeated `/api/state` 409s on a fresh launcher page
- selecting a project returns to the project workspace
- "Build from intent" exposes provider, budget, and max turns
- Codex provider launch posts `--provider codex` plus budget/max-turn flags
- queued/running run detail shows group status, concurrency, logs, diffs,
  telemetry, and proof/evidence surfaces
- browser Back/Forward returns to the expected launcher/workspace/run states

## Merge decision guidance

Keep both branches' intent where possible:

- Prefer branch i2p runtime correctness fixes when they affect real build,
  audit, queue, merge, proof, token, or target-runtime behavior.
- Prefer local main's web-as-user harness/oracle hardening when it makes the
  testing surface stricter.
- For Mission Control UI conflicts, merge the product promise, not just the
  code: the user should be able to launch real work from web, observe what is
  concurrently running, inspect logs/diffs/proof, understand failure/repair
  state, and land/retry without CLI copy-paste.
- For prompt/context conflicts, preserve context slimming only when agents can
  still open the persisted contract/evidence files by path. Do not hide product
  requirements behind summaries only.
- For generated bundles, rebuild after resolving TypeScript/CSS. The final
  bundle hash should correspond to the merged source, not either side's old
  static artifact.

## Known caveats after this branch

- The branch is much better, but not "zero known gaps." Remaining design gaps
  documented in prior handoff/results include broader screenshot/video capture
  coverage for backend-style projects and the need for even better product-level
  large-repo planning contracts.
- The branch added `scripts/test_tiers.py web` browser smoke with
  `OTTO_BROWSER_SKIP_BUILD=1`. This relies on `npm run web:build` already
  having produced a fresh bundle.
- Local main's `scripts/web_as_user.py` is the stronger true-web harness line.
  After merge, it should be the path used for regression pressure tests, but it
  must be updated if merged UI selectors or wording changed.

