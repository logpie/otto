# Local Notes

Commands run during the audit:

```text
pwd && git branch --show-current && git status --short
rg -n "CODEX_STDIO_LIMIT_BYTES|create_subprocess_exec|readline\\(|detect_default_branch|refs/remotes/origin|_merge_target|failed-task-run\\?type=merge|test_web_landing_target" otto tests
rg -n "TODO|FIXME|XXX|NotImplementedError|type: ignore|except Exception|split\\(\"/\"\\)|rsplit\\(\"/\"" otto/agent.py otto/config.py otto/mission_control/service.py tests/test_agent.py tests/test_config.py tests/test_web_mission_control.py
uv run pytest tests/test_agent.py::test_codex_query_normalizes_json_events tests/test_config.py::TestDetectDefaultBranch::test_preserves_origin_head_branch_path tests/test_web_mission_control.py::test_web_keeps_failed_queue_tasks_inspectable_for_requeue tests/test_web_mission_control.py::test_web_landing_target_preserves_detected_branch_path -q
```

Focused audit tests passed: `4 passed`.

The broader sweep had already passed before this local audit:
`924 passed, 18 deselected`.
