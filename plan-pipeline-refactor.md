# Pipeline Refactor Plan (v2 — post Codex review)

## Goal
Simplify otto's build pipeline to:
```
Plan (optional) → Build → Certify → Fix → Verify → Done
```

No "inner loop" / "outer loop". One pipeline, targeted fixes.

## Key design decisions (from Codex review)

### Grounding
- **Monolithic**: intent is the grounding for BOTH coding agent and certifier. Write `intent.md` to project root so fix tasks can reference it (not product-spec.md).
- **Planned**: planner writes product-spec.md. Certifier compiles stories from product-spec.md (not raw intent) so both coding agent and certifier use the same reviewed contract.
- Fix prompts reference `intent.md` (monolithic) or `product-spec.md` (planned) — never a nonexistent file.

### Final full certification
- Intermediate rounds: targeted re-verify (only failed stories)
- **Final round before declaring success: full sweep of ALL stories** to catch regressions from fixes

### Async boundary
- `run_certifier_v2()` calls `asyncio.run()` internally — CANNOT be directly awaited
- Keep `loop.run_in_executor()` boundary in verification.py (same as current outer_loop.py)
- Document this invariant

### Artifact persistence for worktrees
- Before any worktree is created: commit `tasks.yaml` + grounding doc (`intent.md` or `product-spec.md`)
- Pipeline explicitly handles this, not hidden in cli.py

### Task isolation
- Each `build_product()` call generates a `build_id`
- Fix tasks tagged with `build_id` — `run_per` only executes matching tasks
- Prevents stale pending tasks from prior runs leaking into new builds

### Flag semantics
- `--plan` forces planner (even for simple intents)
- `--no-plan` skips planner (even for complex intents)
- Default: no planner for monolithic, planner for `execution_mode: planned`
- Planner can still return `single_task` in planned mode — that's fine, it still writes the spec

## Changes

### 1. Rename `outer_loop.py` → `verification.py`
- Function stays `run_product_verification` (good name already)
- Update imports in cli.py, tests
- Drop "outer loop" from runtime logs (keep in archived docs)

### 2. Create `otto/pipeline.py`
```python
@dataclass
class BuildResult:
    passed: bool
    build_id: str
    rounds: int = 1
    total_cost: float = 0.0
    journeys: list = field(default_factory=list)
    error: str = ""

async def build_product(intent: str, project_dir: Path, config: dict) -> BuildResult:
    build_id = f"build-{int(time.time())}-{os.getpid()}"
    
    # Grounding: write intent to project root for fix task reference
    grounding_path = project_dir / "intent.md"
    if not grounding_path.exists():
        grounding_path.write_text(intent)
    
    # Optional planner (parallel mode or --plan flag)
    use_planner = (
        config.get("execution_mode") == "planned"
        or config.get("use_planner", False)
    )
    
    if use_planner:
        plan = await run_product_planner(intent, project_dir, config)
        tasks = [{"prompt": t.prompt, "depends_on": t.depends_on} for t in plan.tasks]
        # Planner writes product-spec.md — certifier uses it too
        certifier_grounding = (plan.product_spec_path or grounding_path).read_text()
    else:
        tasks = [{"prompt": f"Build the product described below.\n\n{intent}"}]
        certifier_grounding = intent
    
    # Persist tasks + grounding before worktree creation
    tasks_path = project_dir / "tasks.yaml"
    add_tasks(tasks_path, tasks, build_id=build_id)
    subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "otto: build artifacts"], cwd=project_dir, capture_output=True)
    
    # Build
    exit_code = await run_per(config, tasks_path, project_dir)
    if exit_code != 0:
        return BuildResult(passed=False, build_id=build_id, error="build failed")
    
    # Verify → Fix → Verify cycle
    result = await run_product_verification(
        product_spec_path=grounding_path,
        project_dir=project_dir,
        tasks_path=tasks_path,
        config=config,
        intent=certifier_grounding,
        build_id=build_id,
    )
    
    return BuildResult(
        passed=result.get("product_passed", False),
        build_id=build_id,
        rounds=result.get("rounds", 1),
        total_cost=result.get("total_cost", 0.0),
        journeys=result.get("journeys", []),
    )
```

### 3. Update `verification.py` (renamed outer_loop.py)
- Final round does full certification (all stories, not just failed)
- Fix prompts reference `intent.md` not `product-spec.md`
- Tasks tagged with build_id

### 4. Simplify `cli.py build()`
- Call `build_product()` — pipeline owns sequencing
- CLI owns display, approval flow, telemetry
- `BuildResult` provides structured data for display

### 5. Terminology cleanup
- Scope: runtime code + user-facing logs + active docs
- NOT archived docs, NOT git history
- No import shim for outer_loop — clean break on this branch

## Files to modify
- NEW: `otto/pipeline.py`
- RENAME: `otto/outer_loop.py` → `otto/verification.py`
- MODIFY: `otto/cli.py` (build command)
- MODIFY: `otto/tasks.py` (build_id support)
- MODIFY: tests referencing outer_loop

## What NOT to change
- `otto/orchestrator.py` (run_per) — works fine
- `otto/runner.py` — untouched
- `otto/qa.py` — untouched
- `otto/certifier/` — untouched (skip_story_ids already done)

## Review criteria
- [ ] Monolithic build does not invoke planner
- [ ] Planned mode certifier uses spec-derived grounding, not raw intent
- [ ] Final success requires full certifier sweep (all stories)
- [ ] Fix prompts reference existing grounding file (intent.md or product-spec.md)
- [ ] Stale pending tasks from prior builds don't leak
- [ ] Async boundary preserved (run_in_executor for certifier)
- [ ] Artifacts committed before worktree creation
- [ ] No "inner loop" / "outer loop" in runtime code or logs
- [ ] All existing tests pass

## Plan Review

### Round 1 — Codex (9 issues)
- [ISSUE] Split grounding in planned mode — fixed: certifier uses product-spec.md in planned mode
- [ISSUE] Fix prompts reference nonexistent product-spec.md — fixed: reference intent.md for monolithic
- [ISSUE] Targeted re-verify can miss regressions — fixed: final round does full sweep
- [ISSUE] Async boundary (nested event loop) — fixed: keep run_in_executor, document invariant
- [ISSUE] Artifact persistence for worktrees — fixed: commit before worktree creation
- [ISSUE] CLI is more than "thin wrapper" — fixed: BuildResult struct, CLI owns display
- [ISSUE] Shared tasks.yaml state bug — fixed: build_id tagging
- [ISSUE] Terminology too broad — fixed: scoped to runtime code + active docs
- [ISSUE] Flag semantics undefined — fixed: explicit precedence documented

### Round 2 — Codex (3 issues)
- [ISSUE] Post-plan spec edits invalidate grounding — fixed: content fingerprint validation
- [ISSUE] architecture.md not persisted — fixed: persist all planner artifacts
- [ISSUE] Coding agent doesn't read intent.md — fixed: intent embedded in task prompt
- [ISSUE] build_id must cover stale recovery — fixed: scope preflight reset by build_id
- [ISSUE] Story IDs unstable across compilations — fixed: compile once per build, reuse
- [ISSUE] --no-plan doesn't normalize execution_mode — fixed: BuildMode requested vs effective

### Round 3 — Codex (2 issues)
- [ISSUE] plan_fingerprint dropped by tasks.yaml writes — fixed: separate sidecar file
- [ISSUE] Fingerprinting raw output is unstable — fixed: hash parsed canonical plan

### Round 4 — Codex (2 issues)
- [ISSUE] Global plan-manifest conflicts with build_id — fixed: build-scoped `otto_logs/builds/{build_id}/`
- [ISSUE] Hash parsed structure not raw text — fixed: deterministic JSON of ProductPlan fields

### Round 5 — Codex
- APPROVED. No new design issues.

## Implementation Gate

### Round 1 — Codex
- [HIGH] Partial build exits 0 with --no-qa — fixed by Codex
- [HIGH] Build not scoped — stale tasks leak — fixed by Codex (clear_pending_tasks)
- [MEDIUM] Fix prompts reference nonexistent product-spec.md — fixed by Codex

### Round 2 — Codex
- [IMPORTANT] clear_pending_tasks destroys user backlog — fixed by Codex (build_id tagging)
- [NOTE] Fix tasks excluded from build accounting — fixed by Codex (recount after verification)

### Round 3 — Codex
- APPROVED. No new issues. 715 tests pass.
