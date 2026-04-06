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
    resource_models: list[str] = field(default_factory=list)
    schema_source_file: str = ""
    model_fields: dict[str, dict[str, str]] = field(default_factory=dict)
    creatable_fields: dict[str, list[str]] = field(default_factory=dict)
    enum_values: dict[str, list[str]] = field(default_factory=dict)

    # Framework specifics
    api_base: str = "/api"
    response_wrapper: str = ""  # e.g. "data" if responses are {data: ...}
    cli_frameworks: list[str] = field(default_factory=list)
    cli_entrypoints: list[str] = field(default_factory=list)
    cli_commands: list[dict[str, Any]] = field(default_factory=list)  # [{name, args, flags}]

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

    _analyze_routes(project_dir, config)
    _analyze_auth(project_dir, config)
    _analyze_schema(project_dir, config)
    _analyze_cli(project_dir, config)
    _analyze_seeds(project_dir, config)

    logger.info(
        "Project analyzed: auth=%s, %d routes, %d models, %d seeded users",
        config.auth_type, len(config.routes), len(config.models),
        len(config.seeded_users),
    )

    return config


def _analyze_auth(project_dir: Path, config: TestConfig) -> None:
    """Detect auth mechanism and endpoints."""

    # Check for NextAuth route handlers and configs.
    nextauth_files = [
        route_file
        for route_file in _find_source_files(project_dir)
        if route_file.name in ("route.ts", "route.js") and "[...nextauth]" in str(route_file)
    ]
    if nextauth_files or _project_uses_nextauth(project_dir):
        config.auth_type = "nextauth"
        config.auth_csrf_required = True
        config.login_candidates = ["/api/auth/callback/credentials"]
        config.login_endpoint = "/api/auth/callback/credentials"

    # Check for register endpoint — App Router, Pages Router, Express, Flask
    # App Router: src/app/api/auth/register/route.ts
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
            rel = matches[0].relative_to(project_dir)
            parts = list(rel.parts)
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

    # Pages Router: pages/api/auth/register.ts
    if not config.register_endpoint:
        for pattern in [
            "**/pages/api/auth/register.ts",
            "**/pages/api/auth/register.js",
            "**/pages/api/register.ts",
            "**/pages/api/signup.ts",
        ]:
            matches = list(project_dir.glob(pattern))
            if matches:
                rel = matches[0].relative_to(project_dir)
                parts = list(rel.parts)
                if "pages" in parts:
                    parts = parts[parts.index("pages") + 1:]
                if parts and parts[-1].endswith((".ts", ".js")):
                    parts[-1] = parts[-1].rsplit(".", 1)[0]
                endpoint = "/" + "/".join(parts)
                config.register_endpoint = endpoint
                config.register_candidates = [endpoint]
                break

    # Express/Flask: search source files for register route definitions
    if not config.register_endpoint:
        for route_info in config.routes:
            path_lower = route_info.path.lower()
            if "POST" in route_info.methods and any(
                kw in path_lower for kw in ["register", "signup", "sign-up"]
            ):
                config.register_endpoint = route_info.path
                config.register_candidates = [route_info.path]
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
                # Flask: @app.route("/path", methods=["GET", "POST"])
                for match in re.finditer(
                    r'@\w+\.route\s*\(\s*[\'"]([^\'"]+)[\'"]'
                    r'(?:\s*,\s*methods\s*=\s*\[([^\]]*)\])?',
                    content,
                ):
                    path = match.group(1)
                    methods_str = match.group(2)
                    if methods_str:
                        methods = [m.strip().strip("'\"").upper() for m in methods_str.split(",")]
                    else:
                        methods = ["GET"]
                    config.routes.append(RouteInfo(path=path, methods=methods))
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
        config.schema_source_file = str(schema_file.relative_to(project_dir))

        config.enum_values = _parse_prisma_enums(content)
        model_names = re.findall(r'model\s+(\w+)\s*\{', content)
        for match in re.finditer(r'model\s+(\w+)\s*\{', content):
            model = match.group(1)
            config.models.append(model)
            config.resource_models.append(model)
        config.models = _dedupe_preserving_order(config.models)
        config.resource_models = _dedupe_preserving_order(config.resource_models)
        config.model_fields, config.creatable_fields = _parse_prisma_models(
            content,
            model_names=model_names,
            enum_names=set(config.enum_values),
            config_enum_values=config.enum_values,
        )

        # Scan route handlers for inline validation arrays
        # e.g., ["TODO", "IN_PROGRESS", "DONE"].includes(status)
        _discover_inline_enums(project_dir, config)
    except OSError:
        pass


def _analyze_cli(project_dir: Path, config: TestConfig) -> None:
    """Detect CLI entrypoints, parser frameworks, and subcommands."""

    cli_frameworks: set[str] = set()
    entrypoints: list[str] = []
    commands: list[dict[str, Any]] = []

    for source_file in _find_source_files(project_dir):
        try:
            content = source_file.read_text()
        except OSError:
            continue

        rel_path = str(source_file.relative_to(project_dir))

        if source_file.suffix == ".py":
            lower = content.lower()
            if "argparse" in lower or "argumentparser(" in lower or ".add_argument(" in lower:
                cli_frameworks.add("argparse")
                entrypoints.append(rel_path)
                commands.extend(_extract_argparse_commands(content))
            if "import click" in lower or "@click." in lower or "click.command(" in lower:
                cli_frameworks.add("click")
                entrypoints.append(rel_path)
                commands.extend(_extract_click_commands(content))
            if "import typer" in lower or "typer.typer(" in lower:
                cli_frameworks.add("typer")
                entrypoints.append(rel_path)
                commands.extend(_extract_click_commands(content))  # typer uses similar patterns

        elif source_file.suffix == ".rs":
            if "clap" in content or "structopt" in content:
                cli_frameworks.add("clap")
                # Rust entrypoints handled by classifier (cargo run)

        elif source_file.suffix == ".go":
            if "cobra" in content or "flag.Parse" in content or '"flag"' in content:
                cli_frameworks.add("go-flag")
                # Go entrypoints handled by classifier (go run)

    config.cli_frameworks = sorted(cli_frameworks)
    config.cli_entrypoints = _dedupe_preserving_order(entrypoints)
    config.cli_commands = _dedupe_commands(commands)


def _extract_argparse_commands(content: str) -> list[dict[str, Any]]:
    """Extract subcommand names from argparse add_subparsers/add_parser calls."""
    commands: list[dict[str, Any]] = []
    for match in re.finditer(r'add_parser\s*\(\s*["\'](\w+)["\']', content):
        commands.append({"name": match.group(1), "args": [], "flags": []})
    return commands


def _extract_click_commands(content: str) -> list[dict[str, Any]]:
    """Extract command names from click/typer decorators.

    Handles stacked decorators between @cli.command() and def:
        @cli.command("list")
        @click.option("--tag")
        def list_notes(tag): ...
    """
    commands: list[dict[str, Any]] = []
    # Match .command() with optional explicit name, then skip stacked decorators to find def
    for match in re.finditer(
        r'\.command\s*\(\s*(?:["\'](\w[\w-]*)["\'])?\s*\)'  # @cli.command() or @cli.command("name")
        r'(?:\s*\n\s*@[\w.]+\([^)]*\))*'                    # skip stacked @click.option/argument
        r'\s*\n\s*def\s+(\w+)',                               # def func_name
        content,
    ):
        explicit_name = match.group(1)  # from @cli.command("name")
        func_name = match.group(2)      # from def func_name
        name = explicit_name or func_name.replace("_", "-")
        commands.append({"name": name, "args": [], "flags": []})
    return commands


def _dedupe_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate commands by name."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for cmd in commands:
        name = cmd.get("name", "")
        if name and name not in seen:
            seen.add(name)
            result.append(cmd)
    return result


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
        passwords = re.findall(r'(?:hash|hashSync|bcrypt\.hash|bcrypt\.hashSync)\s*\(\s*["\']([^"\']+)["\']', content)
        roles = re.findall(r'role:\s*["\'](\w+)["\']', content)

        for i, email in enumerate(emails):
            password = passwords[i] if i < len(passwords) else ""
            role = roles[i] if i < len(roles) else "user"
            config.seeded_users.append(SeededUser(
                email=email, password=password, role=role,
            ))
    config.seeded_users = _dedupe_seeded_users(config.seeded_users)


def _find_source_files(project_dir: Path) -> list[Path]:
    """Find all source files, excluding node_modules and build artifacts."""
    files = []
    for ext in ("*.ts", "*.tsx", "*.js", "*.jsx", "*.py", "*.rs", "*.go"):
        for f in project_dir.glob(f"**/{ext}"):
            path_str = str(f)
            if any(skip in path_str for skip in [
                "node_modules", ".next", "__pycache__", "dist",
                "target/", ".venv", "vendor/",
            ]):
                continue
            files.append(f)
    return files


def _project_uses_nextauth(project_dir: Path) -> bool:
    package_json = project_dir / "package.json"
    if package_json.exists():
        try:
            pkg = json.loads(package_json.read_text())
        except (json.JSONDecodeError, OSError):
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if "next-auth" in deps or "@auth/core" in deps:
            return True

    for source_file in _find_source_files(project_dir):
        try:
            content = source_file.read_text()
        except OSError:
            continue
        if "next-auth" in content and (
            "NextAuth" in content
            or "CredentialsProvider" in content
            or "Credentials(" in content
        ):
            return True
    return False


def _dedupe_seeded_users(users: list[SeededUser]) -> list[SeededUser]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[SeededUser] = []
    for user in users:
        key = (user.email.lower(), user.password, user.role.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(user)
    return deduped


def _parse_prisma_enums(content: str) -> dict[str, list[str]]:
    enums: dict[str, list[str]] = {}
    for match in re.finditer(r'enum\s+(\w+)\s*\{([^}]*)\}', content, re.DOTALL):
        enum_name = match.group(1)
        values = []
        for raw_line in match.group(2).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            values.append(line.split()[0])
        enums[enum_name] = values
    return enums


def _parse_prisma_models(
    content: str,
    *,
    model_names: list[str],
    enum_names: set[str],
    config_enum_values: dict[str, list[str]] | None = None,
) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    scalar_types = {"String", "Int", "Float", "Boolean", "DateTime", "Json", "Decimal", "BigInt", "Bytes"}
    known_model_names = set(model_names)
    model_fields: dict[str, dict[str, str]] = {}
    creatable_fields: dict[str, list[str]] = {}
    if config_enum_values is None:
        config_enum_values = {}

    for match in re.finditer(r'model\s+(\w+)\s*\{([^}]*)\}', content, re.DOTALL):
        model_name = match.group(1)
        fields: dict[str, str] = {}
        writable_fields: list[str] = []

        for raw_line in match.group(2).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue

            field_name = parts[0]
            field_type = parts[1]
            base_type = field_type.rstrip("?").rstrip("[]")
            is_relation = base_type in known_model_names
            is_supported_scalar = base_type in scalar_types or base_type in enum_names
            if not is_supported_scalar or is_relation:
                continue

            fields[field_name] = base_type

            # Extract @default("VALUE") as enum hint for String fields
            default_match = re.search(r'@default\("([^"]+)"\)', line)
            if default_match and base_type == "String":
                default_val = default_match.group(1)
                # Record as a pseudo-enum: "field_name_values"
                pseudo_enum_key = f"{model_name}.{field_name}"
                if pseudo_enum_key not in config_enum_values:
                    config_enum_values[pseudo_enum_key] = [default_val]

            if field_name in {"id", "createdAt", "updatedAt"}:
                continue
            if field_type.endswith("[]"):
                continue
            if "@updatedAt" in line:
                continue
            writable_fields.append(field_name)

        model_fields[model_name] = fields
        creatable_fields[model_name] = writable_fields

    return model_fields, creatable_fields


def _discover_inline_enums(project_dir: Path, config: TestConfig) -> None:
    """Scan route handlers for inline validation arrays like ["TODO", "IN_PROGRESS", "DONE"].includes()."""
    route_dirs = [
        project_dir / "src" / "app" / "api",
        project_dir / "app" / "api",
        project_dir / "pages" / "api",
    ]
    for route_dir in route_dirs:
        if not route_dir.is_dir():
            continue
        for ts_file in route_dir.rglob("*.ts"):
            if "node_modules" in str(ts_file):
                continue
            try:
                content = ts_file.read_text()
            except OSError:
                continue
            # Match patterns like: ["TODO", "IN_PROGRESS", "DONE"].includes(status)
            # or: !["TODO", "IN_PROGRESS", "DONE"].includes(status)
            for m in re.finditer(
                r'\[(["\'][A-Z_]+["\'](?:\s*,\s*["\'][A-Z_]+["\'])+)\]\.includes\((\w+)\)',
                content,
            ):
                values_str = m.group(1)
                field_name = m.group(2)
                values = [v.strip().strip("'\"") for v in values_str.split(",")]
                if values:
                    # Find which model this field belongs to
                    for model_name, fields in config.model_fields.items():
                        if field_name in fields:
                            key = f"{model_name}.{field_name}"
                            config.enum_values[key] = values
                            break
                    else:
                        # No model match — store under generic key
                        config.enum_values[field_name] = values


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


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
    print(f"  Resource models: {config.resource_models}")
    print(f"  Routes: {len(config.routes)}")
    for r in config.routes[:10]:
        auth = " [auth]" if r.requires_auth else ""
        admin = " [admin]" if r.requires_admin else ""
        print(f"    {','.join(r.methods):12s} {r.path}{auth}{admin}")
    if len(config.routes) > 10:
        print(f"    ... and {len(config.routes) - 10} more")
