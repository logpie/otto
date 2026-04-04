"""Product manifest — structured description of what a product offers.

Combines static analysis (adapter) with runtime confirmation (HTTP probes)
to produce a complete picture of the product's API surface. This manifest
is what the journey agent uses to understand HOW to interact with the product.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from otto.certifier.adapter import TestConfig
from otto.certifier.classifier import ProductProfile

logger = logging.getLogger("otto.certifier.manifest")


@dataclass
class ProductManifest:
    """Everything the journey agent needs to know about the product."""

    # From classifier
    framework: str
    language: str
    product_type: str
    interaction: str  # "browser" | "http" | "cli"

    # From adapter — auth
    auth_type: str  # "nextauth" | "jwt" | "session" | "none"
    register_endpoint: str
    login_endpoint: str
    seeded_users: list[dict[str, str]]  # [{email, password, role}]

    # From adapter — routes
    routes: list[dict[str, Any]]  # [{path, methods, requires_auth, requires_admin, confirmed}]

    # From adapter — models
    models: list[dict[str, Any]]  # [{name, fields: {name: type}, creatable_fields, enum_values}]

    # From runtime probes
    base_url: str = ""
    app_alive: bool = False
    confirmed_routes: list[str] = field(default_factory=list)
    response_shapes: dict[str, str] = field(default_factory=dict)  # route → "array"|"object"|"wrapped"

    # Metadata
    built_at: str = ""


def build_manifest(
    test_config: TestConfig,
    profile: ProductProfile,
    base_url: str | None = None,
) -> ProductManifest:
    """Build a ProductManifest from adapter + classifier output, optionally enriched with runtime probes."""

    # Format seeded users (omit empty passwords)
    seeded_users = []
    for u in test_config.seeded_users:
        if u.email:
            seeded_users.append({
                "email": u.email,
                "password": u.password or "",
                "role": u.role or "user",
            })

    # Format routes
    routes = []
    for r in test_config.routes:
        routes.append({
            "path": r.path,
            "methods": r.methods,
            "requires_auth": r.requires_auth,
            "requires_admin": r.requires_admin,
            "confirmed": False,
        })

    # Format models with fields, creatable fields, and enum values
    models = []
    for model_name in test_config.models:
        fields = test_config.model_fields.get(model_name, {})
        creatable = test_config.creatable_fields.get(model_name, [])
        # Filter out FK fields from creatable
        creatable_no_fk = [f for f in creatable if not (f.endswith("Id") and f != "id")]
        # Collect enum values for this model's fields
        model_enums = {}
        for field_name in fields:
            key = f"{model_name}.{field_name}"
            if key in test_config.enum_values:
                model_enums[field_name] = test_config.enum_values[key]
            elif field_name in test_config.enum_values:
                model_enums[field_name] = test_config.enum_values[field_name]

        models.append({
            "name": model_name,
            "fields": fields,
            "creatable_fields": creatable_no_fk,
            "enum_values": model_enums,
        })

    manifest = ProductManifest(
        framework=profile.framework,
        language=profile.language,
        product_type=profile.product_type,
        interaction=profile.interaction,
        auth_type=test_config.auth_type,
        register_endpoint=test_config.register_endpoint,
        login_endpoint=test_config.login_endpoint,
        seeded_users=seeded_users,
        routes=routes,
        models=models,
        base_url=base_url or "",
        built_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    # Runtime enrichment if app is running
    if base_url:
        _enrich_from_runtime(manifest, base_url)

    return manifest


def _enrich_from_runtime(manifest: ProductManifest, base_url: str) -> None:
    """Probe the running app to confirm routes and discover response shapes."""
    # Check if app is alive
    try:
        r = requests.get(base_url, timeout=5)
        manifest.app_alive = r.status_code < 500
    except Exception:
        manifest.app_alive = False
        return

    # Confirm each route
    for route in manifest.routes:
        path = route["path"]
        try:
            # Use GET for confirmation (safe, no side effects)
            r = requests.get(f"{base_url}{path}", timeout=3)
            confirmed = r.status_code != 404
            route["confirmed"] = confirmed
            if confirmed:
                manifest.confirmed_routes.append(path)

                # Discover response shape
                if r.status_code == 200:
                    try:
                        data = r.json()
                        if isinstance(data, list):
                            manifest.response_shapes[path] = "array"
                        elif isinstance(data, dict):
                            if "data" in data and isinstance(data["data"], (list, dict)):
                                manifest.response_shapes[path] = "wrapped:data"
                            elif "items" in data and isinstance(data["items"], list):
                                manifest.response_shapes[path] = "wrapped:items"
                            elif "results" in data and isinstance(data["results"], list):
                                manifest.response_shapes[path] = "wrapped:results"
                            else:
                                manifest.response_shapes[path] = "object"
                    except (ValueError, KeyError):
                        pass
        except Exception:
            route["confirmed"] = False


def format_manifest_for_agent(manifest: ProductManifest) -> str:
    """Format the manifest as human-readable text for the journey agent's prompt."""
    lines = [
        "# Product Manifest",
        "",
        f"Framework: {manifest.framework}",
        f"Language: {manifest.language}",
        f"Product type: {manifest.product_type}",
        f"Interaction: {manifest.interaction}",
        f"Base URL: {manifest.base_url}",
        "",
    ]

    # Auth
    lines.append("## Authentication")
    lines.append(f"Auth type: {manifest.auth_type}")
    if manifest.register_endpoint:
        lines.append(f"Register endpoint: POST {manifest.register_endpoint}")
    if manifest.login_endpoint:
        lines.append(f"Login endpoint: POST {manifest.login_endpoint}")
    if manifest.auth_type == "nextauth":
        lines.append("NextAuth CSRF flow: GET /api/auth/csrf → POST /api/auth/callback/credentials with csrfToken + email + password as form data")
    lines.append("")

    if manifest.seeded_users:
        lines.append("## Seeded Users (pre-existing in database)")
        for u in manifest.seeded_users:
            pw = u["password"] if u["password"] else "(unknown password)"
            lines.append(f"- {u['email']} / {pw} (role: {u['role']})")
        lines.append("")

    # Routes
    lines.append("## API Routes")
    for r in manifest.routes:
        methods = ",".join(r["methods"])
        auth = " [auth]" if r["requires_auth"] else ""
        admin = " [admin]" if r["requires_admin"] else ""
        confirmed = " ✓" if r.get("confirmed") else ""
        shape = ""
        if r["path"] in manifest.response_shapes:
            shape = f" → {manifest.response_shapes[r['path']]}"
        lines.append(f"  {methods:12s} {r['path']}{auth}{admin}{confirmed}{shape}")
    lines.append("")

    # Models
    lines.append("## Data Models")
    for m in manifest.models:
        lines.append(f"### {m['name']}")
        for field_name, field_type in m["fields"].items():
            enums = m["enum_values"].get(field_name)
            enum_str = f" ({', '.join(enums)})" if enums else ""
            is_fk = " [FK, server-set]" if field_name.endswith("Id") and field_name != "id" else ""
            is_auto = " [auto]" if field_name in ("id", "createdAt", "updatedAt") else ""
            lines.append(f"  {field_name}: {field_type}{enum_str}{is_fk}{is_auto}")
        if m["creatable_fields"]:
            lines.append(f"  Creatable (client can set): {', '.join(m['creatable_fields'])}")
        lines.append("")

    return "\n".join(lines)
