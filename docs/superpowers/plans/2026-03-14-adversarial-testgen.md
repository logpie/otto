# Adversarial Testgen Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace one-shot `claude -p` testgen with an adversarial testgen agent that writes black-box TDD tests from rubrics before the coding agent runs.

**Architecture:** New `build_blackbox_context()` extracts public API stubs via AST. New `run_testgen_agent()` uses Agent SDK to write tests in a temp dir. New `validate_tests()` does two-phase checking (collection + TDD invariant). Runner reordered: testgen → validate → commit tests → coding agent → tamper check → verify → squash merge.

**Tech Stack:** Python AST module, Claude Agent SDK `query()`, pytest `--collect-only`, git blob SHA for tamper detection.

**Spec:** `docs/superpowers/specs/2026-03-14-adversarial-testgen-design.md`

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `otto/testgen.py` | Black-box context builder, testgen agent, test validation | Modify (add 3 new functions) |
| `otto/runner.py` | Task execution loop — reorder for TDD flow | Modify (rewrite `run_task` testgen section) |
| `tests/test_testgen.py` | Tests for new testgen functions | Modify (add tests) |
| `tests/test_runner.py` | Tests for new runner flow | Modify (add tests) |

---

## Task 1: Build black-box context extractor

**Files:**
- Modify: `otto/testgen.py`
- Test: `tests/test_testgen.py`

- [ ] **Step 1: Write failing tests for `build_blackbox_context()`**

Add tests that verify AST-based stub extraction:

```python
class TestBuildBlackboxContext:
    def test_extracts_function_signatures(self, tmp_path):
        """Should include 'def search(query: str) -> list' but NOT function body."""
        src = tmp_path / "app.py"
        src.write_text('def search(query: str) -> list:\n    """Find items."""\n    return [x for x in items if query in x]\n')
        # init git repo
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        ctx = build_blackbox_context(tmp_path)
        assert "def search(query: str) -> list:" in ctx
        assert '"""Find items."""' in ctx
        assert "return [x for x in items" not in ctx  # body excluded

    def test_extracts_class_with_methods(self, tmp_path):
        src = tmp_path / "store.py"
        src.write_text(
            'class BookmarkStore:\n'
            '    """JSON bookmark store."""\n'
            '    def add(self, url: str, title: str) -> dict:\n'
            '        """Add a bookmark."""\n'
            '        self._bookmarks.append({"url": url})\n'
            '        return {"url": url}\n'
        )
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        ctx = build_blackbox_context(tmp_path)
        assert "class BookmarkStore:" in ctx
        assert "def add(self, url: str, title: str) -> dict:" in ctx
        assert "_bookmarks.append" not in ctx  # body excluded

    def test_includes_file_tree(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1")
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        ctx = build_blackbox_context(tmp_path)
        assert "app.py" in ctx

    def test_includes_existing_test_samples(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_app.py").write_text("import pytest\ndef test_foo(): pass\n")
        (tmp_path / "app.py").write_text("x = 1")
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        ctx = build_blackbox_context(tmp_path)
        assert "import pytest" in ctx
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_testgen.py -v -k "blackbox"`
Expected: FAIL — `build_blackbox_context` not defined

- [ ] **Step 3: Implement `build_blackbox_context()`**

Uses Python `ast` module to extract signatures + docstrings:

```python
def _extract_public_stubs(source_code: str, filename: str) -> str:
    """Extract function/class signatures + docstrings via AST. No function bodies."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return ""

    stubs = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            sig = ast.get_source_segment(source_code, node)
            if sig:
                # Extract only the def line + docstring
                lines = sig.splitlines()
                stub_lines = [lines[0]]  # def line
                # Check for docstring
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    doc = node.body[0].value.value
                    stub_lines.append(f'    """{doc}"""')
                stubs.append("\n".join(stub_lines))

        elif isinstance(node, ast.ClassDef):
            # Class signature + docstring + method signatures
            class_lines = [f"class {node.name}:"]  # simplified, add bases if needed
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)):
                class_lines.append(f'    """{node.body[0].value.value}"""')
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    method_sig = ast.get_source_segment(source_code, item)
                    if method_sig:
                        method_lines = method_sig.splitlines()
                        class_lines.append(f"    {method_lines[0]}")
                        if (item.body and isinstance(item.body[0], ast.Expr)
                                and isinstance(item.body[0].value, ast.Constant)):
                            class_lines.append(f'        """{item.body[0].value.value}"""')
            stubs.append("\n".join(class_lines))

        elif isinstance(node, ast.Assign):
            # Module-level constants
            seg = ast.get_source_segment(source_code, node)
            if seg:
                stubs.append(seg)

    return "\n\n".join(stubs)


def build_blackbox_context(project_dir: Path) -> str:
    """Build a sanitized black-box view of the project for adversarial testgen.

    Includes file tree, public API stubs (signatures + docstrings only),
    CLI help, and existing test samples. Excludes all function bodies.
    """
    sections = []

    # 1. File tree
    try:
        result = subprocess.run(
            ["git", "ls-files"], cwd=project_dir,
            capture_output=True, text=True, timeout=10,
        )
        file_tree = result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        file_tree = ""
    if file_tree:
        sections.append(f"FILE TREE:\n{file_tree}")

    # 2. Public API stubs (AST-extracted)
    stubs = []
    if file_tree:
        for line in file_tree.splitlines():
            path = Path(line.strip())
            if path.suffix != ".py":
                continue
            if any(part.startswith("test") for part in path.parts):
                continue
            if path.name in ("__init__.py", "conftest.py", "setup.py"):
                continue
            full = project_dir / path
            if not full.is_file():
                continue
            try:
                source = full.read_text()
                stub = _extract_public_stubs(source, str(path))
                if stub:
                    stubs.append(f"# {path}\n{stub}")
            except (OSError, UnicodeDecodeError):
                continue
    if stubs:
        sections.append("PUBLIC API (signatures and docstrings only):\n" + "\n\n".join(stubs))

    # 3. CLI help (best effort)
    # Detect package name from file tree
    # Try running --help
    try:
        # Find likely package dirs
        packages = set()
        for line in (file_tree or "").splitlines():
            parts = Path(line.strip()).parts
            if len(parts) >= 2 and parts[0] not in ("tests", "test", "docs"):
                packages.add(parts[0])
        for pkg in list(packages)[:2]:
            help_result = subprocess.run(
                [sys.executable, "-m", pkg, "--help"],
                cwd=project_dir, capture_output=True, text=True, timeout=10,
            )
            if help_result.returncode == 0 and help_result.stdout.strip():
                sections.append(f"CLI HELP ({pkg}):\n{help_result.stdout.strip()}")
    except Exception:
        pass

    # 4. Existing test samples
    existing_tests = _read_existing_tests(project_dir)
    if existing_tests:
        sections.append(f"EXISTING TESTS (for import patterns and fixtures):\n{existing_tests}")

    return "\n\n".join(sections)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_testgen.py -v -k "blackbox"`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add otto/testgen.py tests/test_testgen.py
git commit -m "feat: add build_blackbox_context with AST-based stub extraction"
```

---

## Task 2: Implement testgen agent via Agent SDK

**Files:**
- Modify: `otto/testgen.py`
- Test: `tests/test_testgen.py`

- [ ] **Step 1: Write failing test for `run_testgen_agent()`**

```python
class TestRunTestgenAgent:
    @patch("otto.testgen.query")
    @patch("otto.testgen.ClaudeAgentOptions")
    def test_writes_test_file(self, mock_opts, mock_query, tmp_path):
        """Agent should write a test file to the temp dir."""
        test_file = tmp_path / "tests" / "otto_verify_abc123.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)

        # Mock agent writing a file
        async def fake_query(*, prompt, options=None):
            test_file.write_text("import pytest\n\ndef test_search():\n    assert False\n")
            from otto._agent_stub import ResultMessage
            yield ResultMessage(session_id="s1")

        mock_query.side_effect = fake_query

        result = run_testgen_agent(
            rubric=["search is case-insensitive"],
            key="abc123",
            blackbox_context="FILE TREE:\napp.py",
            project_dir=tmp_path,
            framework="pytest",
        )
        assert result is not None
        assert result.exists()
        assert "def test_search" in result.read_text()
```

- [ ] **Step 2: Run test, verify it fails**

- [ ] **Step 3: Implement `run_testgen_agent()`**

```python
async def run_testgen_agent(
    rubric: list[str],
    key: str,
    blackbox_context: str,
    project_dir: Path,
    framework: str,
) -> Path | None:
    """Run adversarial testgen agent. Writes test file to project tests dir.

    Agent works from black-box context only — no direct repo access.
    Returns path to generated test file, or None on failure.
    """
    rubric_text = "\n".join(f"{i+1}. {item}" for i, item in enumerate(rubric))
    test_filename = f"otto_verify_{key}.py"
    test_rel_path = f"tests/{test_filename}"

    agent_prompt = f"""You are a QA engineer writing black-box tests from a specification.
You have NOT seen the implementation — it hasn't been written yet.
Your job is to write tests that will CATCH BUGS, not confirm correctness.

SPEC (acceptance criteria):
{rubric_text}

PROJECT CONTEXT (public interface only):
{blackbox_context}

Write a complete {framework} test file at: {test_rel_path}

Your tests MUST:
- Test the public interface only (CLI via subprocess, library via imports)
- Be designed to FAIL on the current codebase (the feature doesn't exist yet)
- Be independent and hermetic (use tmp_path, no shared state)
- Use subprocess.run() for CLI testing, not CliRunner
- Include negative tests (what should NOT happen)

Write the test file now. Do NOT explain — just write the file."""

    test_dir = project_dir / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = project_dir / test_rel_path

    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
        )

        async for message in query(prompt=agent_prompt, options=agent_opts):
            # Stream output for visibility
            if AssistantMessage and isinstance(message, AssistantMessage):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        print(block.text, flush=True)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        _print_tool_use(block)
    except Exception as e:
        print(f"  testgen agent error: {e}", flush=True)
        return None

    if test_file.exists() and test_file.stat().st_size > 0:
        return test_file
    return None
```

Note: imports `_print_tool_use` from runner or duplicates it. Simplest: import from runner.

- [ ] **Step 4: Run tests, verify they pass**

- [ ] **Step 5: Commit**

```bash
git add otto/testgen.py tests/test_testgen.py
git commit -m "feat: add run_testgen_agent using Agent SDK"
```

---

## Task 3: Two-phase test validation

**Files:**
- Modify: `otto/testgen.py`
- Test: `tests/test_testgen.py`

- [ ] **Step 1: Write failing tests for `validate_generated_tests()`**

```python
class TestValidateGeneratedTests:
    def test_collection_failure(self, tmp_path):
        """Syntax error in test file should return 'collection_error'."""
        test_file = tmp_path / "test_bad.py"
        test_file.write_text("def test_foo(:\n    pass\n")  # syntax error
        result = validate_generated_tests(test_file, "pytest", tmp_path)
        assert result.status == "collection_error"

    def test_all_fail(self, tmp_path):
        """All tests failing = TDD invariant holds."""
        test_file = tmp_path / "test_good.py"
        test_file.write_text("def test_a():\n    assert False\ndef test_b():\n    assert False\n")
        result = validate_generated_tests(test_file, "pytest", tmp_path)
        assert result.status == "tdd_ok"
        assert result.failed > 0
        assert result.passed == 0

    def test_all_pass(self, tmp_path):
        """All tests passing = tests are trivial."""
        test_file = tmp_path / "test_trivial.py"
        test_file.write_text("def test_a():\n    assert True\ndef test_b():\n    assert True\n")
        result = validate_generated_tests(test_file, "pytest", tmp_path)
        assert result.status == "all_pass"

    def test_mixed(self, tmp_path):
        """Some pass, some fail = acceptable."""
        test_file = tmp_path / "test_mixed.py"
        test_file.write_text("def test_pass():\n    assert True\ndef test_fail():\n    assert False\n")
        result = validate_generated_tests(test_file, "pytest", tmp_path)
        assert result.status == "tdd_ok"
```

- [ ] **Step 2: Run tests, verify they fail**

- [ ] **Step 3: Implement `validate_generated_tests()`**

```python
@dataclass
class TestValidationResult:
    status: str  # "tdd_ok", "all_pass", "collection_error", "no_tests"
    passed: int = 0
    failed: int = 0
    error_output: str = ""


def validate_generated_tests(
    test_file: Path,
    framework: str,
    project_dir: Path,
    timeout: int = 60,
) -> TestValidationResult:
    """Two-phase validation of generated tests."""
    from otto.verify import _subprocess_env

    # Phase A: Collection check
    collect = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(test_file)],
        cwd=project_dir, capture_output=True, text=True,
        timeout=30, env=_subprocess_env(),
    )
    if collect.returncode != 0:
        return TestValidationResult(
            status="collection_error",
            error_output=collect.stdout + collect.stderr,
        )

    # Phase B: Run tests
    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-v"],
        cwd=project_dir, capture_output=True, text=True,
        timeout=timeout, env=_subprocess_env(),
    )

    # Parse results
    passed = run.stdout.count(" PASSED")
    failed = run.stdout.count(" FAILED") + run.stdout.count(" ERROR")
    total = passed + failed

    if total == 0:
        return TestValidationResult(status="no_tests", error_output=run.stdout + run.stderr)
    elif passed == total:
        return TestValidationResult(status="all_pass", passed=passed, failed=0)
    else:
        return TestValidationResult(status="tdd_ok", passed=passed, failed=failed)
```

- [ ] **Step 4: Run tests, verify they pass**

- [ ] **Step 5: Commit**

```bash
git add otto/testgen.py tests/test_testgen.py
git commit -m "feat: add two-phase test validation (collection + TDD check)"
```

---

## Task 4: Rewire runner for adversarial flow

**Files:**
- Modify: `otto/runner.py`
- Test: `tests/test_runner.py`

This is the biggest task — reorders `run_task()` to: testgen → validate → commit → coding agent → tamper check → verify → squash.

- [ ] **Step 1: Write test for tamper detection**

```python
class TestTamperDetection:
    def test_detects_modified_test_file(self, tmp_git_repo):
        test_file = tmp_git_repo / "tests" / "otto_verify_abc.py"
        test_file.parent.mkdir(exist_ok=True)
        test_file.write_text("def test_a(): assert False\n")
        subprocess.run(["git", "add", "."], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "tests"], cwd=tmp_git_repo, capture_output=True)

        # Record SHA
        sha = subprocess.run(
            ["git", "hash-object", str(test_file)],
            capture_output=True, text=True,
        ).stdout.strip()

        # Modify the file
        test_file.write_text("def test_a(): assert True\n")

        # Check should detect tampering
        current_sha = subprocess.run(
            ["git", "hash-object", str(test_file)],
            capture_output=True, text=True,
        ).stdout.strip()
        assert current_sha != sha
```

- [ ] **Step 2: Implement the new `run_task()` flow**

Key changes to `run_task()` in `otto/runner.py`:

```python
# Before the attempt loop, replace concurrent testgen with:

# 1. Build black-box context
if rubric:
    from otto.testgen import build_blackbox_context, run_testgen_agent, validate_generated_tests
    print(f"  {_DIM}Building black-box context...{_RESET}", flush=True)
    blackbox_ctx = build_blackbox_context(project_dir)

    # 2. Run testgen agent
    print(f"  {_DIM}Testgen agent writing tests from rubric ({len(rubric)} criteria)...{_RESET}", flush=True)
    test_file_path = await asyncio.to_thread(
        lambda: asyncio.run(run_testgen_agent(rubric, key, blackbox_ctx, project_dir, framework))
    )

    if test_file_path:
        # 3. Validate tests (two-phase)
        validation = validate_generated_tests(test_file_path, framework, project_dir)

        if validation.status == "collection_error":
            _log_warn(f"Generated tests have errors — regenerating")
            # Regenerate once with error feedback
            # ... (retry with error in prompt)

        elif validation.status == "all_pass":
            print(f"\n  {_YELLOW}{_BOLD}⚠⚠⚠ WARNING: All rubric tests PASS before implementation{_RESET}", flush=True)
            # Regenerate once, then warn and skip

        elif validation.status == "tdd_ok":
            print(f"  {_GREEN}✓{_RESET} {_DIM}Rubric tests ready ({validation.failed} failing, {validation.passed} passing){_RESET}", flush=True)

        # 4. Commit test file
        subprocess.run(["git", "add", str(test_file_path)], cwd=project_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"otto: add rubric tests for task #{task_id}"],
            cwd=project_dir, capture_output=True,
        )
        test_commit_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project_dir,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Record SHA for tamper detection
        test_file_sha = subprocess.run(
            ["git", "hash-object", str(test_file_path)],
            capture_output=True, text=True,
        ).stdout.strip()

# In the attempt loop, add tamper check before verification:
# ... after agent runs, before build_candidate_commit ...

if test_file_sha:
    current = subprocess.run(
        ["git", "hash-object", str(test_file_path)],
        capture_output=True, text=True,
    ).stdout.strip()
    if current != test_file_sha:
        subprocess.run(["git", "checkout", test_commit_sha, "--", str(test_file_path)],
                       cwd=project_dir, capture_output=True)
        print(f"  {_YELLOW}⚠ Test file was modified by coding agent — restored{_RESET}", flush=True)

# In the coding agent prompt, add:
# "Do NOT modify tests/otto_verify_{key}.py — these are acceptance tests you must pass."

# On retry, reset to test commit instead of base:
# git reset --mixed <test_commit_sha>

# On merge success, squash:
# git reset --mixed <base_sha> then stage all + commit
```

- [ ] **Step 3: Run full test suite**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/ -x -q`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add otto/runner.py tests/test_runner.py
git commit -m "feat: rewire runner for adversarial testgen flow"
```

---

## Task 5: Integration test and end-to-end verification

**Files:**
- Modify: `tests/test_integration.py`

- [ ] **Step 1: Update integration test for new flow**

Update `TestRubricEndToEnd` to verify:
- `run_testgen_agent` is called (not `generate_tests_from_rubric`)
- `build_blackbox_context` is called
- Test file is committed before coding agent runs
- Tamper detection works

- [ ] **Step 2: Run full test suite**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/ -x -q`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: update integration tests for adversarial testgen flow"
```

---

## After All Tasks

Run the full test suite, then test with the bookmarks demo:

```bash
cd /tmp/bookmarks
otto reset --yes
cat > features.md << 'EOF'
# Search
Users should be able to search bookmarks by title or URL.
Search should be case-insensitive and support partial matches.

# Favorites
Add a way to mark bookmarks as favorites.
Should be able to filter to show only favorites.
EOF
otto add -f features.md
unset CLAUDECODE
otto run
# Verify: testgen agent runs BEFORE coding agent
# Verify: tests fail before implementation, pass after
# Verify: test file not modified by coding agent
```

Then use `superpowers:finishing-a-development-branch` to complete the work.
