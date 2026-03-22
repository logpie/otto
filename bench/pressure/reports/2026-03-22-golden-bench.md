# Otto Golden Bench Report — 2026-03-22

## Summary

23 ground-truth validated real-repo projects tested with otto.

| Metric | Value |
|---|---|
| **Verify pass rate** | **52% (12/23)** |
| Otto self-reported pass rate | 65% (15/23) — inflated |
| False positive rate | 13% (3/23) |
| Infra failures (spec gen, setup) | 30% (7/23) |
| Total cost | $18.22 |
| Avg cost (projects that ran) | $1.15 |
| Avg time (projects that ran) | 660s (~11 min) |

## Per-Project Results

| Project | Otto | Verify | Cost | Time | Status |
|---------|------|--------|------|------|--------|
| real-box-bugfix | PASS | PASS | $1.49 | 606s | OK |
| real-cachetools-bugfix | PASS | FAIL | $1.05 | 586s | FALSE POSITIVE |
| real-camelcase-number-bug | PASS | PASS | $1.32 | 948s | OK |
| real-camelcase-preserve-uppercase | PASS | PASS | $1.63 | 1198s | OK |
| real-citty-feature | PASS | PASS | $0.79 | 484s | OK |
| real-dotenv-cwd-bug | PASS | FAIL | $2.01 | 522s | FALSE POSITIVE |
| real-humanize-intword | UNKNOWN | FAIL | $0.00 | 0s | SPEC GEN FAIL |
| real-humanize-precisedelta | UNKNOWN | FAIL | $0.00 | 0s | SPEC GEN FAIL |
| real-iniconfig-parse | PASS | PASS | $2.49 | 1145s | OK |
| real-itsdangerous-overflow | PASS | PASS | $0.69 | 498s | OK |
| real-klona-class-bug | PASS | PASS | $0.48 | 910s | OK |
| real-ms-months-feature | SETUP_FAIL | SKIP | $0.00 | 0s | SETUP FAIL |
| real-pathspec-empty | PASS | FAIL | $1.36 | 503s | FALSE POSITIVE |
| real-precommit-debug-bpdb | PASS | PASS | $1.10 | 380s | OK |
| real-precommit-empty-sort | FAIL | FAIL | $0.00 | 870s | FAIL |
| real-radash-feature | UNKNOWN | FAIL | $0.00 | 1s | SPEC GEN FAIL |
| real-semver-bugfix | PASS | PASS | $1.45 | 523s | OK |
| real-string-width-emoji | UNKNOWN | FAIL | $0.00 | 1s | SPEC GEN FAIL |
| real-tinydb-feature | PASS | PASS | $0.70 | 950s | OK |
| real-ufo-filterquery-feature | PASS | PASS | $1.30 | 785s | OK |
| real-ufo-null-prototype | PASS | PASS | $0.36 | 406s | OK |
| real-ufo-parsefilename-bug | UNKNOWN | FAIL | $0.00 | 0s | SPEC GEN FAIL |
| real-yargs-parser-hyphens | UNKNOWN | FAIL | $0.00 | 0s | SPEC GEN FAIL |

## Otto Bugs Found

### BUG: Spec generation silently returns empty (CRITICAL)
- **Affected**: 5 projects (humanize×2, string-width, ufo-parse, yargs-parser, radash)
- **Symptom**: `otto add` runs the spec agent but it returns no output. No error raised. Task never created.
- **Impact**: 22% of projects never even attempt coding
- **Root cause**: Agent SDK `query()` call returns no messages. Happens intermittently — projects early in a batch succeed, later ones fail.

### BUG: False positives — QA passes but verify fails
- **Affected**: 3 projects (cachetools, dotenv, pathspec)
- **Symptom**: Otto's QA agent reports all spec items passing, but independent verify.sh catches real bugs
- **Impact**: 13% false positive rate — otto reports success when code is wrong
- **Root cause**: QA checks are not adversarial enough. For concurrency bugs (cachetools), QA can't spawn threads. For subtle logic bugs (dotenv, pathspec), QA reads the code but doesn't test edge cases.

### BUG: ms-months setup fails (pnpm/husky)
- **Affected**: 1 project (ms-months)
- **Symptom**: SETUP_FAIL — pnpm not found, husky hook fails
- **Impact**: Minor — setup.sh needs fixing
- **Root cause**: `npm install` triggers husky prepare hook which needs pnpm

## Analysis

### What otto is good at (12/23 verified pass)
- Python bug fixes with clear behavioral tests (box, itsdangerous, pathspec concept)
- Feature additions with well-defined APIs (citty aliases, tinydb persist, ufo utilities)
- Small focused changes (klona 2-line fix, ufo-null-prototype)

### What otto struggles with
- **Concurrency bugs** (cachetools stampede) — can't verify threading behavior
- **Projects with complex setup** (ms needs pnpm, humanize needs specific deps)
- **Spec generation reliability** — 22% failure rate on generating specs

### Verify pass rate is the real metric
Otto's self-reported rate (65%) is 13 points higher than the verified rate (52%). The gap = false positives. Without independent verification, we'd think otto is better than it is.
