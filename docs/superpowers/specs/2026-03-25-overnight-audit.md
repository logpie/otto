# Overnight E2E Audit — 2026-03-25

## Runs completed

| # | Task | Cost | Time | Attempts | Result |
|---|------|------|------|----------|--------|
| 16 | Air pressure display | $0.97 | 4m13s | 1 | Pass |
| 17 | Sunrise/sunset times | $1.36 | 8m53s | 2 | Pass (QA caught missing data bug) |

## Bugs found and fixed during session

| Bug | Severity | Status |
|-----|----------|--------|
| Cost double-counting on session resume | High | Fixed (delta tracking) |
| QA cost always $0.00 | High | Fixed (extract cost after loop) |
| Negative cost when session not resumed | Medium | Fixed (treat negative delta as fresh) |
| Duplicate baseline check (inconsistent results) | Medium | Fixed (removed preflight check) |
| test_command: null overridden by auto-detection | Medium | Fixed (check key presence) |
| Worktree bypass warning for flaky perf tests | Low | Fixed (silent bypass) |
| Agent/Skill/TodoWrite invisible in display | Medium | Fixed (now bold) |
| Diff display broken when change beyond preview | Medium | Fixed (fallback to summary) |
| Spec over-generation (11-14 items) | Medium | Fixed (3-8 items guidance) |
| Superpowers overhead (Skill/TodoWrite) in coding agent | Medium | Fixed (project scope default) |
| Spec agent fallback to ["user","project"] | Low | Fixed (consistent project default) |
| orchestrator: "v4" stale key | Low | Removed |

## Remaining observations

1. **Agent subagent dispatch**: CC's built-in Agent tool fires on every task. Can't disable — CC's autonomous choice. Not harmful but adds overhead.

2. **QA searches for unavailable tools**: `ToolSearch select:TodoWrite` and `ToolSearch select:TaskOutput` appear in QA logs — wasted calls looking for tools not in its session.

3. **Test files still created in __tests__/**: Prompt guidance to use .otto-scratch/ is ignored. Agent follows in-context patterns (sees existing test files, copies them). Test count: 923→933→944→954 over 4 tasks.

4. **Spec items: 4-5 per task now** — down from 11-14. Over-spec fix working well.

5. **Cost accounting now accurate** — $0.97 and $1.36 are realistic for the work done.

6. **Spec gen timing accurate** — 53s, 72s with thread-based parallelism.

## Metrics comparison

| Metric | v4.5 baseline (9 tasks) | Post-refactor (2 tasks) |
|--------|------------------------|------------------------|
| Avg cost | $2.02/task | $1.17/task (42% lower) |
| Avg time | 386s (6.4min) | 394s (6.6min) |
| Avg specs | ~10 items | ~4.5 items |
| Success rate | 100% | 100% |
| Spec gen time | 68s avg | 62s avg |

Cost reduction likely from: fewer specs → shorter QA, project scope → no superpowers overhead.
