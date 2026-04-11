# Otto

Build and certify software products from natural language.

```bash
otto build "bookmark manager with tags and search"
otto certify                    # verify any project works
```

## How it works

**`otto build`** launches one autonomous agent that plans, builds, tests, certifies, and fixes:

1. **Explore** — reads existing code (if any), runs existing tests
2. **Build** — implements the product, dispatches subagents for parallel work
3. **Test** — writes tests, runs them, fixes failures
4. **Certify** — dispatches a certifier agent (builder-blind) that tests as a real user
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

The certifier found real bugs in 3 out of 4 open-source projects tested — including
a data isolation failure (`user_id == user_id` instead of `TaskModel.user_id == user_id`)
and missing auth checks on endpoints. Zero false positives.

## Quick start

```bash
uv pip install -e .

cd your-project
otto build "REST API for a todo app with user auth"
```

Works on empty repos (greenfield) and existing codebases (incremental):

```bash
otto build "notes API with auth and CRUD"        # greenfield
otto build "add search by keyword and tag"       # incremental (reads existing code)
otto build "add pagination to note listing"      # incremental
otto history                                      # see all builds
```

## What it handles

- **CLI tools** — argparse, Click (CLI command testing)
- **REST APIs** — Express, Flask, FastAPI (curl testing)
- **Libraries** — Python, Node.js (import + unit testing)
- **Web apps** — server-rendered HTML (curl + agent-browser screenshots + video)
- **Hybrid** — API + CLI + UI tested across all surfaces

## Architecture

```
otto build "intent"
  │
  └─ Coding Agent (one session)
       ├─ Explore existing code / plan architecture
       ├─ Build (subagents for parallel features)
       ├─ Write tests, run, fix
       ├─ Commit
       ├─ Dispatch Certifier Agent (builder-blind)
       │    ├─ Read project fresh, install deps, start app
       │    ├─ Discover auth once, share with subagents
       │    ├─ Dispatch test subagents (parallel stories)
       │    ├─ Visual walkthrough with agent-browser (web apps)
       │    └─ Report: PASS/FAIL per story + evidence
       ├─ If FAIL: read findings, fix, commit, re-certify
       └─ Repeat until PASS

otto certify "intent"
  │
  └─ Certifier Agent (standalone, builder-blind)
       └─ Same as above, without the build phase
```

## CLI

```
otto build "intent"         Build + certify a product
otto build "intent" --no-qa Build without certification
otto certify "intent"       Certify any project (standalone)
otto certify                Reads intent from intent.md or README.md
otto history                Show build history with results
otto setup                  Generate CLAUDE.md for the project
```

## Configuration

`otto.yaml` (auto-created on first run):

```yaml
# Model (optional — defaults to provider's best)
# model: claude-sonnet-4-5-20250514

# Certification
# certifier_timeout: 900         # max seconds for build+certify session
# certifier_browser: null        # null = auto-detect; true/false to force
# certifier_interaction: null    # override product type (http/cli/import)
```

## Logs & evidence

```
otto_logs/
  builds/<build-id>/
    agent.log              Structured: commits, certifier rounds, verdict
    agent-raw.log          Full agent output (deep debugging)
    checkpoint.json        Build metadata: cost, duration, stories
  certifier/
    proof-of-work.html     Styled report with embedded screenshots
    proof-of-work.json     Machine-readable: stories, evidence, rounds
    proof-of-work.md       Markdown summary
    evidence/
      homepage.png         Screenshot per page (web apps)
      recording.webm       Video of browser walkthrough (web apps)
  run-history.jsonl        One line per build for otto history
intent.md                  Cumulative log of all build intents
```

## Project structure

```
otto/                        4,982 lines total
  pipeline.py               Build pipeline (build_agentic_v3)
  certifier/
    __init__.py             Certifier agent (run_agentic_certifier)
    report.py               CertificationReport dataclasses
  prompts/
    build.md                Build prompt (editable without code changes)
    certifier.md            Certifier prompt (editable)
  agent.py                  Agent SDK wrapper
  cli.py                    CLI commands
  config.py                 Config + otto.yaml
  display.py                Terminal display
  observability.py          Log utilities
tests/                       1,950 lines, 122 tests
```

## Requirements

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- Git repository
- Optional: `agent-browser` CLI for visual web app testing + video recording

## License

MIT
