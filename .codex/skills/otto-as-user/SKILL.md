---
name: otto-as-user
description: "Simulate real users using Otto end to end. Use this for behavioral testing, result auditing, bug hunting, and pressure tests on real-world projects through Mission Control Web first, with CLI as the power-user/agent path. Requires real workflows, success measures, logs, artifacts, browser evidence when relevant, and external verification."
---

# Otto As User

## Purpose

Simulate a real person using Otto to accomplish real software work from end to
end. This skill is for:

- behavioral testing of Otto itself
- auditing whether Otto's output actually satisfies the user's goal
- finding bugs, UX issues, reliability gaps, and design gaps
- pressure testing Otto on increasingly complex real-world projects

The primary entry is **Mission Control Web**. Use CLI as a secondary surface
for power users, agents, scripted runs, direct `otto run` pressure tests, queue
runners, proof inspection, and recovery/debug work.

Do not treat this as a command cookbook. Treat it as a real-user simulation
protocol.

## True User Simulation

When the scenario is Mission Control Web, behave like a real person using the
product. The browser is the interface under test. What the user would see and
do is what the agent should see and do.

Focus true-user Web runs on **Mission Control itself**. Generated-app browsing
is external product verification after Otto finishes; it must not replace the
Mission Control journey. The primary user journey is launcher -> project
workspace -> job dialog -> queue/watch progress -> run detail/logs/diff/proof
-> review/land/retry.

Do not use hidden shortcuts for user actions:

- Do not enqueue, cancel, retry, merge, approve, or inspect work through direct
  API calls when a visible UI path exists.
- Do not seed fake UI state unless the scenario is explicitly a seeded
  regression harness, not a true as-user run.
- Do not bypass the project launcher for first-user or project-selection flows.
- Do not use CLI submission unless the scenario is explicitly CLI/Web interop
  or a power-user path.
- Do not treat API/state checks as proof that the user experience worked. API
  and disk checks are supporting evidence after the UI path is exercised.

Exercise normal user behavior, including behavior during waits:

- click through visible tabs, logs, diffs, proof, artifacts, and history while a
  job is queued or running
- use browser back/forward and verify the UI recovers honestly
- reload during a long wait when a real user might wonder whether progress is
  stale
- scroll long logs and panels
- open and close drawers, dialogs, and detail views
- switch tabs or background the page for long-running work when relevant
- use keyboard navigation and typing for realistic text entry and at least one
  accessibility-oriented path when relevant
- try plausible mistaken or exploratory clicks without breaking the scenario

For every user action, write down the expected visible outcome before judging
the page:

- Action: what the user did, such as click New job, submit intent, open Logs,
  browser Back, refresh, close a drawer, or switch projects.
- User expectation: what a reasonable person expects to see next.
- Observation: what Mission Control actually shows in the browser: URL,
  visible controls, loading/error/success state, selected run, drawer/dialog
  state, and any confusing or missing feedback.
- Verdict: expected, surprising-but-acceptable, confusing UX, blocked, or
  state contradiction.

Do not treat a click as tested just because Playwright did not throw. The test
must inspect the post-action page against the user's expectation. API, disk,
and git checks can confirm durable truth, but they do not replace the visible
expectation check.

Use bounded randomness only for realistic Mission Control exploration:

- keep golden scenarios deterministic unless user-behavior variance is
  explicitly enabled
- randomize within a fixed set of plausible Mission Control actions, not blind
  page clicks
- record the seed, phase, chosen action, user expectation, observation,
  selector, URL, verdict, and screenshot/artifact path so the run can be
  replayed
- treat randomized UX findings as hard failures only when they block, mislead,
  hide state, corrupt workflow state, or contradict API/disk/git truth
- prefer seeded variants such as inspect logs first vs proof first, reload vs
  browser back/forward during a wait, desktop vs mobile viewport, keyboard vs
  pointer submission, and close/reopen drawer

Inspect UI/UX as part of the run, not as a separate screenshot pass:

- first-screen clarity: the next useful action is obvious within a few seconds
- layout: no overlapping text, clipped buttons, unreadable logs, or cramped
  controls at the tested viewport
- interaction feedback: submits, loading states, disabled controls, errors, and
  success states are visible and honest
- navigation model: back/forward, project home, task cards, tabs, drawers, and
  dialogs behave predictably
- responsiveness: the same core workflow is usable on the target desktop and
  mobile/tablet viewports when those are in scope
- recovery: refresh/reconnect/restart states do not strand the user or imply
  false success

## Core Rule

For a real-user scenario, success means:

```text
real user goal -> Otto workflow -> inspect evidence -> external verification -> user-acceptable result
```

An Otto PASS, a harness PASS, or a screenshot alone is not enough.

## Doorway Smoke

Before claiming Mission Control Web is usable for a real user, run a doorway
smoke in the same mode the user will use. This is a skill-level requirement,
not a separate script requirement.

Minimum doorway smoke:

1. Start the real server with launcher mode and remote access:

   ```bash
   uv run --extra dev python -m otto.cli web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher --projects-root /Users/yuxuan/otto-projects --no-open
   ```

2. Open the actual URL in a browser with Playwright or agent-browser.
3. Assert the first screen is actionable:
   - project launcher is visible when no project is selected
   - no raw backend error such as "Failed to load sessions"
   - no unexpected first-load 4xx/5xx in normal UI requests
   - static bundle is fresh and served from the running server
   - the primary user job is obvious within five seconds
   - useful controls appear above the fold on 1280x800 and 1440x900 desktop
   - decorative or explanatory content does not dominate the first viewport
4. Create or open a managed project.
5. Confirm the UI transitions into the project workflow:
   - selected project identity is visible
   - run cards/open actions use the intended drawer or detail surface
   - project-home/brand navigation returns to the launcher in launcher mode
6. Save at least one screenshot, the server log path, and any bounding-box or
   browser evidence used for the first-screen verdict.

Doorway smoke proves the entrance works. It does not prove Otto can build or
repair a real product; continue with a real scenario for that.

If the work under test changed Mission Control UI, also run the
`otto-frontend-rua` product-level gate. Do not accept a component-level check
that only proves elements exist or routes click.

## Surface Selection

Default to Mission Control Web when the task is about how a person would use
Otto:

- create or open a project
- submit build/improve/certify work
- review spec, logs, diffs, proof, and artifacts
- watch queue or run progress
- pause/resume/retry/cancel
- land, reject, or repair work
- capture browser screenshots/video/traces

Use CLI when it is the realistic user path:

- direct i2p pressure: `otto run ...`
- agent/script automation
- provider/model/budget/turn overrides
- queue runner operation
- proof/debug inspection
- recovery after interruption
- reproducing a failure with tighter control

Use both surfaces when testing operator workflows: submit or inspect in Web,
run/repair from CLI, then return to Web to review and land.

## Real-User Loop

1. Choose a real project and user goal.
   - Prefer existing repos, brownfield apps, open-source projects, or realistic
     internal products.
   - Toy projects only count as smoke tests.
   - For pressure tests, escalate complexity after each pass.

2. Define success measures before running Otto.
   - Product behavior: what must work for the end user?
   - Preservation: what existing behavior must not regress?
   - External verifier: native tests, build, lint, API checks, browser checks,
     database/state checks, or manual product assertions.
   - Evidence: which logs, proof packets, screenshots, videos, diffs, or
     artifacts will prove the result?
   - Acceptance: would a real user accept the output?

3. Execute like a user.
   - Start in Mission Control Web unless CLI is explicitly the natural path.
   - Use real provider runs when the user asks for live/paid/pressure evidence.
   - Capture session ids, run ids, wall time, provider/model, and cost when
     available.

4. Inspect evidence before judging.
   - Read `otto_logs/sessions/<id>/summary.json`.
   - Read narrative logs, audit logs, proof packet JSON/HTML, screenshots, and
     videos when present.
   - For Web/queue flows, inspect live records, history, command requests, and
     acks when behavior is unclear.

5. Verify outside Otto.
   - Run the target repo's native verifier.
   - Check durable state for persistence-backed apps.
   - Use browser or HTTP assertions for web behavior.
   - Do not weaken the oracle to make Otto pass.

6. Classify every issue.
   - Otto bug
   - Otto UX issue
   - output/product bug
   - external verifier/oracle bug
   - project-specific invalid request
   - provider/runtime flake
   - design gap
   - beyond current capability

7. Fix only generic root causes when asked to fix bugs.
   - Do not overfit to one test project.
   - Add regression tests for code fixes.
   - Re-run the smallest meaningful gate, then re-run the scenario or a
     narrower reproduction.

## Success Measures

Define these per scenario. A good scenario has at least one measure from each
required category.

Required:

- Workflow success: the real user can complete the intended Otto workflow from
  start to finish.
- Product success: the produced or modified software satisfies the user goal.
- Regression safety: existing project behavior still works.
- External verification: an independent command or check passes outside Otto.
- Evidence completeness: logs, proof artifacts, and UI/browser evidence are
  sufficient to debug or trust the outcome.

Use when applicable:

- UX success: Mission Control is understandable, responsive, and recoverable.
- Persistence success: durable state survives reload/restart.
- Queue success: queued/running/ready/landed states are coherent.
- Recovery success: resume/retry/cancel behaves honestly.
- Provider success: requested provider/model/effort propagated to child agents.
- Cost/time success: budget, max-turns, and reported spend are credible.
- Accessibility success: controls have labels, focus behavior, and usable
  keyboard/mouse paths where relevant.

## Scenario Types

Use scenario types rather than memorized harness names.

- First product from scratch: create a new app from a real product intent.
- Brownfield feature: add behavior to an existing app while preserving tests.
- Bug repair: reproduce a real bug, ask Otto to fix it, verify the fix.
- Spec review: edit or approve a generated spec before work starts.
- Operator queue: submit multiple jobs, run watcher, inspect progress, land
  successful work, handle failures.
- Recovery: interrupt, resume, retry, cancel, or restart Mission Control.
- Proof audit: inspect proof packet, logs, screenshots, and verifier evidence.
- Full-stack/persistence: verify durable state, migrations, API, and UI.
- Open-source repo: run the repo's native test/lint/build commands.
- Beyond-capability probe: large repo, ambiguous requirement, migration,
  concurrency, auth, multi-service setup, or deep data-model change.

## Commands

Run from the active worktree. Start with:

```bash
pwd
git branch --show-current
git status --short --branch
```

### Mission Control Web

```bash
uv run --extra dev python -m otto.cli web --no-open
uv run --extra dev python -m otto.cli web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher --projects-root /Users/yuxuan/otto-projects --no-open
```

`otto dashboard` is a compatibility alias for `otto web`.
`otto queue dashboard` has been removed.

Queue runner:

```bash
uv run --extra dev python -m otto.cli queue run --no-dashboard
```

### Direct i2p CLI

Use for power-user/agent pressure tests:

```bash
uv run --extra dev python -m otto.cli run "build a product..." --project-kind webapp --provider codex --budget 3600 --max-turns 160 --verbose
uv run --extra dev python -m otto.cli run "build a CLI tool..." --project-kind cli --provider codex --effort high --budget 2400
uv run --extra dev python -m otto.cli run --from-spec otto_logs/sessions/<id>/spec/spec.json --provider codex --budget 2400
uv run --extra dev python -m otto.cli run --resume --provider codex --auto-approve
```

Important `otto run` flags:

- `--provider`, `--model`, `--effort`
- `--build-provider`, `--build-model`, `--build-effort`
- `--certifier-provider`, `--certifier-model`, `--certifier-effort`
- `--fix-provider`, `--fix-model`, `--fix-effort`
- `--budget`, `--max-turns`
- `--review-gate`, `--auto-approve`, `--gate-timeout`
- `--resume`, `--reset-budget`, `--force`
- `--break-lock`, `--base-url`, `--from-spec`

`otto build`, `otto improve`, and `otto certify` remain compatibility or
specialized surfaces. Use them when testing those surfaces or when Mission
Control queues them. Prefer `otto run` for direct redesigned i2p pressure.

## Harnesses

Harnesses support the simulation; they do not replace judgment.

| Harness | Use | Counts As |
| --- | --- | --- |
| `scripts/e2e_web_mission_control.py` | cheap browser regression over seeded Mission Control states | UI regression evidence |
| `scripts/web_as_user.py` | paid real-provider Mission Control/operator workflows | live web/operator evidence |
| direct `otto run` on real projects | pressure testing i2p output quality | direct i2p evidence |

Useful commands:

```bash
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario all --artifacts /tmp/otto-web-e2e --viewport 1440x900
uv run --extra dev python scripts/web_as_user.py --list
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --mode quick --provider codex
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --scenario W1 --provider codex --user-behavior mc-realistic --user-seed 42
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --tier nightly --provider codex --scenario-delay 10
```

Current high-signal browser scenarios:

- `project-launcher`
- `fresh-queue`
- `ready-land`
- `dirty-blocked`
- `watcher-stop-ui`
- `job-submit-matrix`
- `bulk-land`
- `long-log-layout`
- `control-tour`

Current high-signal live web-as-user scenarios:

- `W1`: first-time user creates project, submits build, inspects tabs.
- `W7`: mobile/iPhone W1 flow.
- `W11`: operator day, CLI/web interop, queue, watcher, cancel, merge.
- `W4`: merge happy path.
- `W5`: merge blocked with clear reason.
- `W12b`: CLI-queued task through Web start/run/merge.
- `W13`: restart `otto web` mid-build and verify recovery.

## Evidence To Collect

Always capture:

- project path/source
- user goal and scenario type
- surface used: Web, CLI, or mixed
- exact command(s)
- provider/model/effort if visible
- Mission Control user-behavior mode, seed, and action log when enabled
- session id and run id
- wall time and cost when available
- Otto verdict
- external verifier command/result
- proof packet path
- bugs/issues found
- root cause if known
- fix made and regression tests if applicable
- final user verdict

Primary artifact paths:

```text
otto_logs/sessions/<session-id>/summary.json
otto_logs/sessions/<session-id>/spec/spec.json
otto_logs/sessions/<session-id>/spec-state.jsonl
otto_logs/sessions/<session-id>/proof-packet.html
otto_logs/sessions/<session-id>/proof-packet.json
otto_logs/sessions/<session-id>/**/narrative.log
otto_logs/sessions/<session-id>/audit/**/feature-verdicts.json
otto_logs/sessions/<session-id>/audit/**/screenshots/
otto_logs/sessions/<session-id>/audit/**/videos/
otto_logs/cross-sessions/runs/live/<run_id>.json
otto_logs/cross-sessions/history.jsonl
otto_logs/sessions/<run_id>/commands/requests.jsonl
otto_logs/sessions/<run_id>/commands/acks.jsonl
bench-results/web-as-user/<run_id>/<scenario>/mc-user-behavior.jsonl
bench-results/web-as-user/<run_id>/<scenario>/mc-layout-*.json
```

## Bug Hunting Rules

- Logs first, then diagnosis.
- Reproduce or narrow before fixing when behavior is ambiguous.
- Fix generic root causes only.
- Add regression tests for code fixes.
- Do not hide product failures behind "provider flake" without evidence.
- Do not declare beyond-current-capability without failed logs, external
  verifier evidence, and at least one retry or narrower reproduction.

## Report Format

Use this compact report for each scenario:

```text
Scenario:
User goal:
Project:
Surface: Web / CLI / mixed
Provider/model:
MC user behavior:
Session/run ids:
Success measures:
Steps performed:
Evidence inspected:
External verifier:
Bugs/issues:
Fixes/tests:
Verdict: accept / reject / retry / fix / defer / beyond-current-capability
Next escalation:
```

For pressure-test campaigns, append every run to the requested handoff/results
document and keep escalating until the stop condition is evidence-based.
