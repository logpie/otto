"""Product manifest — structured description of what a product offers.

Combines static analysis (adapter) with runtime confirmation (HTTP probes)
to produce a complete picture of the product's API surface. This manifest
is what the journey agent uses to understand HOW to interact with the product.
"""

from __future__ import annotations

import logging
import shlex
import time
from dataclasses import asdict, dataclass, field
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

    # CLI-specific (populated when interaction == "cli")
    cli_entrypoint: list[str] = field(default_factory=list)  # ["python", "todo.py"] — argv
    cli_commands: list[dict[str, Any]] = field(default_factory=list)  # [{name, args, flags}]
    cli_help_text: str = ""  # captured --help output

    # Metadata
    built_at: str = ""


def build_manifest(
    test_config: TestConfig,
    profile: ProductProfile,
    base_url: str | None = None,
    interaction: str | None = None,
) -> ProductManifest:
    """Build a ProductManifest from adapter + classifier output, optionally enriched with runtime probes."""
    effective_interaction = interaction or profile.interaction

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

    # CLI entrypoint: normalize source paths to runnable argv
    cli_entrypoint: list[str] = []
    if effective_interaction == "cli":
        if test_config.cli_entrypoints:
            cli_entrypoint = _normalize_cli_entrypoint(test_config.cli_entrypoints[0], profile)
        elif profile.start_command:
            cli_entrypoint = _normalize_cli_entrypoint(profile.start_command, profile)

    manifest = ProductManifest(
        framework=profile.framework,
        language=profile.language,
        product_type=profile.product_type,
        interaction=effective_interaction,
        auth_type=test_config.auth_type,
        register_endpoint=test_config.register_endpoint,
        login_endpoint=test_config.login_endpoint,
        seeded_users=seeded_users,
        routes=routes,
        models=models,
        base_url=base_url or "",
        cli_entrypoint=cli_entrypoint,
        cli_commands=getattr(test_config, "cli_commands", []),
        built_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    # Runtime enrichment if app is running
    if base_url:
        _enrich_from_runtime(manifest, base_url)

    return manifest


def _normalize_cli_entrypoint(source_path: str, profile: ProductProfile) -> list[str]:
    """Convert a source file path to a runnable argv list.

    adapter.py records source file paths (e.g. "todo.py").
    classifier.py records commands (e.g. "cargo run").
    This normalizes to a directly executable argv.
    """
    # Rust — handled by _resolve_cli_entrypoint in worker (uses built binary)
    if profile.framework == "cargo":
        return ["cargo", "run", "--"]

    # Go — similar
    if profile.language == "go":
        return ["go", "run", "."]

    # Already looks like a command (has spaces or known prefixes)
    if " " in source_path:
        return shlex.split(source_path)

    # Python source file → python3 <file> (python3 is more portable than python)
    if source_path.endswith(".py"):
        return ["python3", source_path]

    # Fallback: try to run it directly
    return [source_path]


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
    ]
    if manifest.base_url:
        lines.append(f"Base URL: {manifest.base_url}")
    lines.append("")

    # CLI Interface
    if manifest.cli_entrypoint:
        lines.append("## CLI Interface")
        lines.append(f"Entrypoint: {shlex.join(manifest.cli_entrypoint)}")
        if manifest.cli_commands:
            lines.append("Commands:")
            for cmd in manifest.cli_commands:
                name = cmd.get("name", "")
                args = " ".join(cmd.get("args", []))
                flags = " ".join(cmd.get("flags", []))
                parts = [name]
                if args:
                    parts.append(f"<{args}>")
                if flags:
                    parts.append(f"[{flags}]")
                lines.append(f"  {' '.join(parts)}")
        if manifest.cli_help_text:
            lines.append("")
            lines.append("Help output:")
            lines.append(manifest.cli_help_text[:3000])
        lines.append("")

    # Auth (only for HTTP products with auth)
    if manifest.auth_type and manifest.auth_type not in ("none", "unknown"):
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

    # Routes (only when we have them)
    if manifest.routes:
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
    if manifest.models:
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

    # If no interface info at all, tell the agent to discover
    if not manifest.routes and not manifest.cli_entrypoint:
        lines.append("## Interface")
        lines.append("No routes or CLI entrypoint detected — discover by reading the code.")
        lines.append("")

    return "\n".join(lines)


def manifest_to_dict(m: ProductManifest) -> dict[str, Any]:
    """Serialize a ProductManifest to a JSON-friendly dict."""
    return asdict(m)


def manifest_from_dict(d: dict[str, Any]) -> ProductManifest:
    """Reconstruct a ProductManifest from a dict (JSON round-trip)."""
    return ProductManifest(**d)
