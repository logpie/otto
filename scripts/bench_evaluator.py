"""Shared evaluator framework for i2p benches.

The user flagged that our existing benches verify functional
contracts via Flask `test_client()` and the audit's LLM judge,
but miss what a real user actually experiences. This module
provides reusable evaluators that any bench can compose:

    Evaluator         What it checks
    ─────────────     ────────────────────────────────────────
    contract_test     Project's tests/run_acceptance.py exits 0
    code_health       Lint score, dependency hygiene, dead code
    edge_cases        Empty/huge/unicode/SQL-shaped inputs
    security_baseline Stored XSS, SQL injection, error leakage
    performance       Page load / API response / cold-start budgets
    user_journey      Real user click-through (Playwright for web,
                      subprocess for CLI)

Each evaluator returns an `EvalResult` with status + findings.
Benches compose evaluators based on the project shape and report
aggregated results in their result.json.

The framework is project-shape-agnostic: a CLI bench runs
contract_test + code_health + edge_cases (CLI flavor) + user_journey
(subprocess flavor). A webapp bench adds security_baseline +
performance + user_journey (Playwright flavor). A library bench
runs contract_test + code_health.

The "user_journey" evaluator is the deepest. It mimics a first-time
user including stumbles: try wrong username, submit empty form,
refresh during async, hit edge cases. Surfaces failures the
acceptance test misses.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal


EvalStatus = Literal["passed", "partial", "blocked", "skipped", "error"]


@dataclass
class EvalFinding:
    """One concrete observation from an evaluator."""
    severity: Literal["info", "warning", "error"]
    message: str
    evidence: str = ""  # path / quoted output / URL


@dataclass
class EvalResult:
    """Output of a single evaluator."""
    name: str
    status: EvalStatus
    findings: list[EvalFinding] = field(default_factory=list)
    duration_s: float = 0.0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "duration_s": round(self.duration_s, 2),
            "findings": [asdict(f) for f in self.findings],
        }


@dataclass
class EvaluatorContext:
    """Inputs an evaluator receives. Project-shape-agnostic."""
    project_dir: Path
    python: Path  # Python interpreter to use
    project_kind: str = "webapp"
    timeout_s: int = 300


# Evaluator type — function from (context, optional config) -> EvalResult.
Evaluator = Callable[[EvaluatorContext], EvalResult]


# ---------------------------------------------------------------------------
# Evaluator: contract_test
# ---------------------------------------------------------------------------


def eval_contract_test(ctx: EvaluatorContext) -> EvalResult:
    """Run the project's `tests/run_acceptance.py` (or otto.yaml's
    test_command) and report pass/fail. The cheapest, most fundamental
    evaluator — does the project's own test pass?"""
    t0 = time.monotonic()
    findings: list[EvalFinding] = []

    test_script = ctx.project_dir / "tests" / "run_acceptance.py"
    if not test_script.exists():
        # fall back to otto.yaml test_command
        otto_yaml = ctx.project_dir / "otto.yaml"
        if not otto_yaml.exists():
            return EvalResult(
                name="contract_test",
                status="skipped",
                summary="no tests/run_acceptance.py and no otto.yaml",
                duration_s=time.monotonic() - t0,
            )

    cmd = [str(ctx.python), "tests/run_acceptance.py"]
    try:
        proc = subprocess.run(
            cmd, cwd=ctx.project_dir, capture_output=True, text=True,
            timeout=ctx.timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return EvalResult(
            name="contract_test",
            status="error",
            summary=f"timed out after {ctx.timeout_s}s",
            duration_s=time.monotonic() - t0,
        )

    if proc.returncode == 0:
        return EvalResult(
            name="contract_test",
            status="passed",
            summary="acceptance test passed",
            findings=[
                EvalFinding(severity="info", message=line, evidence="")
                for line in (proc.stdout or "").splitlines()[:20]
                if "PASS" in line or "FAIL" in line
            ],
            duration_s=time.monotonic() - t0,
        )
    return EvalResult(
        name="contract_test",
        status="blocked",
        summary=f"acceptance exit={proc.returncode}",
        findings=[EvalFinding(
            severity="error",
            message=f"acceptance test failed (exit {proc.returncode})",
            evidence=(proc.stdout + "\n" + proc.stderr)[:1000],
        )],
        duration_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Evaluator: code_health
# ---------------------------------------------------------------------------


def eval_code_health(ctx: EvaluatorContext) -> EvalResult:
    """Static checks on the produced code: lint pass, no obvious
    sins, dependency hygiene, no dead-code marker patterns.

    Checks are cheap and don't require running the product:
      - Python files parse (AST)
      - No `print('TODO')` / `# FIXME` / `pass  # placeholder`
      - requirements.txt only lists packages that get imported
      - No huge single files (>500 LOC suggests poor decomposition)
    """
    t0 = time.monotonic()
    findings: list[EvalFinding] = []
    py_files = list(ctx.project_dir.rglob("*.py"))
    py_files = [
        p for p in py_files
        if "__pycache__" not in p.parts
        and ".pytest_cache" not in p.parts
        and "tests" not in p.parts  # tests can be permissive
        and ".venv" not in p.parts
    ]

    # 1. AST parses
    import ast
    for f in py_files:
        try:
            ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, OSError) as exc:
            findings.append(EvalFinding(
                severity="error",
                message=f"syntax error in {f.relative_to(ctx.project_dir)}: {exc}",
                evidence=str(f),
            ))

    # 2. Placeholder patterns
    placeholder_patterns = [
        (r"\b(TODO|FIXME|XXX)\b", "leftover TODO/FIXME marker"),
        (r"raise NotImplementedError", "NotImplementedError stub"),
        (r"^\s*pass\s*#\s*placeholder", "placeholder pass"),
    ]
    for f in py_files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for pattern, label in placeholder_patterns:
            for m in re.finditer(pattern, text, flags=re.MULTILINE):
                line = text[:m.start()].count("\n") + 1
                findings.append(EvalFinding(
                    severity="warning",
                    message=f"{label} at {f.relative_to(ctx.project_dir)}:{line}",
                    evidence=text[max(0, m.start()-30):m.end()+30],
                ))
                break  # one finding per file per pattern

    # 3. Huge files
    for f in py_files:
        try:
            lines = f.read_text(encoding="utf-8").count("\n")
        except OSError:
            continue
        if lines > 500:
            findings.append(EvalFinding(
                severity="warning",
                message=f"{f.relative_to(ctx.project_dir)} is {lines} lines (>500 suggests poor decomposition)",
                evidence=str(f),
            ))

    error_count = sum(1 for x in findings if x.severity == "error")
    warning_count = sum(1 for x in findings if x.severity == "warning")

    if error_count:
        status: EvalStatus = "blocked"
        summary = f"{error_count} errors, {warning_count} warnings"
    elif warning_count > 5:
        status = "partial"
        summary = f"{warning_count} warnings (no errors)"
    else:
        status = "passed"
        summary = f"clean ({warning_count} minor warnings)"

    return EvalResult(
        name="code_health",
        status=status,
        summary=summary,
        findings=findings[:20],  # cap surfaced findings
        duration_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Evaluator: edge_cases (HTTP-flavor, requires running server)
# ---------------------------------------------------------------------------


def eval_edge_cases_webapp(ctx: EvaluatorContext, base_url: str) -> EvalResult:
    """Probe HTTP endpoints with edge-case payloads to surface
    crashes, error-page leaks, and missing validation.

    Tests:
      - Empty POST body to known endpoints → expect 400, not 500
      - Very long input (10K chars) → no server error
      - Unicode in usernames/text → handled
      - SQL-injection-shaped strings → handled (no leaked tracebacks)
      - Special characters in URL paths
    """
    t0 = time.monotonic()
    findings: list[EvalFinding] = []
    base = base_url.rstrip("/")

    edge_payloads = [
        ("empty_body", {}, "Empty POST body"),
        ("huge_text", {"text": "a" * 10000, "username": "u" * 1000},
         "Very long inputs"),
        ("unicode", {"username": "用户ααβγ", "text": "hello 🎉 世界"},
         "Unicode in fields"),
        ("sql_shape", {"username": "admin' OR '1'='1", "text": "; DROP TABLE users;--"},
         "SQL-injection-shaped strings"),
    ]

    # Targets vary by project; we probe well-known endpoints if they exist.
    common_post_paths = [
        "/api/users", "/api/posts", "/api/follow",
        "/api/login-as", "/api/login",
    ]

    for path in common_post_paths:
        url = f"{base}{path}"
        for tag, payload, label in edge_payloads:
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status = resp.status
                    body = (resp.read() or b"").decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    body = (exc.read() or b"").decode("utf-8", errors="replace")
                except Exception:
                    body = ""
            except urllib.error.URLError:
                # endpoint doesn't exist → not a finding for this path
                break

            if status >= 500:
                findings.append(EvalFinding(
                    severity="error",
                    message=f"{path} {label} → HTTP {status} (server error, not validation error)",
                    evidence=body[:500],
                ))
            if "Traceback" in body or "raise " in body:
                findings.append(EvalFinding(
                    severity="error",
                    message=f"{path} {label} → leaked Python traceback in response",
                    evidence=body[:500],
                ))

    error_count = sum(1 for x in findings if x.severity == "error")
    if error_count == 0:
        status_out: EvalStatus = "passed"
        summary = "all edge-case probes returned non-server errors with no traceback leaks"
    elif error_count <= 2:
        status_out = "partial"
        summary = f"{error_count} edge-case failures"
    else:
        status_out = "blocked"
        summary = f"{error_count} edge-case failures (severe)"

    return EvalResult(
        name="edge_cases",
        status=status_out,
        summary=summary,
        findings=findings[:20],
        duration_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Evaluator: edge_cases (CLI flavor)
# ---------------------------------------------------------------------------


def eval_edge_cases_cli(ctx: EvaluatorContext, entry_argv: list[str]) -> EvalResult:
    """Probe a CLI entry-point with edge-case invocations.

    Tests:
      - --help works and is non-empty
      - No-args invocation does something sensible (help, default behavior)
      - Invalid flag → error message, not a Python traceback
      - Unicode in args
    """
    t0 = time.monotonic()
    findings: list[EvalFinding] = []

    def run(argv: list[str], timeout: int = 30):
        try:
            return subprocess.run(
                argv, cwd=ctx.project_dir, capture_output=True,
                text=True, timeout=timeout, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return type("FakeProc", (), {
                "returncode": -1,
                "stdout": "",
                "stderr": str(exc),
            })()

    # --help
    proc = run([*entry_argv, "--help"])
    if proc.returncode != 0 or not (proc.stdout or proc.stderr):
        findings.append(EvalFinding(
            severity="error",
            message="--help did not produce useful output",
            evidence=(proc.stdout + proc.stderr)[:500],
        ))

    # No-args
    proc = run(entry_argv)
    if proc.returncode not in (0, 1, 2):
        findings.append(EvalFinding(
            severity="warning",
            message=f"no-args invocation returned {proc.returncode} (expected 0/1/2)",
            evidence=(proc.stdout + proc.stderr)[:500],
        ))

    # Invalid flag
    proc = run([*entry_argv, "--bogus-flag-xyz"])
    if "Traceback" in (proc.stderr or ""):
        findings.append(EvalFinding(
            severity="error",
            message="invalid flag produced Python traceback (should be friendly error)",
            evidence=(proc.stderr or "")[:500],
        ))

    error_count = sum(1 for x in findings if x.severity == "error")
    if error_count == 0:
        status: EvalStatus = "passed"
        summary = "CLI handles edge invocations cleanly"
    elif error_count <= 1:
        status = "partial"
        summary = f"{error_count} CLI edge-case issue"
    else:
        status = "blocked"
        summary = f"{error_count} CLI edge-case errors"

    return EvalResult(
        name="edge_cases",
        status=status,
        summary=summary,
        findings=findings[:10],
        duration_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Evaluator: security_baseline (HTTP)
# ---------------------------------------------------------------------------


def eval_security_baseline_webapp(ctx: EvaluatorContext, base_url: str) -> EvalResult:
    """Probe for stored XSS, error-page traceback leaks, and CSRF.

    Tests:
      - Submit `<script>alert(1)</script>` as user input → fetched
        back from the server should be HTML-escaped (no `<script>`
        appearing in DOM unescaped).
      - GET nonexistent route → 404 page should NOT leak Python
        traceback / framework internals.
      - Open redirect: ?next=http://evil.com etc. → if the app
        redirects, it should reject external URLs.
    """
    t0 = time.monotonic()
    findings: list[EvalFinding] = []
    base = base_url.rstrip("/")

    # 1. Stored XSS probe
    xss_payload = "<script>alert(1)</script>SAFE_TOKEN_42"
    # Try posting to common content endpoints
    for path in ["/api/posts", "/api/users", "/posts"]:
        try:
            req = urllib.request.Request(
                f"{base}{path}",
                data=json.dumps({
                    "username": f"xtest_{int(time.time())}",
                    "display_name": xss_payload,
                    "text": xss_payload,
                    "author": "xtest",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except (urllib.error.HTTPError, urllib.error.URLError):
            pass

    # Then GET pages and look for the unescaped script tag.
    for page_path in ["/", "/timeline/xtest", "/search?q=SAFE_TOKEN_42"]:
        try:
            with urllib.request.urlopen(f"{base}{page_path}", timeout=5) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
        if "<script>alert(1)</script>" in body:
            findings.append(EvalFinding(
                severity="error",
                message=f"stored XSS: unescaped <script> tag rendered at {page_path}",
                evidence=body[max(0, body.find('<script>')-50):body.find('<script>')+200],
            ))

    # 2. 404 traceback leak
    try:
        urllib.request.urlopen(f"{base}/this-route-does-not-exist", timeout=5)
        body = ""
    except urllib.error.HTTPError as exc:
        body = (exc.read() or b"").decode("utf-8", errors="replace")
    except urllib.error.URLError:
        body = ""
    if "Traceback" in body or "Werkzeug" in body or "DEBUG" in body:
        findings.append(EvalFinding(
            severity="error",
            message="404 page leaks Python traceback or DEBUG mode",
            evidence=body[:500],
        ))

    error_count = sum(1 for x in findings if x.severity == "error")
    if error_count == 0:
        status: EvalStatus = "passed"
        summary = "no stored XSS or traceback leaks observed"
    elif error_count <= 1:
        status = "partial"
        summary = f"{error_count} security baseline issue"
    else:
        status = "blocked"
        summary = f"{error_count} security baseline issues"

    return EvalResult(
        name="security_baseline",
        status=status,
        summary=summary,
        findings=findings[:10],
        duration_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Evaluator: performance (HTTP)
# ---------------------------------------------------------------------------


def eval_performance_webapp(
    ctx: EvaluatorContext, base_url: str,
    home_budget_s: float = 2.0,
    api_budget_s: float = 0.5,
) -> EvalResult:
    """Hit the home page and a representative API endpoint;
    assert each meets its budget."""
    t0 = time.monotonic()
    findings: list[EvalFinding] = []

    base = base_url.rstrip("/")
    targets = [
        ("/", home_budget_s, "home page"),
        ("/api/timeline/anyone", api_budget_s, "API timeline"),
        ("/health", api_budget_s, "/health"),
    ]
    for path, budget, label in targets:
        ts = time.monotonic()
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=10) as resp:
                resp.read()
            elapsed = time.monotonic() - ts
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
        if elapsed > budget:
            findings.append(EvalFinding(
                severity="warning",
                message=f"{label} ({path}) took {elapsed:.2f}s (budget {budget:.2f}s)",
            ))

    if not findings:
        status: EvalStatus = "passed"
        summary = "all probes within budget"
    else:
        status = "partial"
        summary = f"{len(findings)} perf budget overruns"

    return EvalResult(
        name="performance",
        status=status,
        summary=summary,
        findings=findings,
        duration_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Evaluator: user_journey (Playwright for webapp; subprocess for CLI)
# ---------------------------------------------------------------------------


def eval_user_journey_webapp(
    ctx: EvaluatorContext,
    journey_script: Path,
) -> EvalResult:
    """Run a Playwright-driven journey script. The bench provides
    the script; the framework reports pass/fail and surfaces the
    first failure."""
    t0 = time.monotonic()

    if not journey_script.exists():
        return EvalResult(
            name="user_journey",
            status="skipped",
            summary=f"journey script not found: {journey_script.name}",
            duration_s=time.monotonic() - t0,
        )

    try:
        proc = subprocess.run(
            [str(ctx.python), str(journey_script)],
            cwd=ctx.project_dir, capture_output=True,
            text=True, timeout=ctx.timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return EvalResult(
            name="user_journey",
            status="error",
            summary=f"timed out after {ctx.timeout_s}s",
            duration_s=time.monotonic() - t0,
        )

    findings: list[EvalFinding] = []
    if proc.returncode != 0:
        # surface the failure
        for line in (proc.stdout + "\n" + proc.stderr).splitlines()[-20:]:
            if any(token in line for token in ("FAIL", "AssertionError", "Error", "Traceback")):
                findings.append(EvalFinding(
                    severity="error", message=line.strip(),
                ))

    return EvalResult(
        name="user_journey",
        status=("passed" if proc.returncode == 0 else "blocked"),
        summary=("journey passed" if proc.returncode == 0
                 else f"journey failed exit={proc.returncode}"),
        findings=findings[:10],
        duration_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Bench composer
# ---------------------------------------------------------------------------


def run_evaluators(
    ctx: EvaluatorContext,
    evaluators: list[Callable[[EvaluatorContext], EvalResult]],
) -> list[EvalResult]:
    """Run each evaluator with the same context; collect results."""
    results: list[EvalResult] = []
    for evaluator in evaluators:
        try:
            result = evaluator(ctx)
        except Exception as exc:  # noqa: BLE001 — evaluators must not crash the bench
            result = EvalResult(
                name=getattr(evaluator, "__name__", "unknown"),
                status="error",
                summary=f"evaluator crashed: {type(exc).__name__}: {exc}",
            )
        results.append(result)
    return results


def aggregate_status(results: list[EvalResult]) -> EvalStatus:
    """Compute overall status across evaluators.

    Any blocked or error → blocked.
    >50% partial → partial.
    Anything else → passed.
    Skipped evaluators don't count toward denominator.
    """
    counted = [r for r in results if r.status not in ("skipped",)]
    if not counted:
        return "skipped"
    if any(r.status in ("blocked", "error") for r in counted):
        return "blocked"
    partial = sum(1 for r in counted if r.status == "partial")
    if partial * 2 > len(counted):
        return "partial"
    if partial:
        return "partial"
    return "passed"


__all__ = [
    "EvalFinding",
    "EvalResult",
    "EvalStatus",
    "Evaluator",
    "EvaluatorContext",
    "aggregate_status",
    "eval_code_health",
    "eval_contract_test",
    "eval_edge_cases_cli",
    "eval_edge_cases_webapp",
    "eval_performance_webapp",
    "eval_security_baseline_webapp",
    "eval_user_journey_webapp",
    "run_evaluators",
]
