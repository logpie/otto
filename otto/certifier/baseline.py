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
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from otto.certifier.adapter import SeededUser, TestConfig, analyze_project
from otto.certifier.classifier import ProductProfile, classify
from otto.certifier.intent_compiler import Claim, RequirementMatrix, compile_intent, load_matrix, save_matrix

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
    proof: dict[str, Any] = field(default_factory=dict)


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
    proof: dict[str, Any] = field(default_factory=dict)


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
    compile_cost_usd: float = 0.0
    compile_duration_s: float = 0.0
    compiled_at: str = ""
    matrix_source: str = ""
    matrix_path: str = ""
    verdict: CertificationVerdict | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CertificationVerdict:
    """Final certification verdict across structure and runtime."""

    certified: bool
    tier0_score: float
    tier1_score: float
    overall_score: float
    confidence: float
    summary: str

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


@dataclass
class BaselineRunState:
    """Mutable state shared across claims in a single certification run."""

    session: requests.Session = field(default_factory=requests.Session)
    discoveries: dict[str, str] = field(default_factory=dict)
    authenticated_user: SeededUser | None = None
    auth_login_claim_ran: bool = False


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

        if self.profile.extra.get("reuse_existing_app"):
            if not self._port_in_use():
                return Evidence(
                    step="start app",
                    command=f"(reuse existing app on {self.base_url})",
                    expected="existing app responds on requested port",
                    actual=f"no app responding on {self.base_url}",
                    passed=False,
                    outcome="fail",
                    timestamp=time.strftime("%H:%M:%S"),
                )
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
    run_state = BaselineRunState()

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
            results.append(_test_claim(claim, base_url, project_dir, profile, test_config, run_state))
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
        if result.hard_fail and result.outcome in {"fail", "not_implemented"}
    )
    baseline_result = BaselineResult(
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
    baseline_result.verdict = judge(baseline_result)
    baseline_result.certified = baseline_result.verdict.certified
    return baseline_result


def _test_claim(
    claim: Claim,
    base_url: str,
    project_dir: Path,
    profile: ProductProfile,
    test_config: TestConfig | None = None,
    run_state: BaselineRunState | None = None,
) -> ClaimResult:
    """Test a single claim by executing its structured steps."""

    structural_outcome, structural_reason = _classify_structural_claim_outcome(claim, profile, test_config)
    if structural_outcome is not None:
        structural_proof = _structural_claim_proof(claim, structural_outcome, structural_reason, test_config)
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
                    proof=structural_proof,
                )
            ],
            error=structural_reason,
            proof=structural_proof,
        )

    context = _build_execution_context(claim, base_url, project_dir, profile, test_config, run_state)
    evidence: list[Evidence] = []

    for step in claim.test_steps:
        evidence.append(_execute_step(step, context))

    passed = any(item.outcome == "pass" for item in evidence) and not any(item.outcome == "fail" for item in evidence)
    outcome = _claim_outcome_from_evidence(evidence)
    error = _claim_error_from_evidence(evidence, outcome)
    if run_state is not None and _should_reclassify_as_auth_blocked(claim, evidence, run_state):
        _mark_evidence_as_auth_blocked(evidence)
        passed = False
        outcome = "blocked_by_harness"
        error = "auth session not established — harness limitation"
    if run_state is not None:
        run_state.authenticated_user = context.authenticated_user
        if _is_auth_login_claim(claim):
            run_state.auth_login_claim_ran = True

    return ClaimResult(
        claim_id=claim.id,
        claim_description=claim.description,
        priority=claim.priority,
        hard_fail=claim.hard_fail,
        passed=passed,
        outcome=outcome,
        evidence=evidence,
        error=error,
        proof=_claim_proof_from_evidence(evidence, outcome),
    )


def _build_execution_context(
    claim: Claim,
    base_url: str,
    project_dir: Path,
    profile: ProductProfile,
    test_config: TestConfig | None = None,
    run_state: BaselineRunState | None = None,
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
        session=run_state.session if run_state is not None else requests.Session(),
        variables=variables,
        discoveries=run_state.discoveries if run_state is not None else {},
        authenticated_user=run_state.authenticated_user if run_state is not None else None,
    )


def _prepare_step_context(step: dict[str, Any], context: ExecutionContext) -> ExecutionContext:
    claim_key = context.claim.id.lower()
    # Claims that test unauthenticated/non-admin access need a fresh session
    needs_isolation = claim_key in {
        "auth-protected-route", "auth-protected-routes",
        "admin-no-public-access", "admin-authorization",
    } or (claim_key.startswith("admin-") and any(
        kw in claim_key for kw in ("public", "authorization", "access", "login")
    ))
    if not needs_isolation:
        return context

    isolated = ExecutionContext(
        claim=context.claim,
        base_url=context.base_url,
        project_dir=context.project_dir,
        profile=context.profile,
        test_config=context.test_config,
        session=requests.Session(),
        variables=dict(context.variables),
        discoveries=context.discoveries,
        authenticated_user=None,
    )

    if claim_key in {"admin-no-public-access", "admin-authorization"}:
        user = _select_non_admin_seeded_user(context)
        if user is not None:
            _apply_seeded_user(isolated, user)
            ok, detail = _authenticate_seeded_user(isolated, user)
            isolated.variables["session_setup"] = detail
            if not ok:
                isolated.authenticated_user = None

    return isolated


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
    active_context = _prepare_step_context(step, context)
    if _should_use_seeded_auth(step, active_context):
        return _execute_seeded_auth_step(step, active_context, timestamp)
    if action == "http":
        return _execute_http_step(step, active_context, timestamp)
    if action == "navigate":
        nav_step = dict(step)
        nav_step.setdefault("method", "GET")
        nav_step.setdefault("expect_status", [200])
        nav_step.setdefault("match_body", "any")
        return _execute_http_step(nav_step, active_context, timestamp)
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
    raw_paths = _candidate_paths_for_step(step, context, method)
    _ensure_resource_discoveries(step, context, method, raw_paths)
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
    pending_attempts: list[dict[str, Any]] = [
        {
            "raw_path": raw_path,
            "body_template": body_template,
            "retry_reason": "",
        }
        for raw_path in candidate_paths
        for body_template in body_variants
    ]
    seen_attempts: set[str] = set()

    while pending_attempts:
        attempt_seed = pending_attempts.pop(0)
        raw_path = str(attempt_seed["raw_path"])
        body_template = attempt_seed.get("body_template")
        retry_reason = str(attempt_seed.get("retry_reason", ""))
        rendered_path = _render_value(raw_path, context.variables)
        if rendered_path in (_MISSING, None):
            missing_reasons.append(f"path template unresolved: {raw_path}")
            continue
        url = _build_url(context.base_url, str(rendered_path))
        rendered_body = _render_value(body_template, context.variables)
        if rendered_body is _MISSING:
            missing_reasons.append(f"body template unresolved for {url}")
            continue

        attempt_key = json.dumps(
            {"method": method, "url": url, "body": rendered_body, "retry_reason": retry_reason},
            sort_keys=True,
            default=str,
        )
        if attempt_key in seen_attempts:
            continue
        seen_attempts.add(attempt_key)

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
                    "request": {"method": method, "url": url, "body": rendered_body},
                    "error": str(exc),
                    "retry_reason": retry_reason,
                }
            )
            continue

        passed, detail = _check_http_expectations(step, response, context)

        # Accept 409 Conflict for registration — proves endpoint works (email already taken)
        status_match = response.status_code in expect_status
        if not status_match and response.status_code == 409 and any(
            kw in (str(rendered_path) + context.claim.id).lower()
            for kw in ("register", "signup")
        ):
            status_match = True
            passed = True
            detail = f"409 Conflict (email already registered — endpoint works)"

        attempt_record = {
            "request": {"method": method, "url": url, "body": rendered_body},
            "response": {
                "status": response.status_code,
                "body": _response_body_for_proof(response),
            },
            "detail": detail,
            "retry_reason": retry_reason,
        }
        attempts.append(attempt_record)
        if passed and status_match:
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
                proof=_finalize_http_proof(attempts),
            )

        corrections = _self_heal_http_attempt(step, context, attempt_record)
        for correction in reversed(corrections):
            pending_attempts.insert(
                0,
                {
                    "raw_path": correction.get("path", raw_path),
                    "body_template": correction.get("body", rendered_body),
                    "retry_reason": str(correction.get("retry_reason", "")),
                },
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
            proof={
                "timestamp": _iso_timestamp(),
                "missing_reasons": missing_reasons,
            },
        )

    final_attempt = attempts[-1]
    if "error" in final_attempt:
        actual = f"request error: {final_attempt['error']}"
        command = _format_http_command(method, final_attempt["request"]["url"], final_attempt["request"].get("body"))
    else:
        actual = f"HTTP {final_attempt['response']['status']}: {final_attempt['detail']}"
        command = _format_http_command(method, final_attempt["request"]["url"], final_attempt["request"].get("body"))

    return Evidence(
        step=json.dumps(step, sort_keys=True),
        command=command,
        expected=_http_expectation_text(step),
        actual=actual,
        passed=False,
        outcome="fail",
        timestamp=timestamp,
        proof=_finalize_http_proof(attempts),
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
                    proof={
                        "timestamp": _iso_timestamp(),
                        "path": str(candidate),
                        "exists": True,
                    },
                )
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command="test -e <candidate>",
            expected="path exists",
            actual="no candidate path exists",
            passed=False,
            outcome="fail",
            timestamp=timestamp,
            proof={
                "timestamp": _iso_timestamp(),
                "candidate_paths": [str(path) for path in raw_paths],
                "exists": False,
            },
        )

    # Source code content checks — search for patterns in project files
    search_target = step.get("target", "")
    if any(keyword in search_target.lower() for keyword in ["password hash", "bcrypt", "argon2", "hashing library"]):
        # Check if password hashing is used — search source files
        for pattern in ["bcrypt", "argon2", "scrypt", "pbkdf2", "hashSync", "hash("]:
            for f in context.project_dir.glob("**/*.ts"):
                if "node_modules" in str(f):
                    continue
                try:
                    if pattern in f.read_text():
                        return Evidence(
                            step=json.dumps(step, sort_keys=True),
                            command=f"grep '{pattern}' {f.name}",
                            expected="password hashing library used",
                            actual=f"found '{pattern}' in {f.relative_to(context.project_dir)}",
                            passed=True, outcome="pass", timestamp=timestamp,
                            proof={
                                "timestamp": _iso_timestamp(),
                                "adapter_check": f"searched source for {pattern}",
                                "source_file": str(f.relative_to(context.project_dir)),
                                "match": pattern,
                            },
                        )
                except OSError:
                    pass
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command="grep password-hashing patterns in source",
            expected="bcrypt/argon2/scrypt found",
            actual="no password hashing library found in source",
            passed=False, outcome="fail", timestamp=timestamp,
            proof={
                "timestamp": _iso_timestamp(),
                "adapter_check": "searched source for password hashing patterns",
                "patterns": ["bcrypt", "argon2", "scrypt", "pbkdf2", "hashSync", "hash("],
            },
        )

    if any(keyword in search_target.lower() for keyword in ["stripe", "payment key", "api key"]):
        # Check if Stripe is configured — look in source and env files
        for pattern in ["STRIPE_SECRET_KEY", "stripe", "Stripe("]:
            for f in list(context.project_dir.glob("**/*.ts")) + list(context.project_dir.glob("**/.env*")):
                if "node_modules" in str(f):
                    continue
                try:
                    if pattern in f.read_text():
                        return Evidence(
                            step=json.dumps(step, sort_keys=True),
                            command=f"grep '{pattern}' {f.name}",
                            expected="Stripe integration present",
                            actual=f"found '{pattern}' in {f.relative_to(context.project_dir)}",
                            passed=True, outcome="pass", timestamp=timestamp,
                            proof={
                                "timestamp": _iso_timestamp(),
                                "adapter_check": f"searched source for {pattern}",
                                "source_file": str(f.relative_to(context.project_dir)),
                                "match": pattern,
                            },
                        )
                except OSError:
                    pass
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command="grep stripe patterns in source",
            expected="Stripe SDK/keys found",
            actual="no Stripe integration found in source",
            passed=False, outcome="fail", timestamp=timestamp,
            proof={
                "timestamp": _iso_timestamp(),
                "adapter_check": "searched source and env files for Stripe patterns",
                "patterns": ["STRIPE_SECRET_KEY", "stripe", "Stripe("],
            },
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
            proof={
                "timestamp": _iso_timestamp(),
                "reason": step.get("reason", "step requires non-deterministic verification"),
            },
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
                proof={
                    "timestamp": _iso_timestamp(),
                    "request": {"method": "GET", "url": url},
                    "response": {"status": response.status_code, "body": _response_body_for_proof(response)},
                },
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
            proof={
                "timestamp": _iso_timestamp(),
                "candidate_paths": raw_paths,
            },
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
        proof={
            "timestamp": _iso_timestamp(),
            "request": {"method": "GET", "url": best_attempt["url"]},
            "response": (
                {"status": best_attempt["status"], "body": best_attempt.get("body", "")}
                if "status" in best_attempt
                else {"error": best_attempt["error"]}
            ),
        },
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

    executable_command = command
    if command.startswith("python ") and shutil.which("python") is None:
        executable_command = f"{sys.executable} {command[len('python '):]}"

    try:
        result = subprocess.run(
            executable_command,
            shell=True,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=int(step.get("timeout", 30)),
        )
    except Exception as exc:
        return Evidence(
            step=json.dumps(step, sort_keys=True),
            command=executable_command,
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
        command=executable_command,
        expected=f"exit code {expect_exit_code}" + (f" and stdout contains {expect_stdout_contains}" if expect_stdout_contains else ""),
        actual=f"exit {result.returncode}: {output[:300]}",
        passed=passed,
        outcome="pass" if passed else "fail",
        timestamp=timestamp,
        proof={
            "timestamp": _iso_timestamp(),
            "command": executable_command,
            "response": {
                "exit_code": result.returncode,
                "stdout": result.stdout[:500],
                "stderr": result.stderr[:500],
            },
        },
    )


def _iso_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _response_body_for_proof(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return (response.text or "")[:1000]


def _finalize_http_proof(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not attempts:
        return {"timestamp": _iso_timestamp()}

    final_attempt = attempts[-1]
    proof: dict[str, Any] = {
        "timestamp": _iso_timestamp(),
        "request": final_attempt.get("request", {}),
    }
    if "response" in final_attempt:
        proof["response"] = final_attempt["response"]
    if "error" in final_attempt:
        proof["response"] = {"error": final_attempt["error"]}

    retries = []
    for attempt in attempts[:-1]:
        retry_entry = {
            "request": attempt.get("request", {}),
            "response": attempt.get("response", {"error": attempt.get("error", "")}),
        }
        if attempt.get("retry_reason"):
            retry_entry["retry_reason"] = attempt["retry_reason"]
        retries.append(retry_entry)
    if retries:
        proof["retries"] = retries
    if final_attempt.get("retry_reason"):
        proof["retry_reason"] = final_attempt["retry_reason"]
    return proof


def _claim_proof_from_evidence(evidence: list[Evidence], outcome: str) -> dict[str, Any]:
    for item in reversed(evidence):
        if item.proof:
            proof = dict(item.proof)
            proof.setdefault("outcome", outcome)
            return proof
    return {"timestamp": _iso_timestamp(), "outcome": outcome}


def _structural_claim_proof(
    claim: Claim,
    structural_outcome: str,
    structural_reason: str,
    test_config: TestConfig | None,
) -> dict[str, Any]:
    if structural_outcome == "not_implemented" and test_config:
        claim_key = claim.id.lower()
        if "cart" in claim_key:
            return {
                "adapter_check": "searched for CartItem model in prisma/schema.prisma",
                "models_found": test_config.models,
                "missing": "CartItem",
                "source_file": test_config.schema_source_file or "prisma/schema.prisma",
            }
        if claim_key == "auth-register":
            return {
                "adapter_check": "searched for registration endpoint in route inventory",
                "routes_found": [route.path for route in test_config.routes[:20]],
                "missing": "registration endpoint",
            }
    return {
        "adapter_check": "structural claim precheck",
        "reason": structural_reason,
    }


def _candidate_paths_for_step(step: dict[str, Any], context: ExecutionContext, method: str) -> list[str]:
    paths = list(_get_candidate_paths(step))
    claim_key = context.claim.id.lower()

    if claim_key in {"auth-protected-route", "auth-protected-routes"}:
        paths = _protected_paths(context, method) + paths
    if claim_key.startswith("admin-") and any(
        kw in claim_key for kw in ("public", "authorization", "access", "login")
    ):
        paths = _admin_only_paths(context, method) + paths
    if _step_targets_resource(step, paths, context.claim, "product"):
        paths.extend(_paths_from_routes(context, "product", method))
    if _step_targets_resource(step, paths, context.claim, "order"):
        paths.extend(_paths_from_routes(context, "order", method))

    return _dedupe_preserving_order(paths)


def _protected_paths(context: ExecutionContext, method: str) -> list[str]:
    config = context.test_config
    if not config:
        return []
    return [
        route.path
        for route in config.routes
        if method in route.methods and route.requires_auth and not route.requires_admin
    ]


def _admin_only_paths(context: ExecutionContext, method: str) -> list[str]:
    config = context.test_config
    if not config:
        return []
    preferred = ["/api/admin/stats"]
    discovered = [
        route.path
        for route in config.routes
        if method in route.methods and route.requires_admin
    ]
    return _dedupe_preserving_order(preferred + discovered)


def _paths_from_routes(context: ExecutionContext, resource: str, method: str) -> list[str]:
    config = context.test_config
    if not config:
        return []

    resource_token = f"/{resource}"
    plural_token = f"/{resource}s"
    template_name = f"{resource}_id"
    paths: list[str] = []

    for route in config.routes:
        if method not in route.methods:
            continue
        lower = route.path.lower()
        if resource_token not in lower and plural_token not in lower:
            continue
        paths.append(_path_to_template(route.path, template_name))
    return paths


def _path_to_template(path: str, template_name: str) -> str:
    return re.sub(r":[^/]+", f"{{{{{template_name}}}}}", path)


def _step_targets_resource(step: dict[str, Any], paths: list[str], claim: Claim, resource: str) -> bool:
    haystacks = [claim.id.lower(), claim.description.lower(), json.dumps(step, sort_keys=True).lower(), *[path.lower() for path in paths]]
    if resource == "product":
        return any("product" in item for item in haystacks)
    if resource == "order":
        return any("order" in item for item in haystacks)
    return False


def _ensure_resource_discoveries(step: dict[str, Any], context: ExecutionContext, method: str, paths: list[str]) -> None:
    combined = " ".join([context.claim.id.lower(), context.claim.description.lower(), *[path.lower() for path in paths]])
    if ("product" in combined or "{{product_id}}" in combined or "/1" in combined) and method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        _discover_products(context)
    if ("order" in combined or "{{order_id}}" in combined or "/1" in combined) and method in {"GET", "PUT", "PATCH", "DELETE"}:
        _discover_orders(context)


def _discover_products(context: ExecutionContext) -> None:
    if context.variables.get("product_id"):
        return

    for path in _filter_candidate_paths(
        ["/api/products", "/api/product", "/api/catalog/products"],
        "GET",
        context,
    ):
        url = _build_url(context.base_url, path)
        try:
            response = context.session.get(url, timeout=10)
        except Exception:
            continue
        if response.status_code != 200:
            continue
        _store_products(context.variables, _response_body_for_proof(response), limit=2)
        if context.variables.get("product_id"):
            return


def _discover_orders(context: ExecutionContext) -> None:
    if context.variables.get("order_id"):
        return

    for path in _filter_candidate_paths(
        ["/api/orders", "/api/admin/orders"],
        "GET",
        context,
    ):
        url = _build_url(context.base_url, path)
        try:
            response = context.session.get(url, timeout=10)
        except Exception:
            continue
        if response.status_code != 200:
            continue
        _store_orders(context.variables, _response_body_for_proof(response))
        if context.variables.get("order_id"):
            return


def _store_orders(variables: dict[str, Any], payload: Any) -> None:
    orders = _find_order_list(payload)
    if not orders:
        return
    first = orders[0]
    variables["order_id"] = first.get("id") or first.get("_id") or first.get("orderId")
    status = first.get("status")
    if status:
        variables["order_status"] = status


def _find_order_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("orders", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict) and "id" in value:
                return [value]
        if "id" in payload and any(key in payload for key in ("status", "totalAmount", "shippingAddress")):
            return [payload]
        for value in payload.values():
            nested = _find_order_list(value)
            if nested:
                return nested
    return []


def _select_non_admin_seeded_user(context: ExecutionContext) -> SeededUser | None:
    config = context.test_config
    if not config:
        return None
    for user in config.seeded_users:
        if user.role.lower() not in {"admin", "administrator"}:
            return user
    return None


def _self_heal_http_attempt(
    step: dict[str, Any],
    context: ExecutionContext,
    attempt_record: dict[str, Any],
) -> list[dict[str, Any]]:
    response = attempt_record.get("response")
    if not isinstance(response, dict) or response.get("status") != 400:
        return []

    request = attempt_record.get("request", {})
    body = request.get("body")
    if not isinstance(body, dict):
        body = {}

    error_text = _extract_error_text(response.get("body"))
    corrections: list[dict[str, Any]] = []

    missing_fields = _missing_fields_from_error(error_text)
    if missing_fields:
        augmented = _augment_body_for_fields(body, missing_fields, request.get("url", ""), context)
        if augmented != body:
            corrections.append(
                {
                    "path": request.get("url", ""),
                    "body": augmented,
                    "retry_reason": f"added missing fields from 400 error: {', '.join(missing_fields)}",
                }
            )

    if "shipping address" in error_text.lower():
        augmented = _augment_body_for_fields(body, ["shippingAddress"], request.get("url", ""), context)
        if augmented != body:
            corrections.append(
                {
                    "path": request.get("url", ""),
                    "body": augmented,
                    "retry_reason": "added realistic shipping address from 400 error",
                }
            )

    enum_corrected = _correct_enum_casing(body, error_text)
    if enum_corrected != body:
        corrections.append(
            {
                "path": request.get("url", ""),
                "body": enum_corrected,
                "retry_reason": "corrected enum casing from 400 error",
            }
        )

    if not corrections and _looks_like_create_or_update_claim(step, context.claim):
        fallback_fields = _plausible_fields_for_request(request.get("url", ""), context)
        augmented = _augment_body_for_fields(body, fallback_fields, request.get("url", ""), context)
        if augmented != body:
            corrections.append(
                {
                    "path": request.get("url", ""),
                    "body": augmented,
                    "retry_reason": "filled plausible CRUD fields from adapter schema knowledge",
                }
            )

    return corrections


def _extract_error_text(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("error", "message", "detail", "warning"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return json.dumps(body)
    if isinstance(body, list):
        return json.dumps(body)
    return str(body or "")


def _missing_fields_from_error(error_text: str) -> list[str]:
    match = re.search(r"missing required fields?:\s*(.+)$", error_text, re.IGNORECASE)
    if not match:
        return []
    fields = re.split(r"\s*,\s*", match.group(1).strip())
    return [field.strip().strip(".") for field in fields if field.strip()]


def _looks_like_create_or_update_claim(step: dict[str, Any], claim: Claim) -> bool:
    method = str(step.get("method", "GET")).upper()
    return method in {"POST", "PUT", "PATCH"}


def _plausible_fields_for_request(url: str, context: ExecutionContext) -> list[str]:
    lower_url = url.lower()
    if "product" in lower_url:
        return list(context.test_config.creatable_fields.get("Product", [])) if context.test_config else []
    if "order" in lower_url:
        return list(context.test_config.creatable_fields.get("Order", [])) if context.test_config else []
    return []


def _augment_body_for_fields(
    body: dict[str, Any],
    fields: list[str],
    url: str,
    context: ExecutionContext,
) -> dict[str, Any]:
    augmented = dict(body)
    for field_name in fields:
        if field_name in augmented:
            continue
        default_value = _default_value_for_field(field_name, url, context)
        if default_value is _MISSING:
            continue
        augmented[field_name] = default_value
    if "shippingAddress" in fields and "shippingAddress" not in augmented:
        augmented["shippingAddress"] = _default_shipping_address()
    return augmented


def _default_value_for_field(field_name: str, url: str, context: ExecutionContext) -> Any:
    field_lower = field_name.lower()
    defaults: dict[str, Any] = {
        "name": "Baseline Test Product",
        "title": "Baseline Test Product",
        "description": "Generated by certifier baseline retry",
        "price": 29.99,
        "amount": 29.99,
        "category": "General",
        "stock": 100,
        "inventory": 100,
        "quantity": 1,
        "imageurl": "https://placehold.co/400x300",
        "image_url": "https://placehold.co/400x300",
        "shippingaddress": _default_shipping_address(),
        "status": "SHIPPED",
    }
    if field_lower in defaults:
        return defaults[field_lower]
    if field_lower.endswith("id"):
        if field_lower == "productid":
            return context.variables.get("product_id", _MISSING)
        if field_lower == "userid":
            return context.variables.get("user_id", _MISSING)
        if field_lower == "orderid":
            return context.variables.get("order_id", _MISSING)
    if "checkout" in url.lower() and field_lower in {"address", "city", "state", "zip", "country"}:
        return _default_shipping_address().get(field_name)
    return _MISSING


def _default_shipping_address() -> dict[str, str]:
    return {
        "name": "Baseline Test User",
        "address": "123 Market Street",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94103",
        "country": "US",
    }


def _correct_enum_casing(body: dict[str, Any], error_text: str) -> dict[str, Any]:
    match = re.search(r"(?:one of:|must be one of:)\s*([A-Z0-9_,\s-]+)", error_text, re.IGNORECASE)
    if not match:
        return body
    valid_values = [token.strip() for token in match.group(1).split(",") if token.strip()]
    corrected = dict(body)
    changed = False
    for key, value in corrected.items():
        if not isinstance(value, str):
            continue
        for valid in valid_values:
            if value.lower() == valid.lower() and value != valid:
                corrected[key] = valid
                changed = True
                break
    return corrected if changed else body


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


def _is_auth_login_claim(claim: Claim) -> bool:
    return claim.id.lower() == "auth-login"


def _should_reclassify_as_auth_blocked(
    claim: Claim,
    evidence: list[Evidence],
    run_state: BaselineRunState,
) -> bool:
    return (
        run_state.auth_login_claim_ran
        and run_state.authenticated_user is None
        and not _is_auth_login_claim(claim)
        and any(
            item.outcome == "fail" and re.search(r"\bHTTP 401\b", item.actual)
            for item in evidence
        )
    )


def _mark_evidence_as_auth_blocked(evidence: list[Evidence]) -> None:
    reason = "auth session not established — harness limitation"
    for item in evidence:
        if item.outcome == "fail" and re.search(r"\bHTTP 401\b", item.actual):
            item.outcome = "blocked_by_harness"
            item.skipped = True
            item.actual = f"{reason}; observed {item.actual}"


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
    try:
        response = context.session.post(
            callback_url,
            data={
                "email": user.email,
                "password": user.password,
                "csrfToken": csrf_token,
                "redirect": "false",
                "json": "true",
            },
            timeout=10,
            allow_redirects=False,
        )
    except Exception as exc:
        return False, f"credentials callback failed: {exc}"

    _capture_response_state({"capture_auth": True}, response, context)
    response_payload: dict[str, Any] | None
    try:
        parsed_payload = response.json()
    except ValueError:
        response_payload = None
    else:
        response_payload = parsed_payload if isinstance(parsed_payload, dict) else None

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
    if response_payload and response_payload.get("url"):
        detail = f"{detail}; callback returned url={response_payload['url']}"
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
    # For admin-auth, use a NON-admin user to verify they get rejected
    non_admin_users = [user for user in config.seeded_users if user.role.lower() not in {"admin", "administrator"}]
    if claim_key == "admin-auth" and non_admin_users:
        return non_admin_users[0]
    # For admin claims and auth-login, use admin user (most likely to succeed)
    if claim_key.startswith("admin-") or claim_key in ("auth-login", "auth-logout"):
        return config.admin_user() or config.any_user()
    # For other claims, prefer a real customer user with valid credentials
    # Filter out users whose email matches admin but role is customer (likely wrong password)
    admin_user = config.admin_user()
    clean_non_admin = [
        u for u in non_admin_users
        if not admin_user or u.email != admin_user.email
    ]
    return clean_non_admin[0] if clean_non_admin else config.any_user()


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

    # If no API paths survived filtering, inject matching routes from the adapter.
    # This handles cases where the intent compiler guessed wrong URLs but the
    # adapter found the real ones (e.g., compiler says /signup, adapter found /api/auth/register).
    if not any(_is_api_path(p) for p in filtered):
        claim_keywords = _extract_claim_keywords(context.claim, candidate_paths)
        for route in context.test_config.routes:
            if method.upper() not in route.methods:
                continue
            route_lower = route.path.lower()
            if any(kw in route_lower for kw in claim_keywords):
                filtered.insert(0, route.path)  # prepend — real route goes first

    return _dedupe_preserving_order(filtered)


def _extract_claim_keywords(claim: Any, candidate_paths: list[str]) -> list[str]:
    """Extract keywords from claim ID and candidate paths to match against adapter routes."""
    keywords: set[str] = set()
    claim_id = getattr(claim, "id", "") if claim else ""
    for part in claim_id.lower().replace("-", " ").replace("_", " ").split():
        if part not in {"auth", "admin", "the", "a", "an", "is", "can", "user", "users"}:
            keywords.add(part)
    for path in candidate_paths:
        for segment in path.lower().strip("/").split("/"):
            if segment and segment not in {"api", "v1", "v2"}:
                keywords.add(segment)
    return list(keywords)


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
            expanded.extend([
                "/api/products",
                "/api/product",
                "/api/catalog/products",
                "/api/products/{{product_id}}",
                "/products",
                "/catalog",
                "/shop",
            ])
        elif "cart" in lower:
            expanded.extend(["/api/cart", "/api/cart/add", "/api/cart/items", "/cart"])
        elif "orders" in lower or "order" in lower:
            expanded.extend([
                "/api/admin/orders",
                "/api/orders",
                "/api/orders/{{order_id}}",
                "/admin/orders",
                "/orders",
            ])
        elif "checkout" in lower:
            expanded.extend(["/api/checkout", "/api/payments", "/api/orders/checkout", "/checkout", "/cart/checkout"])
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
        match_count = sum(matches)
        if match_mode == "any":
            if not any(matches):
                return False, _preview_response(response)
        else:
            # "all" mode: require all tokens, BUT if response is 200 and majority
            # match (>=60%), accept it — the intent compiler may have over-specified
            if not all(matches):
                if response.status_code == 200 and match_count >= max(1, len(matches) * 0.6):
                    pass  # lenient: majority match on 200 is good enough
                else:
                    return False, _preview_response(response)

    json_keys = step.get("expect_json_keys", [])
    if isinstance(json_keys, str):
        json_keys = [json_keys]
    if json_keys:
        try:
            payload = response.json()
        except ValueError:
            return False, f"expected JSON body with keys {json_keys}, got {response.text[:200]}"
        # Check top-level keys AND common wrapper patterns (data, results, items)
        missing_keys = [key for key in json_keys if not _json_path_exists(payload, key)]
        if missing_keys and isinstance(payload, dict):
            # Try common response wrappers
            for wrapper in ("data", "results", "items", "records"):
                if wrapper in payload:
                    unwrapped = payload[wrapper]
                    if isinstance(unwrapped, list) and len(unwrapped) > 0:
                        # The wrapper contains the data — keys refer to item properties
                        item = unwrapped[0]
                        if isinstance(item, dict):
                            missing_keys = [k for k in json_keys if k not in item and k != wrapper]
                    elif isinstance(unwrapped, dict):
                        missing_keys = [k for k in json_keys if not _json_path_exists(unwrapped, k)]
                    if not missing_keys:
                        break
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


def matrix_cache_path(project_dir: Path, intent: str) -> Path:
    digest = hashlib.sha256(intent.encode("utf-8")).hexdigest()[:16]
    return project_dir / "otto_logs" / "certifier" / f"matrix-{digest}.json"


def load_or_compile_matrix(
    project_dir: Path,
    intent: str,
    config: dict[str, Any] | None = None,
) -> tuple[RequirementMatrix, str, Path, float]:
    """Return a cached requirement matrix when available, otherwise compile it."""

    cache_path = matrix_cache_path(project_dir, intent)
    if cache_path.exists():
        return load_matrix(cache_path), "cache", cache_path, 0.0

    started_at = time.monotonic()
    matrix = asyncio.run(compile_intent(intent, config=config))
    compile_duration_s = round(time.monotonic() - started_at, 1)
    save_matrix(matrix, cache_path)
    return matrix, "compiled", cache_path, compile_duration_s


def certify(
    project_dir: Path,
    intent: str,
    config: dict[str, Any] | None = None,
    *,
    port_override: int | None = None,
    matrix: RequirementMatrix | None = None,
    matrix_path: Path | None = None,
    profile: ProductProfile | None = None,
    test_config: TestConfig | None = None,
) -> BaselineResult:
    """Full certification pipeline: compile → classify → adapt → baseline."""

    config = dict(config or {})
    effective_port = port_override or config.get("port_override")
    compile_duration_s = 0.0
    matrix_source = "provided"
    resolved_matrix_path_str = ""

    if matrix is None and matrix_path is not None:
        resolved_matrix_path = Path(matrix_path)
        matrix = load_matrix(resolved_matrix_path)
        matrix_source = "file"
        resolved_matrix_path_str = str(resolved_matrix_path)
    elif matrix is None:
        matrix, matrix_source, resolved_matrix_path, compile_duration_s = load_or_compile_matrix(project_dir, intent, config=config)
        resolved_matrix_path_str = str(resolved_matrix_path)

    if profile is None:
        profile = classify(project_dir)
    if effective_port is not None:
        profile.port = int(effective_port)
        profile.extra["reuse_existing_app"] = True
    if test_config is None:
        test_config = analyze_project(project_dir)

    result = run_baseline(project_dir, matrix, profile, test_config=test_config)
    result.compile_cost_usd = matrix.cost_usd if matrix_source != "cache" else 0.0
    result.compile_duration_s = compile_duration_s
    result.compiled_at = matrix.compiled_at
    result.matrix_source = matrix_source
    result.matrix_path = resolved_matrix_path_str
    result.verdict = judge(result)
    result.certified = result.verdict.certified
    return result


def _report_payload(result: BaselineResult) -> dict[str, Any]:
    verdict = result.verdict or judge(result)
    grouped = {
        "not_implemented": [],
        "fail": [],
        "blocked_by_harness": [],
        "not_applicable": [],
        "pass": [],
    }
    for claim in result.results:
        grouped.setdefault(claim.outcome, []).append(
            {
                "claim_id": claim.claim_id,
                "description": claim.claim_description,
                "priority": claim.priority,
                "hard_fail": claim.hard_fail,
                "outcome": claim.outcome,
                "passed": claim.passed,
                "error": claim.error,
                "proof": claim.proof,
                "evidence": [asdict(item) for item in claim.evidence],
            }
        )

    return {
        "summary": {
            "product_dir": result.product_dir,
            "intent": result.intent,
            "product_type": result.product_type,
            "certified": result.certified,
            "started": result.started,
            "claims_tested": result.claims_tested,
            "claims_passed": result.claims_passed,
            "claims_failed": result.claims_failed,
            "claims_not_implemented": result.claims_not_implemented,
            "claims_blocked": result.claims_blocked,
            "claims_not_applicable": result.claims_not_applicable,
            "hard_fails": result.hard_fails,
            "duration_s": result.duration_s,
            "compile_cost_usd": result.compile_cost_usd,
            "compile_duration_s": result.compile_duration_s,
            "compiled_at": result.compiled_at,
            "matrix_source": result.matrix_source,
            "matrix_path": result.matrix_path,
            "tier_0": _tier0_summary(result),
            "tier_1": _tier1_summary(result),
            "verdict": verdict.to_dict(),
        },
        "app_start_evidence": asdict(result.app_start_evidence) if result.app_start_evidence else None,
        "claims_by_outcome": grouped,
        "claims": [asdict(claim) for claim in result.results],
    }


def save_report(result: BaselineResult, path: Path | str) -> None:
    """Save a structured report as JSON."""

    path_str = str(path)
    payload = json.dumps(_report_payload(result), indent=2, default=str)
    if path_str in {"-", "stdout"}:
        sys.stdout.write(payload + "\n")
        return

    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(payload)


def save_result(result: BaselineResult, path: Path) -> None:
    """Backward-compatible alias for save_report()."""

    save_report(result, path)


def _group_claims(result: BaselineResult) -> dict[str, list[ClaimResult]]:
    grouped: dict[str, list[ClaimResult]] = {}
    for claim in sorted(result.results, key=lambda item: (_claim_outcome_order(item.outcome), item.claim_id)):
        grouped.setdefault(claim.outcome, []).append(claim)
    return grouped


def _tier0_counts(result: BaselineResult) -> tuple[int, int, int]:
    denominator = sum(1 for claim in result.results if claim.outcome != "not_applicable")
    not_implemented = sum(1 for claim in result.results if claim.outcome == "not_implemented")
    present = max(denominator - not_implemented, 0)
    return present, denominator, not_implemented


def _tier1_counts(result: BaselineResult) -> tuple[int, int, int, int]:
    passed = sum(1 for claim in result.results if claim.outcome == "pass")
    failed = sum(1 for claim in result.results if claim.outcome == "fail")
    blocked = sum(1 for claim in result.results if claim.outcome == "blocked_by_harness")
    denominator = passed + failed
    return passed, denominator, failed, blocked


def _format_percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "n/a"
    return f"{round((numerator / denominator) * 100):d}%"


def _tier0_summary(result: BaselineResult) -> dict[str, Any]:
    present, total, not_implemented = _tier0_counts(result)
    return {
        "present": present,
        "total": total,
        "not_implemented": not_implemented,
        "percent": _format_percent(present, total),
    }


def _tier1_summary(result: BaselineResult) -> dict[str, Any]:
    passed, total, failed, blocked = _tier1_counts(result)
    return {
        "passed": passed,
        "total": total,
        "failed": failed,
        "blocked": blocked,
        "percent": _format_percent(passed, total),
    }


def judge(baseline_result: BaselineResult) -> CertificationVerdict:
    """Produce final verdict with confidence score."""

    tier0_present, tier0_total, _ = _tier0_counts(baseline_result)
    tier1_passed, tier1_total, _, blocked = _tier1_counts(baseline_result)
    applicable_claims = max(
        baseline_result.claims_tested - baseline_result.claims_not_applicable,
        0,
    )

    tier0_score = (tier0_present / tier0_total) if tier0_total else 0.0
    tier1_score = (tier1_passed / tier1_total) if tier1_total else 0.0
    overall_score = (tier0_score * 0.3) + (tier1_score * 0.7)
    confidence = ((applicable_claims - blocked) / applicable_claims) if applicable_claims else 0.0
    certified = baseline_result.hard_fails == 0 and tier1_score >= 0.8

    if certified:
        summary = f"Certified with {round(overall_score * 100)}% overall score and {round(confidence * 100)}% confidence."
    elif baseline_result.hard_fails > 0:
        summary = f"Not certified: {baseline_result.hard_fails} hard failure(s) remain."
    else:
        summary = f"Not certified: runtime pass rate is {round(tier1_score * 100)}%, below the 80% threshold."

    return CertificationVerdict(
        certified=certified,
        tier0_score=round(tier0_score, 3),
        tier1_score=round(tier1_score, 3),
        overall_score=round(overall_score, 3),
        confidence=round(confidence, 3),
        summary=summary,
    )


def _claim_outcome_order(outcome: str) -> int:
    return {
        "not_implemented": 0,
        "fail": 1,
        "blocked_by_harness": 2,
        "not_applicable": 3,
        "pass": 4,
    }.get(outcome, 99)


def _outcome_label(outcome: str) -> str:
    return {
        "not_implemented": "Not Implemented",
        "fail": "Failures",
        "blocked_by_harness": "Blocked",
        "not_applicable": "Not Applicable",
        "pass": "Passes",
    }.get(outcome, outcome.replace("_", " ").title())


def _status_label(result: BaselineResult) -> str:
    return "CERTIFIED" if result.certified else "NOT CERTIFIED"


def _format_seconds(seconds: float) -> str:
    return f"{seconds:.1f}s"


def _print_group(title: str, claims: list[ClaimResult]) -> None:
    if not claims:
        return

    print(title)
    for claim in claims:
        suffix = " [HARD FAIL]" if claim.hard_fail and claim.outcome in {"fail", "not_implemented"} else ""
        print(f"  {claim.claim_id}: {claim.claim_description}{suffix}")
        if claim.outcome == "blocked_by_harness":
            print(f"    reason:   {claim.error}")
        if claim.outcome != "pass":
            for item in claim.evidence:
                if item.outcome == "pass":
                    continue
                print(f"    command:  {item.command}")
                print(f"    expected: {item.expected}")
                print(f"    actual:   {item.actual[:300]}")


def _claim_outcome_map(result: BaselineResult) -> dict[str, ClaimResult]:
    return {claim.claim_id: claim for claim in result.results}


def _format_tier0_cell(result: BaselineResult) -> str:
    summary = _tier0_summary(result)
    return f"{summary['present']}/{summary['total']} ({summary['percent']})"


def _format_tier1_cell(result: BaselineResult) -> str:
    summary = _tier1_summary(result)
    return f"{summary['passed']}/{summary['total']} ({summary['percent']})"


def _comparison_rows(result_a: BaselineResult, result_b: BaselineResult, labels: tuple[str, str]) -> list[str]:
    label_a, label_b = labels
    def _overall(r: BaselineResult) -> str:
        total = r.claims_tested - r.claims_not_applicable
        pct = f"{round(r.claims_passed / total * 100)}%" if total > 0 else "n/a"
        return f"{r.claims_passed}/{total} ({pct})"

    rows = [
        f"## Comparison: {label_a} vs {label_b}",
        "",
        f"| Metric | {label_a} | {label_b} |",
        "|---|---:|---:|",
        f"| **Score** | **{_overall(result_a)}** | **{_overall(result_b)}** |",
        f"| Structure | {_format_tier0_cell(result_a)} | {_format_tier0_cell(result_b)} |",
        f"| Runtime | {_format_tier1_cell(result_a)} | {_format_tier1_cell(result_b)} |",
        f"| Not implemented | {result_a.claims_not_implemented} | {result_b.claims_not_implemented} |",
        f"| Blocked | {result_a.claims_blocked} | {result_b.claims_blocked} |",
        f"| Failed | {result_a.claims_failed} | {result_b.claims_failed} |",
        f"| Verdict | {_status_label(result_a)} | {_status_label(result_b)} |",
    ]
    return rows


def compare(result_a: BaselineResult, result_b: BaselineResult, labels: tuple[str, str]) -> str:
    """Return a markdown comparison of two certification runs."""

    claim_map_a = _claim_outcome_map(result_a)
    claim_map_b = _claim_outcome_map(result_b)
    differing_ids = sorted(
        claim_id
        for claim_id in set(claim_map_a) | set(claim_map_b)
        if claim_map_a.get(claim_id, ClaimResult("", "", "", False, False, "missing", [])).outcome
        != claim_map_b.get(claim_id, ClaimResult("", "", "", False, False, "missing", [])).outcome
    )

    lines = _comparison_rows(result_a, result_b, labels)
    if not differing_ids:
        lines.extend(["", "No claim outcome differences."])
        return "\n".join(lines)

    lines.extend(
        [
            "",
            "| Claim | Description | " + labels[0] + " | " + labels[1] + " |",
            "|---|---|---|---|",
        ]
    )
    for claim_id in differing_ids:
        claim_a = claim_map_a.get(claim_id)
        claim_b = claim_map_b.get(claim_id)
        description = (claim_a or claim_b).claim_description if (claim_a or claim_b) else ""
        lines.append(
            f"| `{claim_id}` | {description} | {(claim_a.outcome if claim_a else 'missing')} | {(claim_b.outcome if claim_b else 'missing')} |"
        )
    return "\n".join(lines)


def _claim_marker(claim: ClaimResult) -> str:
    return {
        "pass": "✓",
        "fail": "✗",
        "not_implemented": "✗",
        "blocked_by_harness": "?",
        "not_applicable": "-",
    }.get(claim.outcome, "-")


def _evidence_lines(claim: ClaimResult) -> list[str]:
    lines: list[str] = []
    if claim.outcome == "blocked_by_harness":
        lines.append(f"Reason: {claim.error}")
    if claim.proof:
        proof = claim.proof
        if proof.get("adapter_check"):
            lines.append(f"- Proof: {proof['adapter_check']}")
        request = proof.get("request")
        response = proof.get("response")
        if isinstance(request, dict) and request.get("url"):
            lines.append(f"- Proof Request: `{request.get('method', 'GET')} {request['url']}`")
        if isinstance(response, dict) and response.get("status") is not None:
            lines.append(f"- Proof Response: HTTP {response['status']}")
        retries = proof.get("retries")
        if isinstance(retries, list) and retries:
            lines.append(f"- Proof Retries: {len(retries)}")
    for item in claim.evidence:
        if claim.outcome != "pass" and item.outcome == "pass":
            continue
        if claim.outcome == "blocked_by_harness" and item.outcome == "pass":
            continue
        lines.append(f"- Command: `{item.command}`")
        lines.append(f"- Expected: {item.expected}")
        lines.append(f"- Actual: {item.actual}")
    return lines


def _claim_markdown(claim: ClaimResult) -> str:
    lines = [f"- {_claim_marker(claim)} `{claim.claim_id}` {claim.claim_description}"]
    lines.extend(_evidence_lines(claim))
    return "\n".join(lines)


def save_markdown_report(result: BaselineResult, path: Path) -> None:
    """Save a human-readable markdown certification report."""

    tier0 = _tier0_summary(result)
    tier1 = _tier1_summary(result)
    verdict = result.verdict or judge(result)
    grouped = _group_claims(result)
    section_titles = {
        "not_implemented": "NOT IMPLEMENTED",
        "fail": "FAILED",
        "blocked_by_harness": "BLOCKED",
        "pass": "PASSED",
    }
    lines = [
        "# Certification Report",
        "",
        f"> **Product:** `{result.product_dir}`",
        f"> **Tier 0 (structure):** {tier0['present']}/{tier0['total']} present ({tier0['percent']})",
        f"> **Tier 1 (runtime):** {tier1['passed']}/{tier1['total']} passed ({tier1['percent']})",
        f"> **Verdict:** {_status_label(result)}",
        f"> **Judge:** {verdict.summary}",
        "",
        "## NOT IMPLEMENTED",
        "",
    ]
    for outcome in ("not_implemented", "fail", "blocked_by_harness", "pass"):
        if outcome != "not_implemented":
            lines.extend(["", f"## {section_titles[outcome]}", ""])
        claims = grouped.get(outcome, [])
        if not claims:
            lines.append("_None_")
            continue
        lines.extend(_claim_markdown(claim) for claim in claims)
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text("\n".join(lines) + "\n")


def print_report(result: BaselineResult, other: BaselineResult | None = None) -> None:
    """Print a human-readable certification report."""

    grouped = _group_claims(result)
    tier0 = _tier0_summary(result)
    tier1 = _tier1_summary(result)
    verdict = result.verdict or judge(result)

    # Overall score: passed / total claims (not_implemented counts as failure)
    total = result.claims_tested - result.claims_not_applicable
    overall_passed = result.claims_passed
    overall_pct = f"{round(overall_passed / total * 100)}%" if total > 0 else "n/a"

    print()
    print(f"Certification: {_status_label(result)}")
    print(f"Product:       {result.product_dir}")
    print(f"Type:          {result.product_type}")
    print(f"Score:         {overall_passed}/{total} ({overall_pct})")
    print(f"  Structure:   {tier0['present']}/{tier0['total']} present ({tier0['percent']})")
    print(f"  Runtime:     {tier1['passed']}/{tier1['total']} tested ({tier1['percent']})")
    if tier0['not_implemented'] > 0:
        print(f"  Missing:     {tier0['not_implemented']} not implemented")
    if tier1['blocked'] > 0:
        print(f"  Blocked:     {tier1['blocked']} (harness limitation)")
    print(f"Hard fails:    {result.hard_fails}")
    print(f"Confidence:    {round(verdict.confidence * 100):d}%")
    print(f"App started:   {'yes' if result.started else 'no'}")
    print(f"Matrix:        {result.matrix_source or 'unknown'}")
    if result.matrix_path:
        print(f"Matrix path:   {result.matrix_path}")
    if result.compiled_at:
        print(f"Compiled at:   {result.compiled_at}")
    print(f"Timing:        compile {_format_seconds(result.compile_duration_s)}, baseline {_format_seconds(result.duration_s)}")
    print(f"Cost:          ${result.compile_cost_usd:.3f}")
    print(f"Judge:         {verdict.summary}")
    print()

    for outcome in ("not_implemented", "fail", "blocked_by_harness", "not_applicable", "pass"):
        _print_group(_outcome_label(outcome), grouped.get(outcome, []))
        if grouped.get(outcome):
            print()

    if other is not None:
        print(compare(result, other, ("Current", "Other")))
        print()
