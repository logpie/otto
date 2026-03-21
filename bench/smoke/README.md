# Otto Smoke Tests

Quick validation suite for core otto functionality across project types.

## Usage

```bash
# Run all smoke tests
./bench/smoke/run.sh

# Run a single project
./bench/smoke/run.sh fibonacci

# Use a specific otto binary
OTTO_BIN=/path/to/otto ./bench/smoke/run.sh
```

## Projects

| Project | Language | Tasks | What it tests |
|---------|----------|-------|---------------|
| `fibonacci` | Python | 1 | Greenfield: create function + tests from scratch |
| `bugfix` | Python | 1 | Fix existing bugs, add edge case tests |
| `express-api` | Node.js | 2 | Multi-task, npm project, REST API |
| `multi-task` | Python | 3 | Sequential dependent tasks, data structures |

## Structure

```
bench/smoke/
  run.sh              # Runner script
  projects/
    <name>/
      setup.sh        # Creates the initial git repo in a temp dir
      tasks.txt       # One task per line, fed to `otto add`
  results/
    YYYY-MM-DD-HHMMSS/
      results.json    # Structured results for comparison
      <name>/
        otto_output.txt  # Full otto output
        tasks.yaml       # Final tasks.yaml after run
```

## Adding a new smoke test

1. Create `bench/smoke/projects/<name>/setup.sh` — must initialize a git repo (git init is done by the runner, just add files and commit)
2. Create `bench/smoke/projects/<name>/tasks.txt` — one task per line, blank lines and `#` comments are skipped
3. Run `./bench/smoke/run.sh <name>` to test it

## Results

Results are written to `bench/smoke/results/<timestamp>/results.json` with per-project pass/fail, task counts, timing, and cost data. Use these to track otto reliability across changes.
