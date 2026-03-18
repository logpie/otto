# Plan: Strengthen Rubric Gen & Testgen Pipeline (Approach B)

## Context

Otto's rubric generation and test generation are the two most critical components — they determine whether autonomous output matches user intent. Both are already agentic (Agent SDK), but lack quality verification: no one checks that rubrics are testable, no one checks that tests actually cover the rubric, and mutation testing uses naive line-commenting. This plan adds compiler-powered quality gates + targeted LLM refinement to close these gaps.

**Research basis:** Meta ACH (mutation-guided testgen, prod at Facebook), Promptfoo (structured scoring, acquired by OpenAI 2026-03-09), Scale AI Agentic Rubrics (structured criteria), mutmut/cosmic-ray (AST mutation testing), coverage.py. Full research in `research-testgen-sota.md`.

**Key insight from research:** Otto's rubric-first + adversarial-testgen-before-code architecture is novel — no other system does this. Most use hidden tests (SWE-bench), LLM-as-judge without execution (Scale AI), or just the user's existing suite (Aider, Copilot). The gap is quality verification of the generated rubrics and tests themselves.

## Approach: Multi-Pass LLM Pipeline + AST Compiler Gates

8 components across 4 ships. Each ship is independently valuable. Self-critique is subsumed into the existing agentic rubric run (no extra LLM call). Downstream mechanical checks (AST smells, mutations, traceability) catch what self-bias misses.

---

## Ship 1: Quality Gates (no breaking changes)

### 1A. Rubric Agent Self-Critique
**File:** `otto/rubric.py` — prompt-only change in `_run_rubric_agent()` (line 54-87)

Add self-critique instructions before the "Write ONLY..." line:
```
SELF-CRITIQUE before finalizing:
Review each criterion you drafted:
1. Is it testable? Can you write a pytest that checks pass/fail? If not, rewrite.
2. Is it specific? "Works correctly" is bad. "Returns sorted results by score descending" is good.
3. Is it behavioral? Describes what USER sees, not HOW it's implemented.
4. Does it have a clear pass/fail signal?
Remove or rewrite any criterion that fails these checks.
```

No code changes, no ripple effects. Existing tests pass unchanged.

**Verify:** Run rubric gen on a sample task, compare specificity before/after.

### 1B. AST Test Smell Detector
**New file:** `otto/test_quality.py` (~70 lines)

```python
@dataclass
class TestSmell:
    test_name: str
    smell_type: str  # "no_assertion", "trivial_assertion", "assertion_roulette", "empty_body"
    description: str
    line: int

def detect_test_smells(test_file: Path) -> list[TestSmell]:
    """ast.NodeVisitor that catches: no assertions, assert True, 10+ asserts, empty body."""
```

Smells detected:
- No assertions in test function
- Trivial assertions (`assert True`, `assert 1 == 1`)
- Assertion roulette (10+ asserts in one test)
- Empty body (`pass` / `...`)

**Integration:** `otto/runner.py` — after `validate_generated_tests()` returns `tdd_ok`, run smell detection. If smells found, feed back to testgen for one retry.

**Tests:** `tests/test_test_quality.py` — one test per smell type + clean test returns empty.

### 1C. AST Semantic Mutation Engine
**File:** `otto/testgen.py` — replace `run_mutation_check()` (lines 829-921)

```python
class SemanticMutator(ast.NodeTransformer):
    """Apply a single semantic mutation."""

@dataclass
class MutationResult:
    operator: str       # "swap_eq", "negate_if", etc.
    description: str
    killed: bool
    line: int

@dataclass
class MutationReport:
    total: int
    killed: int
    survived: int
    results: list[MutationResult]
    kill_rate: float  # property
```

Mutation operators (all via `ast.NodeTransformer`):

| Operator | Transform |
|----------|-----------|
| `swap_eq` | `==` ↔ `!=` |
| `swap_lt` | `<` ↔ `<=` |
| `swap_gt` | `>` ↔ `>=` |
| `swap_add` | `+` ↔ `-` |
| `swap_mul` | `*` ↔ `/` |
| `swap_and` | `and` ↔ `or` |
| `negate_if` | `if x` → `if not x` |
| `swap_bool` | `True` ↔ `False` |
| `remove_return` | `return x` → `return None` |

Algorithm: Parse target file → collect mutable nodes → sample up to 5 → apply one mutation at a time → run tests → restore → report kill rate.

**Backward compatible:** `run_mutation_check()` keeps same signature `(project_dir, test_file, test_command, timeout) -> (bool, str)`. Bool now means "kill rate >= 60%".

**Verify:** `pytest tests/test_testgen.py -k mutation` — existing tests pass with updated expectations.

---

## Ship 2: Structured Rubric (breaking data model, backward compatible)

### 2. Structured Rubric Output
**File:** `otto/rubric.py`

```python
@dataclass
class RubricCriterion:
    id: str         # "RB-001" (auto-assigned after parsing)
    text: str
    weight: float   # 1.0 normal, 2.0 critical, 0.5 nice-to-have
    category: str   # happy_path, error_handling, negative, edge_case, regression
```

Changes:
- `generate_rubric()` returns `list[RubricCriterion]` (was `list[str]`)
- Agent prompt requests JSON array output
- New `_parse_rubric_json()` parser; keep `_parse_rubric_output()` as fallback for malformed output
- New `normalize_rubric(raw: list) -> list[RubricCriterion]` — converts legacy `list[str]` or new `list[dict]`

**Ripple effects (all must ship atomically):**

| File | Change | Lines |
|------|--------|-------|
| `otto/tasks.py` | `add_task()` rubric param: `list[dict] \| None` | ~3 |
| `otto/runner.py` | normalize rubric, adapt task_hint construction, testgen call | ~10 |
| `otto/testgen.py` | adapt `run_testgen_agent()` rubric formatting, include IDs in prompt | ~15 |
| `otto/cli.py` | display `[RB-001] criterion text (w=2.0)` in status/show | ~20 |

**Backward compat:** `normalize_rubric()` detects string items (legacy) vs dict items (new) and converts. Old tasks.yaml files work unchanged.

**Verify:** `pytest` full suite passes. `otto add "test task"` produces structured rubric. `otto status` displays correctly.

---

## Ship 3: Traceability + Scoring (depends on Ship 2)

### 5. Rubric-Test Traceability
**File:** `otto/test_quality.py` (add functions), `otto/testgen.py` (prompt change), `otto/runner.py` (wiring)

```python
def extract_test_coverage_tags(test_file: Path) -> dict[str, list[str]]:
    """Parse 'Covers: RB-001, RB-003' from test function docstrings."""

def build_traceability_matrix(rubric, coverage_tags) -> dict[str, list[str]]:
    """Map criterion IDs to covering test functions."""

def find_coverage_gaps(matrix) -> list[str]:
    """Return criterion IDs with no covering test."""
```

Testgen agent prompt adds: "Each test function MUST include docstring starting with `Covers: RB-XXX`."

**Integration:** After testgen validation, check traceability. If gaps found, feed gap criteria back to testgen for targeted generation.

### 8. Structured Scoring (Promptfoo-inspired)
**File:** `otto/verify.py` (new dataclasses), `otto/runner.py` (wiring)

```python
@dataclass
class CriterionResult:
    criterion_id: str
    passed: bool
    score: float      # 0.0-1.0
    reason: str

@dataclass
class ScoredVerifyResult:
    criteria_results: list[CriterionResult]
    weighted_score: float   # sum(w*s) / sum(w)
    critical_all_passed: bool
    passed: bool            # weighted_score >= threshold AND critical_all_passed
```

Scoring: Map test pass/fail → criteria via traceability matrix. Critical criteria (weight >= 2.0) must all pass. Overall weighted score must meet threshold (default 0.8).

**Verify:** Add scoring tests, run full `otto run` on demo project with structured rubric.

---

## Ship 4: Advanced Signals (optional, highest complexity)

### 7. Coverage-Guided Feedback
**File:** `otto/test_quality.py`, `otto/runner.py`. **New dep:** `coverage` (optional).

Run via subprocess: `python -m coverage run -m pytest test_file && python -m coverage json`. Parse JSON output. Feed untested branches back to testgen.

Make `coverage` optional: `try: import coverage` at runtime. Skip with warning if not installed.

### 6. Pre-Implementation Inverse Mutation
**File:** `otto/testgen.py`, `otto/runner.py`.

Lightweight `claude -p` call synthesizes 2-3 bad implementations per rubric (hardcoded return, missing validation, off-by-one). Write to stub file, run tests, restore. If tests don't catch bad impl → weak tests warning.

**Opt-in** via `otto.yaml`: `inverse_mutation: true`. Default off.

---

## Critical Files

| File | Ships | Role |
|------|-------|------|
| `otto/rubric.py` | 1A, 2 | RubricCriterion dataclass, self-critique prompt, structured output |
| `otto/testgen.py` | 1C, 2, 5, 6 | Semantic mutation engine, rubric format adapt, traceability tags, inverse mutation |
| `otto/test_quality.py` (NEW) | 1B, 5, 7 | Test smell detector, traceability matrix, coverage analysis |
| `otto/runner.py` | 1B, 2, 5, 7, 8 | Wire in all quality gates |
| `otto/verify.py` | 8 | Structured scoring dataclasses |
| `otto/cli.py` | 2 | Display formatting for structured rubric |

## Existing Code to Reuse

- `otto/testgen.py:_extract_public_stubs()` — AST stub extraction, pattern for new AST visitors
- `otto/testgen.py:_build_project_index()` — symbol index + import graph, reuse for mutation target selection
- `otto/testgen.py:_subprocess_env()` — env setup for running pytest
- `otto/testgen.py:validate_generated_tests()` — two-phase validation, extend with smell detection
- `otto/rubric.py:_parse_rubric_output()` — text parser, keep as fallback for JSON parse failure

## Risks

1. **Rubric JSON parsing reliability** — LLM may produce malformed JSON. Mitigated by text-parser fallback.
2. **Mutation engine latency** — 5 mutations x test runtime. Mitigated by `max_mutations` config, skip if tests >30s.
3. **Traceability tag compliance** — Agent may not tag all tests. Mitigated by graceful degradation (skip traceability if <50% tagged).
4. **Coverage dep** — Optional, runtime-checked. No hard dependency.
5. **Inverse mutation cost** — Opt-in only. Default off.

## Verification (end-to-end)

After each ship:
1. `pytest` — full suite passes (160+ tests)
2. `otto add "Add search to bookmarks"` on demo project — verify rubric quality
3. `otto run` on demo project — verify testgen quality, traceability, scoring output
4. Compare mutation kill rate before/after (Ship 1C)
5. Compare rubric specificity before/after (Ship 1A)
