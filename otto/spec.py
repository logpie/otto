"""Spec-gate: generate a reviewable product spec before otto builds.

Flow:
  run_spec_agent()  — agent writes spec.md given an intent
  validate_spec()   — enforce required sections (Intent, Must Have, Must NOT
                      Have Yet, Success Criteria) and size cap
  read_spec_file()  — parse an externally-supplied spec (`--spec-file`)
  review_spec()     — interactive TTY gate (approve / edit / regenerate / quit)
  format_spec_section() — wrap spec content for injection into build/certifier
                          prompts, sanitizing delimiter tokens
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from otto.budget import RunBudget

logger = logging.getLogger("otto.spec")

# Required section headings (exact match after stripping whitespace). The
# spec prompt is constrained to produce these — validation catches drift.
_REQUIRED_HEADINGS = (
    "## Must Have",
    "## Must NOT Have Yet",
    "## Success Criteria",
)

_INTENT_LINE = re.compile(r"^\s*\*\*Intent:\*\*\s+(?P<value>\S.*?)\s*$", re.MULTILINE)
_BULLET_LINE = re.compile(r"^(?:[-*]|\d+\.)\s+(.*)")


@dataclass
class SpecResult:
    """Outcome of a spec generation or load."""
    path: Path
    content: str
    open_questions: int
    cost: float
    duration_s: float
    version: int = 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_spec(content: str) -> list[str]:
    """Return a list of human-readable validation errors. Empty list = valid.

    A valid spec has:
      - a single `**Intent:**` line with a non-empty value
      - all required `##` headings (Must Have, Must NOT Have Yet, Success
        Criteria)
      - non-empty content
    """
    errors: list[str] = []

    if not content.strip():
        errors.append("spec is empty")
        return errors

    intents = _INTENT_LINE.findall(content)
    if not intents:
        errors.append("missing `**Intent:** <one line>` at the top")
    elif len(intents) > 1:
        errors.append(f"multiple `**Intent:**` lines found ({len(intents)}); expected exactly 1")

    for heading in _REQUIRED_HEADINGS:
        if heading not in content:
            errors.append(f"missing required heading: `{heading}`")

    return errors


def count_open_questions(content: str) -> int:
    """Count `[NEEDS CLARIFICATION:` markers in the spec."""
    return content.count("[NEEDS CLARIFICATION:")


# ---------------------------------------------------------------------------
# Spec file parsing (for --spec-file)
# ---------------------------------------------------------------------------

def read_spec_file(path: Path) -> tuple[str, str]:
    """Load a user-supplied spec. Returns (intent, content).

    Raises ValueError with a human-readable message if:
      - the file does not exist or is empty
      - validation fails
      - no `**Intent:**` line is found
      - multiple `**Intent:**` lines are found
    """
    if not path.exists():
        raise ValueError(f"spec file not found: {path}")
    content = path.read_text()
    if not content.strip():
        raise ValueError(f"spec file is empty: {path}")

    errors = validate_spec(content)
    if errors:
        detail = "; ".join(errors)
        raise ValueError(f"spec file failed validation: {detail}")

    matches = _INTENT_LINE.findall(content)
    # validate_spec has already checked for exactly 1, but be explicit
    if len(matches) != 1:
        raise ValueError(
            f"spec file must contain exactly one `**Intent:**` line (found {len(matches)})"
        )
    intent = matches[0].strip()
    if not intent:
        raise ValueError("spec file's `**Intent:**` line is blank")

    return intent, content


# ---------------------------------------------------------------------------
# Hash (for resume-tamper detection)
# ---------------------------------------------------------------------------

def spec_hash(content: str) -> str:
    """SHA256 of normalized spec content.

    Normalization: CRLF → LF, trailing whitespace stripped per line, UTF-8
    bytes. CRLF→LF normalization means a CRLF-only edit DOESN'T register.
    That's the intended behavior — git on Windows may rewrite line endings
    on checkout and we don't want that to trigger a mismatch.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    # Strip trailing whitespace per line (catches accidental trailing spaces
    # from editors, keeps intentional content changes).
    lines = [line.rstrip() for line in normalized.splitlines()]
    normalized = "\n".join(lines)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Prompt injection — safe embedding
# ---------------------------------------------------------------------------

# Tokens that, if present in user-supplied spec content, could break out of
# prompt-embedding wrappers. Sanitized to entity-like placeholders on render.
_DANGEROUS_TOKENS = (
    "</certifier_prompt>",
    "<certifier_prompt>",
    "</spec>",
    "<spec>",
    "</intent>",
    "<intent>",
)


def _sanitize_spec_content(content: str) -> str:
    """Neutralize delimiter tokens that could break prompt wrappers."""
    out = content
    for tok in _DANGEROUS_TOKENS:
        safe = tok.replace("<", "&lt;").replace(">", "&gt;")
        out = re.sub(re.escape(tok), safe, out, flags=re.IGNORECASE)
    return out


def format_spec_section(content: str | None) -> str:
    """Wrap a spec for safe embedding in build.md / certifier-thorough.md.

    Returns empty string if no spec — caller passes this through `render_prompt`
    which substitutes it as a plain value with no extra formatting.
    """
    if not content or not content.strip():
        return ""
    sanitized = _sanitize_spec_content(content)
    return (
        "## Spec\n\n"
        "The build is gated on this approved spec. Treat it as authoritative — it wins on scope conflicts with the raw intent.\n\n"
        "<spec source=\"approved\">\n"
        f"{sanitized}\n"
        "</spec>\n\n"
        "**Must-Have** entries are requirements. **Must NOT Have Yet** entries are OUT OF SCOPE — do not build them."
    )


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------

async def run_spec_agent(
    intent: str,
    project_dir: Path,
    run_dir: Path,
    config: dict[str, object],
    *,
    prior_spec: str | None = None,
    user_notes: str | None = None,
    version: int = 0,
    budget: "RunBudget | None" = None,
) -> SpecResult:
    """Run the spec agent once and return the written spec.

    `version` is used for the `spec-agent.log` filename — 0 = initial, N>0 =
    regeneration N.

    Does NOT call `inject_memory()` — memory is about prior certification
    findings, not relevant to product-level spec generation.

    CONTRACT: `AgentCallError` from budget-exhaustion or timeout propagates
    UNWRAPPED — callers write a paused checkpoint. Other failures (invalid
    spec, missing file) still raise `RuntimeError`.
    """
    from otto.agent import make_agent_options, run_agent_with_timeout
    from otto.prompts import render_prompt

    run_dir.mkdir(parents=True, exist_ok=True)
    spec_path = run_dir / "spec.md"

    prior_block = ""
    if prior_spec:
        prior_block = (
            "## Prior Spec (regeneration)\n"
            "A previous version of the spec was rejected. Consider the user's"
            " note and produce an improved version. Preserve what was correct;"
            " change what the note calls out.\n\n"
            "<prior_spec>\n"
            f"{_sanitize_spec_content(prior_spec)}\n"
            "</prior_spec>\n"
        )
        if user_notes:
            prior_block += f"\n**User note:** {user_notes.strip()}\n"

    prompt = render_prompt(
        "spec-light.md",
        intent=intent,
        spec_path=str(spec_path),
        prior_spec_section=prior_block,
    )

    options = make_agent_options(project_dir, config, agent_type="spec")

    # Spec-agent timeout: `spec_timeout` caps this specific phase (spec is
    # expected to be fast — 1-3 min in practice). With a run budget active,
    # the tighter of the two bounds this call.
    try:
        spec_cap = int(config.get("spec_timeout", 600))
    except (ValueError, TypeError):
        spec_cap = 600
    timeout: int = min(budget.for_call(), spec_cap) if budget is not None else spec_cap

    # Per-version log subdir so regens don't overwrite each other.
    log_subdir = run_dir / (f"agent-v{version}" if version else "agent")

    start = time.monotonic()
    _text, cost, _session, _breakdown = await run_agent_with_timeout(
        prompt, options,
        log_dir=log_subdir,
        phase_name="SPEC",
        timeout=timeout,
        project_dir=project_dir,
        capture_tool_output=False,
    )

    duration = round(time.monotonic() - start, 1)

    if not spec_path.exists():
        raise RuntimeError(
            f"spec agent did not write {spec_path}. See {log_subdir}/narrative.log for details."
        )

    content = spec_path.read_text()
    errors = validate_spec(content)
    if errors:
        raise RuntimeError(
            "spec agent produced an invalid spec: " + "; ".join(errors)
            + f". Inspect {spec_path} and {log_subdir}/narrative.log."
        )

    return SpecResult(
        path=spec_path,
        content=content,
        open_questions=count_open_questions(content),
        cost=float(cost or 0.0),
        duration_s=duration,
        version=version,
    )


# ---------------------------------------------------------------------------
# Review gate
# ---------------------------------------------------------------------------

def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _summarize_spec(content: str) -> str:
    """Produce a short textual summary for the review prompt."""
    intents = _INTENT_LINE.findall(content)
    intent = intents[0].strip() if intents else "(missing)"
    # Extract Must-Have + Must-NOT-Have bullets
    def _section_bullets(heading: str, limit: int = 4) -> list[str]:
        lines = content.splitlines()
        try:
            start = next(i for i, line in enumerate(lines) if line.strip() == heading)
        except StopIteration:
            return []
        out: list[str] = []
        for line in lines[start + 1:]:
            stripped = line.strip()
            if stripped.startswith("##"):
                break
            m = _BULLET_LINE.match(stripped)
            if m:
                out.append(m.group(1).strip())
                if len(out) >= limit:
                    break
        return out

    must_have = _section_bullets("## Must Have", 3)
    must_not = _section_bullets("## Must NOT Have Yet", 3)
    open_q = count_open_questions(content)
    lines = content.count("\n") + 1

    parts = [
        f"  Intent:        {intent[:80]}",
        f"  Must-Have:     {', '.join(b[:40] for b in must_have) if must_have else '(none)'}",
        f"  Must-NOT-Have: {', '.join(b[:40] for b in must_not) if must_not else '(none)'}",
        f"  Open:          {open_q} question(s)",
        f"  Size:          {lines} lines",
    ]
    return "\n".join(parts)


async def review_spec(
    spec_result: SpecResult,
    project_dir: Path,
    run_dir: Path,
    run_id: str,
    intent: str,
    config: dict[str, object],
    *,
    auto_approve: bool,
    initial_regen_count: int = 0,
    budget: "RunBudget | None" = None,
) -> SpecResult:
    """Interactive review gate for a generated spec.

    Returns the final SpecResult the user approved. Raises SystemExit(0) on
    quit. Re-raises KeyboardInterrupt so the caller writes a paused
    checkpoint.

    When `auto_approve` is True, returns immediately (no prompt).
    """
    from otto.checkpoint import clear_checkpoint, write_checkpoint

    if auto_approve:
        return spec_result

    if not _is_tty():
        raise RuntimeError(
            "spec review requires a TTY. Pass --yes to auto-approve, "
            "or --spec-file to skip the spec agent."
        )

    current = spec_result
    regen_count = initial_regen_count

    while True:
        print()
        print(f"  Spec written to {current.path} ({current.path.stat().st_size} bytes, "
              f"{current.open_questions} open question(s))")
        print()
        print(_summarize_spec(current.content))
        print()
        print("  [a] approve and build")
        print("  [e] edit spec.md yourself (I'll wait)")
        print("  [r] regenerate with notes")
        print("  [q] quit")
        try:
            choice = input("  Choice: ").strip().lower()
        except EOFError:
            raise KeyboardInterrupt("EOF during spec review")

        if not choice:
            continue

        action = choice[0]
        if action == "a":
            return current

        if action == "e":
            import os
            import shlex
            import subprocess
            editor_env = os.environ.get("VISUAL") or os.environ.get("EDITOR")
            print()
            if editor_env:
                cmd = shlex.split(editor_env) + [str(current.path)]
                print(f"  Opening {current.path} in {editor_env}...")
                try:
                    subprocess.call(cmd)
                except (OSError, FileNotFoundError) as exc:
                    print(f"  [error] could not launch editor ({exc}).")
                    print(f"  Edit {current.path} manually, then press Enter to continue...")
                    try:
                        input()
                    except EOFError:
                        raise KeyboardInterrupt("EOF during spec review")
            else:
                print(f"  No $EDITOR/$VISUAL set. Edit {current.path} manually, then press Enter to continue...")
                try:
                    input()
                except EOFError:
                    raise KeyboardInterrupt("EOF during spec review")
            try:
                content = current.path.read_text()
            except OSError as exc:
                print(f"  [error] could not read spec: {exc}")
                continue
            errors = validate_spec(content)
            if errors:
                print(f"  [error] spec is invalid after edit: {'; '.join(errors)}")
                print("  [error] fix the file and try again, or press Ctrl-C to abort")
                continue
            current = SpecResult(
                path=current.path,
                content=content,
                open_questions=count_open_questions(content),
                cost=current.cost,
                duration_s=current.duration_s,
                version=current.version,
            )
            continue

        if action == "r":
            try:
                note = input("  One-line note (what to change): ").strip()
            except EOFError:
                raise KeyboardInterrupt("EOF during spec review")
            if not note:
                print("  [skipped — empty note]")
                continue
            regen_count += 1
            # Archive the prior version
            archive_path = run_dir / f"spec-v{regen_count}.md"
            try:
                archive_path.write_text(current.content)
                logger.info("Archived prior spec to %s", archive_path)
            except OSError as exc:
                logger.warning("Could not archive prior spec: %s", exc)
            new = await run_spec_agent(
                intent, project_dir, run_dir, config,
                prior_spec=current.content,
                user_notes=note,
                version=regen_count,
                budget=budget,
            )
            # Combine cost so the caller sees total spec cost
            current = SpecResult(
                path=new.path,
                content=new.content,
                open_questions=new.open_questions,
                cost=current.cost + new.cost,
                duration_s=current.duration_s + new.duration_s,
                version=regen_count,
            )
            write_checkpoint(
                project_dir,
                run_id=run_id,
                command="build",
                phase="spec_review",
                intent=intent,
                spec_path=str(current.path),
                spec_hash=spec_hash(current.content),
                spec_version=regen_count,
                spec_cost=current.cost,
            )
            continue

        if action == "q":
            clear_checkpoint(project_dir)
            print("  Aborted.")
            sys.exit(0)

        print(f"  [unrecognized: {choice!r}]")
