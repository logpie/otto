"""Single source of truth for retry counts, timeouts, budgets, and
audit modes. Per research §5 (configurable budgets) and the no-magic-
numbers rule.

Three-tier precedence: CLI flags override otto.yaml, otto.yaml
overrides baked-in defaults. Tests in tests/test_defaults.py.

Schema (research §5):

    retries.check_loop.max_attempts_per_group        # int
    retries.check_loop.timeout_per_attempt_s         # int (seconds)
    retries.audit_loop.max_repair_attempts_per_run   # int
    retries.audit_loop.max_audit_passes_per_run      # int

    budgets.total_repair_wall_s                      # int (seconds)
    budgets.total_cost_usd                           # float | None (None = uncapped)
    budgets.per_group_cost_usd                       # float

    audit.walkthrough_per_feature                    # bool
    audit.pre_merge_audit_groups                     # list[str]

    agents.default_provider                          # str
    agents.default_model                             # str
    agents.per_group                                 # dict[group_id, {provider, model}]

The legacy `BuildBudget` dataclass in otto/build.py is a runtime
ledger (tracks `_spent_repair_s`, `_spent_cost_usd`); it now reads
its initial values from this module instead of hardcoding.

Subprocess/network timeouts (e.g. `requests.get(timeout=5)`) are NOT
considered configurable budgets — they're transport-layer concerns.
The "no magic numbers" rule targets the retry/budget/audit knobs in
the schema above, not every numeric literal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Baked-in defaults
# ---------------------------------------------------------------------------

# Retries
_DEFAULT_CHECK_LOOP_MAX_ATTEMPTS_PER_GROUP = 3
_DEFAULT_CHECK_LOOP_TIMEOUT_PER_ATTEMPT_S = 30 * 60  # 30 min
_DEFAULT_AUDIT_LOOP_MAX_REPAIR_ATTEMPTS_PER_RUN = 1
_DEFAULT_AUDIT_LOOP_MAX_AUDIT_PASSES_PER_RUN = 2

# Budgets
_DEFAULT_TOTAL_REPAIR_WALL_S = 90 * 60  # 90 min, shared with audit
_DEFAULT_TOTAL_COST_USD: float | None = None  # None = uncapped (per user directive)
_DEFAULT_PER_GROUP_COST_USD = 5.0

# Audit modes
_DEFAULT_AUDIT_WALKTHROUGH_PER_FEATURE = False
_DEFAULT_AUDIT_PRE_MERGE_AUDIT_GROUPS: list[str] = []

# Agents
_DEFAULT_AGENT_PROVIDER = "claude"
_DEFAULT_AGENT_MODEL = "claude-sonnet-4-6"

# Brownfield-compile preamble (A6.1). Caps on what the Python helper
# bundles into the prompt before handing the agent its discovery tools
# (Read/Glob/Grep). Not user-tunable — these target prompt-budget
# concerns, not retry/audit knobs. Public so spec_compile.py and tests
# can import them directly.
BROWNFIELD_PREAMBLE_MAX_FILES = 200
BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE = 200

# Seed stage (A1.5-seed). Per-fixture wall-clock cap on the project-owned
# seed script (e.g. `scripts/otto/seed_user.py`). Transport-layer concern,
# not a retry/budget knob — a single fixture install should never exceed
# this; if it does, the project's seed script is broken.
SEED_PER_FIXTURE_TIMEOUT_S = 60


@dataclass(frozen=True)
class _Snapshot:
    """Frozen snapshot of all defaults values, after override application."""

    check_loop_max_attempts_per_group: int
    check_loop_timeout_per_attempt_s: int
    audit_loop_max_repair_attempts_per_run: int
    audit_loop_max_audit_passes_per_run: int
    total_repair_wall_s: int
    total_cost_usd: float | None
    per_group_cost_usd: float
    audit_walkthrough_per_feature: bool
    audit_pre_merge_audit_groups: tuple[str, ...]
    agent_default_provider: str
    agent_default_model: str
    agent_per_group: dict[str, dict[str, str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Dotted-key → snapshot-field alias map (single source for both pick() and
# _read_dotted). Drops module prefix so CLI flags like
# `--max-check-attempts` can map to `check_loop_max_attempts_per_group`.
# ---------------------------------------------------------------------------

_DOTTED_TO_FIELD: dict[str, str] = {
    "retries.check_loop.max_attempts_per_group": "check_loop_max_attempts_per_group",
    "retries.check_loop.timeout_per_attempt_s": "check_loop_timeout_per_attempt_s",
    "retries.audit_loop.max_repair_attempts_per_run": "audit_loop_max_repair_attempts_per_run",
    "retries.audit_loop.max_audit_passes_per_run": "audit_loop_max_audit_passes_per_run",
    "budgets.total_repair_wall_s": "total_repair_wall_s",
    "budgets.total_cost_usd": "total_cost_usd",
    "budgets.per_group_cost_usd": "per_group_cost_usd",
    "audit.walkthrough_per_feature": "audit_walkthrough_per_feature",
    "audit.pre_merge_audit_groups": "audit_pre_merge_audit_groups",
    "agents.default_provider": "agent_default_provider",
    "agents.default_model": "agent_default_model",
    "agents.per_group": "agent_per_group",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get(
    key: str,
    *,
    project_dir: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> Any:
    """Read a single config value by dotted key.

    Args:
        key: dotted path, e.g. "retries.check_loop.max_attempts_per_group"
        project_dir: project root containing otto.yaml; if None, baked-in only
        cli_overrides: flat-key dict, takes highest precedence

    Precedence: cli_overrides > otto.yaml > baked-in.

    Raises KeyError if the key is unknown.
    """
    snapshot = snapshot_for(project_dir=project_dir, cli_overrides=cli_overrides)
    return _read_dotted(snapshot, key)


def snapshot_for(
    *,
    project_dir: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> _Snapshot:
    """Return a frozen snapshot of all defaults after applying overrides."""
    yaml_overrides = _load_yaml_overrides(project_dir) if project_dir else {}
    cli_overrides = cli_overrides or {}

    def pick(dotted: str, baked: Any) -> Any:
        # cli_overrides accept three forms: dotted, fully-underscored,
        # or the snapshot field name (last-segment-or-alias). The
        # snapshot-field form is what most CLI flags map to.
        if dotted in cli_overrides:
            return cli_overrides[dotted]
        flat = dotted.replace(".", "_")
        if flat in cli_overrides:
            return cli_overrides[flat]
        # snapshot field alias — see _DOTTED_TO_FIELD below
        field_alias = _DOTTED_TO_FIELD.get(dotted)
        if field_alias and field_alias in cli_overrides:
            return cli_overrides[field_alias]
        # otto.yaml — read by dotted path
        try:
            return _read_dotted_dict(yaml_overrides, dotted)
        except KeyError:
            return baked

    return _Snapshot(
        check_loop_max_attempts_per_group=int(
            pick(
                "retries.check_loop.max_attempts_per_group",
                _DEFAULT_CHECK_LOOP_MAX_ATTEMPTS_PER_GROUP,
            )
        ),
        check_loop_timeout_per_attempt_s=int(
            pick(
                "retries.check_loop.timeout_per_attempt_s",
                _DEFAULT_CHECK_LOOP_TIMEOUT_PER_ATTEMPT_S,
            )
        ),
        audit_loop_max_repair_attempts_per_run=int(
            pick(
                "retries.audit_loop.max_repair_attempts_per_run",
                _DEFAULT_AUDIT_LOOP_MAX_REPAIR_ATTEMPTS_PER_RUN,
            )
        ),
        audit_loop_max_audit_passes_per_run=int(
            pick(
                "retries.audit_loop.max_audit_passes_per_run",
                _DEFAULT_AUDIT_LOOP_MAX_AUDIT_PASSES_PER_RUN,
            )
        ),
        total_repair_wall_s=int(
            pick("budgets.total_repair_wall_s", _DEFAULT_TOTAL_REPAIR_WALL_S)
        ),
        total_cost_usd=_coerce_optional_float(
            pick("budgets.total_cost_usd", _DEFAULT_TOTAL_COST_USD)
        ),
        per_group_cost_usd=float(
            pick("budgets.per_group_cost_usd", _DEFAULT_PER_GROUP_COST_USD)
        ),
        audit_walkthrough_per_feature=bool(
            pick(
                "audit.walkthrough_per_feature",
                _DEFAULT_AUDIT_WALKTHROUGH_PER_FEATURE,
            )
        ),
        audit_pre_merge_audit_groups=tuple(
            pick(
                "audit.pre_merge_audit_groups",
                _DEFAULT_AUDIT_PRE_MERGE_AUDIT_GROUPS,
            )
        ),
        agent_default_provider=str(
            pick("agents.default_provider", _DEFAULT_AGENT_PROVIDER)
        ),
        agent_default_model=str(
            pick("agents.default_model", _DEFAULT_AGENT_MODEL)
        ),
        agent_per_group=dict(pick("agents.per_group", {})),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_yaml_overrides(project_dir: Path) -> dict[str, Any]:
    """Load overrides from <project_dir>/otto.yaml. Empty dict if absent or
    malformed (we don't crash on bad YAML; the call site sees baked-ins)."""
    yaml_path = project_dir / "otto.yaml"
    if not yaml_path.exists():
        return {}
    try:
        text = yaml_path.read_text()
        data = yaml.safe_load(text)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _read_dotted(snapshot: _Snapshot, dotted: str) -> Any:
    """Read a dotted key from a snapshot. KeyError on unknown."""
    if dotted in _DOTTED_TO_FIELD:
        return getattr(snapshot, _DOTTED_TO_FIELD[dotted])
    # also accept the field name directly
    if hasattr(snapshot, dotted):
        return getattr(snapshot, dotted)
    raise KeyError(f"unknown defaults key: {dotted}")


def _read_dotted_dict(d: dict[str, Any], dotted: str) -> Any:
    """Walk a nested dict by dotted key. KeyError if any segment missing."""
    parts = dotted.split(".")
    cur: Any = d
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(dotted)
        cur = cur[part]
    return cur


def _coerce_optional_float(value: Any) -> float | None:
    """Coerce to float, but pass through None (= uncapped)."""
    if value is None:
        return None
    return float(value)


# ---------------------------------------------------------------------------
# Convenience: env override for tests/CI
# ---------------------------------------------------------------------------


def env_overrides() -> dict[str, Any]:
    """Read any OTTO_DEFAULT_<KEY> env vars as flat overrides.

    OTTO_DEFAULT_RETRIES_CHECK_LOOP_MAX_ATTEMPTS_PER_GROUP=5
    becomes {"retries.check_loop.max_attempts_per_group": "5"}.
    """
    out: dict[str, Any] = {}
    prefix = "OTTO_DEFAULT_"
    for key, val in os.environ.items():
        if not key.startswith(prefix):
            continue
        flat = key[len(prefix):].lower()
        out[flat] = val
    return out


__all__ = ["get", "snapshot_for", "env_overrides"]
