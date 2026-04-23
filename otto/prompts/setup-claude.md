Write a `CLAUDE.md` for a coding agent working on this project.

Output ONLY the markdown content for `CLAUDE.md`.
No explanation, no preamble, no code fences, no tool use.

`CLAUDE.md` should tell the agent what it needs to know to work effectively here.
Include build/test commands, project structure, key conventions, and anything
that would cause mistakes without guidance.

If a non-trivial `intent.md` section is provided below, treat it as the
authoritative product description for this repository. Make the guidance specific
to that product instead of generic early-stage filler. Synthesize the intent into
practical instructions; do not paste long chunks verbatim.

Project files:
{project_context}

{project_intent_section}
{merge_section}

Guidelines:
- Point to files/directories rather than inlining specifics that go stale.
- Avoid counts, version numbers, or facts that change frequently.
- Keep it concise. The agent reads code well; just orient it.
- Include these principles if relevant to the project:
  - Check for existing patterns before writing new code
  - After changing a shared type/interface, check all consumers
  - Fix root causes, not workarounds or special cases
