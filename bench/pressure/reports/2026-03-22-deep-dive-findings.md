# Deep Dive Findings — 2026-03-22

Analysis of golden bench failures and near-misses. Identifies systemic issues in the pressure-test suite that inflate pass rates or mask real weaknesses.

---

## 1. ufo-null-prototype: 10x slowdown reveals need for adaptive complexity

**Observation:** Otto passed real-ufo-null-prototype in 406s at $0.36 — the fastest and cheapest project in the golden bench. The fix is trivially small (change `{}` to `Object.create(null)` in parseQuery). The verify test only checks three surface-level behaviors: no prototype inheritance, basic parsing, and array keys.

**Problem:** This task gives no signal about otto's ability to handle real-world complexity. It is essentially a one-line change with an obvious pattern. The task description even references "issue #282" and says "safer to use with arbitrary keys" — pointing directly at the null-prototype pattern.

**What this means for v4:**
- Tasks need an adaptive complexity spectrum. A mix of trivial (1-line), moderate (10-50 lines across 2-3 files), and hard (architectural changes, concurrency, multi-file refactors) is needed to calibrate otto's capability curve.
- The 10x cost/time difference between trivial tasks (ufo $0.36/406s) and moderate ones (iniconfig $2.49/1145s) suggests the bench is bimodal — it either tests very easy things or very hard things, with little in between.
- For trivially easy tasks, the bench should at minimum verify that otto doesn't over-engineer the solution (e.g., adding unnecessary abstractions around a one-line fix).

---

## 2. cachetools naive fix: hint bias + lenient verify = false positive

**Observation:** real-cachetools-bugfix was a FALSE POSITIVE in the golden bench — otto reported PASS but verify.sh caught a real bug. On deeper inspection, even the verify.sh was too lenient.

**Problem 1 — Hint bias in tasks.txt:** The original task description said "the lock is released before the value is stored in the cache" and "fix the lock/cache interaction so only one thread executes the function while others wait." This practically tells the agent to move the function call inside the lock. That naive fix (hold the lock during compute) passes the stampede test but completely serializes calls to different keys, destroying all concurrency benefit of the cache.

**Problem 2 — Verify too lenient:** The original verify.sh only tested same-key contention (100 threads, 1 key, assert misses=1). It had a `check_distinct_arguments_still_cache_independently` test, but that ran sequentially (not concurrent), so it couldn't detect serialization. A naive "hold lock during compute" fix passes all original verify checks.

**Fixes applied:**
- Rewrote tasks.txt to describe the problem and both requirements (same-key dedup AND different-key parallelism) without hinting at any implementation approach.
- Added `check_different_keys_not_serialized` to verify.sh: 10 threads, 10 different keys, each sleeping 0.1s. If serialized, total >= 1.0s. If parallel, total ~0.1-0.2s. Assert < 0.5s.

**What this means for v4:**
- Every concurrency task needs a "naive fix detector" in verify — a test that fails for the obvious-but-wrong approach.
- Task descriptions should specify behavioral requirements, not implementation hints. The agent should figure out HOW; the task should only say WHAT and WHY.
- False positives are worse than failures — they erode trust in the bench. Any project where otto reports PASS but verify fails should trigger a root-cause analysis of both the verify and the task description.

---

## 3. humanize-intword: spec generation failure

**Observation:** real-humanize-intword was a SPEC GEN FAIL in the golden bench — otto's spec agent returned empty output, so the task was never created and never attempted. This affected 5/23 projects (22%).

**Problem:** The spec generation failure is an otto infrastructure bug (Agent SDK `query()` returning no messages), not a task quality issue. However, the humanize-intword task itself has a subtle challenge: it requires fixing TWO interacting bugs (plural forms and boundary rounding) in a function with nontrivial numeric formatting logic. The verify.sh tests are well-designed — they check singular ("1 million"), plural ("1.2 million"), boundary rounding up (999500 -> "1 million"), boundary staying low (999499 -> "999 thousand"), and large number formatting (googol).

**Spec gen root cause hypotheses:**
- Agent SDK `query()` intermittently returns no messages, possibly related to batch position (projects later in a batch fail more often).
- The humanize project has a complex dependency setup (specific Python package version with known bugs) that may interact poorly with spec generation context.

**What this means for v4:**
- Spec generation reliability is a blocking issue — 22% of projects never even get attempted.
- The fix is in otto core (Agent SDK call handling), not in the bench projects.
- Once spec gen is fixed, humanize-intword should be a good moderate-difficulty task: multi-bug fix with well-specified verify tests.

---

## 4. Key Recommendations for v4

### Bench Design

1. **Adaptive complexity tiers:** Classify each project as TRIVIAL / MODERATE / HARD based on lines changed, files touched, and conceptual difficulty. Ensure the bench has roughly equal representation. Current bench is skewed toward trivial (ufo-null-prototype, klona) and hard (cachetools concurrency, edge-greenfield-complex).

2. **Naive-fix detectors in all verify scripts:** For every project, identify the most obvious wrong fix and add a verify test that rejects it. Examples:
   - Concurrency: test that different-key calls are parallel, not serialized
   - String processing: test with adversarial inputs (unicode, empty, max-length)
   - API design: test that the public interface hasn't changed unnecessarily

3. **No implementation hints in task descriptions:** Task descriptions should describe the PROBLEM (what's broken, what users see) and the REQUIREMENTS (what correct behavior looks like), never the SOLUTION (which lock to hold, which data structure to use). Review all 23 tasks.txt files for hint leakage.

4. **False positive autopsy:** Every false positive (otto PASS, verify FAIL) should trigger:
   - Was verify too lenient? Add a test for the exact wrong behavior otto produced.
   - Was the task description hinting at a naive fix? Rewrite to be behavior-focused.
   - Was the QA agent unable to catch this class of bug? Document the gap.

### Otto Core

5. **Fix spec generation reliability:** The 22% spec-gen failure rate is the single biggest contributor to low pass rates. This is an Agent SDK issue, not a bench issue.

6. **QA needs concurrency testing capability:** The QA agent reads code and runs simple checks. It cannot spawn threads, measure timing, or verify concurrency properties. For concurrency tasks, QA will always be blind — the verify script is the only real check.

7. **Cost/time budget by complexity:** Trivial tasks should have lower budgets (prevent over-engineering). Hard tasks should have higher budgets (prevent premature timeout). Current flat budget wastes money on easy tasks and starves hard ones.
