"""Tests for otto/defaults.py — override precedence (CLI > yaml > baked)."""

from __future__ import annotations

from pathlib import Path

import pytest

from otto import defaults


def test_baked_in_only_when_no_yaml_no_cli(tmp_path: Path) -> None:
    snap = defaults.snapshot_for(project_dir=tmp_path)
    assert snap.check_loop_max_attempts_per_group == 3
    assert snap.check_loop_timeout_per_attempt_s == 30 * 60
    assert snap.audit_loop_max_repair_attempts_per_run == 6
    assert snap.audit_loop_max_audit_passes_per_run == 4
    assert snap.total_repair_wall_s == 90 * 60
    assert snap.total_cost_usd is None  # uncapped per user directive
    assert snap.per_group_cost_usd == 5.0
    assert snap.audit_walkthrough_per_feature is False
    assert snap.audit_pre_merge_audit_groups == ()
    assert snap.agent_default_provider == "claude"
    assert snap.agent_default_model == "claude-sonnet-4-6"


def test_yaml_overrides_baked(tmp_path: Path) -> None:
    (tmp_path / "otto.yaml").write_text(
        """
retries:
  check_loop:
    max_attempts_per_group: 7
    timeout_per_attempt_s: 600
budgets:
  total_cost_usd: 25.0
audit:
  walkthrough_per_feature: true
"""
    )
    snap = defaults.snapshot_for(project_dir=tmp_path)
    assert snap.check_loop_max_attempts_per_group == 7
    assert snap.check_loop_timeout_per_attempt_s == 600
    assert snap.total_cost_usd == 25.0
    assert snap.audit_walkthrough_per_feature is True
    # untouched fields stay baked
    assert snap.audit_loop_max_repair_attempts_per_run == 6


def test_cli_overrides_yaml(tmp_path: Path) -> None:
    (tmp_path / "otto.yaml").write_text(
        "retries:\n  check_loop:\n    max_attempts_per_group: 7\n"
    )
    snap = defaults.snapshot_for(
        project_dir=tmp_path,
        cli_overrides={"retries.check_loop.max_attempts_per_group": 11},
    )
    assert snap.check_loop_max_attempts_per_group == 11


def test_cli_accepts_underscored_form(tmp_path: Path) -> None:
    snap = defaults.snapshot_for(
        cli_overrides={"check_loop_max_attempts_per_group": 9},
    )
    assert snap.check_loop_max_attempts_per_group == 9


def test_get_dotted_key(tmp_path: Path) -> None:
    val = defaults.get(
        "retries.check_loop.max_attempts_per_group",
        project_dir=tmp_path,
    )
    assert val == 3


def test_get_unknown_key_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        defaults.get("nonsense.key.value", project_dir=tmp_path)


def test_malformed_yaml_falls_back_to_baked(tmp_path: Path) -> None:
    (tmp_path / "otto.yaml").write_text("not: valid: yaml: at: all: ::\n")
    snap = defaults.snapshot_for(project_dir=tmp_path)
    # baked-ins must hold
    assert snap.check_loop_max_attempts_per_group == 3


def test_yaml_lists_pass_through(tmp_path: Path) -> None:
    (tmp_path / "otto.yaml").write_text(
        "audit:\n  pre_merge_audit_groups:\n    - auth\n    - payments\n"
    )
    snap = defaults.snapshot_for(project_dir=tmp_path)
    assert snap.audit_pre_merge_audit_groups == ("auth", "payments")


def test_total_cost_usd_can_be_capped(tmp_path: Path) -> None:
    (tmp_path / "otto.yaml").write_text("budgets:\n  total_cost_usd: 100.0\n")
    snap = defaults.snapshot_for(project_dir=tmp_path)
    assert snap.total_cost_usd == 100.0


def test_per_group_agent_overrides(tmp_path: Path) -> None:
    (tmp_path / "otto.yaml").write_text(
        """
agents:
  default_provider: codex
  per_group:
    auth:
      provider: claude
      model: claude-opus-4-7
"""
    )
    snap = defaults.snapshot_for(project_dir=tmp_path)
    assert snap.agent_default_provider == "codex"
    assert snap.agent_per_group == {
        "auth": {"provider": "claude", "model": "claude-opus-4-7"}
    }


def test_env_overrides_parsed() -> None:
    import os

    os.environ["OTTO_DEFAULT_RETRIES_CHECK_LOOP_MAX_ATTEMPTS_PER_GROUP"] = "13"
    try:
        flat = defaults.env_overrides()
        assert "retries_check_loop_max_attempts_per_group" in flat
        assert flat["retries_check_loop_max_attempts_per_group"] == "13"
    finally:
        os.environ.pop("OTTO_DEFAULT_RETRIES_CHECK_LOOP_MAX_ATTEMPTS_PER_GROUP", None)
