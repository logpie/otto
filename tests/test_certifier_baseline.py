from __future__ import annotations

import json
from pathlib import Path

from otto.certifier.adapter import SeededUser, TestConfig as AdapterTestConfig
from otto.certifier.baseline import (
    AppRunner,
    BaselineResult,
    BaselineRunState,
    ClaimResult,
    Evidence,
    _authenticate_nextauth_user,
    _build_execution_context,
    _test_claim,
    certify,
    compare,
    print_report,
    save_report,
    save_markdown_report,
)
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


def test_nextauth_login_uses_form_encoded_callback_and_stores_session(monkeypatch):
    user = SeededUser(email="admin@store.com", password="admin123", role="admin")
    test_config = AdapterTestConfig(
        auth_type="nextauth",
        auth_csrf_required=True,
        login_endpoint="/api/auth/callback/credentials",
        seeded_users=[user],
    )
    claim = Claim(
        id="cart-add-item",
        description="Users can add items to cart",
        priority="critical",
        category="feature",
        test_approach="api",
        test_steps=[],
        hard_fail=True,
    )
    context = _build_execution_context(
        claim,
        "http://localhost:3004",
        Path("."),
        _profile(),
        test_config,
        BaselineRunState(),
    )

    def fake_get(self, url, **kwargs):
        if url.endswith("/api/auth/csrf"):
            return FakeResponse(200, payload={"csrfToken": "csrf-123"})
        if url.endswith("/api/auth/session"):
            return FakeResponse(200, payload={"user": {"email": user.email}})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(self, url, **kwargs):
        assert url.endswith("/api/auth/callback/credentials")
        assert "data" in kwargs
        assert "json" not in kwargs
        assert kwargs["data"] == {
            "email": "admin@store.com",
            "password": "admin123",
            "csrfToken": "csrf-123",
            "redirect": "false",
            "json": "true",
        }
        self.cookies.set("next-auth.session-token", "session-1")
        return FakeResponse(200, payload={"url": "http://localhost:3004/"})

    monkeypatch.setattr("requests.sessions.Session.get", fake_get)
    monkeypatch.setattr("requests.sessions.Session.post", fake_post)

    ok, detail = _authenticate_nextauth_user(context, user)

    assert ok is True
    assert "NextAuth session established" in detail
    assert context.session.cookies.get("next-auth.session-token") == "session-1"


def test_shared_run_state_reuses_authenticated_nextauth_session(monkeypatch):
    user = SeededUser(email="admin@store.com", password="admin123", role="admin")
    test_config = AdapterTestConfig(
        auth_type="nextauth",
        auth_csrf_required=True,
        login_endpoint="/api/auth/callback/credentials",
        seeded_users=[user],
        has_cart_model=True,
    )
    run_state = BaselineRunState()
    calls = {"csrf": 0, "post": 0, "session": 0}

    def fake_get(self, url, **kwargs):
        if url.endswith("/api/auth/csrf"):
            calls["csrf"] += 1
            return FakeResponse(200, payload={"csrfToken": "csrf-123"})
        if url.endswith("/api/auth/session"):
            calls["session"] += 1
            return FakeResponse(200, payload={"user": {"email": user.email}})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(self, url, **kwargs):
        calls["post"] += 1
        self.cookies.set("next-auth.session-token", "session-1")
        return FakeResponse(200, payload={"url": "http://localhost:3004/"})

    monkeypatch.setattr("requests.sessions.Session.get", fake_get)
    monkeypatch.setattr("requests.sessions.Session.post", fake_post)

    login_step = {
        "action": "http",
        "method": "POST",
        "candidate_paths": ["/api/login"],
        "expect_status": [200],
    }
    claim = Claim(
        id="cart-add-item",
        description="Users can add items to cart",
        priority="critical",
        category="feature",
        test_approach="api",
        test_steps=[login_step],
        hard_fail=True,
    )

    first = _test_claim(claim, "http://localhost:3004", Path("."), _profile(), test_config, run_state)
    second = _test_claim(claim, "http://localhost:3004", Path("."), _profile(), test_config, run_state)

    assert first.passed is True
    assert second.passed is True
    assert calls == {"csrf": 1, "post": 1, "session": 1}
    assert run_state.session.cookies.get("next-auth.session-token") == "session-1"


def test_401_after_failed_auth_login_is_blocked_by_harness(monkeypatch):
    user = SeededUser(email="user@store.com", password="user123", role="user")
    test_config = AdapterTestConfig(
        auth_type="session",
        login_endpoint="/api/login",
        seeded_users=[user],
        has_cart_model=True,
    )
    run_state = BaselineRunState()

    login_claim = Claim(
        id="auth-login",
        description="Users can login",
        priority="critical",
        category="feature",
        test_approach="api",
        test_steps=[
            {
                "action": "http",
                "method": "POST",
                "candidate_paths": ["/api/login"],
                "expect_status": [200],
            }
        ],
        hard_fail=True,
    )
    cart_claim = Claim(
        id="cart-view",
        description="Users can view cart",
        priority="critical",
        category="feature",
        test_approach="api",
        test_steps=[
            {
                "action": "http",
                "method": "GET",
                "candidate_paths": ["/api/cart"],
                "expect_status": [200],
            }
        ],
        hard_fail=True,
    )

    monkeypatch.setattr(
        "otto.certifier.baseline._authenticate_generic_user",
        lambda context, selected_user: (False, "seeded login failed"),
    )
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        lambda self, method, url, **kwargs: FakeResponse(401, text="unauthorized"),
    )

    login_result = _test_claim(login_claim, "http://example.test", Path("."), _profile(), test_config, run_state)
    cart_result = _test_claim(cart_claim, "http://example.test", Path("."), _profile(), test_config, run_state)

    assert login_result.outcome == "fail"
    assert run_state.auth_login_claim_ran is True
    assert run_state.authenticated_user is None
    assert cart_result.outcome == "blocked_by_harness"
    assert cart_result.error == "auth session not established — harness limitation"
    assert "observed HTTP 401" in cart_result.evidence[0].actual


def test_print_report_groups_outcomes_and_shows_comparison(capsys):
    result = BaselineResult(
        product_dir="/tmp/app",
        intent="build a shop",
        product_type="web",
        started=True,
        claims_tested=4,
        claims_passed=1,
        claims_failed=1,
        claims_not_implemented=1,
        claims_blocked=1,
        claims_not_applicable=0,
        hard_fails=3,
        certified=False,
        results=[
            ClaimResult(
                claim_id="pass-claim",
                claim_description="passed claim",
                priority="important",
                hard_fail=False,
                passed=True,
                outcome="pass",
                evidence=[Evidence(step="s", command="GET /ok", expected="200", actual="HTTP 200", passed=True)],
            ),
            ClaimResult(
                claim_id="missing-claim",
                claim_description="missing claim",
                priority="critical",
                hard_fail=True,
                passed=False,
                outcome="not_implemented",
                evidence=[Evidence(step="s", command="adapter", expected="endpoint exists", actual="not found", passed=False, outcome="not_implemented")],
            ),
            ClaimResult(
                claim_id="fail-claim",
                claim_description="failing claim",
                priority="critical",
                hard_fail=True,
                passed=False,
                outcome="fail",
                evidence=[Evidence(step="s", command="GET /fail", expected="200", actual="HTTP 500", passed=False, outcome="fail")],
            ),
            ClaimResult(
                claim_id="blocked-claim",
                claim_description="blocked claim",
                priority="critical",
                hard_fail=True,
                passed=False,
                outcome="blocked_by_harness",
                evidence=[Evidence(step="s", command="GET /cart", expected="200", actual="auth session not established — harness limitation; observed HTTP 401: unauthorized", passed=False, outcome="blocked_by_harness")],
                error="auth session not established — harness limitation",
            ),
        ],
        duration_s=12.3,
        compile_cost_usd=0.125,
        compile_duration_s=1.2,
        compiled_at="2026-03-30 12:00:00",
        matrix_source="cache",
        matrix_path="/tmp/app/otto_logs/certifier/matrix.json",
    )
    other = BaselineResult(
        product_dir="/tmp/app",
        intent="build a shop",
        product_type="web",
        started=True,
        claims_tested=4,
        claims_passed=0,
        claims_failed=2,
        claims_not_implemented=2,
        claims_blocked=1,
        claims_not_applicable=0,
        hard_fails=4,
        certified=False,
        results=[],
        duration_s=20.0,
        compile_cost_usd=0.200,
    )

    print_report(result, other)
    output = capsys.readouterr().out

    assert "Certification: NOT CERTIFIED" in output
    assert "Tier 0:        3/4 present (75%)" in output
    assert "Tier 1:        1/2 passed (50%)" in output
    assert "1 blocked" in output
    assert output.index("Not Implemented") < output.index("Failures") < output.index("Passes")
    assert "reason:   auth session not established — harness limitation" in output
    assert "command:  GET /fail" in output
    assert "expected: 200" in output
    assert "actual:   HTTP 500" in output
    assert "## Comparison: Current vs Other" in output


def test_save_report_writes_structured_json(tmp_path: Path):
    result = BaselineResult(
        product_dir="/tmp/app",
        intent="build a shop",
        product_type="web",
        started=True,
        claims_tested=1,
        claims_passed=1,
        claims_failed=0,
        claims_not_implemented=0,
        claims_blocked=0,
        claims_not_applicable=0,
        hard_fails=0,
        certified=True,
        results=[
            ClaimResult(
                claim_id="pass-claim",
                claim_description="passed claim",
                priority="important",
                hard_fail=False,
                passed=True,
                outcome="pass",
                evidence=[Evidence(step="s", command="GET /ok", expected="200", actual="HTTP 200", passed=True)],
            )
        ],
    )
    output_path = tmp_path / "report.json"

    save_report(result, output_path)

    payload = json.loads(output_path.read_text())
    assert payload["summary"]["certified"] is True
    assert payload["claims_by_outcome"]["pass"][0]["claim_id"] == "pass-claim"
    assert payload["summary"]["tier_0"]["present"] == 1
    assert payload["summary"]["tier_1"]["passed"] == 1


def test_compare_returns_markdown_table():
    result_a = BaselineResult(
        product_dir="/tmp/a",
        intent="build a shop",
        product_type="web",
        started=True,
        claims_tested=2,
        claims_passed=1,
        claims_failed=0,
        claims_not_implemented=0,
        claims_blocked=1,
        claims_not_applicable=0,
        hard_fails=1,
        certified=False,
        results=[
            ClaimResult("auth-login", "Users can login", "critical", True, False, "blocked_by_harness", [], error="auth session not established — harness limitation"),
            ClaimResult("catalog-list", "Users can browse catalog", "critical", True, True, "pass", []),
        ],
    )
    result_b = BaselineResult(
        product_dir="/tmp/b",
        intent="build a shop",
        product_type="web",
        started=True,
        claims_tested=2,
        claims_passed=1,
        claims_failed=1,
        claims_not_implemented=0,
        claims_blocked=0,
        claims_not_applicable=0,
        hard_fails=1,
        certified=False,
        results=[
            ClaimResult("auth-login", "Users can login", "critical", True, False, "fail", []),
            ClaimResult("catalog-list", "Users can browse catalog", "critical", True, True, "pass", []),
        ],
    )

    output = compare(result_a, result_b, ("Otto", "Bare CC"))

    assert "## Comparison: Otto vs Bare CC" in output
    assert "| Tier 0 (structure) | 2/2 (100%) | 2/2 (100%) |" in output
    assert "| `auth-login` | Users can login | blocked_by_harness | fail |" in output


def test_save_markdown_report_writes_sections(tmp_path: Path):
    result = BaselineResult(
        product_dir="/tmp/app",
        intent="build a shop",
        product_type="web",
        started=True,
        claims_tested=4,
        claims_passed=1,
        claims_failed=1,
        claims_not_implemented=1,
        claims_blocked=1,
        claims_not_applicable=0,
        hard_fails=2,
        certified=False,
        results=[
            ClaimResult("missing-claim", "missing claim", "critical", True, False, "not_implemented", [Evidence(step="s", command="adapter", expected="route exists", actual="adapter found no route", passed=False, outcome="not_implemented")], error="adapter found no route"),
            ClaimResult("fail-claim", "failing claim", "critical", True, False, "fail", [Evidence(step="s", command="GET /fail", expected="200", actual="HTTP 500", passed=False, outcome="fail")], error="HTTP 500"),
            ClaimResult("blocked-claim", "blocked claim", "critical", True, False, "blocked_by_harness", [Evidence(step="s", command="GET /cart", expected="200", actual="auth session not established — harness limitation; observed HTTP 401: unauthorized", passed=False, outcome="blocked_by_harness")], error="auth session not established — harness limitation"),
            ClaimResult("pass-claim", "passed claim", "critical", True, True, "pass", [Evidence(step="s", command="GET /ok", expected="200", actual="HTTP 200", passed=True, outcome="pass")]),
        ],
    )
    output_path = tmp_path / "report.md"

    save_markdown_report(result, output_path)

    text = output_path.read_text()
    assert "> **Tier 0 (structure):** 3/4 present (75%)" in text
    assert "## NOT IMPLEMENTED" in text
    assert "## FAILED" in text
    assert "## BLOCKED" in text
    assert "## PASSED" in text
    assert "Reason: auth session not established — harness limitation" in text


def test_certify_loads_matrix_from_matrix_path(monkeypatch, tmp_path: Path):
    project_dir = tmp_path / "app"
    project_dir.mkdir()
    matrix_path = tmp_path / "shared-matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "intent": "build a shop",
                "claims": [
                    {
                        "id": "catalog-list",
                        "description": "Users can browse catalog",
                        "priority": "critical",
                        "category": "feature",
                        "test_approach": "api",
                        "test_steps": [],
                        "hard_fail": True,
                    }
                ],
            }
        )
    )

    monkeypatch.setattr("otto.certifier.baseline.classify", lambda project: _profile())
    monkeypatch.setattr("otto.certifier.baseline.analyze_project", lambda project: AdapterTestConfig())
    monkeypatch.setattr(
        "otto.certifier.baseline.run_baseline",
        lambda project_dir, matrix, profile, test_config=None: BaselineResult(
            product_dir=str(project_dir),
            intent=matrix.intent,
            product_type=profile.product_type,
            started=True,
            claims_tested=1,
            claims_passed=1,
            claims_failed=0,
            claims_not_implemented=0,
            claims_blocked=0,
            claims_not_applicable=0,
            hard_fails=0,
            certified=True,
            results=[ClaimResult("catalog-list", "Users can browse catalog", "critical", True, True, "pass", [])],
        ),
    )

    result = certify(project_dir, "build a shop", matrix_path=matrix_path)

    assert result.intent == "build a shop"
    assert result.matrix_source == "file"
    assert result.matrix_path == str(matrix_path)


def test_port_override_reuses_existing_app_without_autostart(monkeypatch, tmp_path: Path):
    profile = _profile()
    profile.extra["reuse_existing_app"] = True
    profile.port = 3004
    runner = AppRunner(tmp_path, profile)

    monkeypatch.setattr(AppRunner, "_port_in_use", lambda self: False)

    evidence = runner.start()

    assert evidence.passed is False
    assert evidence.command == "(reuse existing app on http://localhost:3004)"
    assert "no app responding" in evidence.actual
