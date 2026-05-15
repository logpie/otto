"""Deterministic repair loop for v5/v6 integration preflight failures."""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import inspect
import json
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from otto.path_ownership import path_matches_any_ownership_pattern
from otto.safe_slug import short_hash
from otto.setup_gitignore import is_common_build_artifact_path, is_otto_owned_path
from otto.v5_clean_verify import (
    CleanOracleIssue,
    CleanOracleResult,
    CleanOracleStepResult,
    verify_from_clean_oracle,
)


@dataclass(frozen=True)
class RepairBudget:
    wall_clock_s: float = 1800.0
    cost_usd: float | None = None
    agent_turns: int = 1
    oracle_invocations: int = 4
    idle_s: float | None = None
    diff_churn: int | None = None
    closeout_agent_turns: int = 0
    provider_max_turns: int | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "wall_clock_s": self.wall_clock_s,
            "cost_usd": self.cost_usd,
            "agent_turns": self.agent_turns,
            "oracle_invocations": self.oracle_invocations,
            "idle_s": self.idle_s,
            "diff_churn": self.diff_churn,
            "closeout_agent_turns": self.closeout_agent_turns,
            "provider_max_turns": self.provider_max_turns,
        }

    @classmethod
    def from_jsonable(cls, raw: dict[str, Any] | None) -> "RepairBudget":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            wall_clock_s=float(raw.get("wall_clock_s") or 1800.0),
            cost_usd=(
                float(raw["cost_usd"])
                if isinstance(raw.get("cost_usd"), (int, float))
                else None
            ),
            agent_turns=max(0, int(raw.get("agent_turns") or 0)),
            oracle_invocations=max(0, int(raw.get("oracle_invocations") or 0)),
            idle_s=(
                float(raw["idle_s"])
                if isinstance(raw.get("idle_s"), (int, float))
                else None
            ),
            diff_churn=(
                int(raw["diff_churn"])
                if isinstance(raw.get("diff_churn"), int)
                else None
            ),
            closeout_agent_turns=max(0, int(raw.get("closeout_agent_turns") or 0)),
            provider_max_turns=(
                int(raw["provider_max_turns"])
                if isinstance(raw.get("provider_max_turns"), int)
                else None
            ),
        )


@dataclass
class RepairPacket:
    repair_unit: dict[str, Any]
    acceptance_oracle: dict[str, Any]
    latest_oracle_result: dict[str, Any]
    product_contract: dict[str, Any]
    integration_context: dict[str, Any]
    attempt_history: list[dict[str, Any]]
    current_state: dict[str, Any]
    budget: RepairBudget
    packet_dir: Path
    agent_session_id: str = ""

    @property
    def packet_path(self) -> Path:
        return self.packet_dir / "repair_packet.json"

    @property
    def events_path(self) -> Path:
        return self.packet_dir / "repair_packet.events.jsonl"

    @property
    def repair_unit_id(self) -> str:
        return str(self.repair_unit.get("id") or self.packet_dir.name)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "repair_unit": self.repair_unit,
            "acceptance_oracle": self.acceptance_oracle,
            "latest_oracle_result": self.latest_oracle_result,
            "product_contract": self.product_contract,
            "integration_context": self.integration_context,
            "attempt_history": self.attempt_history,
            "current_state": self.current_state,
            "budget": self.budget.to_jsonable(),
            "packet_dir": str(self.packet_dir),
            "agent_session_id": self.agent_session_id,
            "_written_at": _iso_now(),
        }

    @classmethod
    def from_jsonable(cls, payload: dict[str, Any], *, packet_path: Path) -> "RepairPacket":
        packet_dir = Path(str(payload.get("packet_dir") or packet_path.parent))
        return cls(
            repair_unit=dict(payload.get("repair_unit") or {}),
            acceptance_oracle=dict(payload.get("acceptance_oracle") or {}),
            latest_oracle_result=dict(payload.get("latest_oracle_result") or {}),
            product_contract=dict(payload.get("product_contract") or {}),
            integration_context=dict(payload.get("integration_context") or {}),
            attempt_history=list(payload.get("attempt_history") or []),
            current_state=dict(payload.get("current_state") or {}),
            budget=RepairBudget.from_jsonable(payload.get("budget")),
            packet_dir=packet_dir,
            agent_session_id=str(payload.get("agent_session_id") or ""),
        )

    @classmethod
    def load(cls, packet_path: Path) -> "RepairPacket":
        payload = json.loads(Path(packet_path).read_text(encoding="utf-8"))
        return cls.from_jsonable(payload, packet_path=Path(packet_path))

    def persist(self) -> None:
        self.packet_dir.mkdir(parents=True, exist_ok=True)
        with _repair_unit_lock(self.packet_dir, self.repair_unit_id):
            tmp_path = self.packet_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(self.to_jsonable(), indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self.packet_path)

    def append_event(
        self,
        event_type: str,
        *,
        digest: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event_payload = dict(payload or {})
        event_payload.setdefault("type", event_type)
        event_payload.setdefault("repair_unit_id", self.repair_unit_id)
        _append_repair_event(
            packet_path=self.packet_path,
            packet_dir=self.packet_dir,
            repair_unit_id=self.repair_unit_id,
            digest=digest,
            event=event_payload,
        )

    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
        return rows

    def capture_scope_baseline(self) -> None:
        worktree = Path(str(self.repair_unit.get("worktree") or "."))
        self.current_state["scope_baseline"] = _scope_baseline(worktree)
        head = _git_capture(worktree, ["rev-parse", "HEAD"])
        if head:
            self.current_state.setdefault("pre_repair_head", head)
            self.current_state["head"] = head
        self.current_state["scope_baseline_captured_at"] = _iso_now()
        self.persist()


@dataclass(frozen=True)
class OracleRepairResult:
    verdict: Literal["pass", "merge_blocked"]
    summary: str
    agent_session_id: str = ""
    cost_usd: float = 0.0
    agent_turns_used: int = 0
    oracle_invocations: int = 0
    packet_path: str = ""
    composite_gate: dict[str, Any] | None = None
    escalation: dict[str, Any] | None = None


@dataclass
class _BudgetUsage:
    cost_usd: float = 0.0
    agent_turns_used: int = 0
    closeout_turns_used: int = 0
    oracle_invocations: int = 0
    first_event_epoch_s: float | None = None
    last_event_epoch_s: float | None = None


OracleRunner = Callable[[RepairPacket], CleanOracleResult | Awaitable[CleanOracleResult]]
AgentRunner = Callable[..., Awaitable[tuple[str, float, str, dict[str, Any]]]]
CommitHook = Callable[[RepairPacket, CleanOracleResult], tuple[bool, str] | Awaitable[tuple[bool, str]]]
_CLOSEOUT_AGENT_REASONS = frozenset({"budget_exhausted", "oracle_budget_exhausted"})


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextlib.contextmanager
def _repair_unit_lock(packet_dir: Path, repair_unit_id: str) -> Any:
    import fcntl

    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", repair_unit_id).strip("-") or "unit"
    lock_dir = packet_dir.parent / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"repair_unit_id={repair_unit_id}\n_written_at={_iso_now()}\n")
        lock_file.flush()
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _append_repair_event(
    *,
    packet_path: Path,
    packet_dir: Path,
    repair_unit_id: str,
    digest: str,
    event: dict[str, Any],
) -> None:
    packet_dir.mkdir(parents=True, exist_ok=True)
    events_path = packet_dir / "repair_packet.events.jsonl"
    with _repair_unit_lock(packet_dir, repair_unit_id):
        seq = 1
        if events_path.exists():
            with events_path.open("r", encoding="utf-8") as fh:
                seq += sum(1 for line in fh if line.strip())
        row = {
            "seq": seq,
            "ts": _iso_now(),
            "digest": digest,
            "event": event,
        }
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def append_repair_packet_oracle_event(
    packet_path: Path,
    result: CleanOracleResult,
    *,
    source: str,
) -> None:
    """Append an oracle_run event for CLI or agent-side oracle invocations."""
    packet_path = Path(packet_path)
    packet_dir = packet_path.parent
    repair_unit_id = packet_dir.name
    if packet_path.exists():
        try:
            packet = RepairPacket.load(packet_path)
            repair_unit_id = packet.repair_unit_id
            packet.latest_oracle_result = result.to_jsonable()
            packet.persist()
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    _append_repair_event(
        packet_path=packet_path,
        packet_dir=packet_dir,
        repair_unit_id=repair_unit_id,
        digest=result.digest,
        event={
            "type": "oracle_run",
            "repair_unit_id": repair_unit_id,
            "source": source,
            "passed": result.passed,
        },
    )


def _git_status_porcelain(worktree: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _git_capture(worktree: Path, args: list[str], *, timeout: int = 10) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _git_changed_paths_between(worktree: Path, base_ref: str, head_ref: str) -> list[str]:
    if not base_ref or not head_ref or base_ref == head_ref:
        return []
    output = _git_capture(
        worktree,
        ["diff", "--name-only", f"{base_ref}..{head_ref}"],
        timeout=20,
    )
    paths = [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not _is_generated_path(line.strip())
    ]
    return sorted(dict.fromkeys(paths))


def _git_diff_churn_between(worktree: Path, base_ref: str, head_ref: str) -> int:
    if not base_ref or not head_ref or base_ref == head_ref:
        return 0
    output = _git_capture(
        worktree,
        ["diff", "--numstat", f"{base_ref}..{head_ref}"],
        timeout=20,
    )
    churn = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or _is_generated_path(parts[-1].strip()):
            continue
        for value in parts[:2]:
            if value.isdigit():
                churn += int(value)
    return churn


def _porcelain_path(line: str) -> str:
    raw = line[3:] if len(line) > 3 else ""
    if " -> " in raw:
        raw = raw.split(" -> ", 1)[1]
    return raw.strip().strip('"')


def _is_generated_path(path: str) -> bool:
    return is_otto_owned_path(path) or is_common_build_artifact_path(path)


def _path_signature(path: Path) -> tuple[bool, str]:
    if not path.exists() or not path.is_file():
        return (False, "")
    try:
        return (True, short_hash(path.read_bytes().hex(), length=32))
    except OSError:
        return (True, "")


def _scope_baseline(worktree: Path) -> dict[str, tuple[bool, str]]:
    baseline: dict[str, tuple[bool, str]] = {}
    for line in _git_status_porcelain(worktree).splitlines():
        rel = _porcelain_path(line)
        if not rel or _is_generated_path(rel):
            continue
        baseline[rel] = _path_signature(worktree / rel)
    return baseline


def _modified_paths_since_baseline(
    worktree: Path,
    baseline: dict[str, Any] | None,
) -> list[str]:
    paths: list[str] = []
    for line in _git_status_porcelain(worktree).splitlines():
        rel = _porcelain_path(line)
        if not rel or _is_generated_path(rel):
            continue
        if baseline is None:
            paths.append(rel)
            continue
        signature = _path_signature(worktree / rel)
        stored = baseline.get(rel)
        if isinstance(stored, list):
            stored = tuple(stored)
        if stored != signature:
            paths.append(rel)
    return sorted(dict.fromkeys(paths))


def _path_allowed(path: str, allowed_paths: list[str]) -> bool:
    if not allowed_paths:
        return True
    return path_matches_any_ownership_pattern(
        path,
        allowed_paths,
        allow_literal_prefix=True,
    )


def _reason_allows_closeout_agent(reason: str) -> bool:
    return reason in _CLOSEOUT_AGENT_REASONS


def _has_conflict_markers(worktree: Path, paths: list[str]) -> bool:
    for rel in paths:
        path = worktree / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "<<<<<<< " in text and "=======" in text and ">>>>>>> " in text:
            return True
    return False


def _has_unmerged_paths(worktree: Path) -> bool:
    for line in _git_status_porcelain(worktree).splitlines():
        status = line[:2]
        if "U" in status or status in {"AA", "DD"}:
            return True
    return False


def _changed_paths_since_repair_start(
    packet: RepairPacket,
    worktree: Path,
    baseline: dict[str, Any] | None,
) -> list[str]:
    paths = _modified_paths_since_baseline(worktree, baseline)
    pre_repair_head = str(
        packet.current_state.get("pre_repair_head")
        or packet.current_state.get("head")
        or ""
    )
    current_head = _git_capture(worktree, ["rev-parse", "HEAD"])
    for path in _git_changed_paths_between(worktree, pre_repair_head, current_head):
        paths.append(path)
    return sorted(dict.fromkeys(paths))


def _evaluate_composite_gate(
    packet: RepairPacket,
    oracle_result: CleanOracleResult,
    *,
    require_clean_worktree: bool = True,
) -> dict[str, Any]:
    worktree = Path(str(packet.repair_unit.get("worktree") or "."))
    baseline_raw = packet.current_state.get("scope_baseline")
    baseline = baseline_raw if isinstance(baseline_raw, dict) else None
    changed_since_baseline = _changed_paths_since_repair_start(packet, worktree, baseline)
    allowed_paths = [str(path) for path in (packet.repair_unit.get("allowed_paths") or [])]
    scope_policy = str(packet.repair_unit.get("scope_policy") or "unrestricted")
    scope_violations = (
        [
            path for path in changed_since_baseline
            if not _path_allowed(path, allowed_paths)
        ]
        if scope_policy == "allowed_paths"
        else []
    )
    dirty_paths = _modified_paths_since_baseline(worktree, None)
    conflict_marker_paths = sorted(dict.fromkeys([*dirty_paths, *changed_since_baseline]))
    conflict_markers = _has_conflict_markers(worktree, conflict_marker_paths)
    unmerged = _has_unmerged_paths(worktree)
    gate = {
        "oracle_passed": oracle_result.passed,
        "clean_worktree": not dirty_paths,
        "require_clean_worktree": require_clean_worktree,
        "dirty_paths": dirty_paths,
        "changed_paths": changed_since_baseline,
        "conflict_markers": not conflict_markers,
        "unmerged_paths": not unmerged,
        "scope_ok": not scope_violations,
        "scope_violations": scope_violations,
        "verdict_consistency": True,
        "graph_invariants": True,
    }
    required_keys = [
        "oracle_passed",
        "conflict_markers",
        "unmerged_paths",
        "scope_ok",
        "verdict_consistency",
        "graph_invariants",
    ]
    if require_clean_worktree:
        required_keys.append("clean_worktree")
    gate["passed"] = all(bool(gate[key]) for key in required_keys)
    return gate


def _diff_churn_since_repair_start(packet: RepairPacket) -> int:
    worktree = Path(str(packet.repair_unit.get("worktree") or "."))
    pre_repair_head = str(
        packet.current_state.get("pre_repair_head")
        or packet.current_state.get("head")
        or ""
    )
    current_head = _git_capture(worktree, ["rev-parse", "HEAD"])
    committed_churn = _git_diff_churn_between(worktree, pre_repair_head, current_head)
    uncommitted_output = _git_capture(worktree, ["diff", "--numstat"], timeout=20)
    uncommitted_churn = 0
    for line in uncommitted_output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or _is_generated_path(parts[-1].strip()):
            continue
        for value in parts[:2]:
            if value.isdigit():
                uncommitted_churn += int(value)
    return committed_churn + uncommitted_churn


def _issue_fingerprint_set(result: CleanOracleResult) -> set[str]:
    return {
        json.dumps(
            {
                "kind": issue.kind,
                "step_id": issue.step_id,
                "ports": sorted(issue.ports),
                "paths": sorted(issue.paths),
            },
            sort_keys=True,
        )
        for issue in result.issues
    }


def oracle_progress_reproducible(
    *,
    previous: CleanOracleResult,
    improved: CleanOracleResult,
    reproduced: CleanOracleResult,
    has_owned_path_diff: bool,
) -> bool:
    if not has_owned_path_diff:
        return False
    if improved.digest != reproduced.digest:
        return False
    if improved.passed and not previous.passed:
        return True
    return len(_issue_fingerprint_set(improved)) < len(_issue_fingerprint_set(previous))


def _oracle_result_from_json(payload: dict[str, Any]) -> CleanOracleResult:
    def _severity(raw: Any) -> Literal["warn", "error", "block"]:
        value = str(raw or "block")
        if value in {"warn", "error", "block"}:
            return cast(Literal["warn", "error", "block"], value)
        return "block"

    def _scope(raw: Any) -> Literal["scaffold", "subtree", "full"]:
        value = str(raw or "subtree")
        if value in {"scaffold", "subtree", "full"}:
            return cast(Literal["scaffold", "subtree", "full"], value)
        return "subtree"

    issues = [
        CleanOracleIssue(
            kind=str(issue.get("kind") or "unknown"),
            severity=_severity(issue.get("severity")),
            message=str(issue.get("message") or ""),
            step_id=str(issue.get("step_id") or ""),
            paths=[str(path) for path in (issue.get("paths") or [])],
            ports=[int(port) for port in (issue.get("ports") or [])],
            command_identity=str(issue.get("command_identity") or ""),
            return_code=(
                int(issue["return_code"])
                if isinstance(issue.get("return_code"), int)
                else None
            ),
        )
        for issue in (payload.get("issues") or [])
        if isinstance(issue, dict)
    ]
    steps = [
        CleanOracleStepResult(
            id=str(step.get("id") or ""),
            status=str(step.get("status") or ""),
            return_code=(
                int(step["return_code"])
                if isinstance(step.get("return_code"), int)
                else None
            ),
            command_identity=str(step.get("command_identity") or ""),
            command=[str(part) for part in (step.get("command") or [])],
            cwd=str(step.get("cwd") or ""),
            env={str(k): str(v) for k, v in (step.get("env") or {}).items()},
            started_at=str(step.get("started_at") or ""),
            duration_s=float(step.get("duration_s") or 0.0),
            artifact_paths=[str(path) for path in (step.get("artifact_paths") or [])],
            stdout_tail=str(step.get("stdout_tail") or ""),
            stderr_tail=str(step.get("stderr_tail") or ""),
            reason=str(step.get("reason") or ""),
        )
        for step in (payload.get("steps") or [])
        if isinstance(step, dict)
    ]
    result = CleanOracleResult(
        passed=bool(payload.get("passed")),
        scope=_scope(payload.get("scope")),
        issues=issues,
        steps=steps,
        artifact_path_refs=[str(path) for path in (payload.get("artifact_path_refs") or [])],
        command=[str(part) for part in (payload.get("command") or [])],
        env={str(k): str(v) for k, v in (payload.get("env") or {}).items()},
        digest=str(payload.get("digest") or ""),
        _written_at=str(payload.get("_written_at") or ""),
    )
    return result


def _infra_oracle_result(packet: RepairPacket, message: str) -> CleanOracleResult:
    step = CleanOracleStepResult(
        id="oracle_infra",
        status="failed",
        return_code=1,
        command_identity="clean-verify",
        command=[str(part) for part in (packet.acceptance_oracle.get("command") or [])],
        cwd=str(packet.repair_unit.get("worktree") or "."),
        env={str(k): str(v) for k, v in (packet.acceptance_oracle.get("env") or {}).items()},
        reason=message,
    )
    issue = CleanOracleIssue(
        kind="oracle_infra_error",
        severity="block",
        message=message,
        step_id=step.id,
        command_identity=step.command_identity,
        return_code=step.return_code,
    )
    return CleanOracleResult.from_parts(
        passed=False,
        scope=cast(
            Literal["scaffold", "subtree", "full"],
            str(packet.acceptance_oracle.get("verify_scope") or "subtree"),
        ),
        issues=[issue],
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env=step.env,
        project_dir=Path(str(packet.repair_unit.get("worktree") or ".")),
        temp_dir=None,
    )


def _parse_event_epoch_s(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return float(calendar.timegm(parsed))


def _float_event_value(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _replay_budget_usage(packet: RepairPacket) -> _BudgetUsage:
    usage = _BudgetUsage()
    with _repair_unit_lock(packet.packet_dir, packet.repair_unit_id):
        if not packet.events_path.exists():
            return usage
        lines = packet.events_path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_event_epoch_s(row.get("ts"))
        if ts is not None:
            usage.first_event_epoch_s = (
                ts
                if usage.first_event_epoch_s is None
                else min(usage.first_event_epoch_s, ts)
            )
            usage.last_event_epoch_s = (
                ts
                if usage.last_event_epoch_s is None
                else max(usage.last_event_epoch_s, ts)
            )
        event = row.get("event")
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type == "agent_turn":
            usage.agent_turns_used += 1
            usage.cost_usd += _float_event_value(event.get("cost_usd"))
        elif event_type == "closeout_agent_turn":
            usage.agent_turns_used += 1
            usage.closeout_turns_used += 1
            usage.cost_usd += _float_event_value(event.get("cost_usd"))
        elif event_type == "closeout_agent_error":
            usage.agent_turns_used += 1
            usage.closeout_turns_used += 1
            usage.cost_usd += _float_event_value(event.get("cost_usd"))
        elif event_type == "agent_error":
            if event.get("agent_turn_charged", True):
                usage.agent_turns_used += 1
            usage.cost_usd += _float_event_value(event.get("cost_usd"))
        elif event_type == "oracle_run":
            usage.oracle_invocations += 1
    return usage


def _reload_packet_state(packet: RepairPacket) -> RepairPacket:
    if not packet.packet_path.exists():
        return packet
    with _repair_unit_lock(packet.packet_dir, packet.repair_unit_id):
        try:
            loaded = RepairPacket.load(packet.packet_path)
        except (OSError, json.JSONDecodeError, ValueError):
            return packet
    loaded.budget = packet.budget
    return loaded


def _cost_from_breakdown(raw: Any) -> float:
    if isinstance(raw, dict):
        for key in ("cost_usd", "total_cost_usd", "estimated_cost_usd"):
            value = raw.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        for value in raw.values():
            nested = _cost_from_breakdown(value)
            if nested:
                return nested
    if isinstance(raw, list):
        for value in raw:
            nested = _cost_from_breakdown(value)
            if nested:
                return nested
    return 0.0


def _closeout_prompt(packet: RepairPacket, reason: str) -> str:
    return (
        "Write a concise structured escalation record for this repair packet. "
        "Do not edit files or run more repair attempts. Use the packet events, "
        "latest oracle result, and current state to explain why landing is "
        f"blocked within budget. Reason: {reason}\n\n"
        f"Repair packet: {packet.packet_path}\n"
    )


async def _default_oracle_runner(packet: RepairPacket) -> CleanOracleResult:
    command = [str(part) for part in (packet.acceptance_oracle.get("command") or [])]
    worktree = Path(str(packet.repair_unit.get("worktree") or "."))
    if not command:
        scope = str(packet.acceptance_oracle.get("verify_scope") or "subtree")
        if scope not in {"scaffold", "subtree", "full"}:
            scope = "subtree"
        return verify_from_clean_oracle(
            worktree,
            scope=cast(Literal["scaffold", "subtree", "full"], scope),
        )
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (packet.acceptance_oracle.get("env") or {}).items()})
    timeout = int(packet.acceptance_oracle.get("timeout_s") or 300)
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _infra_oracle_result(packet, f"{type(exc).__name__}: {exc}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return _infra_oracle_result(
            packet,
            f"clean-verify emitted non-JSON output (exit {proc.returncode})",
        )
    return _oracle_result_from_json(payload)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _repair_prompt(packet: RepairPacket) -> str:
    return (
        "Repair this worktree so the clean-deploy oracle passes. Preserve the "
        "product contract, P0-P4 merge invariants, and owned-path/scope rules. "
        "Diagnose from the complete evidence packet. Run the oracle as your "
        "acceptance loop. Stop only when the oracle passes or you can produce a "
        "structured escalation record explaining why it cannot within budget.\n\n"
        f"Repair packet: {packet.packet_path}\n"
        f"Oracle command: {json.dumps(packet.acceptance_oracle.get('command') or [])}\n"
        f"Repair unit: {json.dumps(packet.repair_unit, sort_keys=True, default=str)}\n"
    )


def _structured_escalation(
    packet: RepairPacket,
    *,
    reason: str,
    agent_turns_used: int,
    oracle_invocations: int,
    cost_usd: float,
    closeout_summary: str = "",
) -> dict[str, Any]:
    worktree = Path(str(packet.repair_unit.get("worktree") or "."))
    baseline_raw = packet.current_state.get("scope_baseline")
    baseline = baseline_raw if isinstance(baseline_raw, dict) else None
    return {
        "reason": reason,
        "oracle_command": packet.acceptance_oracle.get("command") or [],
        "oracle_env": packet.acceptance_oracle.get("env") or {},
        "final_oracle_result": packet.latest_oracle_result,
        "all_issues": (packet.latest_oracle_result or {}).get("issues") or [],
        "attempt_timeline": packet.events(),
        "agent_turns_used": agent_turns_used,
        "oracle_invocations": oracle_invocations,
        "cost_usd": cost_usd,
        "files_changed": _changed_paths_since_repair_start(packet, worktree, baseline),
        "recommendation": "review_packet",
        "closeout_source": "agent_reserve" if closeout_summary else "packet",
        "closeout_summary": closeout_summary,
        "_written_at": _iso_now(),
    }


async def run_oracle_repair_agent(
    repair_packet: RepairPacket,
    *,
    config: dict[str, Any],
    agent_runner: AgentRunner | None = None,
    oracle_runner: OracleRunner | None = None,
    commit_hook: CommitHook | None = None,
) -> OracleRepairResult:
    """Run or resume one durable repair session for a whole oracle unit."""
    from otto.agent import AgentCallError, make_agent_options

    packet = repair_packet
    if packet.packet_path.exists():
        loaded = RepairPacket.load(packet.packet_path)
        # Keep caller-provided budget overrides for tests/new invocations, but
        # replay durable identity/session state from disk.
        loaded.budget = packet.budget
        packet = loaded
    packet.persist()
    if "scope_baseline" not in packet.current_state or "pre_repair_head" not in packet.current_state:
        packet.capture_scope_baseline()
    worktree = Path(str(packet.repair_unit.get("worktree") or "."))
    started = time.monotonic()
    usage = _replay_budget_usage(packet)
    cost_usd = usage.cost_usd
    agent_turns_used = usage.agent_turns_used
    closeout_turns_used = usage.closeout_turns_used
    oracle_invocations = usage.oracle_invocations
    prior_elapsed_wall_s = (
        max(0.0, time.time() - usage.first_event_epoch_s)
        if usage.first_event_epoch_s is not None
        else 0.0
    )
    last_activity_epoch_s = usage.last_event_epoch_s or time.time()
    latest_oracle = _oracle_result_from_json(packet.latest_oracle_result)
    default_oracle = oracle_runner is None

    if agent_runner is None:
        from otto.agent import run_agent_with_timeout

        async def default_runner(
            prompt: str,
            options: Any,
            **kwargs: Any,
        ) -> tuple[str, float, str, dict[str, Any]]:
            return await run_agent_with_timeout(prompt, options, **kwargs)

        selected_runner: AgentRunner = default_runner
    else:
        selected_runner = agent_runner

    def elapsed_wall_s() -> float:
        return prior_elapsed_wall_s + (time.monotonic() - started)

    def repair_turn_limit() -> int:
        return max(0, packet.budget.agent_turns - packet.budget.closeout_agent_turns)

    def repair_turns_used() -> int:
        return max(0, agent_turns_used - closeout_turns_used)

    def budget_exhausted_reason(*, include_turn_limit: bool = True) -> str | None:
        if elapsed_wall_s() > packet.budget.wall_clock_s:
            return "wall_clock_exhausted"
        if packet.budget.idle_s is not None:
            idle_for = max(0.0, time.time() - last_activity_epoch_s)
            if idle_for > packet.budget.idle_s:
                return "idle_exhausted"
        if packet.budget.cost_usd is not None and cost_usd >= packet.budget.cost_usd:
            return "cost_exhausted"
        if (
            packet.budget.diff_churn is not None
            and _diff_churn_since_repair_start(packet) > packet.budget.diff_churn
        ):
            return "diff_churn_exhausted"
        if include_turn_limit and repair_turns_used() >= repair_turn_limit():
            return "budget_exhausted"
        return None

    async def call_agent(
        *,
        prompt: str,
        phase_name: str,
        phase_label: str,
        log_name: str,
    ) -> tuple[str, float, str, dict[str, Any]]:
        options = make_agent_options(
            worktree,
            config,
            agent_type="build",
            resume=packet.agent_session_id or None,
        )
        max_turns = packet.budget.provider_max_turns or int(config.get("max_turns_per_call") or 1)
        options.max_turns = max(1, max_turns)
        options.cwd = str(worktree)
        log_dir = packet.packet_dir / "agent" / log_name
        log_dir.mkdir(parents=True, exist_ok=True)
        timeout_s = int(
            max(
                1.0,
                min(
                    max(1.0, packet.budget.wall_clock_s - elapsed_wall_s()),
                    float(config.get("run_budget_seconds") or 3600),
                ),
            )
        )
        return await selected_runner(
            prompt,
            options,
            log_dir=log_dir,
            phase_name=phase_name,
            phase_label=phase_label,
            timeout=timeout_s,
            project_dir=worktree,
        )

    def reconcile_replayed_usage() -> None:
        nonlocal cost_usd, agent_turns_used, closeout_turns_used, oracle_invocations
        nonlocal last_activity_epoch_s, packet, latest_oracle
        packet = _reload_packet_state(packet)
        latest_oracle = _oracle_result_from_json(packet.latest_oracle_result)
        replayed = _replay_budget_usage(packet)
        cost_usd = max(cost_usd, replayed.cost_usd)
        agent_turns_used = max(agent_turns_used, replayed.agent_turns_used)
        closeout_turns_used = max(closeout_turns_used, replayed.closeout_turns_used)
        oracle_invocations = max(oracle_invocations, replayed.oracle_invocations)
        if replayed.last_event_epoch_s is not None:
            last_activity_epoch_s = max(last_activity_epoch_s, replayed.last_event_epoch_s)

    async def block_with_escalation(
        *,
        reason: str,
        summary: str,
        composite_gate: dict[str, Any] | None = None,
        allow_closeout: bool = True,
    ) -> OracleRepairResult:
        nonlocal cost_usd, agent_turns_used, closeout_turns_used, last_activity_epoch_s
        closeout_summary = ""
        if (
            allow_closeout
            and _reason_allows_closeout_agent(reason)
            and closeout_turns_used < packet.budget.closeout_agent_turns
            and agent_turns_used < packet.budget.agent_turns
        ):
            try:
                text, turn_cost, session_id, breakdown = await call_agent(
                    prompt=_closeout_prompt(packet, reason),
                    phase_name="REPAIR_CLOSEOUT",
                    phase_label="oracle-repair-closeout",
                    log_name=f"closeout-{closeout_turns_used + 1}",
                )
                closeout_cost = float(turn_cost or 0.0) or _cost_from_breakdown(breakdown)
                cost_usd += closeout_cost
                agent_turns_used += 1
                closeout_turns_used += 1
                last_activity_epoch_s = time.time()
                if session_id:
                    packet.agent_session_id = session_id
                closeout_summary = str(text or "")[-4000:]
                packet.attempt_history.append({
                    "type": "closeout_agent_turn",
                    "turn": agent_turns_used,
                    "agent_session_id": packet.agent_session_id,
                    "cost_usd": closeout_cost,
                    "breakdown": breakdown,
                    "reason": reason,
                    "_written_at": _iso_now(),
                })
                packet.append_event(
                    "closeout_agent_turn",
                    digest=latest_oracle.digest,
                    payload={
                        "agent_session_id": packet.agent_session_id,
                        "turn": agent_turns_used,
                        "cost_usd": closeout_cost,
                        "reason": reason,
                    },
                )
                packet.persist()
            except AgentCallError as exc:
                closeout_cost = float(exc.total_cost_usd or 0.0)
                cost_usd += closeout_cost
                agent_turns_used += 1
                closeout_turns_used += 1
                last_activity_epoch_s = time.time()
                if exc.session_id:
                    packet.agent_session_id = exc.session_id
                packet.append_event(
                    "closeout_agent_error",
                    digest=latest_oracle.digest,
                    payload={
                        "agent_session_id": packet.agent_session_id,
                        "reason": exc.reason,
                        "cost_usd": closeout_cost,
                    },
                )
                packet.persist()
        escalation = _structured_escalation(
            packet,
            reason=reason,
            agent_turns_used=agent_turns_used,
            oracle_invocations=oracle_invocations,
            cost_usd=cost_usd,
            closeout_summary=closeout_summary,
        )
        packet.append_event("repair_escalated", digest=latest_oracle.digest, payload=escalation)
        packet.persist()
        return OracleRepairResult(
            verdict="merge_blocked",
            summary=summary,
            agent_session_id=packet.agent_session_id,
            cost_usd=cost_usd,
            agent_turns_used=agent_turns_used,
            oracle_invocations=oracle_invocations,
            packet_path=str(packet.packet_path),
            composite_gate=composite_gate,
            escalation=escalation,
        )

    async def accept_or_block_passed_oracle() -> OracleRepairResult:
        pre_commit_gate = _evaluate_composite_gate(
            packet,
            latest_oracle,
            require_clean_worktree=False,
        )
        if not pre_commit_gate["passed"]:
            return OracleRepairResult(
                verdict="merge_blocked",
                summary="clean-deploy passed but pre-commit composite repair gate blocked landing",
                agent_session_id=packet.agent_session_id,
                cost_usd=cost_usd,
                agent_turns_used=agent_turns_used,
                oracle_invocations=oracle_invocations,
                packet_path=str(packet.packet_path),
                composite_gate=pre_commit_gate,
            )
        if commit_hook is not None:
            ok, detail = await _maybe_await(commit_hook(packet, latest_oracle))
            packet.append_event(
                "commit",
                digest=latest_oracle.digest,
                payload={"ok": ok, "detail": detail},
            )
            packet.persist()
            if not ok:
                return await block_with_escalation(
                    reason="commit_failed",
                    summary=detail,
                    allow_closeout=False,
                )

        post_commit_gate = _evaluate_composite_gate(
            packet,
            latest_oracle,
            require_clean_worktree=True,
        )
        if post_commit_gate["passed"]:
            return OracleRepairResult(
                verdict="pass",
                summary="clean-deploy oracle and composite gate passed",
                agent_session_id=packet.agent_session_id,
                cost_usd=cost_usd,
                agent_turns_used=agent_turns_used,
                oracle_invocations=oracle_invocations,
                packet_path=str(packet.packet_path),
                composite_gate=post_commit_gate,
            )
        return OracleRepairResult(
            verdict="merge_blocked",
            summary="clean-deploy passed but composite repair gate blocked landing",
            agent_session_id=packet.agent_session_id,
            cost_usd=cost_usd,
            agent_turns_used=agent_turns_used,
            oracle_invocations=oracle_invocations,
            packet_path=str(packet.packet_path),
            composite_gate=post_commit_gate,
        )

    while True:
        if latest_oracle.passed:
            return await accept_or_block_passed_oracle()

        reason = budget_exhausted_reason()
        if reason is not None:
            return await block_with_escalation(
                reason=reason,
                summary=f"repair {reason.replace('_', ' ')}",
            )

        try:
            text, turn_cost, session_id, breakdown = await call_agent(
                prompt=_repair_prompt(packet),
                phase_name="REPAIR",
                phase_label="oracle-repair",
                log_name=f"turn-{repair_turns_used() + 1}",
            )
        except AgentCallError as exc:
            error_cost = float(exc.total_cost_usd or 0.0)
            cost_usd += error_cost
            agent_turns_used += 1
            last_activity_epoch_s = time.time()
            if exc.session_id:
                packet.agent_session_id = exc.session_id
            packet.append_event(
                "agent_error",
                digest=latest_oracle.digest,
                payload={
                    "agent_session_id": packet.agent_session_id,
                    "reason": exc.reason,
                    "cost_usd": error_cost,
                    "crash_path": exc.crash_path,
                    "last_events": exc.last_events[-5:],
                    "agent_turn_charged": True,
                },
            )
            packet.persist()
            return await block_with_escalation(
                reason="agent_call_failed",
                summary=f"repair agent failed: {exc.reason}",
                allow_closeout=False,
            )

        del text
        packet = _reload_packet_state(packet)
        latest_oracle = _oracle_result_from_json(packet.latest_oracle_result)
        charged_cost = float(turn_cost or 0.0) or _cost_from_breakdown(breakdown)
        cost_usd += charged_cost
        agent_turns_used += 1
        last_activity_epoch_s = time.time()
        if session_id:
            packet.agent_session_id = session_id
        packet.attempt_history.append({
            "type": "agent_turn",
            "turn": agent_turns_used,
            "agent_session_id": packet.agent_session_id,
            "cost_usd": charged_cost,
            "breakdown": breakdown,
            "_written_at": _iso_now(),
        })
        packet.append_event(
            "agent_turn",
            digest=latest_oracle.digest,
            payload={
                "agent_session_id": packet.agent_session_id,
                "turn": agent_turns_used,
                "cost_usd": charged_cost,
            },
        )
        packet.persist()
        reconcile_replayed_usage()
        if latest_oracle.passed:
            return await accept_or_block_passed_oracle()

        reason = budget_exhausted_reason(include_turn_limit=False)
        if reason is not None:
            return await block_with_escalation(
                reason=reason,
                summary=f"repair {reason.replace('_', ' ')}",
            )

        if oracle_invocations >= packet.budget.oracle_invocations:
            return await block_with_escalation(
                reason="oracle_budget_exhausted",
                summary="repair oracle budget exhausted",
            )
        raw_oracle = oracle_runner(packet) if oracle_runner is not None else _default_oracle_runner(packet)
        latest_oracle = await _maybe_await(raw_oracle)
        oracle_invocations += 1
        last_activity_epoch_s = time.time()
        packet.latest_oracle_result = latest_oracle.to_jsonable()
        if not default_oracle:
            packet.append_event(
                "oracle_run",
                digest=latest_oracle.digest,
                payload={
                    "source": "controller",
                    "passed": latest_oracle.passed,
                },
            )
        packet.persist()
        reconcile_replayed_usage()
