"""Tier 2: Agentic user journey verification.

Tests whether multi-step user flows work end-to-end. The output of
step 1 feeds into step 2. This catches integration bugs that
per-endpoint Tier 1 probes miss.

Two modes:
- sequential (default): API/CLI calls in sequence. Cheaper, faster.
- browser: real browser via chrome-devtools MCP. Catches visual/UX bugs.

Example journey Tier 1 can't test:
  1. Register user → get user ID
  2. Login with that user → get session
  3. Browse products → pick a product ID
  4. Add product to cart → verify cart has item
  5. Checkout → verify order created
  6. View order history → verify order appears

Each step depends on the previous step's output. If step 3 fails,
steps 4-6 can't run. That's the "journey" — a connected flow.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from otto.certifier.adapter import TestConfig
from otto.certifier.intent_compiler import RequirementMatrix
from otto.observability import append_text_log

logger = logging.getLogger("otto.certifier.tier2")


@dataclass
class JourneyStep:
    action: str
    detail: str
    passed: bool
    data: dict[str, Any] = field(default_factory=dict)  # output for next step
    error: str = ""


@dataclass
class JourneyResult:
    name: str
    description: str
    passed: bool
    steps: list[JourneyStep]
    stopped_at: str = ""  # which step broke the flow


@dataclass
class Tier2Result:
    product_dir: str
    intent: str
    base_url: str
    journeys_tested: int
    journeys_passed: int
    journeys_failed: int
    journeys: list[JourneyResult]
    cost_usd: float = 0.0
    duration_s: float = 0.0
    mode: str = "sequential"

    def score(self) -> str:
        if self.journeys_tested == 0:
            return "n/a"
        return f"{self.journeys_passed}/{self.journeys_tested} ({round(self.journeys_passed / self.journeys_tested * 100)}%)"


def _tier2_log(project_dir: Path, *lines: str) -> None:
    append_text_log(project_dir / "certifier-reports" / "tier2.log", lines)


def run_tier2_sequential(
    test_config: TestConfig,
    base_url: str,
    project_dir: Path,
    matrix: RequirementMatrix | None = None,
) -> Tier2Result:
    """Run Tier 2 sequential journey tests. No LLM needed.

    Journeys are derived from claims in the matrix (if provided)
    plus standard flows based on adapter-discovered features.
    """
    start_time = time.monotonic()

    _tier2_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Tier 2 sequential started",
        f"base_url: {base_url}",
    )

    journeys = []

    # Core journeys based on adapter-discovered features
    if test_config.register_endpoint or test_config.has_cart_model:
        journeys.append(_test_shopping_journey(test_config, base_url))

    if test_config.admin_user():
        journeys.append(_test_admin_journey(test_config, base_url))

    # Always test error handling
    journeys.append(_test_error_journey(test_config, base_url))

    # Additional journeys from matrix claims (features not covered above)
    if matrix:
        extra = _derive_extra_journeys(matrix, test_config, base_url)
        journeys.extend(extra)

    duration_s = round(time.monotonic() - start_time, 1)
    passed = sum(1 for j in journeys if j.passed)

    result = Tier2Result(
        product_dir=str(project_dir),
        intent="",
        base_url=base_url,
        journeys_tested=len(journeys),
        journeys_passed=passed,
        journeys_failed=len(journeys) - passed,
        journeys=journeys,
        duration_s=duration_s,
        mode="sequential",
    )

    # Save reports
    report_dir = project_dir / "certifier-reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    save_tier2_report(result, report_dir / "tier2-report.json")
    save_tier2_markdown(result, report_dir / "tier2-report.md")

    _tier2_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Tier 2 done: {result.score()}, {duration_s}s",
    )

    return result


# ---------------------------------------------------------------------------
# Journey: Shopping flow (register → browse → cart → checkout → order history)
# ---------------------------------------------------------------------------

def _test_shopping_journey(config: TestConfig, base_url: str) -> JourneyResult:
    steps: list[JourneyStep] = []
    session = requests.Session()
    flow_data: dict[str, Any] = {}

    # Step 1: Register
    email = f"tier2-{int(time.time())}@eval.test"
    step = _step_register(session, base_url, config, email)
    steps.append(step)
    if not step.passed:
        return JourneyResult(
            name="Shopping Flow", description="Register → browse → cart → checkout → orders",
            passed=False, steps=steps, stopped_at="register",
        )

    # Step 2: Login
    step = _step_login(session, base_url, config, email)
    steps.append(step)
    if not step.passed:
        return JourneyResult(
            name="Shopping Flow", description="Register → browse → cart → checkout → orders",
            passed=False, steps=steps, stopped_at="login",
        )

    # Step 3: Browse products, pick one
    step = _step_browse_products(session, base_url)
    steps.append(step)
    if not step.passed:
        return JourneyResult(
            name="Shopping Flow", description="Register → browse → cart → checkout → orders",
            passed=False, steps=steps, stopped_at="browse",
        )
    flow_data["product_id"] = step.data.get("product_id")
    flow_data["product_name"] = step.data.get("product_name")
    flow_data["product_price"] = step.data.get("product_price")

    # Step 4: Add to cart
    step = _step_add_to_cart(session, base_url, flow_data["product_id"])
    steps.append(step)
    if not step.passed:
        return JourneyResult(
            name="Shopping Flow", description="Register → browse → cart → checkout → orders",
            passed=False, steps=steps, stopped_at="add_to_cart",
        )

    # Step 5: View cart, verify product is there
    step = _step_view_cart(session, base_url, flow_data["product_name"])
    steps.append(step)
    if not step.passed:
        return JourneyResult(
            name="Shopping Flow", description="Register → browse → cart → checkout → orders",
            passed=False, steps=steps, stopped_at="view_cart",
        )

    # Step 6: Checkout
    step = _step_checkout(session, base_url)
    steps.append(step)
    if not step.passed:
        return JourneyResult(
            name="Shopping Flow", description="Register → browse → cart → checkout → orders",
            passed=False, steps=steps, stopped_at="checkout",
        )
    flow_data["order_id"] = step.data.get("order_id")

    # Step 7: View order history, verify order appears
    step = _step_view_orders(session, base_url, flow_data.get("order_id"))
    steps.append(step)

    all_passed = all(s.passed for s in steps)
    return JourneyResult(
        name="Shopping Flow", description="Register → browse → cart → checkout → orders",
        passed=all_passed, steps=steps,
        stopped_at="" if all_passed else steps[-1].action,
    )


def _step_register(session: requests.Session, base_url: str, config: TestConfig, email: str) -> JourneyStep:
    for path in [config.register_endpoint] + config.register_candidates + ["/api/auth/register", "/api/register", "/api/signup"]:
        if not path:
            continue
        try:
            r = session.post(f"{base_url}{path}", json={
                "email": email, "password": "Tier2Test123!", "name": "Tier 2 Test User",
            }, timeout=10)
            if r.status_code in (200, 201):
                return JourneyStep(
                    action="register", detail=f"POST {path} → {r.status_code}",
                    passed=True, data={"email": email, "user": r.json()},
                )
        except Exception:
            continue
    return JourneyStep(action="register", detail="no registration endpoint responded", passed=False, error="registration failed on all endpoints")


def _step_login(session: requests.Session, base_url: str, config: TestConfig, email: str) -> JourneyStep:
    if config.auth_type == "nextauth":
        try:
            csrf_r = session.get(f"{base_url}/api/auth/csrf", timeout=10)
            csrf = csrf_r.json().get("csrfToken", "")
            r = session.post(f"{base_url}/api/auth/callback/credentials", data={
                "email": email, "password": "Tier2Test123!",
                "csrfToken": csrf, "redirect": "false", "json": "true",
            }, timeout=10, allow_redirects=False)
            if "next-auth.session-token" in dict(session.cookies):
                return JourneyStep(action="login", detail=f"NextAuth login → session established", passed=True)
        except Exception as e:
            return JourneyStep(action="login", detail=str(e), passed=False, error=str(e))

    for path in ["/api/auth/login", "/api/login"]:
        try:
            r = session.post(f"{base_url}{path}", json={
                "email": email, "password": "Tier2Test123!",
            }, timeout=10)
            if r.status_code in (200, 201):
                return JourneyStep(action="login", detail=f"POST {path} → {r.status_code}", passed=True)
        except Exception:
            continue

    return JourneyStep(action="login", detail="login failed", passed=False, error="could not establish session")


def _step_browse_products(session: requests.Session, base_url: str) -> JourneyStep:
    for path in ["/api/products", "/api/catalog"]:
        try:
            r = session.get(f"{base_url}{path}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                products = data if isinstance(data, list) else data.get("data") or data.get("products") or []
                if products and isinstance(products, list) and len(products) > 0:
                    p = products[0]
                    return JourneyStep(
                        action="browse_products",
                        detail=f"GET {path} → {len(products)} products. Picked: {p.get('name', '?')} (${p.get('price', '?')})",
                        passed=True,
                        data={"product_id": p.get("id"), "product_name": p.get("name"), "product_price": p.get("price")},
                    )
        except Exception:
            continue
    return JourneyStep(action="browse_products", detail="no products found", passed=False, error="product catalog empty or unreachable")


def _step_add_to_cart(session: requests.Session, base_url: str, product_id: str) -> JourneyStep:
    for path in ["/api/cart", "/api/cart/items"]:
        try:
            r = session.post(f"{base_url}{path}", json={"productId": product_id, "quantity": 1}, timeout=10)
            if r.status_code in (200, 201):
                return JourneyStep(
                    action="add_to_cart",
                    detail=f"POST {path} → {r.status_code}. Product {product_id} added.",
                    passed=True,
                )
        except Exception:
            continue
    return JourneyStep(action="add_to_cart", detail="add to cart failed", passed=False, error="no cart endpoint accepted the request")


def _step_view_cart(session: requests.Session, base_url: str, expected_product: str | None) -> JourneyStep:
    for path in ["/api/cart", "/api/cart/items"]:
        try:
            r = session.get(f"{base_url}{path}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                items = data if isinstance(data, list) else data.get("data") or data.get("items") or data.get("cart") or []
                if items and isinstance(items, list) and len(items) > 0:
                    product_found = not expected_product or any(
                        expected_product.lower() in json.dumps(item).lower() for item in items
                    )
                    return JourneyStep(
                        action="view_cart",
                        detail=f"GET {path} → {len(items)} item(s). Product {'found' if product_found else 'NOT found'}.",
                        passed=product_found,
                        error="" if product_found else f"expected '{expected_product}' in cart but not found",
                    )
        except Exception:
            continue
    return JourneyStep(action="view_cart", detail="cart empty or unreachable", passed=False, error="could not read cart")


def _step_checkout(session: requests.Session, base_url: str) -> JourneyStep:
    body = {
        "shippingAddress": {
            "name": "Tier 2 Test", "address": "123 Test St",
            "city": "Testville", "state": "CA", "zip": "90210", "country": "US",
        },
    }
    for path in ["/api/checkout", "/api/orders/checkout", "/api/payments"]:
        try:
            r = session.post(f"{base_url}{path}", json=body, timeout=10)
            if r.status_code in (200, 201):
                data = r.json()
                order_id = None
                # Extract order ID from various response shapes
                for key_path in [["data", "orderId"], ["data", "id"], ["orderId"], ["id"], ["order", "id"]]:
                    obj = data
                    for k in key_path:
                        if isinstance(obj, dict):
                            obj = obj.get(k)
                        else:
                            obj = None
                            break
                    if obj:
                        order_id = str(obj)
                        break

                warning = ""
                if isinstance(data, dict):
                    warning = data.get("warning", "")

                detail = f"POST {path} → {r.status_code}. Order: {order_id or 'unknown'}."
                if warning:
                    detail += f" Warning: {warning}"

                return JourneyStep(
                    action="checkout", detail=detail, passed=True,
                    data={"order_id": order_id},
                    error=warning if "not configured" in warning.lower() else "",
                )
        except Exception:
            continue
    return JourneyStep(action="checkout", detail="checkout failed", passed=False, error="no checkout endpoint accepted the request")


def _step_view_orders(session: requests.Session, base_url: str, expected_order_id: str | None) -> JourneyStep:
    for path in ["/api/orders", "/api/user/orders"]:
        try:
            r = session.get(f"{base_url}{path}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                orders = data if isinstance(data, list) else data.get("data") or data.get("orders") or []
                if isinstance(orders, list):
                    order_found = not expected_order_id or any(
                        str(o.get("id", "")) == expected_order_id for o in orders
                    )
                    return JourneyStep(
                        action="view_orders",
                        detail=f"GET {path} → {len(orders)} order(s). New order {'found' if order_found else 'NOT found'}.",
                        passed=order_found,
                        error="" if order_found else f"order {expected_order_id} not in history",
                    )
        except Exception:
            continue
    return JourneyStep(action="view_orders", detail="orders unreachable", passed=False, error="could not read order history")


# ---------------------------------------------------------------------------
# Journey: Admin management
# ---------------------------------------------------------------------------

def _test_admin_journey(config: TestConfig, base_url: str) -> JourneyResult:
    steps: list[JourneyStep] = []
    session = requests.Session()
    admin = config.admin_user()

    if not admin:
        return JourneyResult(
            name="Admin Flow", description="Login as admin → create product → view orders",
            passed=False, steps=[JourneyStep(action="admin_login", detail="no admin credentials", passed=False, error="adapter found no admin user")],
            stopped_at="admin_login",
        )

    # Step 1: Login as admin
    step = _step_login(session, base_url, config, admin.email)
    # Override: use admin password
    if config.auth_type == "nextauth":
        session2 = requests.Session()
        try:
            csrf = session2.get(f"{base_url}/api/auth/csrf", timeout=10).json().get("csrfToken", "")
            session2.post(f"{base_url}/api/auth/callback/credentials", data={
                "email": admin.email, "password": admin.password,
                "csrfToken": csrf, "redirect": "false", "json": "true",
            }, timeout=10, allow_redirects=False)
            if "next-auth.session-token" in dict(session2.cookies):
                session = session2
                step = JourneyStep(action="admin_login", detail=f"Logged in as {admin.email} (admin)", passed=True)
        except Exception:
            pass

    steps.append(step)
    if not step.passed:
        return JourneyResult(
            name="Admin Flow", description="Login as admin → create product → view orders",
            passed=False, steps=steps, stopped_at="admin_login",
        )

    # Step 2: Create product
    product_data = {"name": "Tier2 Admin Test Product", "description": "Created by certifier", "price": 42.99, "stock": 10, "category": "Test"}
    step = _step_admin_create_product(session, base_url, product_data)
    steps.append(step)

    # Step 3: View orders
    step = _step_admin_view_orders(session, base_url)
    steps.append(step)

    all_passed = all(s.passed for s in steps)
    return JourneyResult(
        name="Admin Flow", description="Login as admin → create product → view orders",
        passed=all_passed, steps=steps,
    )


def _step_admin_create_product(session: requests.Session, base_url: str, product: dict[str, Any]) -> JourneyStep:
    for path in ["/api/products", "/api/admin/products"]:
        try:
            r = session.post(f"{base_url}{path}", json=product, timeout=10)
            if r.status_code in (200, 201):
                data = r.json()
                created = data.get("data") or data
                return JourneyStep(
                    action="admin_create_product",
                    detail=f"POST {path} → {r.status_code}. Created: {created.get('name', '?')} (${created.get('price', '?')})",
                    passed=True, data={"product": created},
                )
        except Exception:
            continue
    return JourneyStep(action="admin_create_product", detail="create product failed", passed=False, error="no product endpoint accepted the request")


def _step_admin_view_orders(session: requests.Session, base_url: str) -> JourneyStep:
    for path in ["/api/orders", "/api/admin/orders"]:
        try:
            r = session.get(f"{base_url}{path}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                orders = data if isinstance(data, list) else data.get("data") or data.get("orders") or []
                if isinstance(orders, list):
                    return JourneyStep(
                        action="admin_view_orders",
                        detail=f"GET {path} → {len(orders)} order(s)",
                        passed=True,
                    )
        except Exception:
            continue
    return JourneyStep(action="admin_view_orders", detail="orders unreachable", passed=False, error="could not read orders as admin")


# ---------------------------------------------------------------------------
# Journey: Error handling
# ---------------------------------------------------------------------------

def _test_error_journey(config: TestConfig, base_url: str) -> JourneyResult:
    steps: list[JourneyStep] = []

    # Step 1: 404 page
    try:
        r = requests.get(f"{base_url}/nonexistent-page-tier2-test", timeout=10)
        steps.append(JourneyStep(
            action="404_page",
            detail=f"GET /nonexistent → {r.status_code}",
            passed=r.status_code == 404,
            error="" if r.status_code == 404 else f"expected 404, got {r.status_code}",
        ))
    except Exception as e:
        steps.append(JourneyStep(action="404_page", detail=str(e), passed=False, error=str(e)))

    # Step 2: Wrong login credentials
    session = requests.Session()
    if config.auth_type == "nextauth":
        try:
            csrf = session.get(f"{base_url}/api/auth/csrf", timeout=10).json().get("csrfToken", "")
            r = session.post(f"{base_url}/api/auth/callback/credentials", data={
                "email": "wrong@wrong.com", "password": "wrongpassword",
                "csrfToken": csrf, "redirect": "false", "json": "true",
            }, timeout=10, allow_redirects=False)
            # Should NOT have session token
            has_session = "next-auth.session-token" in dict(session.cookies)
            steps.append(JourneyStep(
                action="wrong_credentials",
                detail=f"Login with wrong creds → {'session established (BAD!)' if has_session else 'rejected (correct)'}",
                passed=not has_session,
                error="wrong credentials accepted!" if has_session else "",
            ))
        except Exception as e:
            steps.append(JourneyStep(action="wrong_credentials", detail=str(e), passed=False, error=str(e)))

    # Step 3: Access protected endpoint without auth
    fresh = requests.Session()
    try:
        r = fresh.get(f"{base_url}/api/cart", timeout=10)
        steps.append(JourneyStep(
            action="unauthenticated_access",
            detail=f"GET /api/cart without auth → {r.status_code}",
            passed=r.status_code in (401, 403),
            error="" if r.status_code in (401, 403) else f"expected 401/403, got {r.status_code}",
        ))
    except Exception as e:
        steps.append(JourneyStep(action="unauthenticated_access", detail=str(e), passed=False, error=str(e)))

    all_passed = all(s.passed for s in steps)
    return JourneyResult(
        name="Error Handling", description="404, wrong credentials, unauthorized access",
        passed=all_passed, steps=steps,
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _derive_extra_journeys(
    matrix: RequirementMatrix,
    config: TestConfig,
    base_url: str,
) -> list[JourneyResult]:
    """Derive additional journeys from uncovered matrix claims."""
    extra: list[JourneyResult] = []

    # Search/filter journey — if catalog claims mention search
    has_search = any("search" in c.id.lower() or "filter" in c.id.lower() for c in matrix.claims)
    if has_search:
        extra.append(_test_search_journey(config, base_url))

    # Data persistence journey — verify state survives across sessions
    has_persistence = any("persist" in c.id.lower() or "history" in c.id.lower() for c in matrix.claims)
    if has_persistence:
        extra.append(_test_persistence_journey(config, base_url))

    return extra


def _test_search_journey(config: TestConfig, base_url: str) -> JourneyResult:
    """Search for products and verify results are relevant."""
    steps: list[JourneyStep] = []
    session = requests.Session()

    # Authenticate if needed
    if config.auth_type == "nextauth" and config.any_user():
        user = config.any_user()
        try:
            csrf = session.get(f"{base_url}/api/auth/csrf", timeout=10).json().get("csrfToken", "")
            session.post(f"{base_url}/api/auth/callback/credentials", data={
                "email": user.email, "password": user.password,
                "csrfToken": csrf, "redirect": "false", "json": "true",
            }, timeout=10, allow_redirects=False)
        except Exception:
            pass

    # Step 1: Get products to know what to search for
    product_name = None
    for path in ["/api/products"]:
        try:
            r = session.get(f"{base_url}{path}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                products = data if isinstance(data, list) else data.get("data") or data.get("products") or []
                if products and len(products) > 0:
                    product_name = products[0].get("name", "")
                    steps.append(JourneyStep(
                        action="get_product_name", detail=f"Found product: {product_name}", passed=True,
                    ))
                    break
        except Exception:
            pass

    if not product_name:
        steps.append(JourneyStep(action="get_product_name", detail="no products found", passed=False, error="catalog empty"))
        return JourneyResult(name="Search", description="Search products and verify results", passed=False, steps=steps, stopped_at="get_product_name")

    # Step 2: Search for first word of product name
    search_term = product_name.split()[0] if product_name else "test"
    for param in ["q", "search", "query"]:
        try:
            r = session.get(f"{base_url}/api/products", params={param: search_term}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                results = data if isinstance(data, list) else data.get("data") or data.get("products") or []
                if isinstance(results, list) and len(results) < 20:  # filtered, not all products
                    found = any(search_term.lower() in json.dumps(p).lower() for p in results)
                    steps.append(JourneyStep(
                        action="search",
                        detail=f"Search '{search_term}' ({param}=) → {len(results)} results, target {'found' if found else 'not found'}",
                        passed=found,
                        error="" if found else f"search for '{search_term}' didn't find '{product_name}'",
                    ))
                    all_passed = all(s.passed for s in steps)
                    return JourneyResult(name="Search", description="Search products and verify results", passed=all_passed, steps=steps)
        except Exception:
            continue

    steps.append(JourneyStep(action="search", detail="no search param worked", passed=False, error="search not functional"))
    return JourneyResult(name="Search", description="Search products and verify results", passed=False, steps=steps)


def _test_persistence_journey(config: TestConfig, base_url: str) -> JourneyResult:
    """Verify data persists across sessions."""
    steps: list[JourneyStep] = []

    # Session 1: Create something
    s1 = requests.Session()
    if config.auth_type == "nextauth" and config.any_user():
        user = config.any_user()
        try:
            csrf = s1.get(f"{base_url}/api/auth/csrf", timeout=10).json().get("csrfToken", "")
            s1.post(f"{base_url}/api/auth/callback/credentials", data={
                "email": user.email, "password": user.password,
                "csrfToken": csrf, "redirect": "false", "json": "true",
            }, timeout=10, allow_redirects=False)
        except Exception:
            pass

    # Add to cart in session 1
    product_id = None
    try:
        r = s1.get(f"{base_url}/api/products", timeout=10)
        if r.status_code == 200:
            data = r.json()
            products = data if isinstance(data, list) else data.get("data") or data.get("products") or []
            if products:
                product_id = products[0].get("id")
    except Exception:
        pass

    if product_id:
        try:
            r = s1.post(f"{base_url}/api/cart", json={"productId": product_id, "quantity": 1}, timeout=10)
            steps.append(JourneyStep(
                action="session1_add_cart", detail=f"Added product {product_id} to cart ({r.status_code})",
                passed=r.status_code in (200, 201),
            ))
        except Exception as e:
            steps.append(JourneyStep(action="session1_add_cart", detail=str(e), passed=False, error=str(e)))

    # Session 2: Fresh login, check cart persists
    s2 = requests.Session()
    if config.auth_type == "nextauth" and config.any_user():
        user = config.any_user()
        try:
            csrf = s2.get(f"{base_url}/api/auth/csrf", timeout=10).json().get("csrfToken", "")
            s2.post(f"{base_url}/api/auth/callback/credentials", data={
                "email": user.email, "password": user.password,
                "csrfToken": csrf, "redirect": "false", "json": "true",
            }, timeout=10, allow_redirects=False)
        except Exception:
            pass

    try:
        r = s2.get(f"{base_url}/api/cart", timeout=10)
        if r.status_code == 200:
            data = r.json()
            items = data if isinstance(data, list) else data.get("data") or data.get("items") or []
            has_items = isinstance(items, list) and len(items) > 0
            steps.append(JourneyStep(
                action="session2_check_cart",
                detail=f"New session, cart has {len(items) if isinstance(items, list) else '?'} item(s)",
                passed=has_items,
                error="" if has_items else "cart empty after re-login — data not persisted",
            ))
    except Exception as e:
        steps.append(JourneyStep(action="session2_check_cart", detail=str(e), passed=False, error=str(e)))

    all_passed = all(s.passed for s in steps)
    return JourneyResult(
        name="Data Persistence", description="Cart data survives across sessions",
        passed=all_passed, steps=steps,
    )


def save_tier2_report(result: Tier2Result, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "product_dir": result.product_dir,
        "intent": result.intent,
        "base_url": result.base_url,
        "mode": result.mode,
        "score": result.score(),
        "journeys_tested": result.journeys_tested,
        "journeys_passed": result.journeys_passed,
        "journeys_failed": result.journeys_failed,
        "duration_s": result.duration_s,
        "journeys": [
            {
                "name": j.name,
                "description": j.description,
                "passed": j.passed,
                "stopped_at": j.stopped_at,
                "steps": [
                    {
                        "action": s.action,
                        "detail": s.detail,
                        "passed": s.passed,
                        "data": s.data,
                        "error": s.error,
                    }
                    for s in j.steps
                ],
            }
            for j in result.journeys
        ],
    }
    path.write_text(json.dumps(data, indent=2, default=str))


def save_tier2_markdown(result: Tier2Result, path: Path) -> None:
    lines = [
        "# Tier 2 Certification — User Journey Testing",
        "",
        f"> **Product:** `{result.product_dir}`",
        f"> **Mode:** {result.mode}",
        f"> **Score:** {result.score()}",
        f"> **Duration:** {result.duration_s:.0f}s",
        "",
        "## What This Tests",
        "",
        "Multi-step user flows where each step depends on the previous step's output.",
        "Catches integration bugs that per-endpoint Tier 1 probes miss.",
        "If a step fails, the journey stops — downstream steps can't run.",
        "",
    ]

    for journey in result.journeys:
        icon = "✓" if journey.passed else "✗"
        lines.append(f"## {icon} {journey.name}")
        lines.append(f"_{journey.description}_")
        if journey.stopped_at:
            lines.append(f"**Stopped at:** {journey.stopped_at}")
        lines.append("")

        for step in journey.steps:
            step_icon = "✓" if step.passed else "✗"
            lines.append(f"- {step_icon} **{step.action}**: {step.detail}")
            if step.error:
                lines.append(f"  > ⚠ {step.error}")
        lines.append("")

    lines.extend([
        "---",
        "",
        "## Scope",
        "",
        "Tier 2 tests connected flows, not individual endpoints.",
        "A 100% score means all flows complete end-to-end.",
        "It does not test visual rendering, performance, or accessibility.",
    ])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
