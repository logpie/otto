"""Regression: the integration-smoke repair agent must treat Otto / the
acceptance oracle / the UI-journey executor / the test harness as a BLACK-BOX
CONTRACT and fix PRODUCT behavior — never reverse-engineer the harness.

Root cause (fix8-orcl-052425, 2026-05-18, --tier modular iTracker, decision-tree
path 3 honest fail): foundation pass+merged (fix-6), 3/4 features clean-merged
(fix-7 CHARTER-validated, zero decomp-boundary recurrence) — decomp/contract/
gate/scope all sound. clean_deploy then failed 4 UI journeys. The bounded
integration-smoke repair agent did NOT converge: it burned ~60min / $9.70 / 200
provider-turns, and at turn-end was `grep -r "CONSOLE_TOKEN" /Users/.../cc-
autonomous/` — i.e. reverse-engineering OTTO'S OWN journey-oracle token
extraction instead of fixing the product. It left a speculative uncommitted
`console.log` (not a fix) and hit `error_max_turns` mid-confusion. `_repair_prompt`
+ `_oracle_focus_guidance` gave good data-driven guidance (passed/failed steps,
journey cascade) but NOTHING forbade harness reverse-engineering or pointed the
agent at the per-journey failure artifacts as the diagnostic surface.

Fix: `_repair_prompt` now states, consistent-by-construction, that the oracle /
clean-verify / journey executor / Otto are a fixed black-box contract the agent
must never read/grep/reverse-engineer, and that it must diagnose ONLY from the
product source + the per-journey failure artifacts (screenshot/dom/console/
network/verdict). This constrains the agent's diagnostic METHOD; it does NOT
weaken any gate — the unmodified oracle still decides repair success.

These are content assertions on the repair prompt (same regression pattern as
test_lead_backend_isolation_contract.py / test_v5_ia_contract.py). Before the
fix the black-box clause did not exist (RED); it is GREEN after. The guard test
proves the oracle-gated language is preserved (not gate relaxation).
"""

from __future__ import annotations

from pathlib import Path

from otto.v5_preflight_repair import RepairBudget, RepairPacket, _repair_prompt


def _packet(tmp_path: Path) -> RepairPacket:
    """Minimal packet — `_repair_prompt` only reads repair_unit,
    acceptance_oracle, packet_path, and latest_oracle_result (no git/disk)."""
    return RepairPacket(
        repair_unit={
            "id": "root-integration_smoke-pre_agent",
            "worktree": str(tmp_path),
            "phase": "preflight",
            "allowed_paths": [],
            "scope_policy": "unrestricted",
        },
        acceptance_oracle={
            "command": ["python", "-m", "otto.cli", "clean-verify"],
            "env": {},
        },
        latest_oracle_result={
            "passed": False,
            "steps": [
                {"id": "npm_build:frontend", "status": "passed"},
                {
                    "id": "ui_journeys",
                    "status": "failed",
                    "reason": (
                        "ui journeys failed: register_and_create_issue, "
                        "mention_triggers_inbox"
                    ),
                },
            ],
        },
        product_contract={},
        integration_context={},
        attempt_history=[],
        current_state={},
        budget=RepairBudget(),
        packet_dir=tmp_path / "repair" / "unit",
        agent_session_id="",
    )


def _norm(s: str) -> str:
    import re

    return re.sub(r"\s+", " ", s).strip().lower()


def test_repair_prompt_forbids_harness_reverse_engineering(tmp_path: Path) -> None:
    """The prompt must declare Otto/oracle/journey-executor/harness a fixed
    black-box contract and forbid reading/grepping/reverse-engineering it —
    the exact failure mode that burned the fix8-orcl repair turn."""
    n = _norm(_repair_prompt(_packet(tmp_path)))
    assert "black-box contract" in n
    # forbids reverse-engineering Otto / the oracle / the harness
    assert "reverse-engineer" in n
    assert ("never read" in n) or ("do not read" in n) or ("must never" in n)
    assert "harness" in n
    assert ("otto" in n) and ("oracle" in n)
    # points the agent at the per-journey failure artifacts as the diagnostic
    # surface (not the harness internals)
    assert "screenshot" in n
    assert "dom.html" in n
    assert "console-errors" in n or "console errors" in n
    assert "network.jsonl" in n or "network" in n
    # the goal is PRODUCT behavior, not satisfying the harness
    assert "product" in n
    assert "fixed and correct" in n or "correct by definition" in n


def test_repair_prompt_still_oracle_gated_not_weakened(tmp_path: Path) -> None:
    """Guard: consistent-by-construction, NOT gate relaxation — the prompt
    still runs the unmodified oracle as the acceptance loop and only stops on
    a real pass or a structured escalation."""
    n = _norm(_repair_prompt(_packet(tmp_path)))
    assert "run the oracle as your acceptance loop" in n
    assert "stop only when the oracle passes" in n
    assert "structured escalation" in n
    # the black-box clause must not say "skip" / "ignore" / "weaken" the oracle
    assert "skip the oracle" not in n
    assert "ignore the oracle" not in n
