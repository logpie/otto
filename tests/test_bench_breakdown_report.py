import json

from tools.bench_breakdown_report import collect_runs, parse_qa_agent_breakdown, parse_qa_profile


def test_parse_qa_agent_breakdown_buckets_commands(tmp_path):
    log = tmp_path / "qa-agent.log"
    log.write_text(
        "\n".join(
            [
                "[  10.0s] ● Bash  /bin/zsh -lc 'pwd && rg --files'",
                "[  20.0s] ● Bash  /bin/zsh -lc 'pytest -q'",
                "[  55.0s] ● Bash  /bin/zsh -lc \"python - <<'PY'\"",
                "[ 120.0s] ● Bash  /bin/zsh -lc 'npm install'",
                "[ 150.0s] ● Bash  /bin/zsh -lc \"cat > /tmp/verdict.json <<'EOF'\"",
            ]
        )
    )

    breakdown = parse_qa_agent_breakdown(log)
    assert breakdown.total_s == 150.0
    assert breakdown.buckets["source_read"] == 10.0
    assert breakdown.buckets["test_run"] == 10.0
    assert breakdown.buckets["direct_api"] == 35.0
    assert breakdown.buckets["install"] == 65.0
    assert breakdown.buckets["verdict_write"] == 30.0


def test_collect_runs_loads_task_summaries_and_timeline(tmp_path):
    results_root = tmp_path / "results"
    project_dir = results_root / "run-a" / "demo-project"
    logs_dir = project_dir / "otto_logs" / "task-1"
    logs_dir.mkdir(parents=True)
    (project_dir / "result.json").write_text(json.dumps({"runner_pass": "PASS", "verify_pass": "PASS", "time_s": 42, "attempts": 1}))
    (project_dir / "otto_logs" / "orchestrator.log").write_text("[2026-04-02 12:00:00] batch 1\n")
    (logs_dir / "task-summary.json").write_text(
        json.dumps(
            {
                "attempts": 1,
                "status": "passed",
                "total_duration_s": 12.5,
                "phase_timings": {"coding": 10.0, "test": 2.5},
            }
        )
    )

    runs = collect_runs(results_root, ["run-a"])
    assert runs[0]["label"] == "run-a"
    project = runs[0]["projects"][0]
    assert project["project"] == "demo-project"
    assert project["result"]["time_s"] == 42
    assert project["task_summaries"][0]["name"] == "task-1"
    assert project["timeline"][0]["message"] == "batch 1"


def test_parse_qa_profile_prefers_structured_labels(tmp_path):
    profile = tmp_path / "qa-profile.json"
    profile.write_text(
        json.dumps(
            {
                "proof_of_work": False,
                "total_s": 80.0,
                "bucket_totals": {"direct_api": 50.0, "test_run": 30.0},
                "steps": [
                    {
                        "ts": 30.0,
                        "delta": 30.0,
                        "bucket": "test_run",
                        "label": "pytest -q tests/test_blog.py -k cli",
                        "command": "/bin/zsh -lc 'pytest -q tests/test_blog.py -k cli'",
                    },
                    {
                        "ts": 80.0,
                        "delta": 50.0,
                        "bucket": "direct_api",
                        "label": "from blog import BlogService",
                        "command": "/bin/zsh -lc \"python - <<'PY'\"",
                    },
                ],
            }
        )
    )

    breakdown = parse_qa_profile(profile)
    assert breakdown.total_s == 80.0
    assert breakdown.proof_of_work is False
    assert breakdown.buckets["direct_api"] == 50.0
    assert breakdown.top_steps[0]["label"] == "from blog import BlogService"
