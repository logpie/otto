"""Pre-flight check — quick structural validation before journey testing.

Confirms the product is alive and basic routes respond. Takes ~3 seconds,
no LLM calls. If the product is fundamentally broken (most routes 404),
skip expensive journey testing and report "product not ready."
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from otto.certifier.manifest import ProductManifest

logger = logging.getLogger("otto.certifier.preflight")


@dataclass
class PreflightCheck:
    """Result of a single preflight check."""
    name: str
    passed: bool
    detail: str = ""


@dataclass
class PreflightResult:
    """Overall preflight result."""
    ready: bool
    checks: list[PreflightCheck] = field(default_factory=list)
    reason: str = ""
    duration_s: float = 0.0

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total_count(self) -> int:
        return len(self.checks)

    @property
    def summary(self) -> str:
        return f"{self.passed_count}/{self.total_count} checks pass"


def preflight_check(manifest: ProductManifest, base_url: str) -> PreflightResult:
    """Quick structural check: is the product ready for journey testing?"""
    start = time.monotonic()
    checks: list[PreflightCheck] = []

    # 1. Is the app alive?
    try:
        r = requests.get(base_url, timeout=5)
        alive = r.status_code < 500
        checks.append(PreflightCheck(
            name="app_alive",
            passed=alive,
            detail=f"HTTP {r.status_code}" if alive else f"HTTP {r.status_code} (server error)",
        ))
    except Exception as exc:
        checks.append(PreflightCheck(
            name="app_alive",
            passed=False,
            detail=f"Connection failed: {exc}",
        ))
        # If app isn't alive, no point checking routes
        return PreflightResult(
            ready=False,
            checks=checks,
            reason=f"App not responding at {base_url}",
            duration_s=round(time.monotonic() - start, 1),
        )

    # 2. Can we authenticate? (if seeded users available)
    if manifest.seeded_users:
        user = manifest.seeded_users[0]
        auth_ok = _check_auth(manifest, base_url, user)
        auth_detail = f"Auth as {user['email']}"
        if not auth_ok:
            auth_detail = (
                f"Auth failed for {user['email']}. "
                f"If using --port, ensure NEXTAUTH_URL matches: "
                f"NEXTAUTH_URL={base_url}"
            )
        checks.append(PreflightCheck(
            name="auth_works",
            passed=auth_ok,
            detail=auth_detail,
        ))

    # 3. Do the main routes respond? (non-404)
    route_checks = 0
    route_alive = 0
    for route in manifest.routes:
        path = route["path"]
        if ":id" in path or "[id]" in path:
            continue  # Skip parameterized routes
        try:
            r = requests.get(f"{base_url}{path}", timeout=3)
            responds = r.status_code != 404
            checks.append(PreflightCheck(
                name=f"route:{path}",
                passed=responds,
                detail=f"HTTP {r.status_code}",
            ))
            route_checks += 1
            if responds:
                route_alive += 1
        except Exception:
            checks.append(PreflightCheck(
                name=f"route:{path}",
                passed=False,
                detail="Connection error",
            ))
            route_checks += 1

    # 4. Determine readiness
    total_passed = sum(1 for c in checks if c.passed)
    total = len(checks)
    duration_s = round(time.monotonic() - start, 1)

    if total == 0:
        return PreflightResult(ready=False, checks=checks,
                               reason="No checks could be performed",
                               duration_s=duration_s)

    ratio = total_passed / total
    if ratio < 0.3:
        reason = (
            f"Only {total_passed}/{total} structural checks pass ({ratio:.0%}). "
            f"Product is not structurally ready for journey testing."
        )
        if route_checks > 0 and route_alive == 0:
            reason += " No API routes respond (all 404)."
        return PreflightResult(ready=False, checks=checks, reason=reason,
                               duration_s=duration_s)

    return PreflightResult(ready=True, checks=checks, duration_s=duration_s)


def _check_auth(manifest: ProductManifest, base_url: str, user: dict[str, str]) -> bool:
    """Try to authenticate with seeded credentials."""
    session = requests.Session()

    if manifest.auth_type == "nextauth":
        try:
            csrf_resp = session.get(f"{base_url}/api/auth/csrf", timeout=5)
            csrf = csrf_resp.json().get("csrfToken", "")
            if not csrf:
                return False
            session.post(
                f"{base_url}/api/auth/callback/credentials",
                data={
                    "email": user["email"],
                    "password": user["password"],
                    "csrfToken": csrf,
                    "redirect": "false",
                    "json": "true",
                },
                timeout=10,
                allow_redirects=False,
            )
            # Check if session was established
            sess_resp = session.get(f"{base_url}/api/auth/session", timeout=5)
            if sess_resp.status_code == 200:
                sess_data = sess_resp.json()
                if isinstance(sess_data, dict) and sess_data.get("user"):
                    return True
        except Exception:
            pass
        return False

    # Generic auth: try POST to login endpoint
    login_endpoint = manifest.login_endpoint
    if login_endpoint:
        try:
            r = session.post(
                f"{base_url}{login_endpoint}",
                json={"email": user["email"], "password": user["password"]},
                timeout=10,
            )
            return r.status_code in (200, 201)
        except Exception:
            pass

    return False
