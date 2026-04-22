"""Otto CLI — setup command (CLAUDE.md generation)."""

import asyncio
import glob
import os
from pathlib import Path

import click

from otto.agent import AssistantMessage, AgentOptions, ResultMessage, TextBlock, _safe_read, query
from otto.config import agent_provider, create_config, load_config, resolve_project_dir
from otto.config import ensure_bookkeeping_setup
from otto.display import CONTEXT_SETTINGS, console
from otto.testing import _subprocess_env
from otto.theme import error_console


def _build_file_tree(project_dir: Path, limit: int) -> str:
    """Build a shallow file tree string for LLM context."""
    tree_lines = ["."]
    excluded_dirs = {".git", "node_modules", "__pycache__"}

    for root, dirs, files in os.walk(project_dir):
        root_path = Path(root)
        rel_root = root_path.relative_to(project_dir)
        depth = len(rel_root.parts)

        dirs[:] = sorted(d for d in dirs if d not in excluded_dirs)
        if depth >= 2:
            dirs[:] = []

        base = Path() if depth == 0 else rel_root
        entries = [base / d for d in dirs]
        entries.extend(base / f for f in sorted(files))

        for entry in entries:
            if len(entry.parts) <= 2:
                tree_lines.append(entry.as_posix())

    return "\n".join(tree_lines)[:limit]


def _gather_project_context(project_dir: Path) -> list[str]:
    """Gather project files for LLM context."""
    parts = []

    for name in ("package.json", "pyproject.toml", "tsconfig.json",
                 "Cargo.toml", "go.mod"):
        f = project_dir / name
        if f.exists():
            content = _safe_read(f, 2000)
            if content is not None:
                parts.append(f"--- {name} ---\n{content}")

    for pattern in ("src/components/*.tsx", "src/components/*.vue",
                    "src/**/*.py", "lib/**/*.ts", "app/**/*.rb"):
        matches = sorted(glob.glob(str(project_dir / pattern), recursive=True))
        if matches:
            sample = Path(matches[0])
            content = _safe_read(sample, 2000)
            if content is not None:
                parts.append(f"--- {sample.relative_to(project_dir)} ---\n{content}")
                break

    for pattern in ("__tests__/*.test.*", "tests/test_*.py", "test/**/*.test.*"):
        matches = sorted(glob.glob(str(project_dir / pattern), recursive=True))
        if matches:
            sample = Path(matches[0])
            content = _safe_read(sample, 1500)
            if content is not None:
                parts.append(f"--- {sample.relative_to(project_dir)} (test) ---\n{content}")
                break

    tree = _build_file_tree(project_dir, limit=2000)
    parts.append(f"--- file tree ---\n{tree}")

    return parts


async def _run_setup_query(prompt: str, project_dir: Path, config: dict | None = None) -> str:
    """Run a single LLM query for setup and return the text result."""
    opts = AgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=[],
        system_prompt="You are a project analyst. Output ONLY the markdown content for CLAUDE.md. No explanation, no preamble, no tool use.",
        env=_subprocess_env(project_dir),
        provider=agent_provider(config or {}),
    )
    if config and config.get("model"):
        opts.model = config["model"]
    result_text = ""
    async for message in query(prompt=prompt, options=opts):
        if isinstance(message, ResultMessage) and message.result:
            result_text = message.result
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result_text += block.text
    return result_text


def register_setup_command(main: click.Group) -> None:
    """Register the setup command on the main CLI group."""

    @main.command(context_settings=CONTEXT_SETTINGS)
    def setup():
        """Generate CLAUDE.md with project conventions for the coding agent."""
        from otto.config import require_git

        require_git()
        project_dir = resolve_project_dir(Path.cwd())

        config_path = project_dir / "otto.yaml"
        if not config_path.exists():
            create_config(project_dir)
            console.print("[success]Created otto.yaml[/success]")
        config = load_config(config_path)
        ensure_bookkeeping_setup(project_dir, config)

        claude_md = project_dir / "CLAUDE.md"
        existing_content = None
        mode = "generate"

        if claude_md.exists():
            existing_content = _safe_read(claude_md, 5000)
            console.print(f"[dim]CLAUDE.md already exists ({claude_md.stat().st_size} bytes)[/dim]")
            console.print("  [bold]1[/bold] Merge (keep your rules, add new conventions)")
            console.print("  [bold]2[/bold] Regenerate (replace entirely)")
            console.print("  [bold]3[/bold] Keep existing")
            choice = click.prompt("Choice", type=click.IntRange(1, 3), default=1)
            if choice == 3:
                return
            elif choice == 2:
                existing_content = None
                mode = "generate"
            else:
                mode = "merge"

        # Back up existing CLAUDE.md
        _backup_path = None
        if mode == "generate" and claude_md.exists():
            import tempfile as _tf
            fd, _backup_path = _tf.mkstemp(suffix=".md", prefix="claude_md_backup_")
            os.close(fd)
            Path(_backup_path).write_text(claude_md.read_text())
            claude_md.unlink()

        context_parts = _gather_project_context(project_dir)
        console.print("[dim]Scanning project...[/dim]")

        if existing_content:
            merge_section = f"""
--- existing CLAUDE.md ---
{existing_content}
---

Merge the existing CLAUDE.md with fresh conventions from the codebase.
Keep all user-written rules and principles. Add new project-specific
conventions discovered from the code. Remove outdated entries that
contradict what the codebase actually does. Deduplicate."""
        else:
            merge_section = ""

        prompt = f"""Write a CLAUDE.md for a coding agent working on this project.

CLAUDE.md tells the agent what it needs to know to work effectively here.
Include: build/test commands, project structure, key conventions, and anything
that would cause mistakes without guidance.

Project files:
{chr(10).join(context_parts)}
{merge_section}

Guidelines:
- Point to files/directories rather than inlining specifics that go stale.
- Avoid counts, version numbers, or facts that change frequently.
- Keep it concise — the agent reads code well, just orient it.
- Include these principles if relevant to the project:
  - Check for existing patterns before writing new code
  - After changing a shared type/interface, check all consumers
  - Fix root causes — don't add workarounds or special cases
Output ONLY the markdown content."""

        console.print("[dim]Generating CLAUDE.md...[/dim]")
        try:
            config = load_config(config_path)
        except ValueError as exc:
            error_console.print(f"[error]{exc}[/error]")
            raise SystemExit(2) from exc
        result_text = asyncio.run(_run_setup_query(prompt, project_dir, config))

        if not result_text.strip() and claude_md.exists():
            result_text = _safe_read(claude_md, 10000) or ""
        if not result_text.strip():
            error_console.print("[red]Failed to generate CLAUDE.md content[/red]")
            return

        content = result_text.strip()
        if content.startswith("```"):
            content = "\n".join(content.split("\n")[1:])
        if content.endswith("```"):
            content = "\n".join(content.split("\n")[:-1])

        console.print()
        console.print("[bold]Generated CLAUDE.md:[/bold]")
        console.print("─" * 60)
        console.print(content)
        console.print("─" * 60)
        console.print()

        if click.confirm("Write to CLAUDE.md?", default=True):
            claude_md.write_text(content.strip() + "\n")
            console.print(f"[success]Wrote CLAUDE.md ({len(content)} chars)[/success]")
            console.print("[dim]  Commit it so the coding agent can read it.[/dim]")
            if _backup_path and Path(_backup_path).exists():
                Path(_backup_path).unlink()
        else:
            if _backup_path and Path(_backup_path).exists():
                Path(_backup_path).rename(claude_md)
            console.print("[dim]Cancelled — original CLAUDE.md restored[/dim]")
