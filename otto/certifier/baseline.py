"""Deterministic baseline — reproducible probes against critical claims.

Tier 1 certification. Runs the same checks every time. No LLM needed.
Results are reproducible: same product → same score.

For each claim in the requirement matrix, runs structured test steps:
- Start the app
- HTTP requests to API endpoints
- Navigation checks
- CLI command execution
- Response validation

Evidence is captured for every check: command, response, pass/fail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

from otto.certifier.adapter import SeededUser, TestConfig, analyze_project
from otto.certifier.classifier import ProductProfile, classify
from otto.certifier.intent_compiler import Claim, RequirementMatrix, compile_intent

logger = logging.getLogger("otto.certifier.baseline")

_MISSING = object()
_FULL_TEMPLATE_RE = re.compile(r"^\{\{([a-zA-Z0-9_.-]+)\}\}$")
_PARTIAL_TEMPLATE_RE = re.compile(r"\{\{([a-zA-Z0-9_.-]+)\}\}")
_AUTH_SESSION_COOKIE_NAMES = {
    "next-auth.session-token",
    "__Secure-next-auth.session-token",
    "authjs.session-token",
    "__Secure-authjs.session-token",
}


@dataclass
class Evidence:
    """Proof for a single test step."""

    step: str
    command: str
    expected: str
    actual: str
    passed: bool
    outcome: str = "pass"
    timestamp: str = ""
    skipped: bool = False


@dataclass
class ClaimResult:
    """Result of testing a single claim."""

    claim_id: str
    claim_description: str
    priority: str
    hard_fail: bool
    passed: bool
    outcome: str
    evidence: list[Evidence]
    error: str = ""


@dataclass
class BaselineResult:
    """Complete Tier 1 baseline certification result."""

    product_dir: str
    intent: str
    product_type: str
    started: bool
    claims_tested: int
    claims_passed: int
    claims_failed: int
    claims_not_implemented: int
    claims_blocked: int
    claims_not_applicable: int
    hard_fails: int
    certified: bool
    results: list[ClaimResult]
    app_start_evidence: Evidence | None = None
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionContext:
    """Mutable state shared across steps in a single claim."""

    claim: Claim
    base_url: str
    project_dir: Path
    profile: ProductProfile
    test_config: TestConfig | None = None
    session: requests.Session = field(default_factory=requests.Session)
    variables: dict[str, Any] = field(default_factory=dict)
    discoveries: dict[str, str] = field(default_factory=dict)
    authenticated_user: SeededUser | None = None


class AppRunner:
    """Manages starting and stopping the app under test."""

    def __init__(self, project_dir: Path, profile: ProductProfile):
        self.project_dir = project_dir
        self.profile = profile
        self.process: subprocess.Popen | None = None
        self.port = profile.port or 3000
        self.base_url = f"http://localhost:{self.port}"

    def start(self, timeout: int = 30) -> Evidence:
        """Start the app and wait for it to be ready."""

        if not self.profile.start_command:
            return Evidence(
                step="start app",
                command="(no start command)",
                expected="app starts",
                actual="no start command detected",
                passed=False,
                outcome="fail",
                timestamp=time.strftime("%H:%M:%S"),
            )

        if self.profile.extra.get("reuse_existing_app") and self._port_in_use():
            try:
                response = requests.get(self.base_url, timeout=2)
            except Exception as exc:
                return Evidence(
                    step="start app",
                    command=f"(reuse existing app on {self.base_url})",
                    expected="existing app responds on requested port",
                    actual=f"error: {exc}",
                    passed=False,
                    outcome="fail",
                    timestamp=time.strftime("%H:%M:%S"),
                )
            return Evidence(
                step="start app",
                command=f"(reuse existing app on {self.base_url})",
                expected="existing app responds on requested port",
                actual=f"HTTP {response.status_code} on {self.base_url}",
                passed=(response.status_code < 500),
                outcome="pass" if response.status_code < 500 else "fail",
                timestamp=time.strftime("%H:%M:%S"),
            )

        while self._port_in_use():
            self.port += 1
            self.base_url = f"http://localhost:{self.port}"

        cmd = self.profile.start_command
        if self.profile.framework == "nextjs":
            cmd = f"{cmd} -p {self.port}"
        elif self.profile.framework in ("flask", "fastapi"):
            cmd = f"{cmd} --port {self.port}"

        env = {**os.environ, "PORT": str(self.port), "NODE_ENV": "development"}

        try:
            self.process = subprocess.Popen(
                cmd,
                shell=True,
                cwd=str(self.project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                preexec_fn=os.setsid,
            )

            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    response = requests.get(self.base_url, timeout=2)
                    if response.status_code < 500:
                        return Evidence(
                            step="start app",
                            command=cmd,
                            expected="app responds on port",
                            actual=f"HTTP {response.status_code} on {self.base_url}",
                            passed=True,
                            outcome="pass",
                            timestamp=time.strftime("%H:%M:%S"),
                        )
                except requests.ConnectionError:
                    time.sleep(1)

            return Evidence(
                step="start app",
                command=cmd,
                expected=f"app responds within {timeout}s",
                actual=f"timeout after {timeout}s on {self.base_url}",
                passed=False,
                outcome="fail",
                timestamp=time.strftime("%H:%M:%S"),
            )
        except Exception as exc:
            return Evidence(
                step="start app",
                command=cmd,
                expected="app starts",
                actual=f"error: {exc}",
                passed=False,
                outcome="fail",
                timestamp=time.strftime("%H:%M:%S"),
            )

    def stop(self) -> None:
        """Stop the app."""

        if self.process is None:
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process = None

    def _port_in_use(self) -> bool:
        try:
            requests.get(f"http://localhost:{self.port}", timeout=1)
            return True
        except Exception:
            return False


def run_baseline(
    project_dir: Path,
    matrix: RequirementMatrix,
    profile: ProductProfile,
    test_config: TestConfig | None = None,
) -> BaselineResult:
    """Run deterministic baseline tests against critical and important claims."""

    start_time = time.time()
    results: list[ClaimResult] = []

    runner = None
    app_evidence = None
    if profile.interaction in ("browser", "http"):
        runner = AppRunner(project_dir, profile)
        app_evidence = runner.start()
        if not app_evidence.passed:
            for claim in matrix.claims:
                results.append(
                    ClaimResult(
                        claim_id=claim.id,
                        claim_description=claim.description,
                        priority=claim.priority,
                        hard_fail=claim.hard_fail,
                        passed=False,
                        outcome="fail",
                        evidence=[],
                        error="app failed to start",
                    )
                )
            return BaselineResult(
                product_dir=str(project_dir),
                intent=matrix.intent,
                product_type=profile.product_type,
                started=False,
                claims_tested=0,
                claims_passed=0,
                claims_failed=len(matrix.claims),
                claims_not_implemented=0,
                claims_blocked=0,
                claims_not_applicable=0,
                hard_fails=len([claim for claim in matrix.claims if claim.hard_fail]),
                certified=False,
                results=results,
                app_start_evidence=app_evidence,
                duration_s=round(time.time() - start_time, 1),
            )

    try:
        base_url = runner.base_url if runner else ""
        for claim in matrix.claims:
            if claim.priority == "nice" and claim.test_approach == "code-review":
                continue
            results.append(_test_claim(claim, base_url, project_dir, profile, test_config))
    finally:
        if runner:
            runner.stop()

    passed = sum(1 for result in results if result.outcome == "pass")
    failed = sum(1 for result in results if result.outcome == "fail")
    not_implemented = sum(1 for result in results if result.outcome == "not_implemented")
    blocked = sum(1 for result in results if result.outcome == "blocked_by_harness")
    not_applicable = sum(1 for result in results if result.outcome == "not_applicable")
    hard_fails = sum(
        1
        for result in results
        if result.hard_fail and result.outcome in {"fail", "not_implemented", "blocked_by_harness"}
    )

    return BaselineResult(
        product_dir=str(project_dir),
        intent=matrix.intent,
        product_type=profile.product_type,
        started=True,
        claims_tested=len(results),
        claims_passed=passed,
        claims_failed=failed,
        claims_not_implemented=not_implemented,
        claims_blocked=blocked,
        claims_not_applicable=not_applicable,
        hard_fails=hard_fails,
        certified=(hard_fails == 0),
        results=results,
        app_start_evidence=app_evidence,
        duration_s=round(time.time() - start_time, 1),
    )


def _test_claim(
    claim: Claim,
    base_url: str,
    project_dir: Path,
    profile: ProductProfile,
    test_config: TestConfig | None = None,
) -> ClaimResult:
    """Test a single claim by executing its structured steps."""

    structural_outcome, structural_reason = _classify_structural_claim_outcome(claim, profile, test_config)
    if structural_outcome is not None:
        return ClaimResult(
            claim_id=claim.id,
            claim_description=claim.description,
            priority=claim.priority,
            hard_fail=claim.hard_fail,
            passed=(structural_outcome == "pass"),
            outcome=structural_outcome,
            evidence=[
                Evidence(
                    step="claim precheck",
                    command="adapter structural analysis",
                    expected="claim is testable",
                    actual=structural_reason,
                    passed=False,
                    outcome=structural_outcome,
                    skipped=True,
                    timestamp=time.strftime("%H:%M:%S"),
                )
            ],
            error=structural_reason,
        )

    context = _build_execution_context(claim, base_url, project_dir, profile, test_config)
    evidence: list[Evidence] = []

    for step in claim.test_steps:
        evidence.append(_execute_step(step, context))

    passed = any(item.outcome == "pass" for item in evidence) and not any(item.outcome == "fail" for item in evidence)
    outcome = _claim_outcome_from_evidence(evidence)
    error = _claim_error_from_evidence(evidence, outcome)

    return ClaimResult(
        claim_id=claim.id,
        claim_description=claim.description,
        priority=claim.priority,
        hard_fail=claim.hard_fail,
        passed=passed,
        outcome=outcome,
        evidence=evidence,
        error=error,
    )


def _build_execution_context(
    claim: Claim,
    base_url: str,
    project_dir: Path,
    profile: ProductProfile,
    test_config: TestConfig | None = None,
) -> ExecutionContext:
    seed = f"{claim.id}-{time.time_ns()}"
    email_token = abs(hash(seed)) % 1_000_000
    variables = {
        "email": f"{claim.id}.{email_token}@eval.local",
        "password": "Test12345!",
        "name": "Baseline Test User",
        "admin_email": "admin@eval.local",
        "admin_password": "Admin12345!",
    }
    if test_config and test_config.seeded_users:
        any_user = test_config.any_user()
        admin_user = test_config.admin_user()
        if any_user:
            variables["seeded_email"] = any_user.email
            variables["seeded_password"] = any_user.password
        if admin_user:
            variables["admin_email"] = admin_user.email
            variables["admin_password"] = admin_user.password
    return ExecutionContext(
        claim=claim,
        base_url=base_url.rstrip("/"),
        project_dir=project_dir,
        profile=profile,
        test_config=test_config,
        variables=variables,
    )


def _execute_step(step: dict[str, Any] | Any, context: ExecutionContext) -> Evidence:
    """Execute a single structured step."""

    timestamp = time.strftime("%H:%M:%S")
    if not isinstance(step, dict):
        return Evidence(
            step=str(step),
            command="(unstructured step)",
            expected="structured machine-executable step",
            actual="step was not a dict",
            passed=False,
            outcome="blocked_by_harness",
            skipped=True,
            timestamp=timestamp,
        )

    action = str(step.get("action", "")).lower()
    if _should_use_seeded_auth(step, context):
        return _execute_seeded_auth_step(step, context, timestamp)
    if action == "http":
        return _execute_http_step(step, context, timestamp)
    if action == "navigate":
        nav_step = dict(step)
        nav_step.setdefault("method", "GET")
        nav_step.setdefault("expect_status", [200])
        nav_step.setdefault("match_body", "any")
        return _execute_http_step(nav_step, context, timestamp)
    if action == "cli":
        return _execute_cli_step(step, context.project_dir, timestamp)
    if action == "check_exists":
        return _execute_check_exists_step(step, context, timestamp)

    return Evidence(
        step=json.dumps(step, sort_keys=True),
        command="(unsupported action)",
        expected="action in {http,navigate,cli,check_exists}",
        actual=f"unsupported action: {action or '(missing)'}",
        passed=False,
        outcome="blocked_by_harness",
        skipped=True,
        timestamp=timestamp,
    )


def _execute_http_step(step: dict[str, Any], context: ExecutionContext, timestamp: str) -> Evidence:
    method = str(step.get("method", "GET")).upper()
    raw_paths = _get_candidate_paths(step)
    candidate_paths = _filter_candidate_paths(
        _expand_candidate_paths(method, raw_paths),
        method,
        context,
    )
    body_variants = step.get("body_variants")
    if body_variants is None:
        body_variants = [step.get("body")] if "body" in step else [None]
    expect_status = [int(x) for x in step.get("expect_status", ([200] if method == "GET" else [200, 201]))]
    timeout = int(step.get("timeout", 10))
    allow_redirects = bool(step.get("allow_redirects", False))

    missing_reasons: list[str] = []
    attempts: list[dict[str, Any]] = []

    for raw_path in candidate_paths:
        rendered_path = _render_value(raw_path, context.variables)
        if rendered_path in (_MISSING, None):
            missing_reasons.append(f"path template unresolved: {raw_path}")
            continue
        url = _build_url(context.base_url, str(rendered_path))
        for body_template in body_variants:
            rendered_body = _render_value(body_template, context.variables)
            if rendered_body is _MISSING:
                missing_reasons.append(f"body template unresolved for {url}")
                continue

            request_kwargs: dict[str, Any] = {
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
            headers = _render_value(step.get("headers"), context.variables)
            if headers not in (_MISSING, None):
                request_kwargs["headers"] = headers
            if rendered_body is not None:
                if method in {"GET", "HEAD"} and isinstance(rendered_body, dict):
                    request_kwargs["params"] = rendered_body
                else:
                    request_kwargs["json"] = rendered_body

            try:
                response = context.session.request(method, url, **request_kwargs)
            except Exception as exc:
                attempts.append(
                    {
                        "url": url,
                        "body": rendered_body,
                        "error": str(exc),
                    }
                )
                continue

            passed, detail = _check_http_expectations(step, response, context)
            attempts.append(
                {
                    "url": url,
                    "body": rendered_body,
                    "status": response.status_code,
                    "detail": detail,
                    "response": response,
                }
            )
            if passed and response.status_code in expect_status:
                context.discoveries[_step_discovery_key(step)] = str(rendered_path)
                _capture_response_state(step, response, context)
                return Evidence(
                    step=json.dumps(step, sort_keys=True),
                    command=_format_http_command(method, url, rendered_body),
                    expected=_http_expectation_text(step),
                    actual=f"HTTP {response.status_code}: {detail}",
                    passed=True,
                    outcome="pass",
                    timestamp=timestamp,
                )

    if not attempts:
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command=f"{method} {_first_path_label(candidate_paths)}",
            expected=_http_expectation_text(step),
            actual="; ".join(missing_reasons) or "adapter found no matching executable endpoint",
            passed=False,
            outcome="blocked_by_harness",
            skipped=True,
            timestamp=timestamp,
        )

    best_attempt = _pick_best_attempt(attempts)
    if "error" in best_attempt:
        actual = f"request error: {best_attempt['error']}"
    else:
        actual = f"HTTP {best_attempt['status']}: {best_attempt['detail']}"

    return Evidence(
        step=json.dumps(step, sort_keys=True),
        command=_format_http_command(method, best_attempt["url"], best_attempt.get("body")),
        expected=_http_expectation_text(step),
        actual=actual,
        passed=False,
        outcome="fail",
        timestamp=timestamp,
    )


def _execute_check_exists_step(step: dict[str, Any], context: ExecutionContext, timestamp: str) -> Evidence:
    target = str(step.get("target", "http")).lower()
    if target in {"file", "path"}:
        raw_paths = _get_candidate_paths(step)
        for raw_path in raw_paths:
            rendered_path = _render_value(raw_path, context.variables)
            if rendered_path in (_MISSING, None):
                continue
            candidate = context.project_dir / str(rendered_path)
            if candidate.exists():
                return Evidence(
                    step=json.dumps(step, sort_keys=True),
                    command=f"test -e {candidate}",
                    expected="path exists",
                    actual="exists",
                    passed=True,
                    outcome="pass",
                    timestamp=timestamp,
                )
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command="test -e <candidate>",
            expected="path exists",
            actual="no candidate path exists",
            passed=False,
            outcome="fail",
            timestamp=timestamp,
        )

    if target == "note":
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command="(non-executable note)",
            expected="deterministic baseline check",
            actual=step.get("reason", "step requires non-deterministic verification"),
            passed=False,
            outcome="blocked_by_harness",
            skipped=True,
            timestamp=timestamp,
        )

    existence_statuses = [x for x in step.get("existence_statuses", [200, 201, 204, 301, 302, 303, 307, 308, 401, 403]) if isinstance(x, int) or (isinstance(x, str) and x.isdigit())]
    existence_statuses = [int(x) for x in existence_statuses] or [200, 201, 204, 301, 302, 303, 307, 308, 401, 403]
    raw_paths = _filter_candidate_paths(
        _expand_candidate_paths("GET", _get_candidate_paths(step)),
        "GET",
        context,
    )
    attempts: list[dict[str, Any]] = []

    for raw_path in raw_paths:
        rendered_path = _render_value(raw_path, context.variables)
        if rendered_path in (_MISSING, None):
            continue
        url = _build_url(context.base_url, str(rendered_path))
        try:
            response = context.session.get(url, timeout=10, allow_redirects=False)
        except Exception as exc:
            attempts.append({"url": url, "error": str(exc)})
            continue
        if response.status_code in existence_statuses:
            context.discoveries[_step_discovery_key(step)] = str(rendered_path)
            return Evidence(
                step=json.dumps(step, sort_keys=True),
                command=f"GET {url}",
                expected=f"route exists with status in {existence_statuses}",
                actual=f"HTTP {response.status_code}",
                passed=True,
                outcome="pass",
                timestamp=timestamp,
            )
        attempts.append({"url": url, "status": response.status_code, "body": _preview_response(response)})

    if not attempts:
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command="GET <candidate>",
            expected=f"route exists with status in {existence_statuses}",
            actual="adapter found no matching executable endpoint",
            passed=False,
            outcome="blocked_by_harness",
            skipped=True,
            timestamp=timestamp,
        )

    best_attempt = _pick_best_attempt(attempts)
    actual = (
        f"request error: {best_attempt['error']}"
        if "error" in best_attempt
        else f"HTTP {best_attempt['status']}: {best_attempt.get('body', '')}"
    )
    return Evidence(
        step=json.dumps(step, sort_keys=True),
        command=f"GET {best_attempt['url']}",
        expected=f"route exists with status in {existence_statuses}",
        actual=actual,
        passed=False,
        outcome="fail",
        timestamp=timestamp,
    )


def _execute_cli_step(step: dict[str, Any], project_dir: Path, timestamp: str) -> Evidence:
    command = str(step.get("command", "")).strip()
    if not command:
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command="(missing command)",
            expected="CLI command to run",
            actual="step.command was empty",
            passed=False,
            outcome="blocked_by_harness",
            skipped=True,
            timestamp=timestamp,
        )

    expect_exit_code = int(step.get("expect_exit_code", 0))
    expect_stdout_contains = step.get("expect_stdout_contains", [])
    if isinstance(expect_stdout_contains, str):
        expect_stdout_contains = [expect_stdout_contains]

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=int(step.get("timeout", 30)),
        )
    except Exception as exc:
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command=command,
            expected=f"exit code {expect_exit_code}",
            actual=f"error: {exc}",
            passed=False,
            outcome="fail",
            timestamp=timestamp,
        )

    output = (result.stdout + result.stderr).strip()
    tokens = [_render_value(token, {}) for token in expect_stdout_contains]
    body_ok = all(str(token) in output for token in tokens if token not in (_MISSING, None))
    passed = result.returncode == expect_exit_code and body_ok
    return Evidence(
        step=json.dumps(step, sort_keys=True),
        command=command,
        expected=f"exit code {expect_exit_code}" + (f" and stdout contains {expect_stdout_contains}" if expect_stdout_contains else ""),
        actual=f"exit {result.returncode}: {output[:300]}",
        passed=passed,
        outcome="pass" if passed else "fail",
        timestamp=timestamp,
    )


def _classify_structural_claim_outcome(
    claim: Claim,
    profile: ProductProfile,
    test_config: TestConfig | None,
) -> tuple[str | None, str]:
    if claim.test_approach in {"api", "browser"} and profile.product_type in {"cli", "desktop", "library"}:
        return "not_applicable", f"{profile.product_type} product does not expose {claim.test_approach} probes"
    if claim.test_approach == "cli" and profile.interaction not in {"cli", "unknown"}:
        return "not_applicable", f"{profile.product_type} product does not expose a CLI surface"
    if not test_config:
        return None, ""

    claim_key = claim.id.lower()
    if ("cart" in claim_key or claim_key == "ux-empty-cart") and not test_config.has_cart_model:
        return "not_implemented", "adapter found no cart model in the codebase"
    if claim_key == "auth-register" and not test_config.register_endpoint:
        return "not_implemented", "adapter found no registration endpoint in the codebase"
    if _claim_uses_seed_auth_steps(claim) and not test_config.seeded_users and not test_config.register_endpoint:
        return "blocked_by_harness", "claim needs an authenticated user but adapter found neither seeded credentials nor a register endpoint"
    return None, ""


def _claim_uses_seed_auth_steps(claim: Claim) -> bool:
    return any(
        isinstance(step, dict) and (_is_registration_step(step) or _is_login_step(step))
        for step in claim.test_steps
    )


def _claim_outcome_from_evidence(evidence: list[Evidence]) -> str:
    if any(item.outcome == "fail" for item in evidence):
        return "fail"
    if any(item.outcome == "pass" for item in evidence):
        return "pass"
    if any(item.outcome == "not_implemented" for item in evidence):
        return "not_implemented"
    if any(item.outcome == "not_applicable" for item in evidence):
        return "not_applicable"
    return "blocked_by_harness"


def _claim_error_from_evidence(evidence: list[Evidence], outcome: str) -> str:
    if outcome == "pass":
        return ""
    for item in evidence:
        if item.outcome == outcome:
            return item.actual
    return evidence[0].actual if evidence else "no evidence collected"


def _should_use_seeded_auth(step: dict[str, Any], context: ExecutionContext) -> bool:
    return bool(
        context.test_config
        and context.test_config.seeded_users
        and context.claim.id.lower() != "auth-register"
        and (_is_registration_step(step) or _is_login_step(step))
    )


def _execute_seeded_auth_step(step: dict[str, Any], context: ExecutionContext, timestamp: str) -> Evidence:
    user = _select_seeded_user(context)
    if user is None:
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command="adapter seeded auth",
            expected="seeded credentials available",
            actual="adapter did not find any seeded user credentials",
            passed=False,
            outcome="blocked_by_harness",
            skipped=True,
            timestamp=timestamp,
        )

    _apply_seeded_user(context, user)
    if _is_registration_step(step):
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command="adapter seeded auth",
            expected="usable authenticated account fixture",
            actual=f"using seeded account {user.email} instead of creating a new user",
            passed=True,
            outcome="pass",
            timestamp=timestamp,
        )

    ok, detail = _authenticate_seeded_user(context, user)
    return Evidence(
        step=json.dumps(step, sort_keys=True),
        command=f"login as {user.email}",
        expected="authenticated session established",
        actual=detail,
        passed=ok,
        outcome="pass" if ok else "fail",
        timestamp=timestamp,
    )


def _authenticate_seeded_user(context: ExecutionContext, user: SeededUser) -> tuple[bool, str]:
    if context.authenticated_user and context.authenticated_user.email == user.email:
        return True, f"already authenticated as {user.email}"

    config = context.test_config
    if not config:
        return False, "no adapter config available for authentication"

    if config.auth_type == "nextauth":
        ok, detail = _authenticate_nextauth_user(context, user)
    else:
        ok, detail = _authenticate_generic_user(context, user)

    if ok:
        context.authenticated_user = user
    return ok, detail


def _authenticate_nextauth_user(context: ExecutionContext, user: SeededUser) -> tuple[bool, str]:
    config = context.test_config
    if not config:
        return False, "no adapter config available for NextAuth login"

    csrf_url = _build_url(context.base_url, "/api/auth/csrf")
    try:
        csrf_response = context.session.get(
            csrf_url,
            timeout=10,
            headers={"Accept": "application/json"},
        )
    except Exception as exc:
        return False, f"failed CSRF request: {exc}"

    if csrf_response.status_code != 200:
        return False, f"CSRF endpoint returned HTTP {csrf_response.status_code}: {_preview_response(csrf_response)}"

    try:
        csrf_payload = csrf_response.json()
    except ValueError:
        return False, f"CSRF endpoint returned non-JSON body: {_preview_response(csrf_response)}"

    csrf_token = csrf_payload.get("csrfToken")
    if not csrf_token:
        return False, f"CSRF token missing in {json.dumps(csrf_payload)[:200]}"

    callback_url = _build_url(context.base_url, config.login_endpoint or "/api/auth/callback/credentials")
    base_origin = context.base_url.rstrip("/")
    try:
        response = context.session.post(
            callback_url,
            data={
                "email": user.email,
                "password": user.password,
                "csrfToken": csrf_token,
                "callbackUrl": base_origin + "/",
                "redirect": "false",
                "json": "true",
            },
            timeout=10,
            allow_redirects=False,
            headers={
                "Referer": base_origin + "/",
                "Origin": base_origin,
            },
        )
    except Exception as exc:
        return False, f"credentials callback failed: {exc}"

    _capture_response_state({"capture_auth": True}, response, context)

    session_url = _build_url(context.base_url, "/api/auth/session")
    try:
        session_response = context.session.get(
            session_url,
            timeout=10,
            headers={"Accept": "application/json"},
        )
    except Exception as exc:
        session_response = None
        session_error = str(exc)
    else:
        session_error = ""

    if session_response is not None and session_response.status_code == 200:
        try:
            session_payload = session_response.json()
        except ValueError:
            session_payload = None
        if isinstance(session_payload, dict) and session_payload.get("user"):
            session_user = session_payload["user"]
            context.variables["current_user_email"] = session_user.get("email", user.email)
            return True, f"NextAuth session established for {session_user.get('email', user.email)}"

    if _has_auth_session_cookie(context.session):
        return True, f"NextAuth session cookie stored after HTTP {response.status_code}"

    detail = _preview_response(response)
    if session_error:
        detail = f"{detail}; session check error: {session_error}"
    return False, f"NextAuth login failed with HTTP {response.status_code}: {detail}"


def _authenticate_generic_user(context: ExecutionContext, user: SeededUser) -> tuple[bool, str]:
    login_paths = _discover_login_paths(context)
    if not login_paths:
        return False, "adapter found no login endpoints to try"

    attempts: list[str] = []
    for login_path in login_paths:
        url = _build_url(context.base_url, login_path)
        body_variants = [
            {"email": user.email, "password": user.password},
            {"username": user.email, "password": user.password},
        ]
        for rendered_body in body_variants:
            for request_style in ("json", "data"):
                request_kwargs: dict[str, Any] = {
                    "timeout": 10,
                    "allow_redirects": False,
                }
                if request_style == "json":
                    request_kwargs["json"] = rendered_body
                else:
                    request_kwargs["data"] = rendered_body
                try:
                    response = context.session.post(url, **request_kwargs)
                except Exception as exc:
                    attempts.append(f"{login_path} {request_style}: {exc}")
                    continue

                _capture_response_state({"capture_auth": True}, response, context)
                if _generic_auth_succeeded(response, context):
                    return True, f"{login_path} accepted seeded credentials with HTTP {response.status_code}"
                attempts.append(f"{login_path} {request_style}: HTTP {response.status_code} {_preview_response(response)}")
    return False, "; ".join(attempts[:4]) or "all login attempts failed"


def _generic_auth_succeeded(response: requests.Response, context: ExecutionContext) -> bool:
    if response.status_code not in {200, 201, 202, 204, 302, 303}:
        return False
    if _has_auth_session_cookie(context.session) or "Authorization" in context.session.headers:
        return True
    preview = _preview_response(response).lower()
    if any(token in preview for token in ("invalid", "incorrect", "unauthorized", "forbidden", "error")):
        return False
    return response.status_code in {204, 302, 303} or bool(preview)


def _discover_login_paths(context: ExecutionContext) -> list[str]:
    config = context.test_config
    if not config:
        return []

    candidates = []
    if config.login_endpoint:
        candidates.append(config.login_endpoint)
    candidates.extend(config.login_candidates)
    if config.auth_type == "nextauth":
        candidates.extend(["/api/auth/callback/credentials"])
    for route in config.routes:
        lower = route.path.lower()
        if "POST" in route.methods and any(token in lower for token in ("login", "signin")):
            candidates.append(route.path)
    return _dedupe_preserving_order(_filter_candidate_paths(candidates, "POST", context))


def _select_seeded_user(context: ExecutionContext) -> SeededUser | None:
    config = context.test_config
    if not config or not config.seeded_users:
        return None

    claim_key = context.claim.id.lower()
    non_admin_users = [user for user in config.seeded_users if user.role.lower() not in {"admin", "administrator"}]
    if claim_key == "admin-auth" and non_admin_users:
        return non_admin_users[0]
    if claim_key.startswith("admin-"):
        return config.admin_user() or config.any_user()
    return non_admin_users[0] if non_admin_users else config.any_user()


def _apply_seeded_user(context: ExecutionContext, user: SeededUser) -> None:
    context.variables["email"] = user.email
    context.variables["password"] = user.password
    context.variables["current_user_email"] = user.email
    if user.role.lower() in {"admin", "administrator"}:
        context.variables["admin_email"] = user.email
        context.variables["admin_password"] = user.password


def _is_registration_step(step: dict[str, Any]) -> bool:
    if str(step.get("action", "")).lower() != "http":
        return False
    if str(step.get("method", "GET")).upper() != "POST":
        return False
    return any(token in path.lower() for path in _get_candidate_paths(step) for token in ("register", "signup"))


def _is_login_step(step: dict[str, Any]) -> bool:
    if str(step.get("action", "")).lower() != "http":
        return False
    if str(step.get("method", "GET")).upper() != "POST":
        return False
    return any(token in path.lower() for path in _get_candidate_paths(step) for token in ("login", "signin"))


def _filter_candidate_paths(
    candidate_paths: list[str],
    method: str,
    context: ExecutionContext,
) -> list[str]:
    if not context.test_config or not context.test_config.routes:
        return _dedupe_preserving_order(candidate_paths)

    filtered: list[str] = []
    for path in candidate_paths:
        if not _is_api_path(path):
            filtered.append(path)
            continue
        if _is_nextauth_internal_path(path, context.test_config):
            filtered.append(path)
            continue
        if _route_exists_in_config(context.test_config, path, method):
            filtered.append(path)
    return _dedupe_preserving_order(filtered)


def _is_api_path(path: str) -> bool:
    return _normalize_path(path).startswith("/api/")


def _is_nextauth_internal_path(path: str, config: TestConfig) -> bool:
    normalized = _normalize_path(path)
    return config.auth_type == "nextauth" and normalized in {
        "/api/auth/csrf",
        "/api/auth/callback/credentials",
        "/api/auth/session",
    }


def _route_exists_in_config(config: TestConfig, path: str, method: str) -> bool:
    normalized_candidate = _normalize_path(path)
    for route in config.routes:
        if method.upper() not in route.methods:
            continue
        if re.fullmatch(_route_match_pattern(route.path), normalized_candidate):
            return True
    return False


def _route_match_pattern(path: str) -> str:
    normalized = _normalize_path(path)
    escaped = re.escape(normalized)
    escaped = re.sub(r"\\\{\\\{[^}]+\\\}\\\}", r"[^/]+", escaped)
    escaped = re.sub(r":[^/]+", r"[^/]+", escaped)
    return escaped


def _normalize_path(path: str) -> str:
    trimmed = path.split("?", 1)[0].strip()
    if not trimmed.startswith("/"):
        trimmed = f"/{trimmed}"
    if trimmed != "/" and trimmed.endswith("/"):
        trimmed = trimmed[:-1]
    return trimmed


def _has_auth_session_cookie(session: requests.Session) -> bool:
    return any(cookie.name in _AUTH_SESSION_COOKIE_NAMES for cookie in session.cookies)


def _get_candidate_paths(step: dict[str, Any]) -> list[str]:
    candidate_paths = step.get("candidate_paths")
    if isinstance(candidate_paths, list) and candidate_paths:
        return [str(item) for item in candidate_paths]
    path = step.get("path")
    if path:
        return [str(path)]
    return []


def _expand_candidate_paths(method: str, raw_paths: list[str]) -> list[str]:
    expanded: list[str] = []
    for raw_path in raw_paths:
        expanded.append(raw_path)
        lower = raw_path.lower()
        if "register" in lower or "signup" in lower:
            expanded.extend(["/api/auth/register", "/api/register", "/api/signup", "/api/auth/signup", "/register", "/signup"])
        elif "login" in lower or "signin" in lower:
            expanded.extend(["/api/auth/login", "/api/login", "/api/signin", "/api/auth/signin", "/login", "/signin"])
        elif "products" in lower or "product" in lower:
            expanded.extend(["/api/products", "/api/product", "/api/catalog/products", "/products", "/catalog", "/shop"])
        elif "cart" in lower:
            expanded.extend(["/api/cart", "/api/cart/add", "/api/cart/items", "/cart"])
        elif "orders" in lower or "order" in lower:
            expanded.extend(["/api/admin/orders", "/api/orders", "/admin/orders", "/orders"])
        elif "checkout" in lower:
            expanded.extend(["/checkout", "/cart/checkout", "/api/checkout", "/api/orders"])
        elif "admin" in lower:
            expanded.extend(["/admin", "/admin/orders", "/admin/products", "/api/admin/orders", "/api/admin/products"])

    if method in {"POST", "PATCH", "PUT", "DELETE"}:
        return _dedupe_preserving_order(expanded)
    return _dedupe_preserving_order(expanded)


def _check_http_expectations(
    step: dict[str, Any],
    response: requests.Response,
    context: ExecutionContext,
) -> tuple[bool, str]:
    expect_status = [int(x) for x in step.get("expect_status", [200])]
    if response.status_code not in expect_status:
        return False, _preview_response(response)

    body_text = response.text or ""
    body_text_lower = body_text.lower()
    body_tokens = step.get("expect_body_contains", [])
    if isinstance(body_tokens, str):
        body_tokens = [body_tokens]
    rendered_tokens = []
    for token in body_tokens:
        rendered = _render_value(token, context.variables)
        if rendered in (_MISSING, None):
            return False, f"missing context value for token {token}"
        rendered_tokens.append(str(rendered))
    if rendered_tokens:
        match_mode = str(step.get("match_body", "all")).lower()
        matches = [token.lower() in body_text_lower for token in rendered_tokens]
        if (match_mode == "any" and not any(matches)) or (match_mode != "any" and not all(matches)):
            return False, _preview_response(response)

    json_keys = step.get("expect_json_keys", [])
    if isinstance(json_keys, str):
        json_keys = [json_keys]
    if json_keys:
        try:
            payload = response.json()
        except ValueError:
            return False, f"expected JSON body with keys {json_keys}, got {response.text[:200]}"
        missing_keys = [key for key in json_keys if not _json_path_exists(payload, key)]
        if missing_keys:
            return False, f"missing JSON keys {missing_keys} in {json.dumps(payload)[:200]}"

    return True, _preview_response(response)


def _capture_response_state(step: dict[str, Any], response: requests.Response, context: ExecutionContext) -> None:
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if step.get("capture_auth") and payload is not None:
        token = _find_first_value(payload, ["token", "accessToken", "access_token", "jwt"])
        if token:
            context.variables["auth_token"] = token
            context.session.headers["Authorization"] = f"Bearer {token}"
        email = _find_first_value(payload, ["email", "user.email"])
        if email:
            context.variables["registered_email"] = email
        user_id = _find_first_value(payload, ["id", "user.id"])
        if user_id:
            context.variables["user_id"] = user_id

    extract = step.get("extract")
    if isinstance(extract, dict) and payload is not None:
        extract_type = str(extract.get("type", ""))
        if extract_type == "first_product":
            _store_products(context.variables, payload, limit=1)
        elif extract_type == "first_two_products":
            _store_products(context.variables, payload, limit=2)

    for var_name, path in step.get("store_json_fields", {}).items():
        if payload is None:
            break
        value = _lookup_path(payload, str(path), default=_MISSING)
        if value is not _MISSING:
            context.variables[var_name] = value


def _store_products(variables: dict[str, Any], payload: Any, *, limit: int) -> None:
    products = _find_product_list(payload)
    if not products:
        return
    first = products[0]
    variables["product_id"] = first.get("id") or first.get("_id") or first.get("slug") or first.get("productId")
    variables["product_name"] = first.get("name") or first.get("title") or first.get("productName")
    variables["product_slug"] = first.get("slug") or first.get("id") or first.get("_id")
    variables["product_price"] = first.get("price") or first.get("amount")
    if limit >= 2 and len(products) > 1:
        second = products[1]
        variables["second_product_id"] = second.get("id") or second.get("_id") or second.get("slug") or second.get("productId")
        variables["second_product_name"] = second.get("name") or second.get("title") or second.get("productName")
        variables["second_product_price"] = second.get("price") or second.get("amount")


def _find_product_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("products", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list) and any(isinstance(item, dict) for item in value):
                return [item for item in value if isinstance(item, dict)]
        if all(key in payload for key in ("id", "price")):
            return [payload]
        for value in payload.values():
            nested = _find_product_list(value)
            if nested:
                return nested
    return []


def _find_first_value(payload: Any, paths: list[str]) -> Any:
    for path in paths:
        value = _lookup_path(payload, path, default=_MISSING)
        if value is not _MISSING:
            return value
    return None


def _json_path_exists(payload: Any, path: str) -> bool:
    return _lookup_path(payload, path, default=_MISSING) is not _MISSING


def _lookup_path(payload: Any, path: str, *, default: Any = _MISSING) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


def _render_value(value: Any, variables: dict[str, Any]) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        rendered: dict[str, Any] = {}
        for key, item in value.items():
            item_rendered = _render_value(item, variables)
            if item_rendered is _MISSING:
                return _MISSING
            rendered[key] = item_rendered
        return rendered
    if isinstance(value, list):
        rendered_list = []
        for item in value:
            item_rendered = _render_value(item, variables)
            if item_rendered is _MISSING:
                return _MISSING
            rendered_list.append(item_rendered)
        return rendered_list
    if not isinstance(value, str):
        return value

    full_match = _FULL_TEMPLATE_RE.match(value)
    if full_match:
        return _lookup_path(variables, full_match.group(1), default=_MISSING)

    missing = False

    def replace(match: re.Match[str]) -> str:
        nonlocal missing
        resolved = _lookup_path(variables, match.group(1), default=_MISSING)
        if resolved is _MISSING:
            missing = True
            return match.group(0)
        return str(resolved)

    rendered = _PARTIAL_TEMPLATE_RE.sub(replace, value)
    return _MISSING if missing else rendered


def _build_url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base_url}{path}"


def _pick_best_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    def score(item: dict[str, Any]) -> tuple[int, int]:
        if "error" in item:
            return (0, 0)
        status = int(item.get("status", 0))
        exists_score = 2 if status not in {404, 405, 501} else 0
        healthy_score = 1 if status < 500 else 0
        return (exists_score + healthy_score, status)

    return max(attempts, key=score)


def _format_http_command(method: str, url: str, body: Any) -> str:
    if body is None:
        return f"{method} {url}"
    return f"{method} {url} body={json.dumps(body, sort_keys=True)}"


def _http_expectation_text(step: dict[str, Any]) -> str:
    parts = [f"status in {step.get('expect_status', [])}"]
    if step.get("expect_body_contains"):
        parts.append(f"body contains {step['expect_body_contains']}")
    if step.get("expect_json_keys"):
        parts.append(f"JSON has keys {step['expect_json_keys']}")
    return ", ".join(parts)


def _preview_response(response: requests.Response) -> str:
    preview = (response.text or "").replace("\n", " ").strip()
    return preview[:200] or "(empty)"


def _first_path_label(paths: list[str]) -> str:
    return paths[0] if paths else "<missing path>"


def _step_discovery_key(step: dict[str, Any]) -> str:
    raw_paths = _get_candidate_paths(step)
    if raw_paths:
        return raw_paths[0]
    return json.dumps(step, sort_keys=True)


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def certify(
    project_dir: Path,
    intent: str,
    config: dict[str, Any] | None = None,
    *,
    port_override: int | None = None,
) -> BaselineResult:
    """Full certification pipeline: compile → classify → adapt → baseline."""

    config = dict(config or {})
    effective_port = port_override or config.get("port_override")

    matrix = asyncio.run(compile_intent(intent, config=config))
    profile = classify(project_dir)
    if effective_port is not None:
        profile.port = int(effective_port)
        profile.extra["reuse_existing_app"] = True
    test_config = analyze_project(project_dir)
    return run_baseline(project_dir, matrix, profile, test_config=test_config)


def save_result(result: BaselineResult, path: Path) -> None:
    """Save baseline result to JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))


def print_report(result: BaselineResult) -> None:
    """Print a human-readable certification report."""

    print()
    print(f"  {'CERTIFIED' if result.certified else 'NOT CERTIFIED'}")
    print(f"  Product: {result.product_dir}")
    print(f"  Type: {result.product_type}")
    print(f"  App started: {'yes' if result.started else 'NO'}")
    print(f"  Claims: {result.claims_passed}/{result.claims_tested} passed")
    if result.claims_not_implemented:
        print(f"  Not implemented: {result.claims_not_implemented}")
    if result.claims_blocked:
        print(f"  Blocked by harness: {result.claims_blocked}")
    if result.claims_not_applicable:
        print(f"  Not applicable: {result.claims_not_applicable}")
    if result.hard_fails > 0:
        print(f"  HARD FAILS: {result.hard_fails} critical claims failed")
    print(f"  Duration: {result.duration_s}s")
    print()

    for claim in result.results:
        icon = {
            "pass": "PASS",
            "fail": "FAIL",
            "not_implemented": "NIMP",
            "blocked_by_harness": "BLKD",
            "not_applicable": "N/A",
        }.get(claim.outcome, claim.outcome.upper())
        flag = " [HARD FAIL]" if claim.outcome == "fail" and claim.hard_fail else ""
        print(f"  {icon}  {claim.claim_id}: {claim.claim_description}{flag}")
        if claim.outcome != "pass":
            for item in claim.evidence:
                if item.outcome == "pass":
                    continue
                item_icon = {
                    "fail": "FAIL",
                    "blocked_by_harness": "SKIP",
                    "not_implemented": "NIMP",
                    "not_applicable": "N/A",
                }.get(item.outcome, item.outcome.upper())
                print(f"        {item_icon} {item.command}")
                print(f"        expected: {item.expected}")
                print(f"        actual:   {item.actual[:150]}")
    print()
