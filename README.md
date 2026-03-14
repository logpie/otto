# Otto

Autonomous Claude Code agent runner with adversarial TDD verification.

Otto runs Claude Code agents against a task queue. You describe what you want in natural language, Otto generates adversarial tests from your spec, then dispatches an AI agent to write code that passes them. Tests come first — written by a QA adversary who's never seen the implementation.

## How it works

```
features.md          →  otto add -f features.md  →  otto run
(what you want)         (rubrics generated)          (agents execute)
```

For each task, Otto:

1. **Generates rubrics** — natural language acceptance criteria from your task description
2. **Writes adversarial tests** — a QA agent writes black-box tests from the rubric, before any code exists
3. **Verifies TDD invariant** — tests must fail (feature doesn't exist yet)
4. **Commits tests** — locked, immutable acceptance criteria
5. **Runs coding agent** — Claude implements the feature to pass the tests
6. **Verifies** — full test suite in a clean disposable worktree
7. **Merges** — fast-forward to main, clean git history

The testgen agent and coding agent are **adversaries**, not collaborators. The testgen agent sees only public API signatures (extracted via AST) — never function bodies. If the coding agent tries to modify the test file, Otto detects the tampering and restores it.

## Quick start

```bash
# Install
uv pip install -e .

# Initialize in any git repo
cd your-project
otto init

# Add tasks — write naturally
otto add "Add a search function that matches case-insensitively against title and url"

# Or import from markdown
cat > features.md << 'EOF'
# Search
Users should be able to search by title or URL.
Case-insensitive, partial matches.

# Favorites
Mark bookmarks as favorites. Filter by favorites.
EOF
otto add -f features.md

# Run — watch agents work
otto run
```

## What you see

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Task #1  Add search functionality
  key abc123def456
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Building black-box context...
  Testgen agent writing adversarial tests (8 criteria)...
  → Write  tests/test_otto_abc123.py
  → Bash   pytest --collect-only tests/test_otto_abc123.py
  ✓ Adversarial tests ready (27 failing, 1 regression)

  attempt 1/4
  ● Read   bookmarks/store.py
  ● Edit   bookmarks/store.py
    + def search(self, query: str) -> list[Bookmark]:
    +     """Search bookmarks case-insensitively."""
    +     q = query.lower()
      ...
  ● Edit   bookmarks/cli.py
  ● Bash   python -m pytest tests/ -v
  ✓ existing_tests

  ✓ Task #1 PASSED — merged to main in 2m10s
```

## CLI reference

```
otto init              Initialize otto in a git repo
otto add "prompt"      Add a task (auto-generates rubrics)
otto add --no-rubric   Skip rubric generation
otto add -f file       Import from .md/.txt/.yaml (replaces existing tasks)
otto run               Run all pending tasks
otto run --dry-run     Show what would run
otto run --no-integration  Skip post-run integration gate
otto status            Show task table
otto retry <id>        Reset a failed task to pending
otto retry --force <id> "feedback"  Reset any task with feedback
otto logs <id>         Show logs for a task
otto reset             Clear all tasks, branches, logs
otto -h                Help
```

## Architecture

### Adversarial TDD

The core innovation: **test generation is adversarial**. The QA agent writes tests from the specification alone, mechanically isolated from the implementation.

| | Testgen Agent | Coding Agent |
|---|---|---|
| **Role** | QA adversary | Implementer |
| **Sees** | Rubric + public API stubs (AST-extracted) | Full codebase + test file |
| **Runs in** | Isolated temp directory | Project directory |
| **Writes** | Test file only | Implementation code only |
| **Goal** | Catch bugs | Pass tests |

The testgen agent receives a sanitized "black-box context" — function signatures and docstrings extracted via Python AST, with all function bodies stripped. It literally cannot see implementation details.

### Verification flow

```
rubric items
    ↓
build_blackbox_context()     ← AST extracts signatures only
    ↓
run_testgen_agent()          ← QA agent in temp dir
    ↓
validate_generated_tests()   ← Phase A: collection check
                             ← Phase B: TDD check (must fail)
    ↓
commit tests                 ← locked, SHA tracked
    ↓
coding agent                 ← implements feature
    ↓
tamper check                 ← verify test file unchanged
    ↓
run_verification()           ← full suite in clean worktree
    ↓
squash merge to main
```

### Integration gate

After 2+ tasks pass, Otto generates cross-feature integration tests that exercise features working together. If they fail, an agent attempts to fix the issues.

### Rubric generation

Rubrics are natural language acceptance criteria, auto-generated at `otto add` time:

```yaml
rubric:
  - "search('python') returns bookmarks with 'python' in title or url"
  - "search is case-insensitive"
  - "search does NOT return unrelated bookmarks"
  - "search does NOT mutate the store"
  - "CLI search with no match exits 0 and shows a message"
```

Categories enforced: happy path, error handling, negative/anti-pattern ("does NOT"), edge cases.

### Import formats

```bash
otto add -f features.md    # Markdown — LLM parses into tasks + rubrics
otto add -f tasks.txt      # Text — one task per line, rubrics auto-generated
otto add -f tasks.yaml     # YAML — structured, preserves pre-written rubrics
```

## Configuration

`otto.yaml` is minimal:

```yaml
max_retries: 3
default_branch: main
verify_timeout: 300
```

Everything else is auto-detected:
- **test_command** — detected from project structure (pytest, npm test, go test, etc.)
- **model** — uses Claude CLI default

Override anything by adding it to `otto.yaml`:

```yaml
model: opus
test_command: "uv run pytest -x"
```

## Requirements

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- Git repository
- `uv` (recommended) or `pip` for installation

## Project structure

```
otto/
  cli.py        — Click CLI (add, run, status, retry, logs, reset)
  runner.py     — Core execution loop, adversarial TDD, agent streaming
  testgen.py    — Black-box context builder, testgen agent, test validation
  rubric.py     — Rubric generation, markdown parsing
  verify.py     — Verification in disposable worktrees, integration gate
  tasks.py      — Task CRUD with file locking
  config.py     — Config loading, auto-detection
```

## License

MIT
