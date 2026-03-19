# Instruction Following — Techniques for Faithful Constraint Compliance

Learned from otto spec gen agent silently weakening user constraints (e.g., "<300ms" → "cached <300ms").

## What works (ranked by impact)

### 1. System prompt over user prompt
Put non-negotiable rules in `system_prompt`, not the user prompt. Empirical benchmark: moving rules to system prompt improved compliance from 2.4/10 to 6.3/10 (163% improvement). The Agent SDK supports `system_prompt` as a string parameter on `ClaudeAgentOptions`.

### 2. Compliance self-check (generate-then-verify)
After generating output, have the agent re-read the original input and verify each constraint is preserved. Chain of Verification research: "the model's answers to fact-checking questions tended to be more accurate than facts in the original draft." The key is making verification a separate step, not inline.

```
COMPLIANCE CHECK (mandatory before output):
1. Re-read the user's task description
2. List every explicit constraint
3. For each, confirm it appears in output equally or more strict
4. If any was softened, fix it now
```

### 3. Anti-examples (show violations)
Show the model a BAD output with the exact softening pattern and explain WHY it's bad. More effective than just saying "don't soften."

```xml
<example type="violation">
User: "API response time must be under 200ms"
BAD:  "API response time should be under 200ms for cached requests"
WHY:  Added "for cached requests" — the user said ALL requests
</example>
```

### 4. Escape hatch for "unrealistic" constraints
If the agent thinks a constraint is unrealistic, it will silently soften it. Give it a safe alternative: "include the constraint verbatim AND add a separate [CONCERN] note." This prevents silent weakening while letting the agent flag concerns.

### 5. XML structure for rules
Use XML tags (`<constraint_rules>`, `<compliance_check>`, `<output_format>`) for unambiguous parsing. Anthropic recommends this pattern for complex instructions.

### 6. Context framing (explain why rules matter)
"Your output becomes the contract the coding agent must satisfy. If you weaken a requirement, the test passes but the real requirement fails in production." Providing motivation improves compliance.

## What doesn't work

- **Repeating rules more aggressively** — diminishing returns; if everything is emphasized, nothing is
- **Pre-loading context** — if the agent has tool access, it re-reads anyway; just wastes tokens
- **Longer prompts** — LLMs follow ~150-200 instructions; Claude Code system prompt uses ~50 already; every instruction degrades ALL instructions

## Sources

- [System vs User Prompts: 18-model benchmark](https://aimuse.blog/article/2025/06/14/system-prompts-versus-user-prompts-empirical-lessons-from-an-18-model-llm-benchmark-on-hard-constraints)
- [Anthropic: Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic: Claude 4 Best Practices](https://platform.claude.com/docs/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices)
- [Chain of Verification (CoVe)](https://www.rohan-paul.com/p/chain-of-verification-in-llm-evaluations)
