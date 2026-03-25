"""Tests for otto.claim_verify module."""

from pathlib import Path

import pytest

from otto.claim_verify import verify_claims, format_claim_findings


class TestVerifyClaims:
    def test_no_files(self, tmp_path):
        """Returns empty when log files don't exist."""
        findings = verify_claims(
            tmp_path / "nonexistent.log",
            tmp_path / "nonexistent-verify.log",
        )
        assert findings == []

    def test_no_claims(self, tmp_path):
        """Returns empty when agent log has no test claims."""
        agent_log = tmp_path / "agent.log"
        agent_log.write_text("I looked at the code and made some changes.\n")
        findings = verify_claims(agent_log)
        assert findings == []

    def test_test_pass_claim_verified(self, tmp_path):
        """Agent claims tests pass, verify log confirms."""
        agent_log = tmp_path / "agent.log"
        agent_log.write_text("All 14 tests passed successfully.\n")
        verify_log = tmp_path / "verify.log"
        verify_log.write_text("tier1: PASS\n14 passed\n")

        findings = verify_claims(agent_log, verify_log)
        assert len(findings) == 1
        assert findings[0]["verified"] is True
        assert findings[0]["severity"] == "info"

    def test_test_pass_claim_contradicted(self, tmp_path):
        """Agent claims tests pass, but verify log shows failures."""
        agent_log = tmp_path / "agent.log"
        agent_log.write_text("All 14 tests passed. The implementation is complete.\n")
        verify_log = tmp_path / "verify.log"
        verify_log.write_text("tier1: FAIL\n12 passed, 2 failed\nAssertionError in test_foo\n")

        findings = verify_claims(agent_log, verify_log)
        hard_fails = [f for f in findings if not f["verified"] and f["severity"] == "hard"]
        assert len(hard_fails) == 1
        assert "contradict" in hard_fails[0]["evidence"].lower() or "fail" in hard_fails[0]["evidence"].lower()

    def test_build_claim_contradicted(self, tmp_path):
        """Agent claims build succeeds, but verify log shows errors."""
        agent_log = tmp_path / "agent.log"
        agent_log.write_text("The build succeeds with no errors.\n")
        verify_log = tmp_path / "verify.log"
        verify_log.write_text("tier1: FAIL\nerror TS2345: Argument of type 'string'\n")

        findings = verify_claims(agent_log, verify_log)
        hard_fails = [f for f in findings if not f["verified"] and f["severity"] == "hard"]
        assert len(hard_fails) == 1

    def test_unacknowledged_test_failure(self, tmp_path):
        """Agent ran tests that failed but didn't acknowledge it."""
        agent_log = tmp_path / "agent.log"
        agent_log.write_text(
            "● Bash npx jest --testPathPattern=feature\n"
            "FAIL src/feature.test.ts\n"
            "  1 failed, 3 passed\n"
            "● Write src/index.ts\n"
            "I've updated the feature to include the new functionality.\n"
        )
        findings = verify_claims(agent_log)
        advisory = [f for f in findings if f["severity"] == "advisory"]
        assert len(advisory) >= 1

    def test_acknowledged_test_failure_no_advisory(self, tmp_path):
        """Agent ran tests that failed and acknowledged it."""
        agent_log = tmp_path / "agent.log"
        agent_log.write_text(
            "● Bash npx jest --testPathPattern=feature\n"
            "FAIL src/feature.test.ts\n"
            "  1 failed, 3 passed\n"
            "I see the test failed. Let me fix the issue.\n"
            "● Edit src/feature.ts\n"
        )
        findings = verify_claims(agent_log)
        advisory = [f for f in findings if f["severity"] == "advisory"]
        assert len(advisory) == 0

    def test_no_verify_log(self, tmp_path):
        """Claims about tests don't produce findings without verify log."""
        agent_log = tmp_path / "agent.log"
        agent_log.write_text("All tests passed.\n")
        # No verify log — can't confirm or deny
        findings = verify_claims(agent_log)
        assert len(findings) == 0


class TestFormatClaimFindings:
    def test_empty(self):
        assert format_claim_findings([]) == ""

    def test_hard_failure(self):
        findings = [{
            "claim": "Agent claimed: 'all tests passed'",
            "evidence": "Verify log shows failures",
            "verified": False,
            "severity": "hard",
        }]
        result = format_claim_findings(findings)
        assert "FAILURES" in result
        assert "all tests passed" in result

    def test_confirmed(self):
        findings = [{
            "claim": "Agent claimed: 'all tests passed'",
            "evidence": "Verify log confirms",
            "verified": True,
            "severity": "info",
        }]
        result = format_claim_findings(findings)
        assert "Confirmed" in result
        assert "FAILURES" not in result
