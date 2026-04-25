# Phase 0 — Existing Harness Coverage Migration

**Source harness:** `scripts/e2e_web_mission_control.py` — 1594 lines, 11 scenarios, agent-browser-driven, COVERAGE_MODEL with 12 states + 18 actions, runs outside pytest.
**Target:** Playwright pytest tests (Phase 3.5+ recordings + Phase 3 functional tests + Phase 5 live `web_as_user.py` scenarios).
**Retirement gate:** mark `[deprecated]` in script header only after every COVERAGE_MODEL entry has at least parity in the new suite.

## Existing scenarios → new mapping

| Existing scenario | Existing intent | Maps to (new) | Status |
|---|---|---|---|
| `project-launcher` | Create managed project, target selection | t01–t04 (project lifecycle); R-launcher recording (deferred until launcher tests need it); `/api/projects` excluded from typical recordings | parity planned |
| `fresh-queue` | First build from web before watcher starts | t06 (build submit); R5 partial (post-merge); recorded as part of W1 capture | parity planned |
| `ready-land` | Review and land clean completed task | t15 (single merge); R5 (merged audit branch); inspector diff = t21 | parity planned |
| `dirty-blocked` | Dirty working tree blocks landing | t09 (dirty-target preflight); W5 (live block confirmation) | parity planned |
| `multi-state` | Queued / failed / ready / landed in one board | R9 (mixed-row queue) + diagnostics t25 | parity planned |
| `command-backlog` | Pending command backlog with watcher stopped | R10 / R11 (active + stale watcher) + t29 (start failure) | parity planned |
| `watcher-stop-ui` | Cancel + confirm watcher stop | t26 (start) + t27 (stop) | parity planned |
| `job-submit-matrix` | Improve + certify with advanced queue options | t07 (improve) + t08 (certify) + t09 (dirty preflight) | parity planned |
| `bulk-land` | Land multiple ready tasks through bulk action | t16 (bulk merge-all); W11 includes bulk merge step | parity planned |
| `long-log-layout` | Large log + artifact bounded layout | R7 (large-run recording) + t20 (inspector tabs) + t23 (artifact viewer) | parity planned |
| `control-tour` | Click through main controls, dialogs, inspectors, tabs | covered across t18 (browse) + t19 (filter/no-match) + t34 (tasks↔diagnostics) + tv01–tv05 (visual regression) | parity planned |

## Existing COVERAGE_MODEL states → new mapping

| State id | Existing scenario | Maps to (new) |
|---|---|---|
| `project.launcher` | project-launcher | t01–t04 |
| `project.clean.empty` | fresh-queue | t05 + R8 (minimal/edge) |
| `queue.queued` | fresh-queue | R9 (queued row) |
| `queue.command_backlog` | command-backlog | R10 (with backlog) |
| `watcher.running` | watcher-stop-ui | R10 |
| `task.ready` | ready-land | R5 (pre-merge ready) |
| `task.bulk_ready` | bulk-land | R9 (multiple ready rows in mixed queue) |
| `task.failed` | multi-state | R2 (failed) |
| `task.landed` | multi-state | R5 (post-merge) |
| `repo.dirty_blocked` | dirty-blocked | R8 augmented OR dedicated dirty-state recording |
| `evidence.large_log` | long-log-layout | R7 (large-run) |
| `filters.no_match` | control-tour | t19 (filter no-match) |

## Existing COVERAGE_MODEL actions → new mapping

| Action id | Maps to (new) |
|---|---|
| `project.create` | t01 (cold start) |
| `queue.build.submit` | t06 |
| `queue.improve.submit` | t07 |
| `queue.certify.submit` | t08 |
| `watcher.start` | t26 |
| `watcher.stop.cancel` | t27 (variant: cancel confirm) |
| `watcher.stop.confirm` | t27 (variant: confirm) |
| `run.land.selected` | t15 |
| `run.land.bulk.cancel` | t16 (variant: cancel) |
| `run.land.bulk.confirm` | t16 (variant: confirm) |
| `run.cleanup.cancel` | t14 (variant: cancel) |
| `inspector.diff` | t21 |
| `inspector.proof` | t22 |
| `inspector.logs` | t20 (logs tab) |
| `inspector.artifact.drilldown` | t23 |
| `diagnostics.open` | t25 |
| `filters.search` | t19 |
| `responsive.mobile` | tv01–tv08 (visual at 3 viewports incl. iPhone via webkit), W7 (live mobile) |

## Gaps in BOTH directions

### What the existing harness covers but the new test plan doesn't yet have

- *(none identified)* — every existing state/action maps. New plan is a strict superset.

### What the new plan adds that the existing harness lacks

- Job dialog edge cases (`t09` dirty-target, `t10` invalid input)
- Run lifecycle: cancel/retry/cleanup individual entries (`t12`, `t13`, `t14`)
- Bulk merge-all surfaces (`t16` + W11 closing step)
- Diff truncation/no-changes/error states (`t21`)
- Proof drawers expand/collapse details (`t22`)
- Artifact binary vs text vs missing (`t23`)
- Run detail with no proof report (`t24`)
- Stale/unverified watcher PID surfacing (`t28`, `t29`)
- Server outage mid-session (`t30`, W13)
- Tab background/return (`t31`)
- Two-tab consistency (`t32`)
- Invalid deep link (`t36`, `t37`)
- Keyboard-only flows (`t38`–`t40`, W8)
- Visual regression at 3 viewports (`tv01`–`tv08`)
- Live real-build harness scenarios W1–W13 (operator day W11, CLI/web interop W12a/W12b, outage recovery W13)
- Recorded-from-reality fixtures replacing hand-authored seeds

## Retirement plan

1. Phase 4 closeout: every COVERAGE_MODEL entry has a green Playwright test referenced in this doc.
2. Mark `scripts/e2e_web_mission_control.py` header `# DEPRECATED — see docs/mc-audit/coverage-migration.md`.
3. Move `agent-browser` invocation to opt-in flag (`--legacy-harness`) so it doesn't run by default in any future audit pass.
4. Delete in a follow-up release once Playwright suite has run a full sweep cleanly for ≥2 weeks.
