"""Tests for QA proof artifact generation."""

from pathlib import Path

from otto.qa import _write_proof_artifacts


class TestWriteProofArtifacts:
    def _make_verdict(self, must_passed=True, must_items=None):
        return {
            "must_passed": must_passed,
            "must_items": must_items or [
                {"criterion": "Function returns correct value", "status": "pass", "evidence": "Test passes"},
                {"criterion": "Error handling works", "status": "pass", "evidence": "Throws on invalid input"},
            ],
            "should_notes": [
                {"criterion": "Clean code", "observation": "Good variable names"},
            ],
            "regressions": [],
        }

    def test_creates_proofs_dir(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        verdict = self._make_verdict()
        task = {"key": "test123"}
        count = _write_proof_artifacts(log_dir, verdict, [], task, "Test prompt", 0.50)
        assert (log_dir / "qa-proofs").is_dir()
        assert count > 0

    def test_writes_must_item_proofs(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        verdict = self._make_verdict()
        task = {"key": "test123"}
        _write_proof_artifacts(log_dir, verdict, [], task, "Test prompt", 0.50)

        must1 = log_dir / "qa-proofs" / "must-1.md"
        must2 = log_dir / "qa-proofs" / "must-2.md"
        assert must1.exists()
        assert must2.exists()
        content = must1.read_text()
        assert "Function returns correct value" in content
        assert "Status: pass" in content

    def test_writes_regression_script(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        verdict = self._make_verdict()
        task = {"key": "test123"}
        qa_actions = [
            {"type": "bash", "command": "npx jest --testPathPattern=feature", "output": "14 passed"},
            {"type": "bash", "command": "curl http://localhost:3000/api/health", "output": "200 OK"},
        ]
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Test prompt", 0.50)

        script = log_dir / "qa-proofs" / "regression-check.sh"
        assert script.exists()
        content = script.read_text()
        assert "#!/bin/bash" in content
        assert "set -e" in content
        assert "npx jest" in content
        assert "curl" in content
        # Check it's executable
        import os
        assert os.access(script, os.X_OK)

    def test_regression_script_skips_destructive(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        verdict = self._make_verdict()
        task = {"key": "test123"}
        qa_actions = [
            {"type": "bash", "command": "npx jest", "output": "ok"},
            {"type": "bash", "command": "kill -9 12345", "output": ""},
            {"type": "bash", "command": "rm -rf /tmp/test", "output": ""},
        ]
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Test prompt", 0.50)

        script = log_dir / "qa-proofs" / "regression-check.sh"
        content = script.read_text()
        assert "npx jest" in content
        assert "# Skipped (non-replayable): kill" in content
        assert "# Skipped (non-replayable): rm -rf" in content

    def test_regression_script_skips_server_and_background_commands(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        verdict = self._make_verdict()
        task = {"key": "test123"}
        qa_actions = [
            {"type": "bash", "command": "npx jest --runInBand", "output": "ok"},
            {"type": "bash", "command": "npm run dev", "output": ""},
            {"type": "bash", "command": "python -m http.server 8000", "output": ""},
            {"type": "bash", "command": "uvicorn app:app --reload", "output": ""},
            {"type": "bash", "command": "nohup npm start >/tmp/app.log 2>&1", "output": ""},
            {"type": "bash", "command": "serve dist &", "output": ""},
        ]
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Test prompt", 0.50)

        script = log_dir / "qa-proofs" / "regression-check.sh"
        content = script.read_text()
        assert "npx jest --runInBand" in content
        assert "# Skipped (non-replayable): npm run dev" in content
        assert "# Skipped (non-replayable): python -m http.server 8000" in content
        assert "# Skipped (non-replayable): uvicorn app:app --reload" in content
        assert "# Skipped (non-replayable): nohup npm start >/tmp/app.log 2>&1" in content
        assert "# Skipped (non-replayable): serve dist &" in content

    def test_regression_script_allows_replayable_ampersand_and_next_build_commands(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        verdict = self._make_verdict()
        task = {"key": "test123"}
        qa_actions = [
            {"type": "bash", "command": "npm test 2>&1", "output": "ok"},
            {"type": "bash", "command": "npm run lint && npm test", "output": "ok"},
            {"type": "bash", "command": "npx next build", "output": "compiled"},
            {"type": "bash", "command": "npx next dev", "output": ""},
            {"type": "bash", "command": "npx next start", "output": ""},
        ]
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Test prompt", 0.50)

        script = log_dir / "qa-proofs" / "regression-check.sh"
        content = script.read_text()
        assert "npm test 2>&1" in content
        assert "npm run lint && npm test" in content
        assert "npx next build" in content
        assert "# Skipped (non-replayable): npx next dev" in content
        assert "# Skipped (non-replayable): npx next start" in content

    def test_writes_proof_report(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        verdict = self._make_verdict()
        task = {"key": "test123"}
        qa_actions = [
            {"type": "bash", "command": "npx jest", "output": "14 passed"},
            {"type": "browser", "action": "take_screenshot", "detail": "http://localhost:3000"},
        ]
        _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Test prompt", 0.50)

        report = log_dir / "qa-proofs" / "proof-report.md"
        assert report.exists()
        content = report.read_text()
        assert "Proof of Work" in content
        assert "test123" in content
        assert "$0.50" in content
        assert "Function returns correct value" in content
        assert "Browser Verification" in content
        assert "take_screenshot" in content

    def test_no_bash_commands_no_script(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        verdict = self._make_verdict()
        task = {"key": "test123"}
        # Only browser actions
        qa_actions = [
            {"type": "browser", "action": "navigate", "detail": "http://localhost:3000"},
        ]
        count = _write_proof_artifacts(log_dir, verdict, qa_actions, task, "Test prompt", 0.50)

        # Should have must proofs + report but no regression script
        assert (log_dir / "qa-proofs" / "must-1.md").exists()
        assert (log_dir / "qa-proofs" / "proof-report.md").exists()
        assert not (log_dir / "qa-proofs" / "regression-check.sh").exists()

    def test_empty_verdict(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        verdict = {"must_passed": True, "must_items": [], "should_notes": []}
        task = {"key": "test123"}
        count = _write_proof_artifacts(log_dir, verdict, [], task, "Test prompt", 0.0)
        # Should write proof-report.md at minimum
        assert (log_dir / "qa-proofs" / "proof-report.md").exists()
        assert count >= 1

    def test_overwrites_stale_proof_files(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        proofs_dir = log_dir / "qa-proofs"
        proofs_dir.mkdir()
        (proofs_dir / "must-9.md").write_text("stale")
        (proofs_dir / "regression-check.sh").write_text("stale")
        (proofs_dir / "proof-report.md").write_text("stale")
        (proofs_dir / "extra.txt").write_text("stale")

        verdict = {
            "must_passed": True,
            "must_items": [
                {"criterion": "Fresh criterion", "status": "pass", "evidence": "Fresh evidence"},
            ],
            "should_notes": [],
        }
        task = {"key": "test123"}
        _write_proof_artifacts(log_dir, verdict, [], task, "Test prompt", 0.0)

        assert not (proofs_dir / "must-9.md").exists()
        assert not (proofs_dir / "regression-check.sh").exists()
        assert not (proofs_dir / "extra.txt").exists()
        assert (proofs_dir / "must-1.md").exists()
        assert (proofs_dir / "proof-report.md").exists()
