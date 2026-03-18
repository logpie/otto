# Research: Reliable Code Output from LLMs for Test Generation

**Date**: 2026-03-14
**Context**: Otto uses `claude -p` (Claude Code CLI in print mode) to generate pytest test files from rubric criteria. The LLM frequently returns prose/explanations instead of code, or wraps output in markdown fences, or tries to "write files" instead of outputting code.

---

## 1. How Existing Tools Solve This Problem

### Qodo-Cover (formerly CodiumAI Cover-Agent)

**Architecture**: Prompt Builder -> AI Caller (LiteLLM) -> YAML Response Parser -> Test Validator -> Coverage Checker

**Key technique**: They ask the LLM to output a **YAML structure** (not raw code), with each test in a structured field:
- `test_name`: name of the test
- `test_code`: the actual code
- `new_imports_code`: required imports

The YAML output maps to a Pydantic `NewTests` model, which gives them structured extraction. They parse the YAML, extract the `test_code` field, and validate by **actually running the tests** against the codebase and checking coverage.

**Failed test feedback loop**: When a test fails or doesn't increase coverage, they feed the failure back to the LLM prompt as a "Failed Tests" section, so it doesn't regenerate the same broken test.

**Source**: [qodo-cover prompt template](https://github.com/qodo-ai/qodo-cover/blob/main/cover_agent/settings/test_generation_prompt.toml)

### Aider

**Approach**: Multiple pluggable "edit formats" — each format uses explicit delimiters to separate code from prose:
- **Search/Replace blocks**: `<<<<<<< SEARCH` / `>>>>>>> REPLACE` markers
- **Unified diff format**: Modified `diff -U0` style
- **Whole-file**: Complete file in a fenced code block with filename

**Key insight**: Aider doesn't try to prevent prose — it uses **structural markers** to delineate code regions, then extracts only what's between the markers. Prose outside markers is ignored.

**Error recovery**: Layered matching — exact match first, then whitespace-insensitive, then fuzzy (Levenshtein distance).

**Source**: [Aider edit formats](https://aider.chat/docs/more/edit-formats.html)

### Codex (OpenAI)

Uses structured patch format with clear boundaries: `*** Begin Patch` / `*** End Patch`. Context lines for targeting. Avoids line numbers.

### General Pattern Across Tools

All successful tools converge on the same principle: **don't fight the LLM's tendency to explain — instead, provide structural markers and extract code from within them**.

---

## 2. Techniques for Forcing Code-Only Output

### Technique A: `--json-schema` Flag (RECOMMENDED — Best Fit for Otto)

Claude Code CLI supports `--json-schema` in print mode. This uses **constrained decoding** (grammar-based token restriction) to guarantee the output matches a JSON schema.

**How it works for code generation**:

```bash
claude -p "Generate pytest tests for..." \
  --output-format json \
  --json-schema '{
    "type": "object",
    "properties": {
      "test_code": {"type": "string"}
    },
    "required": ["test_code"],
    "additionalProperties": false
  }'
```

Then extract with:
```bash
... | jq -r '.structured_output.test_code'
```

**Advantages**:
- **Guaranteed valid JSON** — no parsing failures, no regex needed
- Code is cleanly in a string field — no markdown fences, no prose contamination
- Works with current Claude models (Opus 4.6, Sonnet 4.6)
- No retry needed for format compliance (only for code quality)

**Disadvantages**:
- Code in a JSON string means escaped newlines/quotes — need to deserialize
- First request with a new schema has extra latency
- Cannot use extended thinking when forcing tool use

**Example for Otto**:
```python
import json
import subprocess

schema = json.dumps({
    "type": "object",
    "properties": {
        "test_code": {
            "type": "string",
            "description": "Complete, valid pytest test code starting with import statements"
        }
    },
    "required": ["test_code"],
    "additionalProperties": False
})

result = subprocess.run(
    ["claude", "-p", "--output-format", "json", "--json-schema", schema],
    input=prompt,
    capture_output=True, text=True, timeout=180,
)

data = json.loads(result.stdout)
code = data["structured_output"]["test_code"]
# code is guaranteed to be a string, no fences, no prose wrapper
```

### Technique B: System Prompt Replacement (Current approach, can be improved)

Otto currently uses `--output-format text` with prompt instructions like "Output ONLY valid pytest test code." This is the weakest approach — the LLM often ignores these instructions.

**Improvements to current approach**:
1. Use `--system-prompt` to REPLACE the entire system prompt (removes Claude Code's default agentic instructions that encourage explanation):
   ```bash
   claude -p --system-prompt "You are a code generator. Output ONLY executable Python code. Never include explanations, markdown, or commentary." "Write pytest tests for..."
   ```
2. The default Claude Code system prompt includes instructions about being helpful, explaining reasoning, etc. — **this conflicts with code-only output**. Replacing it removes that conflict.

### Technique C: Prefill (DEPRECATED — Do Not Use)

Prefilling the assistant response (e.g., starting with `import pytest`) was a classic technique but is **deprecated and returns 400 errors** on Claude Opus 4.6, Sonnet 4.6, and Sonnet 4.5. Not viable.

**Source**: [Prefill deprecation guide](https://blog.laozhang.ai/en/posts/claude-opus-prefill-error-fix)

### Technique D: Tool Use as Structured Output (API-level alternative)

For direct API usage (not CLI), you can define a "fake tool" whose input schema matches your desired output, then force it with `tool_choice`:

```python
response = client.messages.create(
    model="claude-opus-4-6",
    messages=[{"role": "user", "content": prompt}],
    tools=[{
        "name": "submit_test_code",
        "description": "Submit the generated test code",
        "input_schema": {
            "type": "object",
            "properties": {
                "test_code": {"type": "string"}
            },
            "required": ["test_code"]
        }
    }],
    tool_choice={"type": "tool", "name": "submit_test_code"},
)
# Extract from tool use block
code = response.content[0].input["test_code"]
```

**Limitation**: Cannot use extended thinking when `tool_choice` forces a specific tool.

### Technique E: JSON Outputs Mode (API-level, production recommended)

```python
from pydantic import BaseModel

class TestOutput(BaseModel):
    test_code: str

response = client.messages.parse(
    model="claude-opus-4-6",
    max_tokens=4096,
    output_format=TestOutput,
    messages=[{"role": "user", "content": prompt}],
)
code = response.parsed_output.test_code
```

Uses constrained decoding at the API level. Same as `--json-schema` but via Python SDK.

### Technique F: Post-Processing with Regex (Fallback)

Otto already does this — extract from markdown fences with `re.search(r"```(?:\w*)\n(.*?)```", output, re.DOTALL)`. This should be the **fallback**, not the primary strategy.

---

## 3. Recommended Changes for Otto

### Priority 1: Use `--json-schema` (High impact, moderate effort)

Replace the current `_call_and_validate` approach:

```python
def _call_and_validate(prompt: str, framework: str) -> tuple[str | None, str | None]:
    schema = json.dumps({
        "type": "object",
        "properties": {
            "test_code": {
                "type": "string",
                "description": (
                    f"Complete, valid {framework} test file content, "
                    "starting with import statements. "
                    "No markdown, no prose, no explanations."
                )
            }
        },
        "required": ["test_code"],
        "additionalProperties": False
    })

    result = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--json-schema", schema],
        input=prompt,
        capture_output=True, text=True, timeout=TESTGEN_TIMEOUT,
        start_new_session=True,
    )

    if result.returncode != 0:
        return None, None

    try:
        data = json.loads(result.stdout)
        code = data.get("structured_output", {}).get("test_code", "")
    except (json.JSONDecodeError, KeyError):
        return None, result.stdout

    if _validate_test_output(code, framework):
        return code, None
    return None, code
```

This **eliminates** the markdown fence problem entirely. The code arrives in a JSON string field — clean, no fences, no prose wrapper.

### Priority 2: Use `--system-prompt` to Replace Default Prompt

The default Claude Code system prompt has instructions about being helpful and explaining things. Replace it for testgen calls:

```python
result = subprocess.run(
    ["claude", "-p",
     "--system-prompt", f"You are a {framework} test code generator. Output only valid test code.",
     "--output-format", "json",
     "--json-schema", schema],
    input=prompt,
    ...
)
```

### Priority 3: Separate Schema Fields for Better Structure

Instead of one big `test_code` string, break it into parts (like qodo-cover does):

```json
{
    "type": "object",
    "properties": {
        "imports": {
            "type": "string",
            "description": "All import statements needed"
        },
        "fixtures": {
            "type": "string",
            "description": "Pytest fixtures and setup code (empty string if none needed)"
        },
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "code": {"type": "string"}
                },
                "required": ["name", "code"]
            }
        }
    },
    "required": ["imports", "tests"],
    "additionalProperties": false
}
```

This gives you per-test granularity — you can validate each test individually, map tests to rubric items, and retry individual failing tests.

### Priority 4: Retry with Feedback (Keep, but enhance)

The current retry mechanism is good. Enhance it:
- On structured output failure, fall back to text mode with stronger instructions
- Include the specific validation error in the retry prompt
- Consider reducing max_retries to 1 since structured output rarely needs retry for format

---

## 4. Key Insight: The "Prose Problem" Has a Clean Solution

The root cause of the "LLM outputs prose instead of code" problem is:

1. **The default Claude Code system prompt** encourages explanation and helpfulness
2. **Free-form text output** gives the LLM no structural constraint
3. **Prompt instructions alone** ("output ONLY code") are weak guardrails

The solution stack (in order of reliability):
1. **Constrained decoding** (`--json-schema`) — grammatically impossible to output prose outside the schema
2. **System prompt replacement** (`--system-prompt`) — removes competing instructions
3. **Structural markers** (XML tags, YAML keys) — gives extraction points even if prose leaks
4. **Post-processing** (regex, AST parse) — catches remaining edge cases
5. **Validation + retry** — catches code quality issues

Otto currently uses only layers 4 and 5. Adding layers 1-2 should dramatically reduce retry rates.

---

## 5. Other Approaches Worth Noting

### egghead.io / Gomplate Pipeline Pattern

A community workflow chains: template engine (inject context) -> `claude -p` -> TypeScript validator:
```bash
gomplate -f prompt.txt | claude -p | bun run validate.ts
```

**Key quote**: "If you only say output JSON, it still tries to respond in markdown. You have to be explicit: 'Output _only_ the JSON, nothing else. Don't even output the JSON markdown codefence.'"

This confirms that prompt-only instructions are unreliable — structural enforcement is needed.

### AlphaCodium / QodoFlow (Iterative Refinement)

Qodo's research on code contests uses a multi-stage flow:
1. Generate initial code
2. Run against test cases
3. Feed failures back to LLM
4. Iterate until tests pass

This is essentially what Otto's retry mechanism does, but AlphaCodium is more systematic about it — each iteration includes structured feedback about which tests failed and why.

---

## 6. Claude CLI Flags Reference (for Otto)

| Flag | Purpose | Example |
|------|---------|---------|
| `--system-prompt` | Replace entire system prompt | `--system-prompt "You are a code generator"` |
| `--append-system-prompt` | Add to default prompt | `--append-system-prompt "Output only code"` |
| `--output-format json` | Get JSON with metadata | `--output-format json` |
| `--json-schema` | Force output to match schema | `--json-schema '{"type":"object",...}'` |
| `--max-turns` | Limit agent loop iterations | `--max-turns 1` |
| `--allowedTools` | Control which tools can be used | `--allowedTools "Read"` |
| `--tools` | Restrict available tools | `--tools ""` (disable all tools) |

**Note on `--tools ""`**: For pure code generation (no file reading needed), disabling all tools prevents Claude from trying to "write files" instead of outputting code. This is relevant to the "tries to write files" problem Otto encounters.

---

## Sources

- [Claude CLI Reference](https://code.claude.com/docs/en/cli-reference) — `--json-schema`, `--system-prompt`, `--output-format` flags
- [Claude Structured Outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) — JSON mode, strict tool use
- [Claude Agent SDK Structured Outputs](https://platform.claude.com/docs/en/agent-sdk/structured-outputs) — `outputFormat` in programmatic usage
- [Run Claude Code Programmatically](https://code.claude.com/docs/en/headless) — CLI print mode examples
- [Prefill Deprecation](https://blog.laozhang.ai/en/posts/claude-opus-prefill-error-fix) — migration guide
- [Increase Output Consistency](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/increase-consistency) — format control techniques
- [Qodo-Cover](https://github.com/qodo-ai/qodo-cover) — YAML-based test extraction
- [Aider Edit Formats](https://aider.chat/docs/more/edit-formats.html) — structural markers for code extraction
- [AI Code Extraction Techniques](https://fabianhertwig.com/blog/coding-assistants-file-edits/) — how coding assistants parse LLM output
- [Enforcing Structured Output in Claude Code](https://egghead.io/enforcing-structured-output-with-json-schema-and-zod-in-claude-code-workflows~fm674) — practical workflow patterns
- [Qodo AI Review](https://aiproductivity.ai/tools/qodo/) — overview of Qodo's approach
- [AlphaCodium / QodoFlow](https://www.qodo.ai/blog/qodoflow-state-of-the-art-code-generation-for-code-contests/) — iterative code generation
