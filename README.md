# Otto

Build, certify, and improve software products from natural language.

```bash
otto build "bookmark manager with tags and search"
otto certify                              # verify any project works
otto improve bugs                         # find and fix bugs
otto improve target "latency < 100ms"     # optimize toward a metric
```

## How it works

**`otto build`** launches one autonomous agent that plans, builds, tests, certifies, and fixes:

1. **Explore** — reads existing code (if any), runs existing tests
2. **Build** — implements the product, dispatches subagents for parallel work
3. **Test** — writes tests, runs them, fixes failures
4. **Certify** — dispatches a builder-blind certifier agent that tests as a real user
5. **Fix** — if certification fails, reads findings, fixes code, re-certifies
6. **Ship** — commits when certification passes

**`otto certify`** runs independently on any project — regardless of how it was built:

```
$ otto certify "notes API with auth, CRUD, and search"

    ✓ Full CRUD lifecycle works correctly
    ✓ Users cannot access each other's notes
    ✓ Unauthenticated requests rejected with 401
    ✓ Search by keyword and tag works
    ✓ Edge cases handled

  PASSED — 5/5 stories
  Cost: $1.10  Duration: 164s
  Report: otto_logs/certifier/proof-of-work.html
```

**`otto improve`** iterates on existing code with three modes:

```bash
# Find and fix bugs (adversarial certifier → fix loop)
otto improve bugs
otto improve bugs "error handling"         # focused

# Suggest and implement features (product advisor → implement loop)
otto improve feature
otto improve feature "search UX"           # focused

# Hit a measurable target (measure → optimize → re-measure loop)
otto improve target "response time < 50ms"
otto improve target "test coverage > 90%"
```

Each mode creates an improvement branch, runs up to N rounds, and writes a report with merge instructions.

## Quick start

```bash
# Install
uv pip install -e ".[claude]"

# Build from scratch (greenfield)
cd your-project && git init
otto build "REST API for a todo app with SQLite"

# Build on existing code (incremental)
otto build "add search by keyword and tag"
otto build "add pagination to listings"

# Verify any project works
otto certify

# Find and fix bugs
otto improve bugs

# Optimize toward a target
otto improve target "all endpoints respond in < 100ms"
```

## What it handles

- **CLI tools** — argparse, Click (runs commands, checks output)
- **REST APIs** — Express, Flask, FastAPI (curl testing)
- **Libraries** — Python, Node.js (import + unit testing)
- **Web apps** — Next.js, React (browser testing, screenshots, video)

## Command Reference

### `otto build`

Build a product from a natural language intent. One agent builds, certifies, and fixes autonomously.

```bash
otto build "bookmark manager with tags and search"
otto build "CLI tool that converts CSV to JSON"
otto build "add dark mode toggle to the settings page"   # incremental

# Options
otto build "intent" --no-qa        # skip certification (just build)
otto build "intent" --split        # system-controlled certify loop (vs agent-driven)
otto build "intent" -n 5           # max 5 certification rounds (default: 8)
```

### `otto certify`

Certify any project — independent, builder-blind verification. Tests the product as a real user. Works regardless of how it was built (otto, Claude Code, human).

The intent describes what the product should do. The certifier generates test stories from it.

```bash
otto certify                                            # reads intent.md or README.md
otto certify "notes API with auth, CRUD, and search"    # explicit intent
otto certify --thorough                                 # adversarial: edge cases, code review
```

### `otto improve`

Iterate on existing code. Three modes, each with a specialized certifier prompt. Creates an improvement branch for isolation.

#### `otto improve bugs`

Find and fix bugs, edge cases, error handling gaps. Adversarial certifier tries to break the product, then the build agent fixes what it finds.

```bash
otto improve bugs                       # find and fix all bugs
otto improve bugs "error handling"      # focus on error handling
otto improve bugs "auth and security"   # focus on auth
otto improve bugs -n 5                  # up to 5 rounds (default: 3)
```

#### `otto improve feature`

Suggest and implement product improvements. Evaluates the product as a real user, identifies missing features and UX gaps, then implements them.

```bash
otto improve feature                    # suggest and implement improvements
otto improve feature "search UX"        # focus on search experience
otto improve feature "mobile layout"    # focus on mobile
otto improve feature -n 5              # up to 5 rounds (default: 3)
```

#### `otto improve target`

Optimize toward a measurable target. Measures a metric, compares to the target, and iterates until met. The goal is a required argument.

```bash
otto improve target "response time < 100ms"
otto improve target "test coverage > 90%"
otto improve target "bundle size < 500kb"
otto improve target "lighthouse score > 95" -n 10    # up to 10 rounds (default: 5)
```

### `otto history`

Show build history with results, cost, and duration.

```bash
otto history             # show recent builds
otto history -n 20       # show last 20 builds
```

### `otto setup`

Generate a `CLAUDE.md` file with project conventions for the coding agent. Reads the project structure and creates instructions automatically.

```bash
otto setup
```

## Configuration

`otto.yaml` (auto-created on first run):

```yaml
# Model (optional — defaults to provider's best)
# model: claude-sonnet-4-5-20250514

# Certification
# certifier_timeout: 900         # max seconds for build+certify session
# max_certify_rounds: 8          # max rounds in build loop
```

## Logs

```
otto_logs/
  builds/<build-id>/
    agent.log              Structured: commits, certifier rounds, verdict
    agent-raw.log          Full agent output
    checkpoint.json        Cost, duration, stories tested/passed
  certifier/<run-id>/
    proof-of-work.html     Report with embedded screenshots
    proof-of-work.json     Machine-readable results
    evidence/              Screenshots, video recordings
  run-history.jsonl        One line per build (for otto history)
build-journal.md           Round-by-round tracking (improve mode)
improvement-report.md      Final improve summary with merge instructions
```

## Project structure

```
otto/                        ~3,500 lines
  pipeline.py               Build pipeline, certify-fix loop
  certifier/__init__.py     Certifier agent
  markers.py                Parse STORY_RESULT/VERDICT/METRIC from agent output
  prompts/                  Editable prompts (build, certify, improve modes)
  agent.py                  Agent SDK wrapper
  cli.py                    CLI: build, certify
  cli_improve.py            CLI: improve (bugs, feature, target)
  config.py                 Config, intent resolution, helpers
  journal.py                Build journal for improve rounds
tests/                       89 tests
```

## Requirements

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- Git repository

## License

MIT
