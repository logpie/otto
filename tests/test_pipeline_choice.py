"""Tests for `otto.cli_run.resolve_pipeline_choice` (B.3 default-flip prep).

Source-of-truth for the dispatch decision used by `otto build`/
`otto certify`/`otto improve bugs`. The actual default-flip happens by
changing `otto.yaml`'s `default_pipeline` field — no code change.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from otto.cli_run import resolve_pipeline_choice


def _write_yaml(project: Path, content: str) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "otto.yaml").write_text(content)


def test_no_flags_no_config_defaults_to_i2p_post_b3(tmp_path: Path) -> None:
    """B.3 cutover (tick 62): no otto.yaml + no flags → i2p (the new default).
    Pre-cutover this returned "legacy"; the rename + assertion change
    document that the cutover actually flipped the default."""
    assert resolve_pipeline_choice(
        i2p_flag=False, legacy_flag=False, project_dir=tmp_path
    ) == "i2p"


def test_no_flags_default_pipeline_legacy_returns_legacy(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "default_pipeline: legacy\n")
    assert resolve_pipeline_choice(
        i2p_flag=False, legacy_flag=False, project_dir=tmp_path
    ) == "legacy"


def test_no_flags_default_pipeline_i2p_returns_i2p(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "default_pipeline: i2p\n")
    assert resolve_pipeline_choice(
        i2p_flag=False, legacy_flag=False, project_dir=tmp_path
    ) == "i2p"


def test_i2p_flag_overrides_legacy_default(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "default_pipeline: legacy\n")
    assert resolve_pipeline_choice(
        i2p_flag=True, legacy_flag=False, project_dir=tmp_path
    ) == "i2p"


def test_legacy_flag_overrides_i2p_default(tmp_path: Path) -> None:
    """When config flips to i2p, --legacy is the escape hatch."""
    _write_yaml(tmp_path, "default_pipeline: i2p\n")
    assert resolve_pipeline_choice(
        i2p_flag=False, legacy_flag=True, project_dir=tmp_path
    ) == "legacy"


def test_both_flags_raise_usage_error(tmp_path: Path) -> None:
    with pytest.raises(click.UsageError):
        resolve_pipeline_choice(
            i2p_flag=True, legacy_flag=True, project_dir=tmp_path
        )


def test_unrecognized_value_falls_back_to_legacy(tmp_path: Path) -> None:
    """A typo or future value falls back to legacy (the safe default)."""
    _write_yaml(tmp_path, "default_pipeline: experimental\n")
    assert resolve_pipeline_choice(
        i2p_flag=False, legacy_flag=False, project_dir=tmp_path
    ) == "legacy"


def test_uppercase_i2p_value_normalizes(tmp_path: Path) -> None:
    """Case-insensitive match on the config value."""
    _write_yaml(tmp_path, "default_pipeline: I2P\n")
    assert resolve_pipeline_choice(
        i2p_flag=False, legacy_flag=False, project_dir=tmp_path
    ) == "i2p"


def test_malformed_yaml_falls_back_to_legacy(tmp_path: Path) -> None:
    """Unparseable otto.yaml → safe fallback (legacy), no crash."""
    _write_yaml(tmp_path, "this is: not: valid: yaml: at: all\n   - ?")
    # Don't crash; fall back.
    result = resolve_pipeline_choice(
        i2p_flag=False, legacy_flag=False, project_dir=tmp_path
    )
    assert result == "legacy"
