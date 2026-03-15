# Otto Architecture — 2026-03-15

## System Overview

```
User writes features.md
        ↓
   otto add -f features.md
        ↓
   ┌─────────────────────┐
   │  Rubric Agent        │  Agentic (Agent SDK, max_turns=10)
   │  Reads project files │  Writes behavioral acceptance criteria
   │  Runs CLI --help     │  Self-reviews and improves criteria
   │  Writes rubrics      │  Aborts if generation fails (no ghost tasks)
   └─────────────────────┘
        ↓
   tasks.yaml created (task + rubrics)
        ↓
   otto run
        ↓
   For each pending task:
   ┌─────────────────────────────────────────────────────┐
   │                    TASK EXECUTION                     │
   │                                                       │
   │  1. TESTGEN AGENT (adversarial, pre-implementation)   │
   │  2. TDD CHECK                                         │
   │  3. COMMIT TESTS                                      │
   │  4. CODING AGENT (implementation)                     │
   │  5. TAMPER CHECK                                      │
   │  6. VERIFICATION                                      │
   │  7. MUTATION CHECK                                    │
   │  8. MERGE                                             │
   └─────────────────────────────────────────────────────┘
        ↓
   After 2+ tasks pass:
   ┌─────────────────────────┐
   │  INTEGRATION GATE        │
   │  Cross-feature tests     │
   │  Agent fixes failures    │
   └─────────────────────────┘
        ↓
   Run summary with costs
```

## Detailed Task Execution Flow

```
                    ┌──────────────────┐
                    │  Start Task      │
                    │  Create branch   │
                    │  otto/<key>      │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  Has rubric?     │
                    └──┬──────────┬────┘
                    yes│          │no
           ┌───────────▼──┐  ┌───▼──────────────┐
           │ ADVERSARIAL  │  │ FALLBACK TESTGEN  │
           │ TESTGEN      │  │ (Agent SDK)       │
           │              │  │ Runs concurrently │
           │ Isolated     │  │ with coding agent │
           │ temp dir     │  └───────────────────┘
           │ AST stubs    │
           │ only         │
           │              │
           │ Writes tests │
           │ → validate   │
           │ → self-review│
           └──────┬───────┘
                  │
           ┌──────▼───────┐
           │  TDD CHECK   │
           │              │
           │ Phase A:     │
           │  --collect-  │
           │  only        │
           │  (syntax)    │
           │              │
           │ Phase B:     │
           │  Run tests   │
           │  (must fail) │
           └──┬───────┬───┘
              │       │
         tdd_ok   all_pass
              │       │
              │   ┌───▼──────────────┐
              │   │ Feature exists?  │
              │   └──┬──────────┬────┘
              │    yes│         │no
              │      │    ┌────▼─────────┐
              │      │    │ Regenerate   │
              │      │    │ tests once   │
              │      │    └──────────────┘
              │      │
              │   Keep as regression
              │
           ┌──▼──────────┐
           │ COMMIT TESTS │
           │ Record SHA   │
           │ for tamper   │
           │ detection    │
           └──────┬───────┘
                  │
    ┌─────────────▼───────────────┐
    │     ATTEMPT LOOP            │
    │     (up to max_retries+1)   │
    │                             │
    │  ┌────────────────────┐     │
    │  │ CODING AGENT       │     │
    │  │ Agent SDK          │     │
    │  │ max_turns=20       │     │
    │  │                    │     │
    │  │ Gets relevant      │     │
    │  │ source files in    │     │
    │  │ prompt             │     │
    │  │                    │     │
    │  │ "Implement ONLY    │     │
    │  │  what spec asks"   │     │
    │  └─────────┬──────────┘     │
    │            │                │
    │  ┌─────────▼──────────┐     │
    │  │ TAMPER CHECK       │     │
    │  │ SHA match?         │     │
    │  │ Restore if not     │     │
    │  └─────────┬──────────┘     │
    │            │                │
    │  ┌─────────▼──────────┐     │
    │  │ BUILD CANDIDATE    │     │
    │  │ Squash commits     │     │
    │  │ Include test file  │     │
    │  └─────────┬──────────┘     │
    │            │                │
    │  ┌─────────▼──────────┐     │
    │  │ VERIFICATION       │     │
    │  │ Disposable worktree│     │
    │  │ Full test suite    │     │
    │  └──┬────────────┬────┘     │
    │   pass          fail        │
    │     │             │         │
    │     │    ┌────────▼──────┐  │
    │     │    │ JUDGE: test   │  │
    │     │    │ bug or impl   │  │
    │     │    │ bug?          │  │
    │     │    └──┬────────┬───┘  │
    │     │   test_bug  impl_bug  │
    │     │       │         │     │
    │     │  Regenerate   Retry   │
    │     │  tests        attempt │
    │     │                       │
    └─────┼───────────────────────┘
          │
   ┌──────▼───────┐
   │ MUTATION      │
   │ CHECK         │
   │               │
   │ Comment out   │
   │ random impl   │
   │ line          │
   │ Run tests     │
   │ Caught? Y/N   │
   │ Restore       │
   └──────┬────────┘
          │
   ┌──────▼───────┐
   │ SQUASH MERGE │
   │ to main      │
   │ Single commit│
   │ Test file    │
   │ included     │
   └──────────────┘
```

## Agent Roles and Isolation

```
┌─────────────────────────────────────────────────────────────────┐
│                        AGENTS                                    │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ RUBRIC AGENT │  │ TESTGEN      │  │ CODING AGENT         │  │
│  │              │  │ AGENT        │  │                      │  │
│  │ Role: PM/QA  │  │ Role: QA     │  │ Role: Developer      │  │
│  │              │  │ adversary    │  │                      │  │
│  │ Sees: full   │  │              │  │ Sees: full source    │  │
│  │ source       │  │ Sees: AST    │  │ in prompt            │  │
│  │              │  │ stubs only   │  │                      │  │
│  │ Runs in:     │  │              │  │ Runs in: project     │  │
│  │ project dir  │  │ Runs in:     │  │ dir                  │  │
│  │              │  │ isolated     │  │                      │  │
│  │ Writes:      │  │ temp dir     │  │ Writes: impl code    │  │
│  │ rubric file  │  │              │  │ Cannot modify tests  │  │
│  │              │  │ Writes:      │  │                      │  │
│  │ max_turns:10 │  │ test file    │  │ max_turns: 20        │  │
│  │              │  │              │  │                      │  │
│  │              │  │ max_turns:15 │  │                      │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ INTEGRATION  │  │ JUDGE AGENT  │  │ INTEGRATION FIX      │  │
│  │ TESTGEN      │  │              │  │ AGENT                │  │
│  │              │  │ Role: arbiter│  │                      │  │
│  │ Role: QA     │  │              │  │ Role: Developer      │  │
│  │              │  │ Sees: rubric │  │                      │  │
│  │ Sees: full   │  │ + test +     │  │ Sees: full source    │  │
│  │ source in    │  │ failure      │  │                      │  │
│  │ prompt       │  │              │  │ Runs in: disposable  │  │
│  │              │  │ One-shot     │  │ worktree             │  │
│  │ Runs in:     │  │ claude -p    │  │                      │  │
│  │ project dir  │  │              │  │ max_turns: 15        │  │
│  │              │  │ Decides:     │  │                      │  │
│  │ max_turns:15 │  │ TEST_BUG or  │  │                      │  │
│  │              │  │ IMPL_BUG     │  │                      │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
│                                                                  │
│  PRINCIPLE: Pre-implementation → stubs only                      │
│             Post-implementation → full source                    │
│             Testgen and coding agent are ADVERSARIES              │
└─────────────────────────────────────────────────────────────────┘
```

## Smart Context Pipeline

```
  Project (153 files)
        │
        ▼
  git ls-files → file tree
        │
        ▼
  AST parse each .py file
        │
        ├─→ Symbol Index: {class/function name → file path}
        │
        └─→ Import Graph: {file → set of files it imports from}
        │
        ▼
  Task hint (prompt + rubric keywords)
        │
        ▼
  _find_relevant_files():
    1. Substring match: hint keywords → symbol names
    2. File path match: hint keywords → file paths
    3. Import graph: expand one level (importers + dependencies)
    4. Cap at 15 files
        │
        ▼
  For TESTGEN (pre-impl):           For CODING AGENT (impl):
  _extract_public_stubs()            get_relevant_file_contents()
  → signatures + docstrings only     → full file contents
  → NO function bodies               → ready to edit
        │                                    │
        ▼                                    ▼
  build_blackbox_context()           Included in agent prompt
  → FILE TREE + STUBS + CLI HELP    "RELEVANT SOURCE FILES
  + EXISTING TESTS                    (already read for you)"
```

## File Layout

```
otto/
├── cli.py          CLI commands (add, run, status, retry, logs, diff, show, reset)
├── runner.py       Core execution loop, agent orchestration, cost tracking
│                   ~1400 lines — largest file, handles:
│                   - Task execution (testgen → coding → verify → merge)
│                   - Attempt loop with retries
│                   - Tamper detection (git blob SHA)
│                   - Mutation checks
│                   - Integration gate
│                   - Agent streaming + logging
│                   - Squash merge logic
│                   - Test bug diagnosis (judge agent)
│                   - Auto-stash dirty tree
│
├── testgen.py      Test generation — multiple functions:
│                   - build_blackbox_context() — AST stubs + import graph
│                   - _build_project_index() — symbol-to-file + import graph
│                   - _find_relevant_files() — smart file selection
│                   - get_relevant_file_contents() — full source for coding agent
│                   - run_testgen_agent() — adversarial testgen in temp dir
│                   - generate_tests() — fallback (no rubric)
│                   - generate_integration_tests() — post-run cross-feature
│                   - validate_generated_tests() — two-phase (collect + TDD)
│                   - run_mutation_check() — comment out line, check tests catch it
│                   - _extract_public_stubs() — AST → signatures + docstrings
│
├── rubric.py       Rubric generation:
│                   - generate_rubric() — agentic, self-reviewing
│                   - parse_markdown_tasks() — agentic, markdown → tasks + rubrics
│                   - _parse_rubric_output() — text → list of criteria
│
├── verify.py       Verification:
│                   - run_verification() — disposable worktree, run test suite
│                   - run_integration_gate() — post-run, clean worktree
│                   - run_tier1/2/3() — individual verification tiers
│
├── display.py      Shared agent output formatting:
│                   - print_agent_tool() — styled tool use with temp path stripping
│                   - _truncate_at_word() — word-boundary truncation
│                   - _strip_temp_prefix() — remove /otto_testgen_*/ from paths
│
├── tasks.py        Task CRUD on tasks.yaml with file locking
├── config.py       Auto-detection (test_command, default_branch)
├── _agent_stub.py  Mock Agent SDK for testing
└── __init__.py, __main__.py

tests/
├── conftest.py         Shared fixtures (tmp_git_repo)
├── test_cli.py         CLI command tests
├── test_config.py      Config detection tests
├── test_integration.py End-to-end tests with mocked agents
├── test_rubric.py      Rubric generation tests
├── test_runner.py      Runner logic tests (clean tree, tamper, etc.)
├── test_tasks.py       Task CRUD tests
├── test_testgen.py     Testgen function tests
└── test_verify.py      Verification tests
```

## Data Flow

```
features.md ──→ otto add -f ──→ tasks.yaml
                    │
                    ▼
              Rubric Agent
              (agentic, reads project)
                    │
                    ▼
              tasks.yaml with rubrics
                    │
                    ▼
              otto run
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
    Task #1     Task #2     Task #3
        │           │           │
        ▼           ▼           ▼
    otto/<key>  otto/<key>  otto/<key>
    branch      branch      branch
        │           │           │
        ▼           ▼           ▼
    tests/      tests/      tests/
    test_otto_  test_otto_  test_otto_
    <key>.py    <key>.py    <key>.py
        │           │           │
        ▼           ▼           ▼
    Implement   Implement   Implement
    + verify    + verify    + verify
        │           │           │
        ▼           ▼           ▼
    Squash      Squash      Squash
    merge       merge       merge
    to main     to main     to main
        │           │           │
        └───────────┼───────────┘
                    ▼
            Integration Gate
            (cross-feature tests)
                    │
                    ▼
            tests/otto_integration.py
            committed to main
                    │
                    ▼
            Run Summary
            (pass/fail, costs, timing)
```

## Log Files Per Task

```
otto_logs/<key>/
├── testgen-agent.log       Adversarial testgen conversation
├── tdd-check.log           TDD validation: status, pass/fail counts
├── attempt-N-agent.log     Coding agent conversation
├── attempt-N-verify.log    Verification tier results
├── attempt-N-mutation.log  Mutation check: caught/not, which line
└── timing.log              Phase durations (blackbox_context, testgen, total)
```

## Key Invariants

1. **Testgen never sees implementation** — runs in temp dir with AST stubs only
2. **Coding agent cannot modify test file** — tamper detection restores via git blob SHA
3. **Tests must fail before implementation** — TDD check enforces this
4. **Squash merge includes test files** — explicit git add after reset
5. **Pre-impl agents get stubs, post-impl get full source**
6. **All agents have max_turns limits** — prevents infinite loops
7. **Rubric gen failure aborts task creation** — no ghost tasks
8. **git clean only removes otto-created files** — pre-existing untracked files preserved
9. **Integration gate runs in worktree** — main not mutated until gate passes
10. **Judge decides test bug vs impl bug** — prevents coding agent from hallucinating workarounds
