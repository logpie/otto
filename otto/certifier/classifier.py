"""Product classifier — detects product type and how to start it.

Mostly deterministic: checks for package.json, setup.py, Cargo.toml, etc.
Auto-detects the startup command and interaction surface.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProductProfile:
    """Detected product type and how to interact with it."""
    product_type: str           # "web" | "cli" | "api" | "desktop" | "library" | "unknown"
    framework: str              # "nextjs" | "react" | "flask" | "express" | "electron" | etc.
    language: str               # "typescript" | "python" | "rust" | etc.
    start_command: str          # "npm run dev" | "python app.py" | etc.
    port: int | None            # expected port for web/api apps
    test_command: str           # "npm test" | "pytest" | etc.
    interaction: str            # "browser" | "http" | "cli" | "unknown"
    has_database: bool = False
    has_auth: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def classify(project_dir: Path) -> ProductProfile:
    """Classify a project by inspecting its files. No LLM needed."""

    profile = ProductProfile(
        product_type="unknown", framework="unknown", language="unknown",
        start_command="", port=None, test_command="", interaction="unknown",
    )

    # --- Node.js / JavaScript ---
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            scripts = pkg.get("scripts", {})
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

            profile.language = "typescript" if (project_dir / "tsconfig.json").exists() else "javascript"

            # Framework detection
            if "next" in deps:
                profile.framework = "nextjs"
                profile.product_type = "web"
                profile.start_command = "npm run dev"
                profile.port = 3000
                profile.interaction = "browser"
            elif "express" in deps:
                profile.framework = "express"
                profile.product_type = "api"
                profile.start_command = scripts.get("start", "node index.js")
                profile.port = 3000
                profile.interaction = "http"
            elif "react" in deps and "next" not in deps:
                profile.framework = "react"
                profile.product_type = "web"
                profile.start_command = "npm start"
                profile.port = 3000
                profile.interaction = "browser"
            elif "electron" in deps:
                profile.framework = "electron"
                profile.product_type = "desktop"
                profile.start_command = scripts.get("start", "electron .")
                profile.interaction = "browser"
            else:
                profile.product_type = "api" if "start" in scripts else "library"
                profile.start_command = scripts.get("start", "")

            # Test command
            if "test" in scripts:
                profile.test_command = "npm test"
            elif "vitest" in deps:
                profile.test_command = "npx vitest run"
            elif "jest" in deps:
                profile.test_command = "npx jest"

            # Database
            if "prisma" in deps or "@prisma/client" in deps:
                profile.has_database = True
            if "sqlite3" in deps or "better-sqlite3" in deps:
                profile.has_database = True
            if "pg" in deps or "mysql2" in deps or "mongoose" in deps:
                profile.has_database = True

            # Auth
            if "next-auth" in deps or "passport" in deps or "jsonwebtoken" in deps:
                profile.has_auth = True
            if "@auth/core" in deps:
                profile.has_auth = True

        except (json.JSONDecodeError, OSError):
            pass

    # --- Python ---
    for py_config in ["setup.py", "pyproject.toml", "setup.cfg"]:
        if (project_dir / py_config).exists() and profile.language == "unknown":
            profile.language = "python"
            break

    if profile.language == "python" or any(
        (project_dir / f).exists() for f in ["app.py", "main.py", "manage.py"]
    ):
        profile.language = "python"

        if (project_dir / "manage.py").exists():
            profile.framework = "django"
            profile.product_type = "web"
            profile.start_command = "python manage.py runserver"
            profile.port = 8000
            profile.interaction = "browser"
        elif _file_contains(project_dir, "flask", ["app.py", "main.py", "server.py"]):
            profile.framework = "flask"
            profile.product_type = "api"
            profile.start_command = "python app.py"
            profile.port = 5000
            profile.interaction = "http"
        elif _file_contains(project_dir, "fastapi", ["app.py", "main.py", "server.py"]):
            profile.framework = "fastapi"
            profile.product_type = "api"
            profile.start_command = "uvicorn main:app"
            profile.port = 8000
            profile.interaction = "http"
        elif _file_contains(project_dir, "click", ["*.py"]) or \
             _file_contains(project_dir, "argparse", ["*.py"]):
            profile.product_type = "cli"
            profile.interaction = "cli"
            # Find the main script
            for f in ["main.py", "cli.py", "app.py"]:
                if (project_dir / f).exists():
                    profile.start_command = f"python {f}"
                    break

        # Python test command
        if (project_dir / "pytest.ini").exists() or \
           (project_dir / "conftest.py").exists() or \
           any((project_dir / d).is_dir() for d in ["tests", "test"]):
            profile.test_command = "pytest"

    # --- Rust ---
    if (project_dir / "Cargo.toml").exists():
        profile.language = "rust"
        profile.framework = "cargo"
        profile.start_command = "cargo run"
        profile.test_command = "cargo test"
        profile.product_type = "cli"  # default, could be web with actix/axum
        profile.interaction = "cli"

    # --- Database detection (generic) ---
    for db_file in project_dir.glob("**/*.db"):
        if "node_modules" not in str(db_file):
            profile.has_database = True
            break
    if (project_dir / "prisma").is_dir():
        profile.has_database = True

    return profile


def _file_contains(project_dir: Path, pattern: str, files: list[str]) -> bool:
    """Check if any of the listed files contain the pattern."""
    for fname in files:
        if "*" in fname:
            for f in project_dir.glob(fname):
                if f.is_file() and "node_modules" not in str(f):
                    try:
                        if pattern in f.read_text():
                            return True
                    except OSError:
                        pass
        else:
            f = project_dir / fname
            if f.exists():
                try:
                    if pattern in f.read_text():
                        return True
                except OSError:
                    pass
    return False
