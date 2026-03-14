# Otto Rubric Feature — Design Spec

## Problem

Otto's verification requires either pre-existing tests or produces auto-generated tests that are opaque, often broken, and untargeted. Users can describe *what to build* in natural language but not *what to verify*. There's no way to express acceptance criteria before the agent runs.

## Solution

Add **rubrics** — natural language acceptance criteria that get converted to real, runnable test code. Users write what matters in plain language; Otto generates deterministic tests from those criteria.

## User Flow

### 1. Write tasks in markdown

```markdown
# Search
Users should be able to search bookmarks by title or URL.
Search should be case-insensitive and support partial matches.
Searching for something that doesn't exist should return an empty list.

# Tags
Each bookmark can have multiple tags. I want to:
- filter bookmarks by tag
- add tags when creating a bookmark (--tag flag, repeatable)
- list all tags with bookmark counts
```

### 2. Import

```bash
otto add -f features.md
```

Output:
```
Parsing features.md...
  Task #1: Add search method to BookmarkStore
    Rubric:
      1. search('python') matches bookmarks with python in title or url
      2. search is case-insensitive
      3. partial matches work — 'exam' matches 'example.com'
      4. no matches returns empty list
      5. search on empty store returns empty list
  Task #2: Add tags support to bookmark system
    Rubric:
      1. Bookmark dataclass has tags field (list[str], default empty)
      2. by_tag('dev') returns all bookmarks tagged 'dev'
      ...
Imported 3 tasks. Review rubrics in tasks.yaml before running.
```

### 3. Review and edit (optional)

Edit `tasks.yaml` — delete, modify, or add rubric items. Then `otto run`.

## Data Model

### tasks.yaml

```yaml
tasks:
  - id: 1
    key: abc123def456
    prompt: "Add a search method to BookmarkStore that matches case-insensitively against title and url"
    status: pending
    rubric:
      - "search('python') returns bookmarks with 'python' in title or url"
      - "search is case-insensitive — 'Python' matches 'python'"
      - "partial matches work — 'exam' matches 'example.com'"
      - "no matches returns empty list"
      - "search on empty store returns empty list"
    context: "BookmarkStore is in bookmarks/store.py, JSON file-backed, has add/delete/get/list_all methods"
```

New fields:
- **`rubric`** (list[str], optional): Natural language acceptance criteria. Each item becomes a test.
- **`context`** (str, optional): Background info for the coding agent. Prepended to the agent prompt.

### Import formats

| Format | Detection | Behavior |
|--------|-----------|----------|
| `.md` | Extension | LLM parses into tasks + rubrics + context |
| `.txt` | Extension | One task per non-empty line (skip `#` comments), auto-generate rubrics per task |
| `.yaml`/`.yml` | Extension | Structured import, auto-generate rubrics for tasks missing them |

Pre-written rubrics in YAML imports are preserved — Otto only generates for tasks that don't have them. Markdown imports always go through LLM parsing (no structured preservation guarantee — the LLM extracts what it can from freeform text).

## New Module: `otto/rubric.py`

### `parse_markdown_tasks(filepath, project_dir) -> list[dict]`

Sends the markdown file + project context (file tree, key source files) to `claude -p` with a structured output prompt.

Prompt persona: Senior QA engineer + technical PM. The LLM must:
- Split the markdown into discrete, independent tasks
- Write a clear, actionable `prompt` for each (what the coding agent should do)
- Extract `rubric` items — concrete, testable acceptance criteria
- Extract `context` — background info useful for the agent but not a task or rubric

Returns a list of dicts, each with `prompt`, `rubric`, and `context` keys.

Output format: JSON (easier to parse reliably than YAML from LLM output).

Validation:
- Must return valid JSON
- Each task must have a non-empty `prompt`
- Each rubric item must be a non-empty string
- If parsing fails, raise with the raw output for debugging

### `generate_rubric(prompt, project_dir) -> list[str]`

Single-task rubric generation for `otto add "prompt"` and plain-text import.

Sends task prompt + project context to `claude -p` with QA engineer persona.

Prompt: "Given this task and project, write 5-8 concrete, testable acceptance criteria. Each must be specific enough to generate a test from. Cover: happy path, edge cases, error conditions."

Returns a list of strings. Validated: must be non-empty, each item must be a non-empty string.

### Source context gathering

Both functions need project context beyond just the file tree. New helper:

`_gather_project_context(project_dir) -> str`

Returns:
- File tree (`git ls-files`)
- Contents of key source files (detect main modules — not tests, not config, not lockfiles). Read up to 100 lines each, up to 5 files.
- Existing test file samples (reuse `_read_existing_tests` from testgen)

This gives the LLM enough context to write rubrics that reference actual class names, method signatures, and import paths.

## CLI Changes

### `otto add "prompt"`

1. Call `generate_rubric(prompt, project_dir)` — synchronous, ~5-10s
2. Call `add_task(tasks_path, prompt, rubric=rubric_items)`
3. Print task + rubric items

New flag: `--no-rubric` — skip generation, task has no rubric (falls back to auto-testgen at run time).

### `otto add -f <file>`

**All-or-nothing import:** Parse, generate rubrics, and validate everything first. Only write to `tasks.yaml` if all tasks succeed. If any step fails, report the error and don't import any tasks. This prevents half-imported state.

1. Detect format by extension
2. For `.md`: call `parse_markdown_tasks(filepath, project_dir)` — single LLM call
3. For `.txt`: read lines, call `generate_rubric()` per task — multiple LLM calls
4. For `.yaml`: load structured data, call `generate_rubric()` for tasks without rubric
5. Validate all parsed tasks
6. Write all tasks to `tasks.yaml` in one locked batch via `add_tasks()` (new batch variant)
7. Print summary

### `otto status`

Add rubric count column:
```
  ID  Key           Status    Rubric  Prompt
   1  abc123def456  pending   5       Add search to the bookmark store
   2  def789abc012  pending   0       Fix typo in README
```

## Verification Integration

### Runner changes (`runner.py`)

When `task.get("rubric")` is non-empty:
- Pass rubric items to a new `generate_tests_from_rubric()` function instead of `generate_tests()`
- The generated test file replaces tier 2 (auto-testgen)
- Tier 1 (existing tests) and tier 3 (custom verify) unchanged

When rubric is empty/missing:
- Fall back to current `generate_tests()` behavior (auto-testgen, unchanged)

### New in `testgen.py`: `generate_tests_from_rubric()`

```python
def generate_tests_from_rubric(
    rubric: list[str],
    prompt: str,
    project_dir: Path,
    key: str,
) -> Path | None:
```

Similar to `generate_tests()` but the prompt includes explicit rubric items:

```
You are a QA engineer writing tests for these acceptance criteria:

1. search('python') returns bookmarks with 'python' in title or url
2. search is case-insensitive
3. partial matches work
...

Write one test function per criterion. Each test must exercise real behavior.

PROJECT SOURCE:
<key source files>

EXISTING TESTS:
<test samples>
```

Validation is framework-aware (shared with `generate_tests()`):
- **pytest**: `ast.parse` + must contain `def test_`
- **jest/vitest/mocha**: check for `describe(` or `it(` or `test(`
- **go/cargo**: basic syntax check (non-empty, no prose preamble)

If validation fails, returns None (tier 2 skipped, not a failure).

### Context field usage

`context` is prepended to the agent prompt on **every attempt** (initial and retries):

```python
# Initial attempt
agent_prompt = f"Context: {context}\n\n{prompt}\n\nYou are working in {project_dir}..."

# Retry attempt
agent_prompt = f"Context: {context}\n\nVerification failed. Fix the issue.\n\n{last_error}\n\nOriginal task: {prompt}\n\n..."
```

## Task changes (`tasks.py`)

### `add_task()` signature

```python
def add_task(
    tasks_path: Path,
    prompt: str,
    verify: str | None = None,
    max_retries: int | None = None,
    rubric: list[str] | None = None,
    context: str | None = None,
) -> dict[str, Any]:
```

Rubric and context are stored in the task dict, serialized to YAML.

## What Doesn't Change

- Task lifecycle (pending → running → passed/failed)
- Branch-per-task, ff-only merge
- Retry mechanics — rubric persists across retries, tests generated **once per task** (before first attempt) and reused across retries, same as current auto-testgen
- Process locking, signal handling
- Tier 1 (existing tests), tier 3 (custom verify)
- `otto run "one-off prompt"` — no rubric (one-off tasks use auto-testgen)

## File Summary

| File | Changes |
|------|---------|
| `otto/rubric.py` | **New** — `parse_markdown_tasks()`, `generate_rubric()`, `_gather_project_context()` |
| `otto/testgen.py` | Add `generate_tests_from_rubric()` |
| `otto/tasks.py` | `add_task()` accepts `rubric` and `context` params; new `add_tasks()` batch variant for all-or-nothing import |
| `otto/cli.py` | `add` command: rubric generation, `--no-rubric` flag, `.md`/`.txt` support, status rubric column |
| `otto/runner.py` | Route to rubric-based testgen when rubric present, prepend context to agent prompt |
| `tests/` | Tests for each changed module |
