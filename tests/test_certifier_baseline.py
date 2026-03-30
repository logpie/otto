from __future__ import annotations

import json
from pathlib import Path

from otto.certifier.baseline import _test_claim
from otto.certifier.classifier import ProductProfile
from otto.certifier.intent_compiler import Claim, load_matrix


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        if text:
            self.text = text
        elif payload is None:
            self.text = ""
        else:
            self.text = json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _profile() -> ProductProfile:
    return ProductProfile(
        product_type="web",
        framework="nextjs",
        language="typescript",
        start_command="",
        port=3000,
        test_command="",
        interaction="browser",
    )


def test_load_matrix_normalizes_legacy_steps(tmp_path: Path):
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "intent": "build a shop",
                "claims": [
                    {
                        "id": "auth-register",
                        "description": "Users can register",
                        "priority": "critical",
                        "category": "feature",
                        "test_approach": "browser",
                        "test_steps": [
                            "Navigate to registration page",
                            "Submit the form",
                        ],
                        "hard_fail": True,
                    }
                ],
            }
        )
    )

    matrix = load_matrix(matrix_path)

    assert matrix.claims[0].test_steps
    assert all(isinstance(step, dict) for step in matrix.claims[0].test_steps)
    assert matrix.claims[0].test_steps[0]["action"] == "navigate"


def test_http_step_discovers_alternate_endpoint(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_request(self, method, url, **kwargs):
        calls.append((method, url))
        if url.endswith("/api/auth/register"):
            return FakeResponse(404, text="not found")
        if url.endswith("/api/register"):
            return FakeResponse(201, payload={"id": "u1", "email": kwargs["json"]["email"]})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("requests.sessions.Session.request", fake_request)

    claim = Claim(
        id="auth-register",
        description="Users can register",
        priority="critical",
        category="feature",
        test_approach="api",
        test_steps=[
            {
                "action": "http",
                "method": "POST",
                "path": "/api/auth/register",
                "body": {"email": "{{email}}", "password": "{{password}}", "name": "{{name}}"},
                "expect_status": [201],
                "expect_body_contains": ["email"],
                "expect_json_keys": ["id", "email"],
            }
        ],
        hard_fail=True,
    )

    result = _test_claim(claim, "http://example.test", Path("."), _profile())

    assert result.passed is True
    assert calls[0][1].endswith("/api/auth/register")
    assert any(url.endswith("/api/register") for _, url in calls)


def test_claim_fails_when_step_is_skipped():
    claim = Claim(
        id="unsupported",
        description="Unsupported step",
        priority="critical",
        category="feature",
        test_approach="api",
        test_steps=[{"action": "browser-click", "selector": "button"}],
        hard_fail=True,
    )

    result = _test_claim(claim, "http://example.test", Path("."), _profile())

    assert result.passed is False
    assert result.evidence[0].skipped is True


def test_http_step_requires_expected_body(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        return FakeResponse(201, payload={"id": "u1", "message": "created"})

    monkeypatch.setattr("requests.sessions.Session.request", fake_request)

    claim = Claim(
        id="auth-register",
        description="Users can register",
        priority="critical",
        category="feature",
        test_approach="api",
        test_steps=[
            {
                "action": "http",
                "method": "POST",
                "path": "/api/auth/register",
                "body": {"email": "{{email}}", "password": "{{password}}"},
                "expect_status": [201],
                "expect_body_contains": ["email"],
                "expect_json_keys": ["id", "email"],
            }
        ],
        hard_fail=True,
    )

    result = _test_claim(claim, "http://example.test", Path("."), _profile())

    assert result.passed is False
    assert result.evidence[0].skipped is False
