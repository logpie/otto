from __future__ import annotations

from pathlib import Path

from otto.queue.schema import load_queue

from tests._web_mc_helpers import _append_queue_task, _client, _init_repo, _write_empty_queue_state


def test_web_queue_build_enqueues_without_click_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    client = _client(repo)
    response = client.post(
        "/api/queue/build",
        json={
            "intent": "add saved searches",
            "as": "saved-searches",
            "extra_args": ["--provider", "codex", "--model", "gpt-5.4", "--effort", "medium"],
        },
    )
    assert response.status_code == 200
    assert response.json()["task"]["id"] == "saved-searches"
    tasks = load_queue(repo)
    assert [task.id for task in tasks] == ["saved-searches"]
    assert tasks[0].command_argv == [
        "build",
        "add saved searches",
        "--provider",
        "codex",
        "--model",
        "gpt-5.4",
        "--effort",
        "medium",
    ]

    state = client.get("/api/state?type=queue").json()
    row = state["live"]["items"][0]
    assert row["queue_task_id"] == "saved-searches"
    assert row["provider"] == "codex"
    assert row["model"] == "gpt-5.4"
    assert row["reasoning_effort"] == "medium"
    assert row["build_config"]["provider"] == "codex"
    assert row["build_config"]["certifier_mode"] == "fast"
    assert row["build_config"]["queue"]["task_timeout_s"] == 4200.0

    hidden = client.get("/api/state?type=queue&query=unmatched").json()
    assert hidden["live"]["items"] == []

    matching = client.get("/api/state?type=queue&query=saved").json()
    assert matching["live"]["items"][0]["queue_task_id"] == "saved-searches"

def test_web_queue_build_spec_defaults_to_web_review_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    response = _client(repo).post(
        "/api/queue/build",
        json={
            "intent": "add reports",
            "as": "reports",
            "extra_args": ["--spec", "--split"],
        },
    )

    assert response.status_code == 200
    task = load_queue(repo)[0]
    assert task.command_argv == [
        "build",
        "add reports",
        "--spec",
        "--split",
        "--spec-review-mode",
        "web",
    ]
    config = _client(repo).get("/api/state").json()["live"]["items"][0]["build_config"]
    assert config["planning"] == "spec_review"

def test_web_queue_accepts_split_mode_and_phase_provider_args(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    response = _client(repo).post(
        "/api/queue/build",
        json={
            "intent": "add saved searches",
            "as": "saved-searches",
            "extra_args": [
                "--split",
                "--provider",
                "claude",
                "--build-provider",
                "codex",
                "--certifier-provider",
                "claude",
                "--fix-provider",
                "codex",
                "--fix-effort",
                "high",
            ],
        },
    )

    assert response.status_code == 200
    task = load_queue(repo)[0]
    assert "--split" in task.command_argv
    state = _client(repo).get("/api/state").json()
    config = state["live"]["items"][0]["build_config"]
    assert config["split_mode"] is True
    assert config["agents"]["build"]["provider"] == "codex"
    assert config["agents"]["certifier"]["provider"] == "claude"
    assert config["agents"]["fix"]["provider"] == "codex"
    assert config["agents"]["fix"]["reasoning_effort"] == "high"

def test_web_queue_accepts_improve_improver_provider_args(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    response = _client(repo).post(
        "/api/queue/improve",
        json={
            "subcommand": "feature",
            "focus": "improve search UX",
            "as": "improve-search",
            "extra_args": [
                "--split",
                "--provider",
                "claude",
                "--certifier-provider",
                "claude",
                "--improver-provider",
                "codex",
                "--improver-effort",
                "high",
            ],
        },
    )

    assert response.status_code == 200
    task = load_queue(repo)[0]
    assert "--improver-provider" in task.command_argv
    state = _client(repo).get("/api/state").json()
    config = state["live"]["items"][0]["build_config"]
    assert config["command_family"] == "improve"
    assert config["provider"] == "codex"
    assert config["agents"]["fix"]["provider"] == "codex"
    assert config["agents"]["fix"]["reasoning_effort"] == "high"

def test_web_queue_rejects_unknown_after_dependency(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    response = _client(repo).post(
        "/api/queue/improve",
        json={
            "subcommand": "feature",
            "focus": "add saved views",
            "as": "saved-views",
            "after": ["missing-task"],
        },
    )

    assert response.status_code == 400
    assert "after references unknown task(s): ['missing-task']" in response.json()["message"]
    assert load_queue(repo) == []

def test_web_queue_rejects_invalid_inner_command_args(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    response = _client(repo).post(
        "/api/queue/improve",
        json={
            "subcommand": "feature",
            "focus": "add saved views",
            "extra_args": ["--fast"],
        },
    )

    assert response.status_code == 400
    assert "Unsupported options for `otto improve feature`" in response.json()["message"]
    assert "--fast" in response.json()["message"]
    assert load_queue(repo) == []

def test_web_state_exposes_effective_project_defaults(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "otto.yaml").write_text(
        "\n".join([
            "provider: codex",
            "model: gpt-5.4",
            "effort: high",
            "certifier_mode: standard",
            "skip_product_qa: true",
            "run_budget_seconds: 2400",
            "spec_timeout: 300",
            "max_certify_rounds: 5",
            "max_turns_per_call: 120",
            "strict_mode: true",
            "split_mode: true",
            "allow_dirty_repo: true",
            "default_branch: main",
            "test_command: uv run pytest",
            "queue:",
            "  concurrent: 4",
            "  worktree_dir: .otto-trees",
            "  on_watcher_restart: fail",
            "  task_timeout_s: 1200",
            "  merge_certifier_mode: thorough",
            "",
        ]),
        encoding="utf-8",
    )

    state = _client(repo).get("/api/state").json()
    defaults = state["project"]["defaults"]

    assert defaults["provider"] == "codex"
    assert defaults["model"] == "gpt-5.4"
    assert defaults["reasoning_effort"] == "high"
    assert defaults["certifier_mode"] == "standard"
    assert defaults["skip_product_qa"] is True
    assert defaults["run_budget_seconds"] == 2400
    assert defaults["spec_timeout"] == 300
    assert defaults["max_certify_rounds"] == 5
    assert defaults["max_turns_per_call"] == 120
    assert defaults["strict_mode"] is True
    assert defaults["split_mode"] is True
    assert defaults["allow_dirty_repo"] is True
    assert defaults["default_branch"] == "main"
    assert defaults["test_command"] == "uv run pytest"
    assert defaults["queue_concurrent"] == 4
    assert defaults["queue_task_timeout_s"] == 1200.0
    assert defaults["queue_worktree_dir"] == ".otto-trees"
    assert defaults["queue_on_watcher_restart"] == "fail"
    assert defaults["queue_merge_certifier_mode"] == "thorough"
    assert defaults["config_file_exists"] is True
    assert defaults["config_error"] is None

def test_web_state_exposes_queue_task_build_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "otto.yaml").write_text(
        "\n".join([
            "provider: claude",
            "model: sonnet",
            "effort: medium",
            "certifier_mode: standard",
            "run_budget_seconds: 3600",
            "max_certify_rounds: 4",
            "queue:",
            "  concurrent: 2",
            "  task_timeout_s: 1500",
            "",
        ]),
        encoding="utf-8",
    )
    _append_queue_task(
        repo,
        "configured-task",
        command_argv=[
            "build",
            "configured task",
            "--provider",
            "codex",
            "--model",
            "gpt-5.4",
            "--effort",
            "high",
            "--thorough",
            "--rounds",
            "6",
            "--budget",
            "900",
            "--max-turns",
            "80",
            "--strict",
            "--split",
            "--allow-dirty",
        ],
        resolved_intent="configured task",
    )
    _write_empty_queue_state(repo)

    client = _client(repo)
    state = client.get("/api/state").json()
    live_config = state["live"]["items"][0]["build_config"]
    landing_config = state["landing"]["items"][0]["build_config"]

    for config in (live_config, landing_config):
        assert config["provider"] == "codex"
        assert config["model"] == "gpt-5.4"
        assert config["reasoning_effort"] == "high"
        assert config["certifier_mode"] == "thorough"
        assert config["skip_product_qa"] is False
        assert config["run_budget_seconds"] == 900
        assert config["max_certify_rounds"] == 6
        assert config["max_turns_per_call"] == 80
        assert config["strict_mode"] is True
        assert config["split_mode"] is True
        assert config["allow_dirty_repo"] is True
        assert config["queue"]["concurrent"] == 2
        assert config["queue"]["task_timeout_s"] == 1500.0
        assert config["agents"]["build"]["provider"] == "codex"
        assert config["agents"]["certifier"]["provider"] == "codex"

    detail = client.get(f"/api/runs/{state['live']['items'][0]['run_id']}").json()
    assert detail["build_config"]["certifier_mode"] == "thorough"
    assert detail["build_config"]["queue"]["task_timeout_s"] == 1500.0
