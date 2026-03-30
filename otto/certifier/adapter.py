"""Code-aware adapter — reads the project to understand HOW to test it.

The intent compiler decides WHAT to test (from the intent).
The adapter decides HOW to test (from the code).

It inspects route files, auth config, seed scripts, and prisma schema
to produce a TestConfig that the baseline runner uses for accurate probing.
No LLM needed — static analysis only.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("otto.certifier.adapter")


@dataclass
class SeededUser:
    email: str
    password: str
    role: str = "user"


@dataclass
class RouteInfo:
    path: str
    methods: list[str]
    requires_auth: bool = False
    requires_admin: bool = False


@dataclass
class TestConfig:
    """How to test this specific project. Code-derived, not intent-derived."""

    # Auth
    auth_type: str = "unknown"  # "nextauth" | "jwt" | "session" | "none" | "unknown"
    auth_csrf_required: bool = False
    register_endpoint: str = ""
    register_candidates: list[str] = field(default_factory=list)
    login_endpoint: str = ""
    login_candidates: list[str] = field(default_factory=list)
    seeded_users: list[SeededUser] = field(default_factory=list)

    # Routes
    routes: list[RouteInfo] = field(default_factory=list)

    # Data model
    models: list[str] = field(default_factory=list)  # e.g. ["User", "Product", "Order"]
    has_cart_model: bool = False

    # Framework specifics
    api_base: str = "/api"
    response_wrapper: str = ""  # e.g. "data" if responses are {data: ...}

    def admin_user(self) -> SeededUser | None:
        for u in self.seeded_users:
            if u.role.lower() in ("admin", "administrator"):
                return u
        return None

    def any_user(self) -> SeededUser | None:
        return self.seeded_users[0] if self.seeded_users else None


def analyze_project(project_dir: Path) -> TestConfig:
    """Analyze a project's code to produce a TestConfig."""
    config = TestConfig()

    _analyze_auth(project_dir, config)
    _analyze_routes(project_dir, config)
    _analyze_schema(project_dir, config)
    _analyze_seeds(project_dir, config)

    logger.info(
        "Project analyzed: auth=%s, %d routes, %d models, %d seeded users",
        config.auth_type, len(config.routes), len(config.models),
        len(config.seeded_users),
    )

    return config


def _analyze_auth(project_dir: Path, config: TestConfig) -> None:
    """Detect auth mechanism and endpoints."""

    # Check for NextAuth
    nextauth_files = list(project_dir.glob("**/[...nextauth]/route.ts")) + \
                     list(project_dir.glob("**/[...nextauth]/route.js"))
    if nextauth_files:
        config.auth_type = "nextauth"
        config.auth_csrf_required = True
        config.login_candidates = ["/api/auth/callback/credentials"]
        config.login_endpoint = "/api/auth/callback/credentials"

    # Check for register endpoint
    for pattern in [
        "**/api/auth/register/route.ts",
        "**/api/auth/register/route.js",
        "**/api/auth/signup/route.ts",
        "**/api/auth/signup/route.js",
        "**/api/register/route.ts",
        "**/api/signup/route.ts",
    ]:
        matches = list(project_dir.glob(pattern))
        if matches:
            # Derive endpoint from file path
            rel = matches[0].relative_to(project_dir)
            parts = list(rel.parts)
            # Remove src/app prefix and route.ts suffix
            if "src" in parts:
                parts = parts[parts.index("src") + 1:]
            if "app" in parts:
                parts = parts[parts.index("app") + 1:]
            if parts and parts[-1] in ("route.ts", "route.js"):
                parts = parts[:-1]
            endpoint = "/" + "/".join(parts)
            config.register_endpoint = endpoint
            config.register_candidates = [endpoint]
            break

    # If no register endpoint found but NextAuth exists, check auth.ts for providers
    if not config.register_endpoint and config.auth_type == "nextauth":
        for auth_file in project_dir.glob("**/lib/auth.ts"):
            try:
                content = auth_file.read_text()
                if "CredentialsProvider" in content:
                    # Has credentials but no register — users can only login with seeded accounts
                    config.register_candidates = []
            except OSError:
                pass

    # Check for JWT auth
    if config.auth_type == "unknown":
        for f in _find_source_files(project_dir):
            try:
                content = f.read_text()
                if "jsonwebtoken" in content or "jwt.sign" in content:
                    config.auth_type = "jwt"
                    break
            except OSError:
                pass


def _analyze_routes(project_dir: Path, config: TestConfig) -> None:
    """Find all API route files and extract endpoints."""

    # Next.js App Router: src/app/api/**/route.ts
    for route_file in project_dir.glob("**/app/api/**/route.ts"):
        _parse_nextjs_route(route_file, project_dir, config)
    for route_file in project_dir.glob("**/app/api/**/route.js"):
        _parse_nextjs_route(route_file, project_dir, config)

    # Express/Flask: look for route definitions in source files
    if not config.routes:
        for f in _find_source_files(project_dir):
            try:
                content = f.read_text()
                # Express: app.get('/api/...', ...) or router.get('/...', ...)
                for match in re.finditer(r'(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*[\'"]([^\'"]+)', content):
                    method = match.group(1).upper()
                    path = match.group(2)
                    config.routes.append(RouteInfo(path=path, methods=[method]))
            except OSError:
                pass


def _parse_nextjs_route(route_file: Path, project_dir: Path, config: TestConfig) -> None:
    """Parse a Next.js route file to extract methods and auth requirements."""
    try:
        content = route_file.read_text()
    except OSError:
        return

    rel = route_file.relative_to(project_dir)
    parts = list(rel.parts)
    if "src" in parts:
        parts = parts[parts.index("src") + 1:]
    if "app" in parts:
        parts = parts[parts.index("app") + 1:]
    if parts and parts[-1] in ("route.ts", "route.js"):
        parts = parts[:-1]

    # Convert [param] to :param for readability
    path = "/" + "/".join(p.replace("[", ":").replace("]", "") for p in parts)

    methods = []
    for method in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
        if re.search(rf'export\s+(?:async\s+)?function\s+{method}\b', content):
            methods.append(method)

    requires_auth = "getServerSession" in content or "auth(" in content or "session" in content.lower()
    requires_admin = "admin" in content.lower() and ("role" in content.lower() or "ADMIN" in content)

    if methods:
        config.routes.append(RouteInfo(
            path=path, methods=methods,
            requires_auth=requires_auth, requires_admin=requires_admin,
        ))


def _analyze_schema(project_dir: Path, config: TestConfig) -> None:
    """Parse Prisma schema for model names."""
    schema_file = project_dir / "prisma" / "schema.prisma"
    if not schema_file.exists():
        return

    try:
        content = schema_file.read_text()
        for match in re.finditer(r'model\s+(\w+)\s*\{', content):
            model = match.group(1)
            config.models.append(model)
            if model.lower() in ("cartitem", "cart_item", "cart"):
                config.has_cart_model = True
    except OSError:
        pass


def _analyze_seeds(project_dir: Path, config: TestConfig) -> None:
    """Find seeded user credentials."""

    seed_files = list(project_dir.glob("prisma/seed.*")) + \
                 list(project_dir.glob("prisma/seed/**/*")) + \
                 list(project_dir.glob("**/seed.ts")) + \
                 list(project_dir.glob("**/seed.js"))

    for seed_file in seed_files:
        if "node_modules" in str(seed_file):
            continue
        try:
            content = seed_file.read_text()
        except OSError:
            continue

        # Find email/password pairs
        # Pattern: email: "...", password or passwordHash with hash("...")
        emails = re.findall(r'email:\s*["\']([^"\']+)["\']', content)
        passwords = re.findall(r'(?:hash|bcrypt\.hash)\s*\(\s*["\']([^"\']+)["\']', content)
        roles = re.findall(r'role:\s*["\'](\w+)["\']', content)

        for i, email in enumerate(emails):
            password = passwords[i] if i < len(passwords) else "password123"
            role = roles[i] if i < len(roles) else "user"
            config.seeded_users.append(SeededUser(
                email=email, password=password, role=role,
            ))


def _find_source_files(project_dir: Path) -> list[Path]:
    """Find all source files, excluding node_modules and build artifacts."""
    files = []
    for ext in ("*.ts", "*.tsx", "*.js", "*.jsx", "*.py"):
        for f in project_dir.glob(f"**/{ext}"):
            path_str = str(f)
            if any(skip in path_str for skip in ["node_modules", ".next", "__pycache__", "dist"]):
                continue
            files.append(f)
    return files


def print_config(config: TestConfig) -> None:
    """Print a human-readable summary of the test config."""
    print(f"  Auth: {config.auth_type}" +
          (f" (CSRF required)" if config.auth_csrf_required else ""))
    if config.register_endpoint:
        print(f"  Register: {config.register_endpoint}")
    elif config.register_candidates:
        print(f"  Register candidates: {config.register_candidates}")
    else:
        print(f"  Register: NOT FOUND")
    if config.seeded_users:
        for u in config.seeded_users:
            print(f"  Seeded user: {u.email} / {u.password} ({u.role})")
    else:
        print(f"  Seeded users: NONE")
    print(f"  Models: {config.models}")
    print(f"  Cart model: {'yes' if config.has_cart_model else 'NO'}")
    print(f"  Routes: {len(config.routes)}")
    for r in config.routes[:10]:
        auth = " [auth]" if r.requires_auth else ""
        admin = " [admin]" if r.requires_admin else ""
        print(f"    {','.join(r.methods):12s} {r.path}{auth}{admin}")
    if len(config.routes) > 10:
        print(f"    ... and {len(config.routes) - 10} more")
