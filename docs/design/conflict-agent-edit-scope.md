# Conflict-Agent Edit Scope Policy

Status: implemented

Owner: merge/orchestrator path

Primary files:
- `otto/merge/conflict_agent.py`
- `otto/merge/orchestrator.py`
- `otto/prompts/merger-conflict-agentic.md`

## Summary

The consolidated conflict-resolution agent runs with full file-editing tools, so the orchestrator
now computes a deterministic edit scope before the call and validates against it afterward. The
older marker-only policy that motivated this design was:

- `legacy_marker_scope = set(accumulated_conflict_files)` in
  `otto/merge/orchestrator.py:770-795`
- `out_of_scope = (post_diff_files - pre_diff_files) - legacy_marker_scope` in
  `otto/merge/conflict_agent.py:148-152`

That rule is too strict for realistic semantic merges. In nightly scenario N8 on 2026-04-22, the
agent correctly resolved the visible conflict in `tests/visible/test_payments.py`, but also updated
`tests/visible/test_weekend_billing.py` so that helper imports stayed coherent after
`app/services/billing.py` had been renamed to `app/services/payments.py`. The validator rejected the
result because the second file was outside the literal conflict set, even though the edit was a
direct consequence of the merge.

We should keep a strict validator, but the allowed edit scope must be more principled than
"files-with-markers only". The recommendation in this doc is:

- keep hard validator enforcement
- compute a deterministic pre-agent allowlist
- classify files as `primary` conflict files vs `secondary` coherence files
- allow `secondary` edits only when they are independently derivable from repository structure, not
  from agent self-report
- surface all `secondary` edits in merge logs / merge summary

Concretely, the recommended policy is a bounded hybrid:

- `primary_scope = exact conflict files`
- `secondary_scope = branch-touched files in the same Python package / directory as a primary file`
- `secondary_scope` also includes direct import neighbors of a primary file, including tests that
  import the conflicted product module
- if scope expansion becomes large or ambiguous, fail closed and keep the current manual handoff

This policy fixes N8, keeps the validator meaningful, and avoids trusting the agent to describe its
own permissions honestly.

## Problem Statement

### What the current policy actually enforces

Today the consolidated path is intentionally narrow and fail-closed:

1. The orchestrator merges branches sequentially.
2. On each conflict, it commits marker-laden files so later branch merges can continue.
3. After all branches are attempted, it calls one agent session on the union of unresolved files.
4. When the agent returns, `validate_post_agent(...)` enforces four guarantees.
5. Out-of-scope edits are forbidden.
6. Agent-created untracked files are cleaned or the merge fails.
7. No conflict markers remain.
8. `HEAD` must be unchanged.

The relevant code is explicit:

- `otto/merge/conflict_agent.py:10-15` documents the guarantee as
  `post_diff - pre_diff <= legacy_marker_scope`
- `otto/merge/conflict_agent.py:148-152` implements it with:

```python
post_diff_files = set(git_ops.changed_files(project_dir))
delta = post_diff_files - pre_diff_files
out_of_scope = delta - legacy_marker_scope
if out_of_scope:
    return (False, f"agent edited files outside conflict set: {sorted(out_of_scope)!r}")
```

- `otto/merge/orchestrator.py:770-775` defined that legacy marker scope as:

```python
pre_head = git_ops.head_sha(project_dir)
pre_diff_files = set(git_ops.changed_files(project_dir))
pre_untracked_files = set(git_ops.untracked_files(project_dir))
expected_uu = set(accumulated_conflict_files)
```

So the current edit-scope contract is not "files related to the conflict". It is strictly:

> the agent may only edit files that currently contain unresolved merge markers

The prompt matches the validator. `otto/prompts/merger-conflict-agentic.md:27-31` tells the agent:

1. No conflict markers remain anywhere
2. No files outside the conflict list above were modified
3. HEAD has not changed

### Why this is too strict

A semantic merge often requires one or two follow-on edits outside the marker-bearing file:

- rename propagation
- import coherence
- helper/test fixture updates
- consistency fixes in same-package modules
- small call-site adjustments after keeping both sides of a conflict

Those edits are not unrelated. They are the natural closure of the merge.

The current policy treats all of them as forbidden because it uses marker presence as a proxy for
"allowed to edit". Marker presence is a Git state property, not a semantic dependency boundary.

### N8 as the concrete reproducer

Nightly N8 is exactly the failure mode we care about.

Scenario shape:

- Branch A: rename `app/services/billing.py` to `app/services/payments.py`, update imports
- Branch B: keep working from stale context at the old path and add weekend billing behavior plus a
  new test file `tests/visible/test_weekend_billing.py`
- Branch C: add regression tests against the new `payments.py` location
- Merge order: A merged, B merged, C conflicted in `tests/visible/test_payments.py`

Observed failure, from
`bench-results/as-user-nightly/2026-04-22-232717-fa6bfd/N8/debug.log:192-201`:

```text
✗ build/payments-tests-2026-04-22 (agent_giveup)
  consolidated agent failed on the shared conflict set for 1 files across 1
  conflicted branches: agent finished but validation failed: agent edited files
  outside conflict set: ['tests/visible/test_weekend_billing.py']

Merge incomplete (id: merge-1776902149-86390)
consolidated agent-mode resolver gave up: agent finished but validation failed:
agent edited files outside conflict set:
['tests/visible/test_weekend_billing.py']
Files with markers: ['tests/visible/test_payments.py']
```

This is the key point:

- the only file with markers was `tests/visible/test_payments.py`
- the agent also touched `tests/visible/test_weekend_billing.py`
- the extra edit was semantically justified, because Branch A changed the canonical import path and
  Branch B's newly-added test had to stay coherent with that rename
- the validator killed a semantically correct merge because the allowlist was too literal

N8 is not an edge case. Rename-plus-stale-context is normal in any queue that merges parallel work.

## Why We Cannot Remove The Validator

The wrong conclusion from N8 would be "just let the agent edit whatever it wants". That would be
worse than the current failure.

Without a validator, the conflict agent can hide unrelated work under the cover of conflict
resolution:

- refactor modules unrelated to the conflict
- opportunistically "fix" nearby bugs
- rewrite tests to match a broken implementation
- add or remove behavior not present in any input branch
- perform hygiene changes that make review and blame harder
- accidentally fan out into a large edit set after one search/replace

The consolidated path is the live merge path. Silent false negatives are much more dangerous than
loud false positives:

- false positive: good merge is rejected, user resolves manually
- false negative: bad merge lands on `main`

The current validator exists for a good reason. The bug is not that validation exists. The bug is
that the scope model is too naive.

The design target is therefore:

- preserve independent, orchestrator-side enforcement
- broaden the scope just enough to admit coherent semantic follow-on edits
- keep the policy bounded, explainable, and auditable

## Goals

- Fix N8-class failures where a conflict resolution requires a small, semantically-adjacent edit
  outside the literal conflict file.
- Keep the validator fail-closed and independent of agent honesty.
- Keep the policy simple enough to explain in the prompt and test in unit tests.
- Surface all non-primary edits clearly so a human can audit them if a merge later looks suspect.

## Non-Goals

- Allow arbitrary repo-wide cleanup during conflict resolution.
- Turn the merge agent into a general repair/refactor pass.
- Depend on the agent to self-report its scope honestly.
- Solve every possible dynamic dependency pattern on day one.

## Current Data Flow And Change Seams

The main seams are already clean:

### Conflict set creation

`git_ops.conflicted_files(...)` in `otto/merge/git_ops.py:86-103` parses `git status --porcelain`
and returns paths in unmerged states (`UU`, `AA`, `DD`, `AU`, `UA`, `DU`, `UD`).

The orchestrator accumulates those files at `otto/merge/orchestrator.py:663-739`:

```python
accumulated_conflict_files.extend(conflicts)
...
accumulated_conflict_files = sorted(set(accumulated_conflict_files))
```

### Agent invocation

The consolidated context currently includes:

- target branch
- all branches
- all branch intents
- all stories
- `conflict_files`
- `conflict_diff`
- `test_command`

See `ConsolidatedConflictContext` in `otto/merge/conflict_agent.py:43-58` and construction in
`otto/merge/orchestrator.py:787-795`.

### Post-agent validation

`validate_post_agent(...)` in `otto/merge/conflict_agent.py:128-172` is where scope enforcement
lives today.

### Staging after success

The success path stages only the literal conflict files:

```python
add_r = git_ops.add_paths(project_dir, accumulated_conflict_files)
```

See `otto/merge/orchestrator.py:836-861`.

This means any scope expansion must update two places:

1. validation allowlist
2. stage/commit path

Changing only the validator would create a new bug where allowed secondary edits remain unstaged and
silently disappear from the resolution commit.

## Candidate Policies

This section evaluates three policy families. All of them assume we keep independent validator
enforcement.

### Candidate A: Conflict Set + Direct Import Closure

Policy:

- primary scope is the literal conflict set
- add a deterministic, repo-local import closure around each conflict file
- bound the depth to 1 hop initially
- for Python, include files directly imported by a conflicted file
- for Python, include files that directly import a conflicted file
- for Python, include tests that directly import the conflicted product module
- make rename handling alias-aware when possible, so a rename from `billing.py` to `payments.py`
  treats both import paths as candidates during scope expansion

What it permits:

- rename propagation into nearby callers and tests
- same-module helper fixes
- test import updates caused by a module move

What it prevents:

- arbitrary edits outside the static dependency neighborhood
- broad package refactors that are not directly connected to a conflict file

Why it helps N8:

- if `test_weekend_billing.py` directly imports the renamed billing/payments helper, it enters the
  secondary scope and the merge succeeds

Attack vectors / failure modes:

- central modules can have huge inbound fan-in, making the scope too broad
- Python static import resolution is imperfect:
  - dynamic imports are missed
  - re-export chains are tricky
  - relative import resolution across packages needs care
- rename-aware import matching adds implementation complexity
- for test-only conflicts like N8, a same-package test file might be semantically adjacent even if
  there is no direct import edge between the two test files

Prompt cost:

- moderate
- the prompt must explain `primary` vs `secondary` files and say that secondary edits are allowed
  only when required to preserve coherence

Assessment:

- good signal in product-code-heavy repos
- not sufficient as the only rule for N8-like test clusters unless import analysis is strong

### Candidate B: Conflict Set + Same-Package / Same-Directory, With Test Carve-Out

Policy:

- primary scope is the literal conflict set
- add files in the same Python package or same directory as a primary file
- if a conflict is in product code, allow test files that target that package/module
- if a conflict is already under `tests/**`, allow same-package tests in that test directory

What it permits:

- nearby helper and fixture updates
- grouped test modules that are intentionally split across files
- stale test modules that need the same import/path migration as the conflicted test

What it prevents:

- unrelated edits outside the local package/directory
- repo-wide cleanup or feature work

Why it helps N8:

- `tests/visible/test_payments.py` and `tests/visible/test_weekend_billing.py` are in the same
  test package/directory, so the secondary edit becomes allowed

Attack vectors / failure modes:

- in a large package, "same package" can still be quite broad
- package co-location is only a structural approximation; some files are near but unrelated
- if the package is something like `tests/visible/` with many files, the agent gets more room than
  necessary

Prompt cost:

- low
- the prompt can list primary files and secondary same-package files directly

Assessment:

- simplest rule
- robust for N8
- too coarse as a standalone policy in large packages

### Candidate C: Two-Pass Scope Expansion Request

Policy:

1. Pass 1: the agent inspects the conflict and proposes additional files it needs to touch, with a
   rationale.
2. The orchestrator independently decides whether each requested file is admissible using structural
   rules such as same-package, import edges, branch-touch metadata, rename evidence, or test
   adjacency.
3. Pass 2: the agent performs the edit using the approved expanded scope.

What it permits:

- precise one-off expansions when deterministic precomputation is too narrow
- explicit rationale for why a non-conflict file had to change

What it prevents:

- silent, unreviewed scope expansion
- agent broadening scope without an observable checkpoint

Why it helps N8:

- the agent could request `tests/visible/test_weekend_billing.py`; the orchestrator could approve it
  because it is a same-package test file implicated by the merge

Attack vectors / failure modes:

- more moving parts in the live merge path
- more latency and token cost
- more prompt complexity
- if the approval heuristics are weak, the system becomes "agent asks, orchestrator rubber-stamps"
- if the agent forgets to ask for a needed file, we still get false positives

Prompt cost:

- high
- the prompt must split reasoning and execution into explicit passes

Assessment:

- safest in theory
- probably too heavy for the common path
- better as a later escalation mechanism than as the default path

## Diff Budget As A Supplement, Not A Primary Policy

A line-count budget is worth discussing because it is simple, but it is not a good primary gate.

Example:

- allow at most `N` changed lines outside the conflict set
- log them as secondary edits

Why this is weak on its own:

- line count is not a semantic boundary
- an agent can still make a dangerous small change
- a legitimate rename-propagation edit can exceed a tiny budget
- a generous budget quietly reintroduces "edit whatever nearby things you want"

Recommendation:

- do not use diff budget as the main allowlist
- optionally use it as telemetry or as an extra fail-closed threshold on secondary edits after a
  deterministic scope is computed

## Recommendation

Recommend a deterministic hybrid:

1. Keep the hard validator.
2. Replace the marker-only allowlist with a richer `allowed_edit_scope`.
3. Compute `allowed_edit_scope` before the agent runs, entirely on the orchestrator side.
4. Split scope into `primary_files` and `secondary_files`.
5. `primary_files` are the exact files with markers.
6. `secondary_files` are bounded coherence files derived from repo structure.
7. Log any actual edits to `secondary_files` as `secondary edits` in the merge summary.
8. Fail closed if scope expansion becomes too broad or ambiguous.

### Recommended scope rule

Let:

- `conflict_files = files with unresolved markers`
- `branch_touch_union = union of files touched by branches participating in this merge`

Then compute:

- `primary_files = conflict_files`
- `secondary_files` come from `branch_touch_union`
- include files in the same Python package or same directory as a primary file
- include direct import neighbors of a primary file
- include test files that directly import a conflicted product module
- include test files in the same test package as a conflicted test file

Additional safeguards:

- bound import traversal to depth 1
- only include repo-local files
- if `secondary_files` exceeds a conservative threshold, fail closed and require manual resolution
- always report which secondary files were actually edited

### Why this recommendation

It is the best trade-off for the live merge path:

- safer than same-package alone, because it requires either adjacency or branch implication
- simpler than two-pass orchestration
- independent of agent honesty
- directly fixes N8
- general enough to handle the common rename/import/test coherence cases that arise in parallel
  queue merges

### Why `branch_touch_union` matters

Using same-package or import closure alone can authorize too much in a large package. Intersecting
with "files already touched by one of the merged branches" keeps the envelope closer to the merge's
actual blast radius.

For N8 this is exactly right:

- `test_weekend_billing.py` was introduced by Branch B, so it is in `branch_touch_union`
- it is also in the same test package as the conflicted `test_payments.py`
- therefore it qualifies as a secondary coherence file

An unrelated file elsewhere in the package that none of the merged branches touched would stay out
of scope.

### Why not recommend two-pass first

Two-pass is defensible but too expensive for the default path:

- more latency in a path that already pays for a live LLM call
- more complexity in the prompt and orchestrator state machine
- still needs deterministic approval logic underneath

If the deterministic hybrid later proves too narrow, two-pass can be added as an escalation path for
"scope expansion needed but not derivable".

## Proposed Interface And Implementation Sketch

This section is intentionally high-level. It describes the seams and the shape of the change, not
the final code.

### 1. Introduce an explicit edit-scope object

The legacy code passed a marker-only file set.

Proposed replacement:

```python
@dataclass
class ConflictEditScope:
    primary_files: set[str]
    secondary_files: set[str]

    @property
    def allowed_files(self) -> set[str]:
        return self.primary_files | self.secondary_files
```

Optional richer result from validation:

```python
@dataclass
class PostAgentValidationResult:
    ok: bool
    error: str | None
    edited_files: set[str]
    edited_primary_files: set[str]
    edited_secondary_files: set[str]
```

This avoids overloading a single marker-only parameter with multiple meanings.

### 2. Compute scope in the orchestrator before agent invocation

Current shape in `otto/merge/orchestrator.py:770-803`:

```python
pre_head = git_ops.head_sha(project_dir)
pre_diff_files = set(git_ops.changed_files(project_dir))
pre_untracked_files = set(git_ops.untracked_files(project_dir))
expected_uu = set(accumulated_conflict_files)

ctx = ConsolidatedConflictContext(...)
attempt = await resolve_all_conflicts(
    ...,
    edit_scope=scope,
    ...
)
```

Proposed shape:

```python
pre_head = git_ops.head_sha(project_dir)
pre_diff_files = set(git_ops.changed_files(project_dir))
pre_untracked_files = set(git_ops.untracked_files(project_dir))

scope = build_conflict_edit_scope(
    project_dir=project_dir,
    target=options.target,
    branches=branches,
    conflict_files=set(accumulated_conflict_files),
)

ctx = ConsolidatedConflictContext(
    ...,
    conflict_files=accumulated_conflict_files,
    allowed_secondary_files=sorted(scope.secondary_files),
)

attempt = await resolve_all_conflicts(
    ...,
    edit_scope=scope,
    ...
)
```

Notes:

- `build_conflict_edit_scope(...)` should be deterministic and side-effect free
- it should fail closed on ambiguity or large scope
- its inputs should come from Git and the on-disk repo, not from the agent

### 3. Update the validator to use allowed scope, not just conflict files

Current validator core in `otto/merge/conflict_agent.py:148-152`:

```python
post_diff_files = set(git_ops.changed_files(project_dir))
delta = post_diff_files - pre_diff_files
out_of_scope = delta - edit_scope.allowed_files
```

Proposed validator core:

```python
post_diff_files = set(git_ops.changed_files(project_dir))
delta = post_diff_files - pre_diff_files
out_of_scope = delta - edit_scope.allowed_files
edited_secondary = delta & edit_scope.secondary_files

if out_of_scope:
    fail(...)

return PostAgentValidationResult(
    ok=True,
    error=None,
    edited_files=delta,
    edited_primary_files=delta & edit_scope.primary_files,
    edited_secondary_files=edited_secondary,
)
```

The rest of the existing validation stays:

- clean agent-created untracked files
- scan for markers
- `git diff --check`
- confirm `HEAD` unchanged

### 4. Stage actual edited files, not just literal conflict files

Current success path in `otto/merge/orchestrator.py:836-861`:

```python
add_r = git_ops.add_paths(project_dir, accumulated_conflict_files)
commit_msg = f"resolve {len(accumulated_conflict_files)} files across {len(branches)} branches"
```

Proposed shape:

```python
paths_to_stage = sorted(validation.edited_files)
add_r = git_ops.add_paths(project_dir, paths_to_stage)
```

The commit message can stay mostly the same, but logs / state notes should include something like:

- `secondary edits: tests/visible/test_weekend_billing.py`

Important:

- stage what was actually edited and allowed
- do not stage every file in the allowed secondary scope

### 5. Extend the prompt to describe `primary` vs `secondary`

Current prompt language in `otto/prompts/merger-conflict-agentic.md` says:

- no files outside the conflict list above were modified

That becomes false under the new policy and must change.

Proposed prompt structure:

- list `Files needing resolution` as the current conflict files
- add `Additional files you MAY edit if needed for coherence`
- state the rule explicitly:
  - prefer editing conflict files
  - edit secondary files only when required to keep imports/tests/helpers coherent with the merged
    behavior
  - edits outside both lists will fail validation

This keeps the prompt aligned with the orchestrator's actual contract.

### 6. Surface secondary edits in logs and state

Today a successful resolution note looks like:

- `resolved by consolidated agent in one shared call ...`

Under the new policy, the success note and/or logs should additionally record:

- whether any secondary files were edited
- which ones
- possibly the count of secondary lines changed

This is important for later auditing. If a merge later looks suspicious, the first question will be
"did the conflict agent touch anything besides the marker files?"

## High-Level Helper Design

No final API is required yet, but the change likely wants a helper layer with logic like:

- `collect_branch_touch_union(project_dir, target, branches) -> set[str]`
- `find_python_import_neighbors(project_dir, files) -> set[str]`
- `build_conflict_edit_scope(project_dir, target, branches, conflict_files) -> ConflictEditScope`

Potential file placement:

- keep scope construction near `otto/merge/conflict_agent.py` if it is tightly tied to validation
- or move it to a small `otto/merge/conflict_scope.py` helper if the logic grows

I would favor a separate helper module if import analysis and rename handling become more than a few
dozen lines. The orchestrator already has enough responsibility.

## Testing Strategy

The repo already has targeted tests around this seam:

- `tests/test_merge_conflict_agent.py`
- `tests/test_merge_orchestrator.py`

The new policy should expand those tests rather than invent a separate harness.

### Unit tests: scope construction

Add unit tests for the deterministic scope builder:

1. Same-package test expansion
   - conflict in `tests/visible/test_payments.py`
   - `tests/visible/test_weekend_billing.py` is in `branch_touch_union`
   - expect it in `secondary_files`

2. Direct import neighbor expansion
   - conflict in `app/services/payments.py`
   - `tests/visible/test_payments.py` imports it
   - expect the test in `secondary_files`

3. Branch-touch bound
   - same-package file exists but was not touched by any merged branch
   - expect it not to enter `secondary_files`

4. Fail-closed broad expansion
   - synthetic case where same-package/import closure would yield too many files
   - expect builder to reject or clamp in a way that causes manual handoff

5. Rename-aware test import matching
   - if rename support is included in v1, verify old-path and new-path imports both match the same
     logical module for scope expansion

### Unit tests: validator behavior

Extend `tests/test_merge_conflict_agent.py` with:

1. Allowed secondary edit passes
   - pre-state clean
   - agent edits one primary and one secondary file
   - validation succeeds
   - returned `edited_secondary_files` includes the secondary file

2. Out-of-scope file still fails
   - same setup, but edit a file outside `allowed_files`
   - validation fails with the existing "outside conflict/edit scope" style message

3. Marker scan remains primary-file-focused
   - ensure the new scope model does not accidentally stop checking marker-bearing files

4. Untracked cleanup still works
   - existing tests should continue to pass unchanged

### Orchestrator tests

Extend `tests/test_merge_orchestrator.py` with:

1. Successful secondary edit is staged and committed
   - fake resolver edits a primary file plus one allowed secondary file
   - assert final commit contains both

2. Secondary edit is reported
   - state or note includes the `secondary edits` summary

3. Disallowed secondary edit still causes `agent_giveup`
   - fake resolver edits a file outside the computed scope
   - assert the merge fails loudly

### Integration tests

Add at least one non-LLM integration test that recreates the N8 structure in miniature:

- branch A renames module path
- branch B adds stale-context logic and a new neighboring test
- branch C conflicts in a test against the renamed location
- fake resolver edits both the conflict file and the secondary neighboring test
- merge should succeed under the new scope policy

This test should live in the normal pytest suite so the behavior is covered without paying for an
LLM run.

### Real-LLM validation

Reuse nightly N8 as the live reproducer.

Success criteria:

- the agent is still rejected if it edits unrelated files
- the 2026-04-22 N8 failure mode no longer gives up merely because
  `tests/visible/test_weekend_billing.py` changed
- visible and hidden tests pass after merge

N8 should remain in the nightly suite permanently because it protects exactly this rename-plus-stale
context class.

## Migration And Rollout

### Does this need a feature flag?

Recommend a short-lived rollout flag or shadow mode, even though the final policy should become the
default.

Reason:

- this is the live merge path
- scope expansion mistakes can silently widen agent authority
- we want one cycle of evidence before making it the only behavior

### Suggested rollout

Stage 1: behavior off, telemetry on

- compute the expanded scope
- log what `secondary_files` would have been
- keep the current validator for one short soak if desired
- optionally record whether a failed merge like N8 would have passed

Stage 2: behavior on behind config

- enable expanded validation/staging under a config flag
- run nightly N8 and a few merge-heavy fixtures

Stage 3: flip default, keep escape hatch briefly

- make expanded scope the default
- keep a fallback toggle for one release in case a repo shape exposes an unexpected broadening bug

If we want fewer steps, this can still land in one PR as long as the PR includes:

- deterministic scope builder
- validator/staging changes
- unit and integration tests
- nightly N8 confirmation before merge

My bias is:

- one implementation PR
- short-lived config flag
- remove the flag after the nightly signal is clean

### What to measure

We should measure both safety and usefulness.

Usefulness:

- number of merges that previously failed with `edited files outside conflict set`
- of those, how many now succeed
- nightly N8 pass rate

Safety:

- number of successful merges with `secondary edits`
- distribution of secondary file counts
- distribution of secondary line counts
- any post-merge certification failures on merges that used secondary scope

If we see many merges with large secondary scope usage, that is a sign the heuristic is too broad or
that the agent is leaning on the extra room too often.

## Open Questions

1. Should `branch_touch_union` be based on each branch's full diff against target, or only on files
   changed since merge-base? The latter is usually the right blast radius.
2. Do we want rename-aware import matching in v1, or is same-package plus direct imports enough to
   fix the first wave of failures?
3. Should the fail-closed threshold be file-count based, line-count based, or both?
4. Should secondary-edit summaries be persisted into `state.json`, or just logged?

None of these block the basic policy change. They affect how polished v1 is.

## Final Recommendation

Implement a deterministic expanded edit scope:

- primary files are the literal conflict files
- secondary files are branch-touched same-package/import-adjacent coherence files
- validator still enforces the scope independently
- orchestrator stages actual edited secondary files and logs them explicitly

One-line recommendation:

> Keep the validator, but replace "marker files only" with a deterministic,
> fail-closed allowlist of conflict files plus bounded branch-touched coherence files.
