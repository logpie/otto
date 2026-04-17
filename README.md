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

## CLI

```
otto build "intent"                    Build + certify a product
otto build "intent" --no-qa           Build without certification
otto build "intent" --split           System-controlled certify loop
otto build "intent" -n 5              Max 5 certification rounds

otto certify                           Certify (reads intent.md)
otto certify "intent"                  Certify with explicit intent
otto certify --thorough                Adversarial deep inspection

otto improve bugs                      Find and fix bugs
otto improve bugs "error handling"     Focused bug hunting
otto improve feature                   Suggest improvements
otto improve feature "search UX"       Focused feature work
otto improve target "metric < value"   Optimize toward a target
otto improve target "metric" -n 10     Up to 10 rounds

otto history                           Show build history
otto setup                             Generate CLAUDE.md for project
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
