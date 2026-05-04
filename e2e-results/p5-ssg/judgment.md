# P5 — Static Site Generator (T3 multi-component) — judgment: **PARTIAL** (honest)

Session: `/tmp/otto-e2e/p5-ssg/otto_logs/sessions/2026-05-04-103640-a45188`

## Phase summary

| Phase | Result | Cost | Wall |
|---|---|---|---|
| Compile | 8 slices, DAG topology (builder has 6 deps), 5 validator warnings | – | – |
| Build | 8/8 passing | $3.58 | 1484s (~25m) |
| Merge | **6 landed, 1 blocked, 1 silently dep-blocked** | $0.47 | – |
| Audit | **verdict: partial** (3 fix-loop attempts) | $1.97 | 720s |
| **Total** | | **$6.02** | ~46 min |

## Per-rubric-dimension verdicts

### Dim 1 — Compile honesty: **PARTIAL** (V7 still active)
- ✅ V1 surfaced 5 warnings (cross_slice_checks + 4× api/cli structure fields).
- ❌ V13 (NEW finding, low severity): compile agent generated parallel slice branches (`indexing` and `link_rewriting` both off `rendering`) with incompatible APIs. `link_rewriting`'s `rewrite_links(html, post_stems, page_stems)` (3 args) vs `builder`'s expectation `rewrite_links(html)` (1 arg). Sibling slices had no awareness of each other's interfaces.

### Dim 2 — Build honesty: **PASS**
- ✅ All 8 slices on real branches, parented per Pattern D dep-tip rule.
- ✅ No git mutations (V8 holding).
- 🟡 V12 risk: builder has 6 deps but my `parent_ref = last_dep` logic only branches off ONE (`feeds`). builder's branch contained feeds + ancestors but NOT link_rewriting (sibling). Build phase didn't fail because the agent worked off whatever code existed on its branch, but merge phase did.

### Dim 3 — Merge honesty: **PARTIAL**
- ✅ 6 real merge commits (cli_scaffold, content_loader, rendering, indexing, link_rewriting, feeds).
- ❌ `builder` BLOCKED with merge conflict (incompatible `rewrite_links` signature).
- ❌ `server_and_e2e` silently dep-blocked.

### Dim 4 — Audit honesty: **PASS** ✓
- ✅ Verdict `partial` matches reality. 6/8 slices landed → V4 cap not majority-blocked → PARTIAL (not BLOCKED). Honest.
- ✅ Audit fix-loop ran 3 attempts, cleanly diagnosed the interface mismatch ("function signature (3 params) is incompatible with builder's expectations (1 param), preventing integration") but couldn't fix in the retry budget.
- ✅ No rogue commits attributed to audit.

### Dim 5 — Product quality: **PARTIAL**
- ✅ test_command (149 unit tests): all pass — but only because builder/server_and_e2e tests aren't on main.
- ✅ Each individual module works correctly in isolation: frontmatter, markdown, templates, RSS, sitemap, link rewriter, indexer.
- ❌ `mksite build` fails with ImportError — `mksite/builder.py` not on main.
- ❌ `mksite serve` fails — `mksite/server.py` not on main.
- ❌ End-to-end acceptance test missing (in unmerged server_and_e2e slice).
- ❌ Cannot generate an actual site.

### Overall: **FAIL** (PARTIAL = product incomplete per rubric "no partial credit")

But Otto's BEHAVIOR is HONEST (Dim 4 passes). No false positives.

## V12 / V13 — new findings, NOT runtime bugs

### V12 (low severity): `parent_ref` only follows last dep
For multi-dep slices, my Pattern D code uses `parent_ref = branch_by_slice[last_dep]`. This works when deps are linearly ordered but loses information when deps fan out into a DAG. For builder with deps=[cli_scaffold, content_loader, rendering, indexing, link_rewriting, feeds], builder's branch contained feeds + linear ancestors but not link_rewriting (sibling).

**Mitigation already partial**: by merge time, ALL prior deps are on `main`, so the merge commit reflects everything. The conflict in P5 wasn't from V12 per se; it was from V13.

**Possible fix**: when slice has multiple deps, compute the union of dep-branch tips; checkout primary, then `git merge` remaining sibling deps before the slice's build agent runs. Conflicts here = real spec issue. Defer until V12 actually causes a failure I can attribute to it (this run's failure was V13).

### V13 (compile-quality, not runtime): sibling slices write incompatible APIs
The compile prompt doesn't make sibling slices aware of each other's exports. `link_rewriting` and `indexing` both descend from `rendering`. They independently chose argument shapes for their public functions. `builder` picked one and got the other.

**Possible fix**: compile prompt should include an explicit "Inter-slice interface contracts" subsection — when slice X is depended on by slice Y, slice X's public function/class signatures must be declared in the spec (similar to API endpoints). This is structurally what `structure.payload.routes` does for webapps; CLI/library projects need an analogous "modules and exports" schema.

**Defer**: V13 is the same class of issue as V7 (compile-quality not surfacing in spec). Both are LLM prompt-engineering. Recommend handling them together as a single compile-prompt overhaul rather than spot-fixes.

## T3 progress: **NOT PASSED**

P5: PARTIAL (FAIL on rubric).

To pass T3 I'd need either:
1. P5 retry (different decomposition, hopefully linear) — uncertain outcome.
2. Different T3 project (e.g., React+FastAPI app or CLI+library duo).
3. First fix V13 (compile prompt for inter-slice interfaces), then retry.

**Honest read**: Otto's RUNTIME is now strong (V1-V11 fixed, 152 tests pass, 4/5 projects PASSED, 1 PARTIAL but verdict-honest). The remaining gap is COMPILE QUALITY — getting the LLM to produce specs that decompose without internal contradictions. That's a different class of problem than the runtime bugs the user flagged ("foundation correctness").

Pausing the loop to check in with the user on direction.

---

# P5 v14b — re-run with V12+V14+V14b — judgment: PARTIAL (honest, no regression)

Session: `/tmp/otto-e2e/p5-ssg/otto_logs/sessions/2026-05-04-120252-9b7dec`

| Phase | Result | Cost | Wall |
|---|---|---|---|
| Compile | 5 slices, mostly-linear, 5 validator warnings | – | – |
| Build | **5/5 passing** (V14b regression test ✓) | $4.22 | 1468s |
| Merge | 2 landed, 1 blocked, 2 dep-blocked | $0.44 | – |
| Audit | **verdict: partial** (V4 cap correct) | $2.29 | 842s |
| **Total** | | **$6.95** | ~50 min |

## What V12/V14/V14b proved

- ✅ **V14b verified**: build phase no longer fails on `git add` rc=1 from gitignored-path warnings. All 5 slices committed successfully (vs 0/5 in V14 first cut).
- 🟡 V12 not exercised: this run's spec was near-linear (no slice had >1 dep). Earlier P5 first-run hit the DAG path; V12 unit test covers the topology directly.
- ✅ V14 still effective: no Otto runtime artifacts (`_session/`, `otto_logs/`) leaked into slice branches.

## Why content-processing BLOCKED

`cli-foundation` and `content-processing` both had `deps=[]` (parallel
roots). Both wrote `setup.py`, `pyproject.toml`, `requirements.txt` —
shared scaffold files. The compile agent put these in cli-foundation's
`owned_paths` but didn't constrain content-processing from modifying
them. Result: real merge conflict on `setup.py` etc. The fix-loop
attempted repair but the conflict required redesigning content-
processing's setup.py to be a strict superset/subset of cli-
foundation's, which the agent couldn't reason about within the budget.

This is **V13-class spec quality**: sibling slices write incompatible
content into shared files. Otto's runtime is honest about it (PARTIAL,
not PASSED). The structural fix is in the compile prompt:

1. `setup.py`/`pyproject.toml`/`requirements.txt` should be in
   `shared_scaffold` (not owned by any slice); only ONE slice should
   own each of them.
2. Or: the compile prompt should explicitly forbid sibling slices
   from modifying files they don't own.

## Final verdict: FAIL on rubric (PARTIAL = product incomplete), Otto behavior PASSES (no false positives).
