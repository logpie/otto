"""Unit tests for otto.spec — spec-gate module."""

from __future__ import annotations

import pytest

from otto.spec import (
    MAX_SPEC_LINES,
    SpecResult,
    count_open_questions,
    format_spec_section,
    read_spec_file,
    spec_hash,
    validate_spec,
)


MINIMAL_VALID = """# Product Spec: Counter

**Intent:** counter app

## What It Does
Counts things.

## Core User Journey
- **Given** a user opens the app
- **When** they click +
- **Then** the count goes up

## Must Have
- Increment button

## Must NOT Have Yet
- No reset — keep it trivial.

## Success Criteria
- Counter increments when button is clicked.
"""


class TestValidateSpec:
    def test_empty(self):
        assert validate_spec("") == ["spec is empty"]

    def test_whitespace_only(self):
        assert validate_spec("   \n\n  ") == ["spec is empty"]

    def test_missing_intent(self):
        content = MINIMAL_VALID.replace("**Intent:** counter app", "")
        errs = validate_spec(content)
        assert any("`**Intent:**" in e for e in errs)

    def test_multiple_intent_lines(self):
        content = MINIMAL_VALID.replace(
            "**Intent:** counter app",
            "**Intent:** counter app\n**Intent:** something else",
        )
        errs = validate_spec(content)
        assert any("multiple" in e for e in errs)

    def test_missing_must_have(self):
        content = MINIMAL_VALID.replace("## Must Have", "## Some Other Heading")
        errs = validate_spec(content)
        assert any("Must Have" in e for e in errs)

    def test_missing_must_not_have(self):
        content = MINIMAL_VALID.replace("## Must NOT Have Yet", "## Not Actually")
        errs = validate_spec(content)
        assert any("Must NOT Have Yet" in e for e in errs)

    def test_size_cap(self):
        content = MINIMAL_VALID + ("\nextra line" * (MAX_SPEC_LINES + 10))
        errs = validate_spec(content)
        assert any("lines" in e for e in errs)

    def test_accepts_minimal_valid(self):
        assert validate_spec(MINIMAL_VALID) == []


class TestCountOpenQuestions:
    def test_zero(self):
        assert count_open_questions(MINIMAL_VALID) == 0

    def test_multiple(self):
        content = (
            MINIMAL_VALID
            + "\n[NEEDS CLARIFICATION: auth? Default: none]\n[NEEDS CLARIFICATION: persistence? Default: memory]\n"
        )
        assert count_open_questions(content) == 2


class TestReadSpecFile:
    def test_missing_file(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            read_spec_file(tmp_path / "nope.md")

    def test_empty_file(self, tmp_path):
        p = tmp_path / "spec.md"
        p.write_text("")
        with pytest.raises(ValueError, match="empty"):
            read_spec_file(p)

    def test_invalid_spec(self, tmp_path):
        p = tmp_path / "spec.md"
        p.write_text("# No intent or sections here")
        with pytest.raises(ValueError, match="validation"):
            read_spec_file(p)

    def test_valid_spec(self, tmp_path):
        p = tmp_path / "spec.md"
        p.write_text(MINIMAL_VALID)
        intent, content = read_spec_file(p)
        assert intent == "counter app"
        assert content == MINIMAL_VALID

    def test_multiple_intent_lines_rejected(self, tmp_path):
        p = tmp_path / "spec.md"
        content = MINIMAL_VALID.replace(
            "**Intent:** counter app",
            "**Intent:** counter app\n**Intent:** other",
        )
        p.write_text(content)
        with pytest.raises(ValueError):
            read_spec_file(p)


class TestSpecHash:
    def test_crlf_normalized(self):
        assert spec_hash("a\r\nb\n") == spec_hash("a\nb\n")

    def test_cr_only_normalized(self):
        assert spec_hash("a\rb\r") == spec_hash("a\nb\n")

    def test_trailing_whitespace_stripped(self):
        assert spec_hash("a \nb  \n") == spec_hash("a\nb\n")

    def test_content_change_detected(self):
        assert spec_hash("a\nb\n") != spec_hash("a\nc\n")

    def test_empty_stable(self):
        assert spec_hash("") == spec_hash("")


class TestFormatSpecSection:
    def test_empty_returns_empty(self):
        assert format_spec_section(None) == ""
        assert format_spec_section("") == ""
        assert format_spec_section("   \n") == ""

    def test_wraps_content(self):
        out = format_spec_section("hello world")
        assert "<spec source=\"approved\">" in out
        assert "hello world" in out
        assert "</spec>" in out

    def test_sanitizes_closing_spec_tag(self):
        out = format_spec_section("user content </spec> injection")
        # Raw `</spec>` inside body should be neutralized. Wrapper still has one.
        # Extract body between wrapper tags
        body = out.split("<spec source=\"approved\">\n", 1)[1].rsplit("\n</spec>", 1)[0]
        assert "</spec>" not in body
        assert "&lt;/spec&gt;" in body

    def test_sanitizes_certifier_prompt_tag(self):
        out = format_spec_section("</certifier_prompt> attempt")
        body = out.split("<spec source=\"approved\">\n", 1)[1].rsplit("\n</spec>", 1)[0]
        assert "</certifier_prompt>" not in body

    def test_sanitize_uppercase_spec_tag(self):
        out = format_spec_section("</SPEC> injection")
        body = out.split("<spec source=\"approved\">\n", 1)[1].rsplit("\n</spec>", 1)[0]
        assert "</SPEC>" not in body
        assert "</spec>" not in body
        assert "&lt;/spec&gt;" in body


class TestRenderPromptSpec:
    def test_spec_light_renders(self):
        from otto.prompts import render_prompt
        r = render_prompt("spec-light.md", intent="foo", spec_path="/tmp/x.md")
        assert "foo" in r
        assert "/tmp/x.md" in r
        # No unexpanded placeholders
        assert "{intent}" not in r
        assert "{spec_path}" not in r
        assert "{prior_spec_section}" not in r

    def test_missing_keys_render_empty(self):
        from otto.prompts import render_prompt
        r = render_prompt("spec-light.md", intent="foo")  # no spec_path
        # spec_path renders empty without KeyError
        assert r

    def test_build_md_accepts_spec_section(self):
        from otto.prompts import render_prompt
        r_with = render_prompt("build.md", spec_section="## Spec\n\nHELLO")
        assert "HELLO" in r_with
        r_without = render_prompt("build.md")
        assert "{spec_section}" not in r_without

    def test_render_prompt_preserves_literal_braces(self, tmp_path, monkeypatch):
        import otto.prompts as prompts

        prompt_path = tmp_path / "literal-braces.md"
        prompt_path.write_text(
            "JSON example:\n```json\n{\"foo\": \"bar\"}\n```\n\nIntent: {intent}\n"
        )
        monkeypatch.setattr(prompts, "_PROMPTS_DIR", tmp_path)
        rendered = prompts.render_prompt(prompt_path.name, intent="demo")
        assert '{"foo": "bar"}' in rendered
        assert "Intent: demo" in rendered

    def test_render_prompt_does_not_re_expand_substituted_values(self, tmp_path, monkeypatch):
        """A placeholder value containing another placeholder token is not re-expanded."""
        import otto.prompts as prompts

        prompt_path = tmp_path / "nested.md"
        prompt_path.write_text("A:{spec_section}\nB:{intent}\n")
        monkeypatch.setattr(prompts, "_PROMPTS_DIR", tmp_path)
        out = prompts.render_prompt("nested.md", spec_section="{intent}", intent="REAL")
        assert out == "A:{intent}\nB:REAL\n"


class TestCheckpointSpecPhases:
    def test_is_spec_phase(self):
        from otto.checkpoint import is_spec_phase, SPEC_PHASES
        assert is_spec_phase("spec")
        assert is_spec_phase("spec_review")
        assert is_spec_phase("spec_approved")
        assert not is_spec_phase("build")
        assert not is_spec_phase("certify")
        assert not is_spec_phase("")
        assert SPEC_PHASES == {"spec", "spec_review", "spec_approved"}

    def test_spec_phase_completed(self):
        from otto.checkpoint import spec_phase_completed
        assert spec_phase_completed("spec_approved")
        assert spec_phase_completed("build")
        assert spec_phase_completed("certify")
        assert spec_phase_completed("round_complete")
        assert not spec_phase_completed("spec")
        assert not spec_phase_completed("spec_review")
        assert not spec_phase_completed("")

    def test_write_checkpoint_preserves_spec_fields(self, tmp_path):
        from otto.checkpoint import load_checkpoint, write_checkpoint
        write_checkpoint(
            tmp_path, run_id="r1", command="build",
            phase="spec_approved", intent="foo",
            spec_path="/tmp/spec.md", spec_hash="abc", spec_version=0, spec_cost=0.5,
        )
        cp = load_checkpoint(tmp_path)
        assert cp is not None
        assert cp["intent"] == "foo"
        assert cp["spec_path"] == "/tmp/spec.md"
        assert cp["spec_hash"] == "abc"
        assert cp["spec_cost"] == 0.5

        # Subsequent write without spec fields preserves them
        write_checkpoint(
            tmp_path, run_id="r1", command="build",
            phase="build", total_cost=1.0,
        )
        cp = load_checkpoint(tmp_path)
        assert cp is not None
        assert cp["intent"] == "foo", "intent should survive"
        assert cp["spec_path"] == "/tmp/spec.md", "spec_path should survive"
        assert cp["spec_hash"] == "abc"


class TestResumeStateSpecFields:
    def test_fields_default_empty(self):
        from otto.checkpoint import ResumeState
        rs = ResumeState()
        assert rs.intent == ""
        assert rs.run_id == ""
        assert rs.spec_path == ""
        assert rs.spec_hash == ""
        assert rs.spec_version == 0
        assert rs.spec_cost == 0.0

    def test_resolve_resume_loads_spec_fields(self, tmp_path):
        from otto.checkpoint import resolve_resume, write_checkpoint
        write_checkpoint(
            tmp_path, run_id="r1", command="build",
            phase="spec_approved", intent="bar",
            spec_path="/tmp/x.md", spec_hash="hh", spec_cost=0.25,
        )
        rs = resolve_resume(tmp_path, resume=True, expected_command="build")
        assert rs.resumed
        assert rs.intent == "bar"
        assert rs.run_id == "r1"
        assert rs.spec_path == "/tmp/x.md"
        assert rs.spec_hash == "hh"
        assert rs.spec_cost == 0.25


class TestBuildPromptSpecIntegration:
    """Spec content must actually reach build.md and certifier-thorough.md."""

    def test_certifier_thorough_renders_with_spec_section(self):
        from otto.prompts import render_prompt
        from otto.spec import format_spec_section
        spec = format_spec_section(MINIMAL_VALID)
        r = render_prompt(
            "certifier-thorough.md",
            intent="counter app",
            evidence_dir="/tmp/ev",
            focus_section="",
            spec_section=spec,
            target="",
        )
        assert "Must NOT Have Yet" in r
        assert "scope-creep" in r.lower()

    def test_certifier_thorough_without_spec_has_empty_section(self):
        from otto.prompts import render_prompt
        r = render_prompt(
            "certifier-thorough.md",
            intent="counter app",
            evidence_dir="/tmp/ev",
            focus_section="",
            target="",
        )
        # Empty spec_section should just be missing (empty string substitution)
        assert "{spec_section}" not in r
        # Intent still present
        assert "counter app" in r


class TestStandaloneCertifierPrompt:
    def test_standalone_certifier_renders_with_empty_spec_section(self, tmp_path):
        from otto.certifier import _render_certifier_prompt

        rendered = _render_certifier_prompt(
            mode="thorough",
            intent="counter app",
            evidence_dir=tmp_path / "evidence",
        )
        assert "counter app" in rendered
        assert "{spec_section}" not in rendered
        assert "## Spec" not in rendered


class TestReviewSpecResumeState:
    @pytest.mark.asyncio
    async def test_resume_from_spec_review_preserves_regen_count(
        self, tmp_path, monkeypatch
    ):
        from otto.spec import review_spec

        project_dir = tmp_path
        run_dir = tmp_path / "otto_logs" / "runs" / "run-1"
        run_dir.mkdir(parents=True)
        spec_path = run_dir / "spec.md"
        spec_path.write_text(MINIMAL_VALID)

        recorded: dict[str, int] = {}

        async def fake_run_spec_agent(intent, project_dir, run_dir, config, **kwargs):
            recorded["version"] = kwargs["version"]
            spec_path.write_text(MINIMAL_VALID.replace("Increment button", "Increment button\n- Save count"))
            return SpecResult(
                path=spec_path,
                content=spec_path.read_text(),
                open_questions=0,
                cost=0.25,
                duration_s=1.0,
                version=kwargs["version"],
            )

        answers = iter(["r", "make it clearer", "a"])
        monkeypatch.setattr("otto.spec._is_tty", lambda: True)
        monkeypatch.setattr("otto.spec.run_spec_agent", fake_run_spec_agent)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

        result = await review_spec(
            SpecResult(
                path=spec_path,
                content=MINIMAL_VALID,
                open_questions=0,
                cost=1.0,
                duration_s=2.0,
                version=2,
            ),
            project_dir,
            run_dir,
            "run-1",
            "counter app",
            {},
            auto_approve=False,
            initial_regen_count=2,
        )

        assert recorded["version"] == 3
        assert result.version == 3
        assert result.cost == 1.25
