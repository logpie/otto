"""E2E tests for proof-of-work pipeline.

Exercises all proof-of-work logic paths: QA proof generation, regression
script content & filtering, claim verification, display integration, and
stale proof cleanup.
"""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from otto.qa import _write_proof_artifacts
from otto.claim_verify import verify_claims, format_claim_findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_verdict(
    must_passed: bool = True,
    must_items: list | None = None,
    should_notes: list | None = None,
) -> dict:
    """Build a QA verdict dict."""
    return {
        "must_passed": must_passed,
        "must_items": must_items or [],
        "should_notes": should_notes or [],
        "regressions": [],
        "prompt_intent": "Implementation matches original prompt.",
        "extras": [],
    }


def _make_log_dir(tmp_path: Path, name: str = "logs") -> Path:
    log_dir = tmp_path / name
    log_dir.mkdir(parents=True)
    return log_dir


class TestProofOfWorkE2E:
    """End-to-end scenarios testing the proof-of-work pipeline."""

    # ------------------------------------------------------------------
    # Scenario 1: Happy path — QA passes, proofs generated correctly
    # ------------------------------------------------------------------
    def test_happy_path_all_must_pass(self, tmp_path):
        """QA passes with 3 must items, all proofs have correct content."""
        log_dir = _make_log_dir(tmp_path)
        verdict = _make_verdict(
            must_passed=True,
            must_items=[
                {"criterion": "add() returns sum", "status": "pass",
                 "evidence": "calc.test.js passes: add(2,3)===5"},
                {"criterion": "subtract() returns difference", "status": "pass",
                 "evidence": "calc.test.js passes: subtract(5,3)===2"},
                {"criterion": "multiply() returns product", "status": "pass",
                 "evidence": "calc.test.js passes: multiply(4,3)===12"},
            ],
        )
        qa_actions = [
            {"type": "bash", "command": "npx jest --testPathPattern=calc", "output": "3 passed, 0 failed"},
            {"type": "bash", "command": "node -e \"require('./calc').add(2,3)\"", "output": "5"},
            {"type": "bash", "command": "tsc --noEmit", "output": ""},
        ]
        task = {"key": "CALC-101"}
        prompt = "Implement calculator functions: add, subtract, multiply"

        count = _write_proof_artifacts(log_dir, verdict, qa_actions, task, prompt, 0.42)

        proofs_dir = log_dir / "qa-proofs"
        assert proofs_dir.is_dir()

        # 3 must-N.md + regression-check.sh + proof-report.md = 5
        assert count == 5

        # Verify must-N.md files
        for i in range(1, 4):
            must_file = proofs_dir / f"must-{i}.md"
            assert must_file.exists(), f"must-{i}.md missing"
            content = must_file.read_text()
            assert "Status: pass" in content

        must1 = (proofs_dir / "must-1.md").read_text()
        assert "add() returns sum" in must1
        assert "calc.test.js passes" in must1

        must3 = (proofs_dir / "must-3.md").read_text()
        assert "multiply() returns product" in must3

        # Verify regression-check.sh
        script = proofs_dir / "regression-check.sh"
        assert script.exists()
        content = script.read_text()
        assert "#!/bin/bash" in content
        assert "set -e" in content
        assert "npx jest --testPathPattern=calc" in content
        assert "tsc --noEmit" in content
        assert os.access(script, os.X_OK)

        # Verify proof-report.md
        report = proofs_dir / "proof-report.md"
        assert report.exists()
        report_text = report.read_text()
        assert "CALC-101" in report_text
        assert "$0.42" in report_text
        assert "add() returns sum" in report_text
        assert "subtract() returns difference" in report_text
        assert "multiply() returns product" in report_text
        assert "\u2713" in report_text  # checkmark for pass

        # Regression script should NOT contain destructive / server commands
        assert "npm run dev" not in content
        assert "kill" not in content

    # ------------------------------------------------------------------
    # Scenario 2: QA fails — proofs still generated for failed items
    # ------------------------------------------------------------------
    def test_qa_fails_proofs_reflect_failure(self, tmp_path):
        """QA fails, must-N.md files correctly show fail status."""
        log_dir = _make_log_dir(tmp_path)
        verdict = _make_verdict(
            must_passed=False,
            must_items=[
                {"criterion": "API returns 200", "status": "pass",
                 "evidence": "curl returns 200"},
                {"criterion": "Response has valid JSON", "status": "fail",
                 "evidence": "Response body is plain text, not JSON"},
                {"criterion": "Rate limiting works", "status": "fail",
                 "evidence": "11th request still returns 200"},
            ],
        )
        qa_actions = [
            {"type": "bash", "command": "curl -s http://localhost:3000/api/data", "output": "200 OK"},
        ]
        task = {"key": "API-42"}
        count = _write_proof_artifacts(
            log_dir, verdict, qa_actions, task,
            "Implement data API with rate limiting", 0.85,
        )

        proofs_dir = log_dir / "qa-proofs"

        must1 = (proofs_dir / "must-1.md").read_text()
        assert "Status: pass" in must1

        must2 = (proofs_dir / "must-2.md").read_text()
        assert "Status: fail" in must2
        assert "plain text" in must2

        must3 = (proofs_dir / "must-3.md").read_text()
        assert "Status: fail" in must3

        report = (proofs_dir / "proof-report.md").read_text()
        assert "\u2717" in report  # cross mark for fail
        assert "API-42" in report

    # ------------------------------------------------------------------
    # Scenario 3: Mixed bash + browser actions
    # ------------------------------------------------------------------
    def test_mixed_bash_and_browser_actions(self, tmp_path):
        """Browser actions appear in report but NOT in regression script."""
        log_dir = _make_log_dir(tmp_path)
        proofs_dir = log_dir / "qa-proofs"
        proofs_dir.mkdir()
        screenshot = proofs_dir / "screenshot-form.png"
        screenshot.write_bytes(b"png")
        verdict = _make_verdict(
            must_passed=True,
            must_items=[
                {"criterion": "Page loads", "status": "pass", "evidence": "200 OK"},
                {"criterion": "Form submits", "status": "pass", "evidence": "Screenshot confirms"},
            ],
        )
        qa_actions = [
            {"type": "bash", "command": "npx jest --runInBand", "output": "2 passed"},
            {"type": "browser", "action": "navigate", "detail": "http://localhost:3000"},
            {
                "type": "browser",
                "action": "evaluate_script",
                "detail": ".form-status",
                "input": json.dumps({"function": "() => document.querySelector('.form-status').textContent"}),
                "output": "Submitted",
            },
            {
                "type": "browser",
                "action": "take_screenshot",
                "detail": "Form success state",
                "path": str(screenshot),
            },
            {"type": "bash", "command": "curl http://localhost:3000/api/health", "output": "ok"},
        ]
        task = {"key": "UI-7"}
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Build form page", 1.20)

        report = (proofs_dir / "proof-report.md").read_text()
        assert "Browser Assertions" in report
        assert "Submitted" in report
        assert "Screenshots" in report
        assert "Form success state" in report
        assert "navigate" not in report

        # regression-check.sh has only bash commands (no browser)
        script = (proofs_dir / "regression-check.sh").read_text()
        assert "npx jest" in script
        assert "curl" in script
        assert "navigate" not in script
        assert "screenshot" not in script
        assert "browser" not in script.lower()

    # ------------------------------------------------------------------
    # Scenario 4: Claim verification detects contradiction
    # ------------------------------------------------------------------
    def test_claim_contradiction_detected(self, tmp_path):
        """Agent claims all tests pass, verify log contradicts."""
        agent_log = tmp_path / "agent.log"
        agent_log.write_text(
            "I've implemented all the calculator functions.\n"
            "All 14 tests passed successfully.\n"
            "The implementation is complete and ready for review.\n"
        )
        verify_log = tmp_path / "verify.log"
        verify_log.write_text(
            "tier1: FAIL\n"
            "Running: npx jest\n"
            "12 passed, 2 failed\n"
            "FAIL src/calc.test.js\n"
            "  AssertionError: expected 12 to equal 15\n"
        )

        findings = verify_claims(agent_log, verify_log)
        hard_fails = [f for f in findings if not f["verified"] and f["severity"] == "hard"]
        assert len(hard_fails) == 1
        assert "14 tests pass" in hard_fails[0]["claim"]

        # Format it and verify it looks right for claims.md
        formatted = format_claim_findings(findings)
        assert "FAILURES" in formatted
        assert "Verify log shows test failures" in formatted

    # ------------------------------------------------------------------
    # Scenario 5: Claim verification confirms truthful claim
    # ------------------------------------------------------------------
    def test_claim_truthful_confirmed(self, tmp_path):
        """Agent truthfully claims tests pass, verify log confirms."""
        agent_log = tmp_path / "agent.log"
        agent_log.write_text(
            "All 14 tests passed. The implementation is complete.\n"
        )
        verify_log = tmp_path / "verify.log"
        verify_log.write_text(
            "tier1: PASS\n"
            "Running: npx jest\n"
            "14 passed\n"
        )

        findings = verify_claims(agent_log, verify_log)
        assert len(findings) == 1
        assert findings[0]["verified"] is True
        assert findings[0]["severity"] == "info"

        formatted = format_claim_findings(findings)
        assert "Confirmed" in formatted
        assert "FAILURES" not in formatted

    # ------------------------------------------------------------------
    # Scenario 6: Stale proof cleanup on retry
    # ------------------------------------------------------------------
    def test_stale_proof_cleanup_on_retry(self, tmp_path):
        """Old proof files from attempt 1 are cleaned before attempt 2."""
        log_dir = _make_log_dir(tmp_path)

        # --- Attempt 1: 4 must items, all pass ---
        verdict1 = _make_verdict(
            must_passed=True,
            must_items=[
                {"criterion": "C1", "status": "pass", "evidence": "e1"},
                {"criterion": "C2", "status": "pass", "evidence": "e2"},
                {"criterion": "C3", "status": "pass", "evidence": "e3"},
                {"criterion": "C4", "status": "pass", "evidence": "e4"},
            ],
        )
        qa_actions1 = [
            {"type": "bash", "command": "npx jest --testPathPattern=attempt1", "output": "attempt1"},
        ]
        task = {"key": "RETRY-1"}
        _write_proof_artifacts(log_dir, verdict1, qa_actions1, task, "Prompt A", 0.30)

        proofs_dir = log_dir / "qa-proofs"
        assert (proofs_dir / "must-4.md").exists()
        assert (proofs_dir / "regression-check.sh").exists()
        old_script = (proofs_dir / "regression-check.sh").read_text()
        assert "attempt1" in old_script

        # --- Attempt 2: only 2 must items, different content ---
        verdict2 = _make_verdict(
            must_passed=True,
            must_items=[
                {"criterion": "NEW-C1", "status": "pass", "evidence": "new-e1"},
                {"criterion": "NEW-C2", "status": "fail", "evidence": "new-e2"},
            ],
        )
        qa_actions2 = [
            {"type": "bash", "command": "npx jest --testPathPattern=attempt2", "output": "attempt2"},
        ]
        _write_proof_artifacts(log_dir, verdict2, qa_actions2, task, "Prompt B", 0.55)

        # Old must-3.md and must-4.md should be gone (rmtree cleans dir)
        assert not (proofs_dir / "must-3.md").exists()
        assert not (proofs_dir / "must-4.md").exists()

        # New files reflect attempt 2
        assert (proofs_dir / "must-1.md").exists()
        assert (proofs_dir / "must-2.md").exists()
        must1 = (proofs_dir / "must-1.md").read_text()
        assert "NEW-C1" in must1

        new_script = (proofs_dir / "regression-check.sh").read_text()
        assert "attempt2" in new_script
        assert "attempt1" not in new_script

        report = (proofs_dir / "proof-report.md").read_text()
        assert "Prompt B" in report
        assert "$0.55" in report
        assert "Prompt A" not in report

    # ------------------------------------------------------------------
    # Scenario 7: Regression script is runnable
    # ------------------------------------------------------------------
    def test_regression_script_runnable(self, tmp_path):
        """Generate regression-check.sh with simple commands and run it."""
        log_dir = _make_log_dir(tmp_path)
        verdict = _make_verdict(
            must_passed=True,
            must_items=[
                {"criterion": "Echo works", "status": "pass", "evidence": "yes"},
            ],
        )
        qa_actions = [
            {"type": "bash", "command": "node -e 'console.log(1+1)'", "output": "2"},
            {"type": "bash", "command": "python -c 'print(3)'", "output": "3"},
        ]
        task = {"key": "RUN-1"}
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Test runnable", 0.01)

        script = log_dir / "qa-proofs" / "regression-check.sh"
        assert script.exists()
        assert os.access(script, os.X_OK)

        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "2" in result.stdout  # node -e 'console.log(1+1)' outputs 2
        assert "3" in result.stdout

    # ------------------------------------------------------------------
    # Scenario 8: Regression script fails on regression
    # ------------------------------------------------------------------
    def test_regression_script_fails_on_regression(self, tmp_path):
        """Regression script with a failing command exits non-zero."""
        log_dir = _make_log_dir(tmp_path)
        verdict = _make_verdict(
            must_passed=True,
            must_items=[
                {"criterion": "Something", "status": "pass", "evidence": "ok"},
            ],
        )
        qa_actions = [
            {"type": "bash", "command": "echo 'step 1'", "output": "step 1"},
            {"type": "bash", "command": "false", "output": ""},
        ]
        task = {"key": "FAIL-1"}
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Test failing", 0.01)

        script = log_dir / "qa-proofs" / "regression-check.sh"
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=10,
        )
        # set -e causes exit on first failure
        assert result.returncode != 0

    # ------------------------------------------------------------------
    # Scenario 9: Display integration — otto show proof section
    # ------------------------------------------------------------------
    def test_display_integration_proof_section(self, tmp_path):
        """CLI show logic finds and summarizes proof artifacts."""
        # Simulate the directory structure: project/otto_logs/TASK-1/qa-proofs/
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        log_dir = project_dir / "otto_logs" / "TASK-1"
        log_dir.mkdir(parents=True)

        # Generate proof artifacts
        verdict = _make_verdict(
            must_passed=True,
            must_items=[
                {"criterion": "Feature A", "status": "pass", "evidence": "works"},
                {"criterion": "Feature B", "status": "pass", "evidence": "works"},
            ],
        )
        qa_actions = [
            {"type": "bash", "command": "npx jest", "output": "2 passed"},
        ]
        task = {"key": "TASK-1"}
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Build features", 0.50)

        # Also write a claims file (simulating runner behavior)
        claims_content = format_claim_findings([{
            "claim": "Agent claimed: 'all tests passed'",
            "evidence": "Verify log confirms pass",
            "verified": True,
            "severity": "info",
        }])
        (log_dir / "attempt-1-claims.md").write_text(claims_content)

        proofs_dir = log_dir / "qa-proofs"

        # Verify the artifacts exist as the display code expects
        assert proofs_dir.exists()
        proof_report = proofs_dir / "proof-report.md"
        regression = proofs_dir / "regression-check.sh"
        must_proofs = sorted(proofs_dir.glob("must-*.md"))

        assert proof_report.exists()
        assert regression.exists()
        assert len(must_proofs) == 2

        # Simulate what the CLI show() does to build display parts
        parts = []
        if must_proofs:
            parts.append(f"{len(must_proofs)} must-item proofs")
        if regression.exists():
            parts.append("regression-check.sh")
        if proof_report.exists():
            parts.append("proof-report.md")

        display_line = f"Proofs:  {', '.join(parts)}"
        assert "2 must-item proofs" in display_line
        assert "regression-check.sh" in display_line
        assert "proof-report.md" in display_line

        # Verify proof-report content can be shown (first 20 lines)
        content = proof_report.read_text()
        lines = content.strip().splitlines()
        assert len(lines) > 0
        assert "Proof Report" in lines[0]

        # Verify claims display logic
        claims_files = sorted(log_dir.glob("attempt-*-claims.md"))
        assert len(claims_files) == 1
        claims_text = claims_files[0].read_text()
        has_failures = "FAILURES" in claims_text
        assert not has_failures  # truthful claim, no failures

    # ------------------------------------------------------------------
    # Scenario 10: Claim verification with build claims
    # ------------------------------------------------------------------
    def test_claim_build_contradiction(self, tmp_path):
        """Agent claims build succeeds, verify log has TypeScript errors."""
        agent_log = tmp_path / "agent.log"
        agent_log.write_text(
            "I've completed the implementation.\n"
            "The build succeeds with no errors.\n"
            "All type checks pass.\n"
        )
        verify_log = tmp_path / "verify.log"
        verify_log.write_text(
            "tier1: FAIL\n"
            "Running: tsc --noEmit\n"
            "src/calc.ts(14,5): error TS2345: Argument of type 'string' is not "
            "assignable to parameter of type 'number'.\n"
            "src/calc.ts(22,10): error TS2322: Type 'undefined' is not assignable "
            "to type 'number'.\n"
        )

        findings = verify_claims(agent_log, verify_log)
        hard_fails = [f for f in findings if not f["verified"] and f["severity"] == "hard"]
        assert len(hard_fails) >= 1
        # Should detect the build claim as contradicted
        assert any("build" in f["claim"].lower() or "error" in f["claim"].lower()
                    for f in hard_fails)

        formatted = format_claim_findings(findings)
        assert "FAILURES" in formatted
        assert "build errors" in formatted.lower() or "Verify log" in formatted

    # ------------------------------------------------------------------
    # Scenario 11: Empty QA actions — no regression script
    # ------------------------------------------------------------------
    def test_empty_qa_actions_no_regression_script(self, tmp_path):
        """No bash commands means no regression-check.sh, but report exists."""
        log_dir = _make_log_dir(tmp_path)
        verdict = _make_verdict(
            must_passed=True,
            must_items=[
                {"criterion": "Code review item", "status": "pass",
                 "evidence": "Manual inspection"},
            ],
        )
        # No qa_actions at all
        task = {"key": "REVIEW-1"}
        count = _write_proof_artifacts(log_dir, verdict, [], task, "Code review task", 0.10)

        proofs_dir = log_dir / "qa-proofs"

        # must-1.md and proof-report.md exist
        assert (proofs_dir / "must-1.md").exists()
        assert (proofs_dir / "proof-report.md").exists()

        # regression-check.sh should NOT be created
        assert not (proofs_dir / "regression-check.sh").exists()

        # Count: 1 must file + 1 proof report = 2
        assert count == 2

    # ------------------------------------------------------------------
    # Scenario 12: Command filtering in regression script
    # ------------------------------------------------------------------
    def test_command_filtering_in_regression_script(self, tmp_path):
        """Only reproducible verification commands should survive filtering."""
        log_dir = _make_log_dir(tmp_path)
        verdict = _make_verdict(
            must_passed=True,
            must_items=[
                {"criterion": "Tests pass", "status": "pass", "evidence": "ok"},
            ],
        )
        qa_actions = [
            # Should be KEPT (replayable verification commands)
            {"type": "bash", "command": "npx jest --runInBand", "output": "ok"},
            {"type": "bash", "command": "curl -s http://localhost:3000/api", "output": "200"},
            {"type": "bash", "command": "tsc --noEmit", "output": ""},
            {"type": "bash", "command": "npm run lint && npm test", "output": "ok"},
            {"type": "bash", "command": "node -e \"console.log(1+1)\"", "output": "2"},
            # Should be EXCLUDED (non-replayable or exploration)
            {"type": "bash", "command": "find . -name '*.tsx'", "output": "src/App.tsx"},
            {"type": "bash", "command": "git diff main...HEAD --name-only", "output": "src/App.tsx"},
            {"type": "bash", "command": "npm run dev", "output": ""},
            {"type": "bash", "command": "kill -9 12345", "output": ""},
            {"type": "bash", "command": "python -m http.server 8000", "output": ""},
            {"type": "bash", "command": "npm start", "output": ""},
            {"type": "bash", "command": "uvicorn app:app --reload", "output": ""},
            {"type": "bash", "command": "gunicorn app:app", "output": ""},
            {"type": "bash", "command": "nohup node server.js &", "output": ""},
            {"type": "bash", "command": "rm -rf /tmp/test", "output": ""},
            {"type": "bash", "command": "git push origin main", "output": ""},
            {"type": "bash", "command": "npx next dev", "output": ""},
            {"type": "bash", "command": "serve dist", "output": ""},
            {"type": "bash", "command": "flask run --port 5000", "output": ""},
            {"type": "bash", "command": "python3 -m http.server 9090", "output": ""},
            {"type": "bash", "command": "node server.js", "output": ""},
            {"type": "bash", "command": "pkill -f node", "output": ""},
        ]
        task = {"key": "FILTER-1"}
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Test filtering", 0.20)

        script = (log_dir / "qa-proofs" / "regression-check.sh").read_text()

        # --- KEPT commands (appear as real commands, not commented) ---
        assert "npx jest --runInBand" in script
        assert "curl -s http://localhost:3000/api" in script
        assert "tsc --noEmit" in script
        assert "npm run lint && npm test" in script
        assert 'node -e "console.log(1+1)"' in script

        # --- EXCLUDED commands ---
        assert "find . -name '*.tsx'" not in script
        assert "git diff main...HEAD --name-only" not in script
        assert "npm run dev" not in script
        assert "python -m http.server 8000" not in script
        assert "npm start" not in script
        assert "uvicorn app:app --reload" not in script
        assert "gunicorn app:app" not in script
        assert "npx next dev" not in script
        assert "serve dist" not in script
        assert "flask run --port 5000" not in script
        assert "python3 -m http.server 9090" not in script
        assert "node server.js" not in script
        assert "nohup node server.js &" not in script
        assert "kill -9 12345" not in script
        assert "pkill -f node" not in script
        assert "rm -rf /tmp/test" not in script
        assert "git push origin main" not in script

    def test_regression_script_escapes_quotes_in_echo(self, tmp_path):
        """Commands with double quotes don't break the echo label in the script."""
        log_dir = _make_log_dir(tmp_path)
        verdict = _make_verdict(must_passed=True)
        qa_actions = [
            {"type": "bash", "command": 'node -e "console.log((3.7).toFixed(0))"', "output": "4"},
        ]
        task = {"key": "QUOTE-1"}
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Test quotes", 0.0)
        script = log_dir / "qa-proofs" / "regression-check.sh"
        # The script must be syntactically valid bash
        import subprocess
        result = subprocess.run(
            ["bash", "-n", str(script)],  # -n = syntax check only
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Script has syntax errors: {result.stderr}"
        # Also verify it runs without error
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"

    def test_screenshot_mapping_uses_action_filepath_not_sorted_order(self, tmp_path):
        log_dir = _make_log_dir(tmp_path)
        proofs_dir = log_dir / "qa-proofs"
        proofs_dir.mkdir()
        alpha = proofs_dir / "screenshot-alpha.png"
        zeta = proofs_dir / "screenshot-zeta.png"
        alpha.write_bytes(b"alpha")
        zeta.write_bytes(b"zeta")

        verdict = _make_verdict(
            must_passed=True,
            must_items=[
                {"criterion": "Screenshots captured", "status": "pass", "evidence": "See screenshots"},
            ],
        )
        qa_actions = [
            {
                "type": "browser",
                "action": "take_screenshot",
                "detail": "Zeta state",
                "path": str(zeta),
            },
            {
                "type": "browser",
                "action": "take_screenshot",
                "detail": "Alpha state",
                "path": str(alpha),
            },
        ]

        _write_proof_artifacts(log_dir, verdict, qa_actions, {"key": "SS-1"}, "Capture screenshots", 0.0)

        report = (proofs_dir / "proof-report.md").read_text()
        zeta_index = report.index("SS1 [screenshot-zeta.png](screenshot-zeta.png)")
        alpha_index = report.index("SS2 [screenshot-alpha.png](screenshot-alpha.png)")
        assert zeta_index < alpha_index
        assert "Description: Zeta state" in report
        assert "Description: Alpha state" in report
