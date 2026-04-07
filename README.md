# Otto

Intent to product. Describe what you want, Otto builds it, certifies it works, and fixes what doesn't.

One autonomous agent session: plan → build → test → certify → fix → re-certify → ship.

## How it works

```
otto build "bookmark manager with tags and search"
```

Otto launches a single coding agent that:

1. **Plans** — reads the intent, designs architecture
2. **Builds** — implements the product, dispatches subagents for parallel work on independent features
3. **Tests** — writes comprehensive tests, runs them, fixes failures
4. **Certifies** — dispatches a certifier agent (builder-blind) that tests the product as a real user
5. **Fixes** — if certification fails, reads the findings, fixes the code, re-certifies
6. **Ships** — commits when certification passes

The certifier is a separate agent that doesn't know how the product was built. It reads the project fresh, installs deps, starts the app, and runs real user stories (curl for APIs, CLI commands for tools, import for libraries, agent-browser for web UIs). It reports what's wrong — the coding agent figures out the fix.

```
  Agentic mode — one agent builds, certifies, fixes

  All journeys passed (round 1)
    ✓ Registration, login, and first note creation work with JWT token flow
    ✓ Create/Read/Update/Delete notes with tags return correct status codes
    ✓ Cross-user read/update/delete blocked; list only shows own notes
    ✓ Unauthenticated and invalid-token requests rejected
    ✓ Tag and keyword search return correct results
    ✓ Data survives server restart
    ✓ Input validation, special characters, boundary conditions handled

  Build Summary  (build-1775522841-19595)
  Stories: 7 passed, 0 failed
  Total cost: $1.04
  Duration: 4.8 min
```

## Quick start

```bash
# Install
uv pip install -e .

# In any git repo
cd your-project
otto build "REST API for a todo app with user auth"
```

## What it builds

Otto handles any product type:
- **CLI tools** — argparse, Click, Cobra (curl + CLI command testing)
- **REST APIs** — Express, Flask, FastAPI (curl + HTTP testing)
- **Libraries** — Python, Node.js (import + unit testing)
- **Web apps** — server-rendered HTML (curl + agent-browser visual testing)
- **Hybrid** — API + CLI + UI tested across all surfaces

## Architecture

```
otto build "intent"
  │
  └─ Coding Agent (one session, drives everything)
       │
       ├─ Plan architecture
       ├─ Build (subagents for parallel features)
       ├─ Write tests, run, fix
       ├─ Commit
       │
       ├─ Dispatch Certifier Agent (builder-blind)
       │    ├─ Read project fresh
       │    ├─ Install deps, start app
       │    ├─ Discover auth (once, share with subagents)
       │    ├─ Dispatch test subagents (parallel stories)
       │    │    ├─ First Experience
       │    │    ├─ CRUD Lifecycle
       │    │    ├─ Data Isolation
       │    │    ├─ Persistence / Access Control
       │    │    └─ Edge Cases
       │    └─ Report: PASS/FAIL per story + evidence
       │
       ├─ If FAIL: read findings, fix code, commit
       ├─ Re-dispatch certifier
       └─ Repeat until PASS
```

The certifier reports symptoms only — no fix suggestions. The coding agent diagnoses and fixes.

## Build modes

```
otto build "intent"              # default: agent-driven (recommended)
otto build "intent" --split      # separate build + certify sessions
otto build "intent" --orchestrated  # legacy PER pipeline with task queue
```

## CLI reference

```
otto build "intent"     Build a product from natural language intent
otto add "prompt"       Add a task to the queue (for --orchestrated mode)
otto run                Run pending tasks (--orchestrated mode)
otto status             Show task states
otto logs <id>          Show agent logs
```

## Configuration

`otto.yaml` (auto-created on first run):

```yaml
# Model (optional — defaults to provider's best)
# model: claude-sonnet-4-5-20250514

# Product certification
# certifier_timeout: 900        # max seconds for build+certify
# certifier_browser: null       # null = auto-detect; true/false to force
# certifier_interaction: null   # override product type (http/cli/import)
```

## Logs

```
otto_logs/
  builds/<build-id>/
    agent.log            Key events: commits, certifier rounds, verdict
    agent-raw.log        Full agent output (for deep debugging)
    checkpoint.json      Build metadata: cost, duration, stories
  certifier/
    proof-of-work.json   Machine-readable: stories, evidence, rounds
    proof-of-work.html   Human-readable: styled report with evidence
    proof-of-work.md     Markdown summary
```

## Project structure

```
otto/
  pipeline.py          Build pipelines (v3 default + v2 split + orchestrated)
  certifier/
    __init__.py        Agentic certifier (single agent + subagents)
    report.py          CertificationReport, Finding, Outcome dataclasses
  agent.py             Agent SDK wrapper (query, run_agent_query)
  session.py           AgentSession for split mode (resume across rounds)
  cli.py               CLI commands (build, add, run, status)
  config.py            Config loading, otto.yaml creation
  orchestrator.py      PER pipeline (--orchestrated mode)
  runner.py            Task runner for orchestrated mode
  qa.py                QA agent for orchestrated mode
  display.py           Live terminal display
  observability.py     Log writing utilities
```

## Requirements

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- Git repository
- Optional: `agent-browser` CLI for visual web app testing

## License

MIT
