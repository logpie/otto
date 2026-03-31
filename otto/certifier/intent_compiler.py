"""Intent compiler — turns natural language intent into testable claims.

This is the most important step. Without explicit claims, the evaluator
is "grading vibes." The compiler decomposes an intent into a requirement
matrix where each claim is testable, prioritized, and has a suggested
test approach.

The compiler runs ONCE before any testing. It doesn't see the product
code — only the original intent. This ensures evaluation criteria are
defined independently of the implementation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query

logger = logging.getLogger("otto.certifier")

TestStep = dict[str, Any]


@dataclass
class Claim:
    """A single testable claim derived from the intent."""

    id: str                    # e.g., "auth-register"
    description: str           # "Users can register with email and password"
    priority: str              # "critical" | "important" | "nice"
    category: str              # "feature" | "ux" | "data" | "error-handling" | "security"
    test_approach: str         # "api" | "browser" | "cli" | "code-review"
    test_steps: list[TestStep]  # machine-executable steps for the baseline runner
    hard_fail: bool = False    # if True, failing this claim fails the entire certification


@dataclass
class RequirementMatrix:
    """The full set of testable claims for a product."""

    intent: str
    claims: list[Claim]
    non_goals: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    product_type_hint: str = ""  # "web" | "cli" | "api" | "desktop"
    compiled_at: str = ""
    cost_usd: float = 0.0

    def critical_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.priority == "critical"]

    def by_category(self, cat: str) -> list[Claim]:
        return [c for c in self.claims if c.category == cat]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


INTENT_COMPILER_PROMPT = """\
You are a requirements analyst. Given a product intent (what someone asked
to be built), produce a requirement matrix of TESTABLE CLAIMS.

Intent: {intent}

For each claim:
- id: short kebab-case identifier (e.g., "auth-register", "cart-add-item")
- description: one sentence describing the observable behavior
- priority: "critical" (product is broken without it), "important" (significant
  gap), or "nice" (polish/enhancement)
- category: "feature", "ux", "data", "error-handling", or "security"
- test_approach: how to verify — "api" (HTTP requests), "browser" (navigate
  and interact), "cli" (run commands), "code-review" (inspect source)
- test_steps: 1-5 MACHINE-EXECUTABLE JSON objects only. Never natural language strings.
- hard_fail: true if this claim MUST pass for certification (critical features)

Also identify:
- non_goals: things the intent does NOT ask for (prevent over-testing)
- ambiguities: parts of the intent that are unclear (flag, don't assume)
- product_type_hint: "web", "cli", "api", "desktop", or "unknown"

RULES:
- Only include claims the intent actually asks for. Don't invent features.
- Each claim must be independently testable with concrete structured steps.
- Critical claims should cover: can a user do the core thing the product promises?
- Mark registration, login, core CRUD, and primary user flow as critical.
- Mark error handling, edge cases, UI polish as important or nice.
- Every test step must be a dict with an "action" key.
- For CRUD request bodies, include ALL plausible required fields, not just the obvious ones.
- For update/delete claims on a single record, include API candidate paths with an explicit resource ID variant.
- For access-control claims, prefer genuinely protected or admin-only endpoints over public catalog routes.
- For checkout/payment claims, include a realistic shipping or billing payload when the API would need one.
- Allowed actions:
  - "http": may include "method", "path", "candidate_paths", "body",
    "body_variants", "expect_status", "expect_body_contains", "expect_json_keys"
  - "navigate": may include "path", "candidate_paths", "expect_status",
    "expect_body_contains"
  - "cli": may include "command", "expect_exit_code", "expect_stdout_contains"
  - "check_exists": may include "path", "candidate_paths", "target",
    "existence_statuses"
- Prefer concrete request bodies with realistic fixtures.
- Prefer candidate_paths when route shape may vary across implementations.
- 10-25 claims for a typical product. Don't over-decompose.

Output JSON only:
{{
  "product_type_hint": "web",
  "non_goals": ["mobile app", "multi-language"],
  "ambiguities": ["unclear if admin needs separate login"],
  "claims": [
    {{
      "id": "auth-register",
      "description": "Users can register with email and password",
      "priority": "critical",
      "category": "feature",
      "test_approach": "api",
      "test_steps": [
        {{
          "action": "http",
          "method": "POST",
          "path": "/api/auth/register",
          "candidate_paths": [
            "/api/auth/register",
            "/api/register",
            "/api/signup"
          ],
          "body": {{
            "email": "test@eval.com",
            "password": "test12345",
            "name": "Test User"
          }},
          "expect_status": [200, 201],
          "expect_body_contains": ["email"],
          "expect_json_keys": ["id", "email"]
        }},
        {{
          "action": "navigate",
          "path": "/account",
          "candidate_paths": ["/account", "/dashboard"],
          "expect_status": [200, 302]
        }}
      ],
      "hard_fail": true
    }}
  ]
}}
"""


async def compile_intent(
    intent: str,
    config: dict[str, Any] | None = None,
) -> RequirementMatrix:
    """Compile a natural language intent into a testable requirement matrix.

    This does NOT see the product code. Only the intent.
    """

    config = config or {}

    prompt = INTENT_COMPILER_PROMPT.format(intent=intent)

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=".",
        setting_sources=["project"],
        env=_subprocess_env(),
        effort="medium",
        max_turns=1,
        system_prompt={"type": "preset", "preset": "claude_code"},
    )
    model = config.get("planner_model")
    if model:
        options.model = str(model)

    started_at = time.monotonic()
    raw_output, cost, _result = await run_agent_query(prompt, options)
    cost_usd = float(cost or 0.0)
    duration_s = round(time.monotonic() - started_at, 1)

    matrix = _parse_matrix(raw_output, intent)
    matrix.cost_usd = cost_usd
    matrix.compiled_at = time.strftime("%Y-%m-%d %H:%M:%S")

    logger.info(
        "Intent compiled: %d claims (%d critical), $%.3f, %.1fs",
        len(matrix.claims), len(matrix.critical_claims()), cost_usd, duration_s,
    )

    return matrix


def _parse_matrix(raw: str, intent: str) -> RequirementMatrix:
    """Parse the LLM output into a RequirementMatrix."""

    text = raw.strip()

    json_str = None
    if "```json" in text:
        parts = text.split("```json")
        if len(parts) > 1:
            json_str = parts[-1].split("```")[0].strip()
    if json_str is None:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            json_str = text[start : end + 1]
    if json_str is None:
        raise ValueError(f"No JSON in intent compiler output: {text[:300]}")

    data = json.loads(json_str)

    claims = []
    for item in data.get("claims", []):
        claims.append(
            Claim(
                id=item.get("id", ""),
                description=item.get("description", ""),
                priority=item.get("priority", "important"),
                category=item.get("category", "feature"),
                test_approach=item.get("test_approach", "api"),
                test_steps=_normalize_claim_steps(item),
                hard_fail=item.get("hard_fail", False),
            )
        )

    return RequirementMatrix(
        intent=intent,
        claims=claims,
        non_goals=data.get("non_goals", []),
        ambiguities=data.get("ambiguities", []),
        product_type_hint=data.get("product_type_hint", "unknown"),
    )


def save_matrix(matrix: RequirementMatrix, path: Path) -> None:
    """Save requirement matrix to JSON for reuse."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(matrix.to_dict(), indent=2))


def load_matrix(path: Path) -> RequirementMatrix:
    """Load a saved requirement matrix."""

    data = json.loads(path.read_text())
    claims = [
        Claim(
            id=item.get("id", ""),
            description=item.get("description", ""),
            priority=item.get("priority", "important"),
            category=item.get("category", "feature"),
            test_approach=item.get("test_approach", "api"),
            test_steps=_normalize_claim_steps(item),
            hard_fail=item.get("hard_fail", False),
        )
        for item in data.get("claims", [])
    ]
    return RequirementMatrix(
        intent=data["intent"],
        claims=claims,
        non_goals=data.get("non_goals", []),
        ambiguities=data.get("ambiguities", []),
        product_type_hint=data.get("product_type_hint", "unknown"),
        compiled_at=data.get("compiled_at", ""),
        cost_usd=data.get("cost_usd", 0.0),
    )


def _normalize_claim_steps(claim: dict[str, Any]) -> list[TestStep]:
    """Ensure every claim exposes structured, machine-executable steps."""

    raw_steps = claim.get("test_steps", [])
    if all(isinstance(step, dict) for step in raw_steps):
        steps = [_normalize_structured_step(step) for step in raw_steps]
    else:
        steps = _normalize_legacy_steps(
        claim_id=claim.get("id", ""),
        description=claim.get("description", ""),
        test_approach=claim.get("test_approach", "api"),
        raw_steps=raw_steps,
        )
    return _enrich_claim_steps(claim.get("id", ""), steps)


def _normalize_structured_step(step: dict[str, Any]) -> TestStep:
    normalized = dict(step)
    normalized["action"] = str(normalized.get("action", "check_exists"))
    if normalized["action"] in {"http", "navigate"}:
        normalized.setdefault(
            "expect_status",
            [200] if normalized["action"] == "navigate" else [200, 201],
        )
    if normalized["action"] == "cli":
        normalized.setdefault("expect_exit_code", 0)
    if normalized["action"] == "check_exists":
        normalized.setdefault("target", "http")
    return normalized


def _enrich_claim_steps(claim_id: str, steps: list[TestStep]) -> list[TestStep]:
    claim_key = claim_id.lower()
    enriched = [dict(step) for step in steps]

    for step in enriched:
        if str(step.get("action", "")).lower() != "http":
            continue

        method = str(step.get("method", "GET")).upper()
        candidate_paths = _step_candidate_paths(step)
        body = step.get("body")
        body_variants = step.get("body_variants")

        if claim_key == "checkout-stripe-integration" and method == "POST":
            if body in (None, {}):
                step["body"] = _default_checkout_body()

        if claim_key == "admin-create-product" and method == "POST":
            step["body"] = _ensure_product_fields(body)
            if isinstance(body_variants, list):
                step["body_variants"] = [_ensure_product_fields(variant) for variant in body_variants]

        if claim_key in {"admin-edit-product", "admin-delete-product"} and method in {"PUT", "PATCH", "DELETE"}:
            step["candidate_paths"] = _merge_candidate_paths(
                candidate_paths,
                [
                    "/api/products/{{product_id}}",
                    "/api/admin/products/{{product_id}}",
                    "/admin/api/products/{{product_id}}",
                ],
            )

        if claim_key in {"admin-manage-order-status", "admin-update-order-status"} and method in {"PUT", "PATCH"}:
            step["candidate_paths"] = _merge_candidate_paths(
                candidate_paths,
                [
                    "/api/orders/{{order_id}}",
                    "/api/admin/orders/{{order_id}}",
                    "/api/admin/orders/{{order_id}}/status",
                ],
            )
            step["body_variants"] = _merge_body_variants(
                step.get("body"),
                step.get("body_variants"),
                [{"status": "SHIPPED"}],
            )

        if claim_key in {"admin-no-public-access", "admin-authorization"} and method == "GET":
            step["candidate_paths"] = _merge_candidate_paths(
                candidate_paths,
                [
                    "/api/admin/stats",
                    "/api/admin/orders",
                    "/api/admin/products",
                ],
            )

    return enriched


def _default_checkout_body() -> dict[str, Any]:
    return {
        "shippingAddress": {
            "name": "Baseline Test User",
            "address": "123 Market Street",
            "city": "San Francisco",
            "state": "CA",
            "zip": "94103",
            "country": "US",
        }
    }


def _ensure_product_fields(body: Any) -> Any:
    if not isinstance(body, dict):
        return body
    enriched = dict(body)
    enriched.setdefault("category", "General")
    enriched.setdefault("stock", 100)
    enriched.setdefault("imageUrl", "https://placehold.co/400x300")
    return enriched


def _merge_candidate_paths(existing: list[str], additions: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for path in [*existing, *additions]:
        path_str = str(path)
        if path_str in seen:
            continue
        seen.add(path_str)
        merged.append(path_str)
    return merged


def _merge_body_variants(
    body: Any,
    body_variants: Any,
    additions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for variant in ([body] if isinstance(body, dict) else []) + (body_variants or []) + additions:
        if not isinstance(variant, dict):
            continue
        if variant not in merged:
            merged.append(dict(variant))
    return merged


def _step_candidate_paths(step: dict[str, Any]) -> list[str]:
    candidate_paths = step.get("candidate_paths")
    if isinstance(candidate_paths, list) and candidate_paths:
        return [str(path) for path in candidate_paths]
    if step.get("path"):
        return [str(step["path"])]
    return []


def _normalize_legacy_steps(
    claim_id: str,
    description: str,
    test_approach: str,
    raw_steps: list[Any],
) -> list[TestStep]:
    claim_key = claim_id.lower()

    if claim_key == "auth-register":
        return [
            _step_navigate(
                ["/register", "/signup", "/auth/register", "/auth/signup"],
                body_contains=["register", "sign up", "email"],
            ),
            _step_http(
                "POST",
                ["/api/auth/register", "/api/register", "/api/signup", "/api/auth/signup"],
                body={"email": "{{email}}", "password": "{{password}}", "name": "{{name}}"},
                expect_status=[200, 201],
                expect_body_contains=["email"],
                expect_json_keys=["id", "email"],
                capture_auth=True,
            ),
        ]

    if claim_key == "auth-login":
        return [
            _seed_register_step(),
            _seed_login_step(),
            _step_check_exists(
                ["/account", "/dashboard", "/checkout", "/cart"],
                existence_statuses=[200, 302, 401, 403],
            ),
        ]

    if claim_key == "auth-logout":
        return [
            _seed_register_step(),
            _seed_login_step(),
            _step_http(
                "POST",
                ["/api/auth/logout", "/api/logout", "/logout"],
                expect_status=[200, 204, 302],
            ),
            _step_check_exists(
                ["/checkout", "/admin", "/cart"],
                existence_statuses=[302, 401, 403],
            ),
        ]

    if claim_key == "auth-protected-routes":
        return [
            _step_check_exists(
                ["/checkout", "/cart", "/admin", "/admin/orders"],
                existence_statuses=[302, 401, 403],
            ),
        ]

    if claim_key == "catalog-list":
        return [
            _step_navigate(
                ["/products", "/shop", "/catalog", "/"],
                body_contains=["product", "price"],
            ),
            _step_http(
                "GET",
                ["/api/products", "/api/product", "/api/catalog/products", "/products"],
                expect_status=[200],
                expect_body_contains=["price"],
                extract={"type": "first_product"},
            ),
        ]

    if claim_key == "catalog-detail":
        return [
            _discover_product_step(),
            _step_http(
                "GET",
                [
                    "/api/products/{{product_id}}",
                    "/api/product/{{product_id}}",
                    "/products/{{product_id}}",
                    "/product/{{product_id}}",
                    "/products/{{product_slug}}",
                ],
                expect_status=[200],
                expect_body_contains=["{{product_name}}", "price", "description"],
            ),
            _step_check_exists(
                ["/cart", "/api/cart", "/api/cart/add", "/api/cart/items"],
                existence_statuses=[200, 201, 302, 401, 403],
            ),
        ]

    if claim_key == "cart-add-item":
        return [
            _seed_register_step(),
            _seed_login_step(),
            _discover_product_step(),
            _step_http(
                "POST",
                ["/api/cart", "/api/cart/add", "/api/cart/items", "/api/users/me/cart"],
                body_variants=[
                    {"productId": "{{product_id}}", "quantity": 1},
                    {"product_id": "{{product_id}}", "quantity": 1},
                    {"id": "{{product_id}}", "quantity": 1},
                ],
                expect_status=[200, 201],
            ),
            _step_http(
                "GET",
                ["/api/cart", "/cart"],
                expect_status=[200],
                expect_body_contains=["{{product_name}}"],
            ),
        ]

    if claim_key == "cart-update-quantity":
        return [
            _seed_register_step(),
            _seed_login_step(),
            _discover_product_step(),
            _step_http(
                "POST",
                ["/api/cart", "/api/cart/add", "/api/cart/items"],
                body_variants=[
                    {"productId": "{{product_id}}", "quantity": 1},
                    {"product_id": "{{product_id}}", "quantity": 1},
                ],
                expect_status=[200, 201],
            ),
            _step_http(
                "PATCH",
                ["/api/cart", "/api/cart/items", "/api/cart/update"],
                body_variants=[
                    {"productId": "{{product_id}}", "quantity": 3},
                    {"product_id": "{{product_id}}", "quantity": 3},
                ],
                expect_status=[200],
            ),
            _step_http(
                "GET",
                ["/api/cart", "/cart"],
                expect_status=[200],
                expect_body_contains=["3"],
            ),
        ]

    if claim_key == "cart-remove-item":
        return [
            _seed_register_step(),
            _seed_login_step(),
            _discover_product_step(),
            _step_http(
                "POST",
                ["/api/cart", "/api/cart/add", "/api/cart/items"],
                body_variants=[
                    {"productId": "{{product_id}}", "quantity": 1},
                    {"product_id": "{{product_id}}", "quantity": 1},
                ],
                expect_status=[200, 201],
            ),
            _step_http(
                "DELETE",
                ["/api/cart", "/api/cart/items/{{product_id}}", "/api/cart/remove"],
                body_variants=[
                    {"productId": "{{product_id}}"},
                    {"product_id": "{{product_id}}"},
                ],
                expect_status=[200, 204],
            ),
        ]

    if claim_key == "cart-total":
        return [
            _seed_register_step(),
            _seed_login_step(),
            _step_http(
                "GET",
                ["/api/products", "/api/product", "/api/catalog/products"],
                expect_status=[200],
                expect_body_contains=["price"],
                extract={"type": "first_two_products"},
            ),
            _step_http(
                "POST",
                ["/api/cart", "/api/cart/add", "/api/cart/items"],
                body_variants=[
                    {"productId": "{{product_id}}", "quantity": 1},
                    {"product_id": "{{product_id}}", "quantity": 1},
                ],
                expect_status=[200, 201],
            ),
            _step_http(
                "POST",
                ["/api/cart", "/api/cart/add", "/api/cart/items"],
                body_variants=[
                    {"productId": "{{second_product_id}}", "quantity": 1},
                    {"product_id": "{{second_product_id}}", "quantity": 1},
                ],
                expect_status=[200, 201],
            ),
            _step_http(
                "GET",
                ["/api/cart", "/cart"],
                expect_status=[200],
                expect_body_contains=["total"],
            ),
        ]

    if claim_key in {"checkout-stripe-payment", "checkout-stripe-decline"}:
        return [
            _step_navigate(
                ["/checkout", "/cart/checkout"],
                body_contains=["checkout", "payment", "stripe"],
            ),
            _step_check_exists(
                ["/api/checkout", "/api/orders", "/api/payments/stripe", "/api/stripe/checkout"],
                existence_statuses=[200, 201, 302, 401, 403],
            ),
        ]

    if claim_key == "admin-auth":
        return [
            _seed_register_step(),
            _seed_login_step(),
            _step_check_exists(
                ["/admin", "/admin/orders", "/api/admin/orders", "/api/orders"],
                existence_statuses=[302, 401, 403],
            ),
        ]

    if claim_key in {"admin-product-create", "admin-product-edit", "admin-product-delete"}:
        return [
            _step_check_exists(
                ["/admin/products", "/admin", "/api/admin/products", "/api/products"],
                existence_statuses=[200, 201, 302, 401, 403],
            ),
        ]

    if claim_key in {"admin-orders-list", "admin-order-detail", "data-order-persists"}:
        return [
            _step_check_exists(
                ["/admin/orders", "/api/admin/orders", "/api/orders", "/orders"],
                existence_statuses=[200, 302, 401, 403],
            ),
        ]

    if claim_key == "ux-empty-cart":
        return [
            _step_navigate(
                ["/cart"],
                body_contains=["cart", "empty", "browse", "shop"],
                expect_status=[200, 302, 401, 403],
            ),
        ]

    if test_approach == "cli":
        for raw_step in raw_steps:
            if isinstance(raw_step, str) and raw_step.strip():
                return [
                    {
                        "action": "cli",
                        "command": raw_step.strip(),
                        "expect_exit_code": 0,
                    }
                ]

    if test_approach == "code-review":
        return [
            {
                "action": "check_exists",
                "target": "note",
                "reason": description or "code review required",
            }
        ]

    return [
        {
            "action": "check_exists",
            "target": "note",
            "reason": f"legacy steps could not be normalized for {claim_id or description}",
            "raw_steps": raw_steps,
        }
    ]


def _step_http(
    method: str,
    candidate_paths: list[str],
    *,
    body: dict[str, Any] | None = None,
    body_variants: list[dict[str, Any]] | None = None,
    expect_status: list[int] | None = None,
    expect_body_contains: list[str] | None = None,
    expect_json_keys: list[str] | None = None,
    extract: dict[str, Any] | None = None,
    capture_auth: bool = False,
) -> TestStep:
    step: TestStep = {
        "action": "http",
        "method": method,
        "candidate_paths": candidate_paths,
        "expect_status": expect_status or ([200] if method == "GET" else [200, 201]),
    }
    if body is not None:
        step["body"] = body
    if body_variants is not None:
        step["body_variants"] = body_variants
    if expect_body_contains is not None:
        step["expect_body_contains"] = expect_body_contains
    if expect_json_keys is not None:
        step["expect_json_keys"] = expect_json_keys
    if extract is not None:
        step["extract"] = extract
    if capture_auth:
        step["capture_auth"] = True
    return step


def _step_navigate(
    candidate_paths: list[str],
    *,
    body_contains: list[str] | None = None,
    expect_status: list[int] | None = None,
) -> TestStep:
    step: TestStep = {
        "action": "navigate",
        "candidate_paths": candidate_paths,
        "expect_status": expect_status or [200],
    }
    if body_contains is not None:
        step["expect_body_contains"] = body_contains
    return step


def _step_check_exists(
    candidate_paths: list[str],
    *,
    existence_statuses: list[int],
) -> TestStep:
    return {
        "action": "check_exists",
        "target": "http",
        "candidate_paths": candidate_paths,
        "existence_statuses": existence_statuses,
    }


def _seed_register_step() -> TestStep:
    return _step_http(
        "POST",
        ["/api/auth/register", "/api/register", "/api/signup", "/api/auth/signup"],
        body={"email": "{{email}}", "password": "{{password}}", "name": "{{name}}"},
        expect_status=[200, 201],
        capture_auth=True,
    )


def _seed_login_step() -> TestStep:
    return _step_http(
        "POST",
        ["/api/auth/login", "/api/login", "/api/signin", "/api/auth/signin"],
        body_variants=[
            {"email": "{{email}}", "password": "{{password}}"},
            {"username": "{{email}}", "password": "{{password}}"},
        ],
        expect_status=[200, 201],
        capture_auth=True,
    )


def _discover_product_step() -> TestStep:
    return _step_http(
        "GET",
        ["/api/products", "/api/product", "/api/catalog/products", "/products"],
        expect_status=[200],
        expect_body_contains=["price"],
        extract={"type": "first_product"},
    )
