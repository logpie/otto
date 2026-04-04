from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from otto.certifier.adapter import TestConfig, analyze_project
from otto.certifier.baseline import load_or_compile_matrix, run_baseline
from otto.certifier.classifier import classify
from otto.certifier.intent_compiler import compile_intent


TODO_SCRIPT = """\
import argparse
import json
from pathlib import Path

DATA_FILE = Path("todo-data.json")


def load_items():
    if not DATA_FILE.exists():
        return []
    return json.loads(DATA_FILE.read_text())


def save_items(items):
    DATA_FILE.write_text(json.dumps(items))


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("task")

    subparsers.add_parser("list")

    done_parser = subparsers.add_parser("done")
    done_parser.add_argument("task_id", type=int)

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("task_id", type=int)

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")

    args = parser.parse_args()
    items = load_items()

    if args.command == "add":
        item = {"id": len(items) + 1, "task": args.task, "status": "pending"}
        items.append(item)
        save_items(items)
        print(f"added {item['id']}: {item['task']}")
        return

    if args.command == "list":
        for item in items:
            print(f"{item['id']}: [{item['status']}] {item['task']}")
        return

    if args.command == "done":
        for item in items:
            if item["id"] == args.task_id:
                item["status"] = "done"
        save_items(items)
        print(f"done {args.task_id}")
        return

    if args.command == "delete":
        items = [item for item in items if item["id"] != args.task_id]
        save_items(items)
        print(f"deleted {args.task_id}")
        return

    if args.command == "search":
        for item in items:
            if args.query.lower() in item["task"].lower():
                print(f"{item['id']}: [{item['status']}] {item['task']}")


if __name__ == "__main__":
    main()
"""


def _todo_intent() -> str:
    intents_path = Path("bench/i2p-bench/intents.yaml")
    data = yaml.safe_load(intents_path.read_text())
    return next(item["intent"] for item in data["intents"] if item["id"] == "todo-cli")


def test_certifier_cli_stack(tmp_path: Path, monkeypatch):
    (tmp_path / "todo.py").write_text(TODO_SCRIPT)

    async def fake_run_agent_query(prompt, options):
        assert "command-line todo app in Python" in prompt
        return (
            json.dumps(
                {
                    "product_type_hint": "cli",
                    "non_goals": [],
                    "ambiguities": [],
                    "claims": [
                        {
                            "id": "todo-add-and-list",
                            "description": "Users can add tasks and list them",
                            "priority": "critical",
                            "category": "feature",
                            "test_approach": "cli",
                            "test_steps": [
                                {
                                    "action": "cli",
                                    "command": "python todo.py add 'buy milk'",
                                    "expect_exit_code": 0,
                                    "expect_stdout_contains": ["added"],
                                },
                                {
                                    "action": "cli",
                                    "command": "python todo.py list",
                                    "expect_exit_code": 0,
                                    "expect_stdout_contains": ["buy milk", "pending"],
                                },
                            ],
                            "hard_fail": True,
                        },
                        {
                            "id": "todo-done",
                            "description": "Users can mark tasks as done",
                            "priority": "critical",
                            "category": "feature",
                            "test_approach": "cli",
                            "test_steps": [
                                {
                                    "action": "cli",
                                    "command": "python todo.py done 1",
                                    "expect_exit_code": 0,
                                    "expect_stdout_contains": ["done 1"],
                                },
                                {
                                    "action": "cli",
                                    "command": "python todo.py list",
                                    "expect_exit_code": 0,
                                    "expect_stdout_contains": ["done", "buy milk"],
                                },
                            ],
                            "hard_fail": True,
                        },
                    ],
                }
            ),
            0.0,
            None,
        )

    monkeypatch.setattr("otto.certifier.intent_compiler.run_agent_query", fake_run_agent_query)

    matrix = asyncio.run(compile_intent(_todo_intent()))
    assert matrix.product_type_hint == "cli"
    assert all(claim.test_approach == "cli" for claim in matrix.claims)
    assert any("python todo.py add" in step["command"] for claim in matrix.claims for step in claim.test_steps)

    profile = classify(tmp_path)
    assert profile.language == "python"
    assert profile.product_type == "cli"
    assert profile.interaction == "cli"
    assert profile.start_command == "python todo.py"

    config = analyze_project(tmp_path)
    assert "argparse" in config.cli_frameworks
    assert "todo.py" in config.cli_entrypoints

    result = run_baseline(tmp_path, matrix, profile, test_config=config)

    assert result.claims_failed == 0
    assert result.claims_passed == len(matrix.claims)
    assert all(claim.proof for claim in result.results)


def test_analyze_project_routes_before_auth_for_express_register(tmp_path: Path):
    (tmp_path / "app.js").write_text(
        """\
const express = require("express");
const app = express();
app.post("/api/register", (req, res) => res.status(201).json({ ok: true }));
"""
    )

    config = analyze_project(tmp_path)

    assert config.register_endpoint == "/api/register"
    assert any(route.path == "/api/register" for route in config.routes)


def test_analyze_project_collects_resource_models_and_empty_seed_password(tmp_path: Path):
    prisma_dir = tmp_path / "prisma"
    prisma_dir.mkdir()
    (prisma_dir / "schema.prisma").write_text(
        """\
model User {
  id    String @id
  email String
}

model Task {
  id    String @id
  title String
}

model Workspace {
  id   String @id
  name String
}
"""
    )
    (prisma_dir / "seed.ts").write_text(
        """\
await prisma.user.create({
  data: {
    email: "seeded@example.test",
    role: "user",
  },
});
"""
    )

    config = analyze_project(tmp_path)

    assert config.resource_models == ["User", "Task", "Workspace"]
    assert config.seeded_users[0].email == "seeded@example.test"
    assert config.seeded_users[0].password == ""


def test_compile_intent_includes_schema_hint(monkeypatch):
    seen_prompt: dict[str, str] = {}

    async def fake_run_agent_query(prompt, options):
        seen_prompt["prompt"] = prompt
        return (
            json.dumps(
                {
                    "product_type_hint": "web",
                    "non_goals": [],
                    "ambiguities": [],
                    "claims": [],
                }
            ),
            0.0,
            None,
        )

    monkeypatch.setattr("otto.certifier.intent_compiler.run_agent_query", fake_run_agent_query)

    asyncio.run(
        compile_intent(
            "build a task manager",
            schema_hint=(
                "Model Task: title: String, dueDate: DateTime, status: TaskStatus (TODO, IN_PROGRESS, DONE)\n"
                "Register endpoint /api/register likely body fields: email, password, name"
            ),
        )
    )

    assert "Use these exact field names in test step bodies, not guesses." in seen_prompt["prompt"]
    assert "dueDate" in seen_prompt["prompt"]
    assert "TODO, IN_PROGRESS, DONE" in seen_prompt["prompt"]


def test_load_or_compile_matrix_cache_varies_with_schema_hint(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    async def fake_compile_intent(intent, config=None, schema_hint=""):
        calls.append(schema_hint)
        return type(
            "Matrix",
            (),
            {
                "intent": intent,
                "claims": [],
                "non_goals": [],
                "ambiguities": [],
                "product_type_hint": "web",
                "compiled_at": "",
                "cost_usd": 0.0,
                "to_dict": lambda self: {
                    "intent": intent,
                    "claims": [],
                    "non_goals": [],
                    "ambiguities": [],
                    "product_type_hint": "web",
                    "compiled_at": "",
                    "cost_usd": 0.0,
                },
            },
        )()

    monkeypatch.setattr("otto.certifier.baseline.compile_intent", fake_compile_intent)

    task_config = TestConfig(
        models=["Task"],
        model_fields={"Task": {"title": "String", "dueDate": "DateTime"}},
    )
    recipe_config = TestConfig(
        models=["Recipe"],
        model_fields={"Recipe": {"title": "String", "cookTime": "Int"}},
    )

    first = load_or_compile_matrix(tmp_path, "build an app", test_config=task_config)[2]
    second = load_or_compile_matrix(tmp_path, "build an app", test_config=recipe_config)[2]

    assert first != second
    assert len(calls) == 2
    assert "dueDate" in calls[0]
    assert "cookTime" in calls[1]
