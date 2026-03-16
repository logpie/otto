# Architect Phase — Design Before Implementation

**Date:** 2026-03-16
**Status:** TODO
**Priority:** High — prevents the most expensive class of bugs (convention drift across tasks)

## Problem

Otto tasks run in isolation. Each testgen and coding agent independently decides on shared conventions: storage formats, CLI output patterns, test helpers, error handling style. When these decisions conflict, downstream tasks waste time reconciling (observed: 10min/$2.30 on a 3-task run where tasks #1 and #2 chose incompatible JSON storage formats).

## Solution

Add an **architect phase** between task breakdown and execution. A design agent examines all tasks together + the existing codebase and produces a living design document that all subsequent agents receive as context.

## Design Doc: `otto_design.md`

Lives in the project root (added to `.git/info/exclude`). Contains:

```markdown
# Otto Design — [project name]

## Conventions (from existing codebase)
- Storage: tasks stored as list of dicts in JSON, keyed by "id"
- CLI: all commands exit 0 on success, print to stdout, errors to stderr
- Testing: pytest, subprocess for CLI tests, tmp_path for isolation

## Shared Interfaces
- `Store.list_tasks() -> list[Task]` — canonical way to read tasks
- `run_taskflow(*args, db_path=None)` — CLI test helper

## Test Helpers (conftest.py additions)
- `run_cli(cmd, db_path)` — subprocess wrapper
- `seed_tasks(db_path, n)` — create n test tasks

## Task-Specific Decisions
- Task #1 (count): output is bare integer on stdout
- Task #2 (clear-done): output is "Removed N tasks"
- Task #3 (summary): output format "Summary: {total} total, {done} done"
```

## Lifecycle

### First `otto add` / `otto add -f`
1. After rubric generation, run architect agent
2. Agent reads: codebase (existing tests, source, CLI help), all task rubrics
3. Agent writes `otto_design.md` with conventions + shared interfaces
4. Agent generates/updates `tests/conftest.py` with shared test helpers

### Subsequent `otto add`
1. Architect agent reads existing `otto_design.md` + new task rubrics
2. Updates design doc with new decisions (append, don't overwrite)
3. Updates conftest.py if new helpers needed

### `otto run`
1. `otto_design.md` injected into every testgen and coding agent prompt
2. Testgen agents import from shared conftest.py instead of inventing helpers
3. Coding agents follow interface contracts from design doc

### After task completion
1. Architect reviews what was actually implemented vs. design
2. Updates design doc with any deviations (reality > plan)

## Implementation

### New file: `otto/architect.py`

```python
async def run_architect(
    tasks: list[dict],
    project_dir: Path,
    existing_design: str | None = None,
) -> str:
    """Run the architect agent. Returns design doc content."""
```

**Agent prompt structure:**
1. Existing codebase context (source stubs, test patterns, CLI help)
2. Existing design doc (if any) — "update, don't rewrite"
3. All task prompts + rubrics
4. Instructions: identify shared interfaces, establish conventions, generate test helpers

**Output:** writes `otto_design.md` and optionally updates `tests/conftest.py`

### Changes to existing code

| File | Change |
|------|--------|
| `otto/architect.py` | New — architect agent |
| `otto/rubric.py` | After `parse_markdown_tasks`, call architect |
| `otto/cli.py` | `otto add` calls architect after rubric generation |
| `otto/runner.py` | `run_task` injects design doc into testgen + coding prompts |
| `otto/testgen.py` | `build_blackbox_context` includes design doc |
| `otto/config.py` | Add `otto_design.md` to git exclude list |

### Prompt injection

Every agent prompt gets a `DESIGN CONVENTIONS` section:
```
DESIGN CONVENTIONS (follow these — do not deviate):
{contents of otto_design.md}
```

For testgen specifically:
```
SHARED TEST HELPERS (import these — do not rewrite):
{contents of tests/conftest.py}
```

## What the architect decides vs. what agents decide

| Decision | Architect | Agent |
|----------|-----------|-------|
| Storage format | ✓ | |
| CLI output format per command | ✓ | |
| Test helper functions | ✓ | |
| Error handling pattern | ✓ | |
| How to implement a specific function | | ✓ |
| Test case selection | | ✓ |
| Bug fix approach | | ✓ |

Rule: if a decision affects multiple tasks, the architect makes it. If it's local to one task, the agent makes it.

## Verification

1. Two independent tasks that share a data store → both use the same format (no reconciliation warnings)
2. Test files import from conftest.py instead of defining their own helpers
3. Adding tasks to an existing run → design doc updated, not rewritten
4. Design doc survives `otto reset` (it's project knowledge, not run state)

## Risks

- Architect agent quality — if it makes bad decisions, all tasks inherit them. Mitigation: design doc is reviewable by user before `otto run`.
- Over-specification — architect constrains agents too much, preventing creative solutions. Mitigation: only specify shared interfaces, not implementation details.
- Stale design doc — after many runs, doc accumulates cruft. Mitigation: architect agent prunes obsolete sections on each update.
