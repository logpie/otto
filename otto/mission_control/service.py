"""Shared Mission Control service for web and CLI clients."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import click

from otto.config import ConfigError, load_config
from otto.config import repo_preflight_issues
from otto.config import resolve_intent_for_enqueue
from otto import paths
from otto.merge import git_ops
from otto.merge.state import load_state as load_merge_state
from otto.mission_control.actions import _otto_cli_argv
from otto.mission_control.actions import (
    ActionResult,
    execute_action,
    execute_merge_abort,
    execute_merge_all,
    execute_merge_recover,
    execute_queue_cleanup,
)
from otto.mission_control.events import append_event
from otto.mission_control.events import events_status
from otto.mission_control.model import (
    DetailView,
    MissionControlFilters,
    MissionControlModel,
    MissionControlState,
)
from otto.mission_control.serializers import (
    run_config_from_argv,
    serialize_action_result,
    serialize_artifact,
    serialize_detail,
    serialize_state,
)
from otto.mission_control.runtime import runtime_status as build_runtime_status
from otto.mission_control.runtime import watcher_health
from otto.mission_control.supervisor import record_watcher_launch
from otto.mission_control.supervisor import record_watcher_stop
from otto.mission_control.supervisor import read_supervisor
from otto.queue.enqueue import enqueue_task
from otto.queue.runtime import IN_FLIGHT_STATUSES, task_display_status, watcher_alive
from otto.queue.runner import child_is_alive, kill_child_safely, runner_config_from_otto_config
from otto.queue.schema import load_queue, load_state as load_queue_state
from otto.token_usage import token_usage_from_mapping as _token_usage_from_mapping
from otto.verification import VerificationCheck, VerificationPlan

LOGGER = logging.getLogger(__name__)
REVIEW_IN_PROGRESS_STATUSES = {"queued", "starting", "initializing", "running", "terminating"}
PROOF_LINK_ATTR_RE = re.compile(r"(?P<prefix>\b(?:src|href)=)(?P<quote>[\"'])(?P<url>.*?)(?P=quote)", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s)>\"]+")
COMMAND_LINE_RE = re.compile(
    r"^\s*(?P<command>(?:uv|python|python3|flask|fastapi|uvicorn|npm|pnpm|yarn|bun|cargo|go|make|pytest|curl|docker)\b.+)$",
    re.MULTILINE,
)
PRODUCT_HANDOFF_KINDS = {"web", "api", "cli", "desktop", "library", "service", "worker", "pipeline", "unknown"}
LANDING_COUNT_KEYS = ("ready", "merged", "blocked", "reviewed")


class MissionControlServiceError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class LandingClassification:
    state: str
    label: str
    count_key: str
    counts_for_collision: bool = False


@dataclass(slots=True)
class LogReadResult:
    path: str | None
    offset: int
    next_offset: int
    text: str
    exists: bool
    # Total size of the log file at read time. The client uses this to render
    # "Final · {total_bytes}" headers and to detect "we are caught up" without
    # having to do a second HEAD request.  ``0`` when the file is missing or
    # the slice came from an in-memory fallback.
    total_bytes: int = 0
    # ``True`` when ``next_offset == total_bytes`` after this slice — i.e. the
    # caller has read every byte we currently know about. Lets the client stop
    # polling once a terminal run has been fully drained without inferring it
    # from sentinel offsets.
    eof: bool = False


class MissionControlService:
    """Client-neutral Mission Control operations."""

    def __init__(self, project_dir: Path, *, queue_compat: bool = True) -> None:
        self.project_dir = Path(project_dir).resolve(strict=False)
        self.model = MissionControlModel(self.project_dir, queue_compat=queue_compat)
        # mc-audit live W13-IMPORTANT-2: the events log only recorded watcher
        # lifecycle (started/stopped); a real build-and-cert happening in the
        # watcher subprocess never showed up. Track the last-seen status per
        # run-id so each refresh can emit `run.started` / `run.terminal` /
        # `run.cancelled` / `run.failed` events into events.jsonl. Bootstrap
        # is lazy — the first refresh seeds the table without emitting (we
        # don't know which were already-known on startup) so we never spam
        # events for runs that completed before MC came online.
        self._lifecycle_seen_states: dict[str, str] = {}
        self._lifecycle_bootstrapped: bool = False

    def state(self, filters: MissionControlFilters | None = None) -> dict[str, Any]:
        state = self._state(filters)
        # mc-audit live W13-IMPORTANT-2: emit run.* events BEFORE serializing
        # `events` so the just-fired transition is visible in the same /api/state
        # response. Watcher/landing/runtime status do not depend on events so
        # ordering relative to them is irrelevant.
        self._emit_run_lifecycle_events(state)
        payload = serialize_state(self.project_dir, state)
        watcher = self.watcher_status()
        landing = self.landing_status()
        payload["watcher"] = watcher
        payload["landing"] = landing
        payload["runtime"] = self.runtime_status(watcher=watcher, landing=landing)
        payload["events"] = self.events(limit=50)
        return payload

    def _emit_run_lifecycle_events(self, state: MissionControlState) -> None:
        """Detect run-start / run-terminal transitions and append events.

        We compare the current set of live-record statuses with the previously
        observed set. Three cases emit events:

        * run.started  — a run-id we have never seen before is in a non-terminal
          state. (A run-id observed first in terminal state never emits start;
          we still emit terminal so post-outage timelines are not blank.)
        * run.<terminal_outcome>  — a previously non-terminal run-id transitioned
          to a terminal status. ``terminal_outcome`` (or status) drives the
          event kind: ``run.completed`` / ``run.failed`` / ``run.cancelled`` /
          ``run.interrupted`` / ``run.terminal``. This is the cluster of events
          the W13 outage-recovery scenario needed.

        Bootstrap: the very first call seeds ``_lifecycle_seen_states`` without
        emitting. Otherwise an MC server starting after a long-running queue
        would emit a flurry of stale events.
        """

        try:
            from otto.runs.schema import is_terminal_status
        except ImportError:  # pragma: no cover — defensive
            return

        current: dict[str, str] = {}
        for item in state.live_runs.items:
            record = item.record
            run_id = record.run_id
            if not run_id:
                continue
            current[run_id] = record.status or ""

        if not self._lifecycle_bootstrapped:
            self._lifecycle_seen_states = dict(current)
            self._lifecycle_bootstrapped = True
            return

        for item in state.live_runs.items:
            record = item.record
            run_id = record.run_id
            if not run_id:
                continue
            status = record.status or ""
            previous = self._lifecycle_seen_states.get(run_id)
            terminal_now = is_terminal_status(status)
            terminal_before = bool(previous) and is_terminal_status(previous)

            if previous is None and not terminal_now:
                self._record_event(
                    kind="run.started",
                    severity="info",
                    message=f"{record.display_name or record.command or 'run'} started",
                    run_id=run_id,
                    task_id=str(record.identity.get("queue_task_id") or "") or None,
                    details={
                        "domain": record.domain,
                        "run_type": record.run_type,
                        "command": record.command,
                        "branch": record.git.get("branch"),
                        "status": status,
                    },
                )
            elif terminal_now and not terminal_before:
                outcome = (record.terminal_outcome or status or "terminal").strip() or "terminal"
                kind_suffix = outcome.replace(" ", "_").lower()
                kind = f"run.{kind_suffix}" if kind_suffix else "run.terminal"
                severity = (
                    "success"
                    if outcome in {"success", "succeeded", "passed", "completed"}
                    else "error"
                    if outcome in {"failed", "failure", "error"}
                    else "warning"
                    if outcome in {"cancelled", "interrupted", "removed"}
                    else "info"
                )
                self._record_event(
                    kind=kind,
                    severity=severity,
                    message=(
                        f"{record.display_name or record.command or 'run'} "
                        f"{outcome}"
                    ),
                    run_id=run_id,
                    task_id=str(record.identity.get("queue_task_id") or "") or None,
                    details={
                        "domain": record.domain,
                        "run_type": record.run_type,
                        "command": record.command,
                        "branch": record.git.get("branch"),
                        "status": status,
                        "terminal_outcome": record.terminal_outcome,
                    },
                )

        self._lifecycle_seen_states = current

    def detail(self, run_id: str, filters: MissionControlFilters | None = None) -> dict[str, Any]:
        detail = self._detail_view(run_id, filters)
        payload = serialize_detail(detail)
        review_packet = _review_packet(self.project_dir, detail)
        payload["review_packet"] = review_packet
        payload["verification_plan"] = _verification_plan_for_detail(detail) or _verification_plan_from_review_packet(
            detail,
            review_packet,
        )
        _apply_landing_context(self.project_dir, payload, detail)
        return payload

    def logs(
        self,
        run_id: str,
        *,
        log_index: int = 0,
        offset: int = 0,
        limit_bytes: int = 128_000,
        filters: MissionControlFilters | None = None,
    ) -> dict[str, Any]:
        detail = self._detail_view(run_id, filters)
        fallback = _queue_failure_log_fallback(self.project_dir, detail.record, offset=offset, limit_bytes=limit_bytes)
        if fallback is not None:
            return asdict(fallback)
        if not detail.log_paths:
            return asdict(LogReadResult(None, offset, offset, "", False, total_bytes=0, eof=True))
        index = min(max(log_index, 0), len(detail.log_paths) - 1)
        path = self._validated_artifact_path(detail.log_paths[index])
        return asdict(self._read_file_slice(path, offset=max(0, offset), limit_bytes=limit_bytes))

    def artifacts(self, run_id: str, filters: MissionControlFilters | None = None) -> dict[str, Any]:
        detail = self._detail_view(run_id, filters)
        return {
            "run_id": detail.run_id,
            "artifacts": [serialize_artifact(artifact, index) for index, artifact in enumerate(detail.artifacts)],
        }

    def artifact_content(
        self,
        run_id: str,
        artifact_index: int,
        *,
        filters: MissionControlFilters | None = None,
        limit_bytes: int = 256_000,
    ) -> dict[str, Any]:
        """Return the artifact body, with MIME-aware binary handling.

        Cluster-evidence-trustworthiness #6: previously every artifact was
        decoded as UTF-8 with replacement and shoved into a `<pre>`, so a
        screenshot or recording rendered as several pages of replacement
        characters. We now sniff MIME before decoding and return a
        ``previewable`` flag plus ``mime_type`` / ``size_bytes`` so the
        client can render image/video previews via ``<img src=...>`` (or
        link to the raw artifact endpoint) and avoid garbage text for
        non-previewable binaries.
        """
        detail = self._detail_view(run_id, filters)
        if artifact_index < 0 or artifact_index >= len(detail.artifacts):
            raise MissionControlServiceError("artifact index out of range", status_code=404)
        artifact = detail.artifacts[artifact_index]
        path = self._validated_artifact_path(artifact.path)
        if path.is_dir():
            raise MissionControlServiceError("artifact is a directory", status_code=400)
        size_bytes = path.stat().st_size if path.exists() else 0
        mime_type = _detect_mime_type(path)
        is_text = _looks_like_text(path, mime_type=mime_type)
        artifact_payload = serialize_artifact(artifact, artifact_index)
        if not is_text:
            # Binary: don't ship a decoded body — give the client a download
            # link via the existing artifacts endpoint and let it render an
            # image/video preview when the MIME is one we can inline.
            return {
                "artifact": artifact_payload,
                "content": "",
                "truncated": False,
                "previewable": False,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
            }
        read = self._read_file_slice(path, offset=0, limit_bytes=limit_bytes)
        return {
            "artifact": artifact_payload,
            "content": read.text,
            "truncated": read.next_offset < size_bytes if size_bytes else False,
            "previewable": True,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
        }

    def artifact_raw_path(
        self,
        run_id: str,
        artifact_index: int,
        *,
        filters: MissionControlFilters | None = None,
    ) -> tuple[Path, str]:
        """Return the on-disk path + MIME for an artifact (for binary serving).

        Cluster-evidence-trustworthiness #6: binary artifacts (PNG, WEBM,
        PDF) need a real file response so the client can ``<img src=...>``
        them instead of decoding the bytes through JSON. The path is
        sandbox-validated through ``_validated_artifact_path`` exactly
        like ``artifact_content`` does.
        """
        detail = self._detail_view(run_id, filters)
        if artifact_index < 0 or artifact_index >= len(detail.artifacts):
            raise MissionControlServiceError("artifact index out of range", status_code=404)
        artifact = detail.artifacts[artifact_index]
        path = self._validated_artifact_path(artifact.path)
        if not path.exists() or path.is_dir():
            raise MissionControlServiceError("artifact not found", status_code=404)
        return path, _detect_mime_type(path)

    def proof_report_path(
        self,
        run_id: str,
        *,
        filters: MissionControlFilters | None = None,
    ) -> Path:
        detail = self._detail_view(run_id, filters)
        report = _proof_report_info(self.project_dir, detail.record)
        html_path = _optional_str(report.get("html_path"))
        if not html_path:
            raise MissionControlServiceError("proof-of-work HTML report not found", status_code=404)
        path = self._validated_artifact_path(html_path)
        if not path.exists() or path.is_dir():
            raise MissionControlServiceError("proof-of-work HTML report not found", status_code=404)
        return path

    def proof_report_html(
        self,
        run_id: str,
        *,
        filters: MissionControlFilters | None = None,
    ) -> str:
        path = self.proof_report_path(run_id, filters=filters)
        html = path.read_text(encoding="utf-8", errors="replace")
        return _rewrite_proof_report_links(html, run_id)

    def proof_report_asset_path(
        self,
        run_id: str,
        asset_path: str,
        *,
        filters: MissionControlFilters | None = None,
    ) -> Path:
        detail = self._detail_view(run_id, filters)
        report = _proof_report_info(self.project_dir, detail.record)
        html_path_text = _optional_str(report.get("html_path"))
        if not html_path_text:
            raise MissionControlServiceError("proof-of-work HTML report not found", status_code=404)
        html_path = self._validated_artifact_path(html_path_text)
        decoded = unquote(str(asset_path or "")).strip()
        if not decoded or Path(decoded).is_absolute():
            raise MissionControlServiceError("proof-report asset path is invalid", status_code=400)
        candidate = (html_path.parent / decoded).resolve(strict=False)
        root = _proof_report_asset_root(detail.record, html_path)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise MissionControlServiceError("proof-report asset path is outside the session", status_code=403) from exc
        if not candidate.exists() or not candidate.is_file():
            raise MissionControlServiceError("proof-report asset not found", status_code=404)
        return candidate

    def diff(
        self,
        run_id: str,
        *,
        filters: MissionControlFilters | None = None,
        limit_chars: int = 240_000,
    ) -> dict[str, Any]:
        detail = self._detail_view(run_id, filters)
        target = _review_target(self.project_dir, detail.record)
        branch = _optional_str(detail.record.git.get("branch"))
        merge_info = _detail_merge_info(self.project_dir, detail)
        diff = (
            _merged_task_diff(self.project_dir, merge_info)
            if merge_info is not None
            else _branch_diff(self.project_dir, branch, target)
        )
        text = ""
        full_text = ""
        truncated = False
        command = (
            _optional_str(diff.get("command"))
            or (f"git diff {target}...{branch}" if branch and branch != target else None)
        )
        if diff["error"] is None and command:
            text_result = (
                _merged_task_diff_text(self.project_dir, merge_info)
                if merge_info is not None
                else _branch_diff_text(self.project_dir, branch, target)
            )
            if text_result["error"] is not None:
                diff = {**diff, "error": text_result["error"]}
            else:
                full_text = str(text_result["text"])
                if len(full_text) > limit_chars:
                    text = full_text[:limit_chars]
                    truncated = True
                else:
                    text = full_text
        # Freshness metadata: SHAs are captured at fetch time so the merge
        # action can later validate that nothing has moved underneath the
        # operator. ``errors`` records *which* lookup failed so the UI can
        # surface a targeted warning instead of a vague "diff unavailable".
        target_sha, branch_sha, merge_base, sha_errors = _diff_freshness_shas(
            self.project_dir,
            branch=branch,
            target=target,
            merge_info=merge_info,
        )
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        full_size_chars = len(full_text)
        shown_hunks = text.count("\n@@ ") + (1 if text.startswith("@@ ") else 0)
        total_hunks = full_text.count("\n@@ ") + (1 if full_text.startswith("@@ ") else 0)
        return {
            "run_id": detail.run_id,
            "branch": branch,
            "target": target,
            "command": command,
            "files": diff["files"],
            "file_count": len(diff["files"]),
            "text": text,
            "error": diff["error"],
            "truncated": truncated,
            "fetched_at": fetched_at,
            "target_sha": target_sha,
            "branch_sha": branch_sha,
            "merge_base": merge_base,
            "limit_chars": limit_chars,
            "full_size_chars": full_size_chars,
            "shown_hunks": shown_hunks,
            "total_hunks": total_hunks,
            "errors": sha_errors,
        }

    def execute(
        self,
        run_id: str,
        action: str,
        *,
        selected_queue_task_ids: list[str] | None = None,
        artifact_index: int | None = None,
        action_payload: dict[str, Any] | None = None,
        expected_target_sha: str | None = None,
        expected_branch_sha: str | None = None,
        filters: MissionControlFilters | None = None,
    ) -> dict[str, Any]:
        key = _action_key(action)
        detail = self._detail_view(run_id, filters)
        legal = {item.key: item for item in detail.legal_actions}
        if key not in legal:
            raise MissionControlServiceError("action unavailable", status_code=404)
        if not legal[key].enabled:
            reason = legal[key].reason or "action disabled"
            raise MissionControlServiceError(reason, status_code=409)
        if key == "m":
            merge_info = _detail_merge_info(self.project_dir, detail)
            if merge_info is not None:
                target = str(merge_info.get("target") or _merge_target(self.project_dir))
                raise MissionControlServiceError(f"Already merged into {target}.", status_code=409)
            self._validate_expected_diff_shas(
                detail,
                expected_target_sha=expected_target_sha,
                expected_branch_sha=expected_branch_sha,
            )
            _ensure_merge_unblocked(self.project_dir)

        selected_artifact_path = None
        if key == "e":
            index = 0 if artifact_index is None else artifact_index
            if index < 0 or index >= len(detail.artifacts):
                raise MissionControlServiceError("artifact index out of range", status_code=404)
            selected_artifact_path = str(self._validated_artifact_path(detail.artifacts[index].path))

        event_action = _event_action_name(key, label=legal[key].label, domain=detail.record.domain)
        result = execute_action(
            detail.record,
            key,
            self.project_dir,
            selected_artifact_path=selected_artifact_path,
            selected_queue_task_ids=selected_queue_task_ids,
            action_payload=action_payload or {},
            post_result=lambda item: self._record_async_action_result(
                kind=f"run.{event_action}.completed",
                result=item,
                run_id=detail.run_id,
                task_id=_optional_str(detail.record.identity.get("queue_task_id")),
                details={"action": event_action, "display_status": detail.record.status},
            ),
        )
        payload = serialize_action_result(result)
        self._record_event(
            kind=f"run.{event_action}",
            severity=_event_severity(payload),
            message=payload.get("message") or f"{event_action} requested",
            run_id=detail.run_id,
            task_id=_optional_str(detail.record.identity.get("queue_task_id")),
            details={
                "ok": payload.get("ok"),
                "action": event_action,
                "display_status": detail.record.status,
            },
        )
        return payload

    def _validate_expected_diff_shas(
        self,
        detail: DetailView,
        *,
        expected_target_sha: str | None,
        expected_branch_sha: str | None,
    ) -> None:
        """Reject the merge if the live SHAs disagree with what the operator reviewed.

        Called only on the merge action and only when the client sends
        ``expected_target_sha`` / ``expected_branch_sha`` from its most-recent
        diff fetch. The intent is the safety hatch for the diff-freshness
        contract: if the target branch has moved or the audit branch has
        been amended since the diff snapshot, the operator is shown a 409
        explaining what changed and asked to refetch the diff. Power users
        who want to skip the gate simply omit the SHAs from their POST.
        """
        expected_target = (expected_target_sha or "").strip().lower() or None
        expected_branch = (expected_branch_sha or "").strip().lower() or None
        if not expected_target and not expected_branch:
            return
        target = _review_target(self.project_dir, detail.record)
        branch = _optional_str(detail.record.git.get("branch"))
        # Resolve the *current* refs to compare against the snapshot.
        current_target_sha, target_err = _resolve_sha(self.project_dir, target) if target else (None, "target ref is empty")
        current_branch_sha: str | None = None
        branch_err: str | None = None
        if branch and branch != target:
            current_branch_sha, branch_err = _resolve_sha(self.project_dir, branch)
        if expected_target and target_err and current_target_sha is None:
            raise MissionControlServiceError(
                f"Could not resolve current target {target}: {target_err}. Re-fetch the diff and try again.",
                status_code=409,
            )
        if expected_branch and branch_err and current_branch_sha is None:
            raise MissionControlServiceError(
                f"Could not resolve current branch {branch}: {branch_err}. Re-fetch the diff and try again.",
                status_code=409,
            )
        if expected_target and current_target_sha and expected_target != current_target_sha.lower():
            raise MissionControlServiceError(
                (
                    f"Target branch {target} has moved since you reviewed the diff "
                    f"(diff was at {expected_target[:7]}, now {current_target_sha[:7]}). "
                    "Re-fetch the diff to confirm what will be merged."
                ),
                status_code=409,
            )
        if expected_branch and current_branch_sha and expected_branch != current_branch_sha.lower():
            raise MissionControlServiceError(
                (
                    f"Branch {branch} has moved since you reviewed the diff "
                    f"(diff was at {expected_branch[:7]}, now {current_branch_sha[:7]}). "
                    "Re-fetch the diff to confirm what will be merged."
                ),
                status_code=409,
            )

    def merge_all(self, *, verification_policy: str | None = "smart") -> dict[str, Any]:
        _ensure_merge_unblocked(self.project_dir)
        payload = serialize_action_result(
            execute_merge_all(
                self.project_dir,
                verification_policy=verification_policy,
                post_result=lambda item: self._record_async_action_result(
                    kind="merge.all.completed",
                    result=item,
                    details={"action": "merge-all", "verification_policy": verification_policy or "smart"},
                ),
            )
        )
        self._record_event(
            kind="merge.all",
            severity=_event_severity(payload),
            message=payload.get("message") or "merge ready tasks requested",
            details={"ok": payload.get("ok"), "verification_policy": verification_policy or "smart"},
        )
        return payload

    def merge_abort(self) -> dict[str, Any]:
        payload = serialize_action_result(execute_merge_abort(self.project_dir))
        self._record_event(
            kind="merge.abort",
            severity=_event_severity(payload),
            message=payload.get("message") or "abort merge requested",
            details={"ok": payload.get("ok")},
        )
        return payload

    def merge_recover(self) -> dict[str, Any]:
        payload = serialize_action_result(
            execute_merge_recover(
                self.project_dir,
                post_result=lambda item: self._record_async_action_result(
                    kind="merge.recover.completed",
                    result=item,
                    details={"action": "merge-recover"},
                ),
            )
        )
        self._record_event(
            kind="merge.recover",
            severity=_event_severity(payload),
            message=payload.get("message") or "landing recovery requested",
            details={"ok": payload.get("ok")},
        )
        return payload

    def resolve_release_issues(self) -> dict[str, Any]:
        landing = self.landing_status()
        recovery_needed = _landing_recovery_needed(landing)
        ready_count = int((landing.get("counts") or {}).get("ready") or 0)
        cleanup_task_ids = _superseded_failed_task_ids(landing)

        if recovery_needed:
            payload = serialize_action_result(
                execute_merge_recover(
                    self.project_dir,
                    post_result=lambda item: self._record_async_action_result(
                        kind="release.resolve.completed",
                        result=item,
                        details={"action": "merge-recover", "cleanup_candidates": cleanup_task_ids},
                    ),
                )
            )
            action = "merge-recover"
        elif bool(landing.get("merge_blocked")) and ready_count:
            blockers = "; ".join(str(item) for item in landing.get("merge_blockers") or []) or "repository is blocked"
            raise MissionControlServiceError(
                f"Release recovery is blocked by local repository state: {blockers}. "
                "Commit, stash, revert, or use Abort merge when an interrupted merge is present.",
                status_code=409,
            )
        elif ready_count:
            payload = serialize_action_result(
                execute_merge_all(
                    self.project_dir,
                    verification_policy="smart",
                    post_result=lambda item: self._record_async_action_result(
                        kind="release.resolve.completed",
                        result=item,
                        details={"action": "merge-all", "verification_policy": "smart"},
                    ),
                )
            )
            action = "merge-all"
        elif cleanup_task_ids:
            payload = serialize_action_result(
                execute_queue_cleanup(
                    self.project_dir,
                    cleanup_task_ids,
                    post_result=lambda item: self._record_async_action_result(
                        kind="release.resolve.completed",
                        result=item,
                        details={"action": "cleanup-superseded", "task_ids": cleanup_task_ids},
                    ),
                )
            )
            action = "cleanup-superseded"
        else:
            unresolved_attention = _blocked_attention_task_ids(landing)
            payload = serialize_action_result(
                ActionResult(
                    ok=not unresolved_attention,
                    message=(
                        "no safe automated release fix found; open the blocked task review packet"
                        if unresolved_attention
                        else "no release issues found"
                    ),
                    severity="warning" if unresolved_attention else "information",
                    refresh=True,
                    clear_banner=True,
                )
            )
            action = "blocked-review-needed" if unresolved_attention else "noop"

        self._record_event(
            kind="release.resolve",
            severity=_event_severity(payload),
            message=payload.get("message") or "release issue resolution requested",
            details={
                "ok": payload.get("ok"),
                "action": action,
                "ready_count": ready_count,
                "cleanup_candidates": cleanup_task_ids,
                "blocked_attention": _blocked_attention_task_ids(landing),
                "merge_blocked": bool(landing.get("merge_blocked")),
            },
        )
        return payload

    def watcher_status(self, *, probe_lock: bool = True) -> dict[str, Any]:
        try:
            state = load_queue_state(self.project_dir)
        except Exception:
            state = {"watcher": None, "tasks": {}}
        try:
            tasks = load_queue(self.project_dir)
        except Exception:
            tasks = []
        task_states = state.get("tasks", {}) if isinstance(state, dict) else {}
        counts = {
            "queued": 0,
            "starting": 0,
            "initializing": 0,
            "running": 0,
            "terminating": 0,
            "interrupted": 0,
            "done": 0,
            "failed": 0,
            "cancelled": 0,
            "removed": 0,
        }
        for task in tasks:
            raw = task_states.get(task.id) if isinstance(task_states, dict) else None
            status = _queue_display_status(raw if isinstance(raw, dict) else None, state)
            counts[status] = counts.get(status, 0) + 1
        watcher = state.get("watcher") if isinstance(state, dict) else None
        health = watcher_health(self.project_dir, state if isinstance(state, dict) else {}, probe_lock=probe_lock)
        return {
            "alive": health["state"] == "running",
            "watcher": watcher if isinstance(watcher, dict) else None,
            "counts": counts,
            "health": health,
        }

    def landing_status(self) -> dict[str, Any]:
        try:
            tasks = load_queue(self.project_dir)
        except Exception:
            tasks = []
        try:
            state = load_queue_state(self.project_dir)
        except Exception:
            state = {"tasks": {}}
        task_states = state.get("tasks", {}) if isinstance(state, dict) else {}
        target = _merge_target(self.project_dir)
        merged_by_branch = _merged_branch_index(self.project_dir, target)
        preflight = _merge_preflight(self.project_dir)

        items: list[dict[str, Any]] = []
        ready_tasks: list[Any] = []
        counts = _empty_landing_counts()
        for task in tasks:
            raw_state = task_states.get(task.id) if isinstance(task_states, dict) else None
            queue_status = _queue_display_status(raw_state if isinstance(raw_state, dict) else None, state)
            branch = str(task.branch or "").strip()
            certification_only = _is_certification_only_task(task)
            merge_info = merged_by_branch.get(branch)
            diff = _landing_task_diff(
                self.project_dir,
                target=target,
                branch=branch,
                queue_status=queue_status,
                merge_info=merge_info,
                certification_only=certification_only,
            )
            classification = _classify_landing_task(
                queue_status=queue_status,
                branch=branch,
                diff=diff,
                merge_info=merge_info,
                certification_only=certification_only,
            )
            counts[classification.count_key] += 1
            if classification.counts_for_collision:
                ready_tasks.append(task)

            counts["total"] += 1
            item = {
                "task_id": task.id,
                "run_id": _task_run_id(raw_state),
                "branch": branch or None,
                "worktree": task.worktree,
                "summary": task.resolved_intent or _task_intent(task.command_argv),
                "build_config": run_config_from_argv(self.project_dir, task.command_argv),
                "queue_status": queue_status,
                "landing_state": classification.state,
                "label": classification.label,
                "merge_id": merge_info.get("merge_id") if merge_info else None,
                "merge_status": merge_info.get("status") if merge_info else None,
                "merge_run_status": merge_info.get("merge_run_status") if merge_info else None,
                "duration_s": _number_from_mapping(raw_state, "duration_s"),
                "cost_usd": _number_from_mapping(raw_state, "cost_usd"),
                "token_usage": _token_usage_from_mapping(raw_state) if isinstance(raw_state, dict) else {},
                "stories_passed": _number_from_mapping(raw_state, "stories_passed"),
                "stories_tested": _number_from_mapping(raw_state, "stories_tested"),
            }
            item["changed_file_count"] = len(diff["files"])
            item["changed_files"] = diff["files"][:8]
            item["diff_error"] = diff["error"]
            items.append(item)

        return {
            "target": target,
            "items": items,
            "counts": counts,
            "collisions": _landing_collisions(self.project_dir, ready_tasks, target),
            **preflight,
        }

    def runtime_status(
        self,
        *,
        watcher: dict[str, Any] | None = None,
        landing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_runtime_status(
            self.project_dir,
            watcher=watcher or self.watcher_status(),
            landing=landing or self.landing_status(),
        )

    def events(self, *, limit: int = 80) -> dict[str, Any]:
        return events_status(self.project_dir, limit=limit)

    def start_watcher(self, *, concurrent: int | None = None, exit_when_empty: bool = False) -> dict[str, Any]:
        status = self.watcher_status()
        if status["alive"]:
            payload = {"ok": True, "message": "queue runner already running", "refresh": True, "watcher": status}
            self._record_event(
                kind="watcher.start.skipped",
                severity="info",
                message=payload["message"],
                details={"state": status.get("health", {}).get("state")},
            )
            return payload
        health = status.get("health") if isinstance(status.get("health"), dict) else {}
        if health.get("state") != "stopped":
            message = str(health.get("next_action") or "Stop the stale queue runner before starting another one.")
            self._record_event(
                kind="watcher.start.blocked",
                severity="warning",
                message=message,
                details={"state": health.get("state"), "blocking_pid": health.get("blocking_pid")},
            )
            raise MissionControlServiceError(message, status_code=409)
        try:
            default_concurrent = runner_config_from_otto_config(load_config(self.project_dir / "otto.yaml")).concurrent
        except (ConfigError, ValueError) as exc:
            raise MissionControlServiceError(str(exc), status_code=400) from exc
        concurrent_value = max(1, int(concurrent if concurrent is not None else default_concurrent))
        argv = [
            *_otto_cli_argv("queue", "run", "--no-dashboard"),
            "--concurrent",
            str(concurrent_value),
        ]
        if exit_when_empty:
            argv.append("--exit-when-empty")
        log_path = paths.logs_dir(self.project_dir) / "web" / "watcher.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] starting {' '.join(argv)}\n")
                proc = subprocess.Popen(
                    argv,
                    cwd=str(self.project_dir),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                    start_new_session=True,
                )
                supervisor = self._record_watcher_launch(
                    watcher_pid=proc.pid,
                    argv=argv,
                    log_path=log_path,
                    concurrent=concurrent_value,
                    exit_when_empty=exit_when_empty,
                )
        except OSError as exc:
            self._record_event(
                kind="watcher.start.failed",
                severity="error",
                message=f"watcher failed to start: {exc}",
                details={"argv": argv, "log_path": str(log_path)},
            )
            raise MissionControlServiceError(f"watcher failed to start: {exc}", status_code=500) from exc

        for _ in range(20):
            if proc.poll() is not None:
                tail = _tail_text(log_path)
                self._record_event(
                    kind="watcher.start.failed",
                    severity="error",
                    message=f"watcher exited immediately with {proc.returncode}",
                    details={"pid": proc.pid, "returncode": proc.returncode, "tail": tail, "log_path": str(log_path)},
                )
                raise MissionControlServiceError(
                    f"watcher exited immediately with {proc.returncode}: {tail}",
                    status_code=500,
                )
            fresh = self.watcher_status(probe_lock=False)
            if fresh["alive"]:
                payload = {
                    "ok": True,
                    "message": "watcher started",
                    "refresh": True,
                    "watcher": fresh,
                    "log_path": str(log_path),
                    "pid": proc.pid,
                    "supervisor": supervisor,
                }
                self._record_event(
                    kind="watcher.started",
                    severity="success",
                    message=payload["message"],
                    details={"pid": proc.pid, "concurrent": concurrent_value, "log_path": str(log_path)},
                )
                return payload
            time.sleep(0.1)
        payload = {
            "ok": True,
            "message": "watcher launch requested",
            "refresh": True,
            "watcher": self.watcher_status(),
            "log_path": str(log_path),
            "pid": proc.pid,
            "supervisor": supervisor,
        }
        self._record_event(
            kind="watcher.launch.requested",
            severity="info",
            message=payload["message"],
            details={"pid": proc.pid, "concurrent": concurrent_value, "log_path": str(log_path)},
        )
        return payload

    def stop_watcher(self) -> dict[str, Any]:
        status = self.watcher_status()
        watcher = status.get("watcher")
        raw_health = status.get("health")
        health = raw_health if isinstance(raw_health, dict) else {}
        pid = health.get("blocking_pid")
        if not health and (not isinstance(pid, int) or pid <= 0) and isinstance(watcher, dict):
            pid = watcher.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            payload = {"ok": True, "message": "watcher is not running", "refresh": True, "watcher": status}
            self._record_event(
                kind="watcher.stop.skipped",
                severity="info",
                message=payload["message"],
                details={"state": health.get("state")},
            )
            return payload
        identity_issue = _watcher_stop_identity_issue(self.project_dir, pid, health)
        if identity_issue is not None:
            self._record_event(
                kind="watcher.stop.blocked",
                severity="error",
                message=identity_issue,
                details={"pid": pid, "state": health.get("state")},
            )
            raise MissionControlServiceError(identity_issue, status_code=409)
        message = "stale watcher stop requested" if health.get("state") == "stale" else "watcher stop requested"
        termination = terminate_watcher_blocking(
            self.project_dir,
            grace=3.0,
            reason=message,
            fallback_pid=pid,
        )
        if termination.get("error"):
            self._record_event(
                kind="watcher.stop.failed",
                severity="error",
                message=str(termination["error"]),
                details={"pid": pid, "state": health.get("state"), "termination": termination},
            )
            raise MissionControlServiceError(str(termination["error"]), status_code=500)
        supervisor = self._record_watcher_stop(target_pid=pid, reason=message)
        payload = {"ok": True, "message": message, "refresh": True, "watcher": self.watcher_status()}
        if supervisor is not None:
            payload["supervisor"] = supervisor
        payload["termination"] = termination
        self._record_event(
            kind="watcher.stop.requested",
            severity="warning" if health.get("state") == "stale" else "info",
            message=message,
            details={"pid": pid, "state": health.get("state"), "termination": termination},
        )
        return payload

    def enqueue(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        command = command.strip().lower()
        explicit_as = _optional_str(payload.get("as") or payload.get("task_id"))
        after = _string_list(payload.get("after"))
        extra_args = _string_list(payload.get("extra_args"))

        try:
            if command == "build":
                intent = _required_str(payload.get("intent"), "intent")
                extra_args = _normalize_web_build_spec_args(extra_args)
                raw_args = [intent, *extra_args]
                _validate_inner_command_args("build", raw_args)
                result = enqueue_task(
                    self.project_dir,
                    command="build",
                    raw_args=raw_args,
                    intent=intent,
                    explicit_intent=intent,
                    after=after,
                    explicit_as=explicit_as,
                    resumable=True,
                )
            elif command == "improve":
                subcommand = _required_str(payload.get("subcommand"), "subcommand")
                if subcommand not in {"bugs", "feature", "target"}:
                    raise MissionControlServiceError("unsupported improve subcommand", status_code=400)
                focus_or_goal = _optional_str(payload.get("focus") or payload.get("goal"))
                # W3-CRITICAL-1: improve must iterate on a prior run's branch,
                # not fork from main and re-collide on the same files. The web
                # JobDialog selects a prior run and posts its run id; we
                # resolve that to a branch ref and pass it as the worktree's
                # base_ref so the new improve branch is rooted on the prior
                # build's tip. Falls back to git's default (HEAD/main) when
                # the operator submits without selecting a prior run, which
                # preserves backwards compat for projects with no history.
                prior_run_id = _optional_str(payload.get("prior_run_id"))
                base_ref = self._resolve_prior_run_branch(prior_run_id) if prior_run_id else None
                raw_args = [subcommand]
                if focus_or_goal:
                    raw_args.append(focus_or_goal)
                raw_args.extend(extra_args)
                _validate_inner_command_args("improve", raw_args)
                snapshot_intent = resolve_intent_for_enqueue(self.project_dir)
                result = enqueue_task(
                    self.project_dir,
                    command="improve",
                    raw_args=raw_args,
                    intent=snapshot_intent,
                    explicit_intent=focus_or_goal,
                    after=after,
                    explicit_as=explicit_as,
                    resumable=True,
                    focus=focus_or_goal if subcommand in {"bugs", "feature"} else None,
                    target=focus_or_goal if subcommand == "target" else None,
                    base_ref=base_ref,
                )
            elif command == "certify":
                intent = _optional_str(payload.get("intent"))
                resolved = resolve_intent_for_enqueue(self.project_dir, explicit=intent)
                raw_args = [intent] if intent else []
                raw_args.extend(extra_args)
                _validate_inner_command_args("certify", raw_args)
                result = enqueue_task(
                    self.project_dir,
                    command="certify",
                    raw_args=raw_args,
                    intent=resolved,
                    explicit_intent=intent,
                    after=after,
                    explicit_as=explicit_as,
                    resumable=False,
                )
            else:
                raise MissionControlServiceError("unsupported queue command", status_code=404)
        except ValueError as exc:
            raise MissionControlServiceError(str(exc), status_code=400) from exc

        response = {
            "ok": True,
            "message": f"queued {result.task.id}",
            "task": asdict(result.task),
            "warnings": result.warnings,
            "refresh": True,
        }
        self._record_event(
            kind=f"queue.{command}",
            severity="warning" if result.warnings else "success",
            message=response["message"],
            task_id=result.task.id,
            details={
                "command": command,
                "branch": result.task.branch,
                "worktree": result.task.worktree,
                "after": result.task.after,
                "warnings": result.warnings,
            },
        )
        return response

    def _resolve_prior_run_branch(self, prior_run_id: str) -> str:
        """Look up the branch for a prior run id.

        Searches live records first (handles "still-warm" runs that haven't
        been GC'd yet), then falls back to history rows. Raises a 400 if the
        run isn't found or doesn't have a recorded branch — the operator
        explicitly selected this run, so a silent fallback to main would
        re-open W3-CRITICAL-1.
        """
        run_id = prior_run_id.strip()
        if not run_id:
            raise MissionControlServiceError("prior_run_id is empty", status_code=400)
        # Live records are the freshest source — if the prior build just
        # finished its branch is already recorded there with the writer's tip.
        try:
            from otto.runs.registry import read_live_records
            for record in read_live_records(self.project_dir):
                if record.run_id == run_id:
                    branch = str((record.git or {}).get("branch") or "").strip()
                    if branch:
                        return branch
                    raise MissionControlServiceError(
                        f"prior run {run_id!r} has no recorded branch", status_code=400
                    )
        except MissionControlServiceError:
            raise
        except Exception:  # pragma: no cover — defensive; fall through to history
            pass
        # History fallback — completed runs that have aged out of the live
        # registry still appear in cross-sessions/history.jsonl.
        try:
            from otto.runs.history import load_project_history_rows
            for row in load_project_history_rows(self.project_dir):
                row_run_id = str(row.get("run_id") or "").strip()
                if row_run_id != run_id:
                    continue
                branch = str((row.get("git") or {}).get("branch") or row.get("branch") or "").strip()
                if branch:
                    return branch
                raise MissionControlServiceError(
                    f"prior run {run_id!r} has no recorded branch", status_code=400
                )
        except MissionControlServiceError:
            raise
        except Exception:  # pragma: no cover — defensive
            pass
        raise MissionControlServiceError(
            f"prior run {run_id!r} not found in live or history records", status_code=404
        )

    def _state(self, filters: MissionControlFilters | None) -> MissionControlState:
        return self.model.initial_state(filters=filters or MissionControlFilters())

    def _detail_view(self, run_id: str, filters: MissionControlFilters | None) -> DetailView:
        del filters
        detail = self.model.detail_view_for_run_id(run_id)
        if detail is None:
            raise MissionControlServiceError("run not found", status_code=404)
        return detail

    def _validated_artifact_path(self, path: str) -> Path:
        raw_candidate = Path(path).expanduser()
        candidate = (raw_candidate if raw_candidate.is_absolute() else self.project_dir / raw_candidate).resolve(strict=False)
        root = self.project_dir.resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise MissionControlServiceError("artifact path is outside the project", status_code=403) from exc
        return candidate

    def _read_file_slice(self, path: Path, *, offset: int, limit_bytes: int) -> LogReadResult:
        if not path.exists() or not path.is_file():
            return LogReadResult(str(path), offset, offset, "", False, total_bytes=0, eof=True)
        total_bytes = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(max(1, limit_bytes))
            next_offset = handle.tell()
        return LogReadResult(
            path=str(path),
            offset=offset,
            next_offset=next_offset,
            text=chunk.decode("utf-8", errors="replace"),
            exists=True,
            total_bytes=total_bytes,
            eof=next_offset >= total_bytes,
        )

    def _record_event(
        self,
        *,
        kind: str,
        message: str,
        severity: str = "info",
        run_id: str | None = None,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            append_event(
                self.project_dir,
                kind=kind,
                message=message,
                severity=severity,
                run_id=run_id,
                task_id=task_id,
                details=details,
            )
        except Exception as exc:
            LOGGER.warning("mission control event write failed: %s", exc)
            return

    def _record_async_action_result(
        self,
        *,
        kind: str,
        result: Any,
        run_id: str | None = None,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = serialize_action_result(result)
        merged_details = dict(details or {})
        merged_details["ok"] = payload.get("ok")
        self._record_event(
            kind=kind,
            severity=_event_severity(payload),
            message=payload.get("message") or kind,
            run_id=run_id,
            task_id=task_id,
            details=merged_details,
        )

    def _record_watcher_launch(
        self,
        *,
        watcher_pid: int,
        argv: list[str],
        log_path: Path,
        concurrent: int,
        exit_when_empty: bool,
    ) -> dict[str, Any] | None:
        try:
            return record_watcher_launch(
                self.project_dir,
                watcher_pid=watcher_pid,
                argv=argv,
                log_path=log_path,
                concurrent=concurrent,
                exit_when_empty=exit_when_empty,
            )
        except Exception as exc:
            self._record_event(
                kind="supervisor.write.failed",
                severity="warning",
                message=f"watcher supervisor metadata was not written: {exc}",
            )
            return None

    def _record_watcher_stop(self, *, target_pid: int, reason: str) -> dict[str, Any] | None:
        try:
            return record_watcher_stop(self.project_dir, target_pid=target_pid, reason=reason)
        except Exception as exc:
            self._record_event(
                kind="supervisor.write.failed",
                severity="warning",
                message=f"watcher supervisor stop metadata was not written: {exc}",
            )
            return None


def filters_from_params(
    *,
    active_only: bool = False,
    type_filter: str = "all",
    outcome_filter: str = "all",
    query: str = "",
    history_page: int = 0,
    history_page_size: int | None = None,
) -> MissionControlFilters:
    if type_filter not in {"all", "build", "improve", "certify", "merge", "queue"}:
        raise MissionControlServiceError("invalid type filter", status_code=400)
    if outcome_filter not in {"all", "success", "failed", "interrupted", "cancelled", "removed", "other"}:
        raise MissionControlServiceError("invalid outcome filter", status_code=400)
    # Whitelist the page-size choices so a malformed/hostile URL cannot ask
    # for an unbounded slice. Any other value falls back to the model
    # default. Mirrors the front-end <select> options.
    normalized_page_size: int | None = None
    if history_page_size is not None:
        try:
            candidate = int(history_page_size)
        except (TypeError, ValueError):
            candidate = 0
        if candidate in {10, 25, 50, 100}:
            normalized_page_size = candidate
    return MissionControlFilters(
        active_only=bool(active_only),
        type_filter=type_filter,  # type: ignore[arg-type]
        outcome_filter=outcome_filter,  # type: ignore[arg-type]
        query=str(query or ""),
        history_page=max(0, int(history_page or 0)),
        history_page_size=normalized_page_size,
    )


def _review_packet(project_dir: Path, detail: DetailView) -> dict[str, Any]:
    record = detail.record
    display_status = "stale" if detail.overlay is not None and detail.overlay.level == "stale" else record.status
    target = _review_target(project_dir, record)
    if record.domain == "merge":
        return _merge_review_packet(project_dir, detail, display_status=display_status, target=target)
    branch = _optional_str(record.git.get("branch"))
    merge_info = _detail_merge_info(project_dir, detail)
    merged = merge_info is not None
    in_progress = display_status in REVIEW_IN_PROGRESS_STATUSES
    certification_only = _is_certification_only_run(record)
    diff = _merged_task_diff(project_dir, merge_info) if merged else (
        {"files": [], "error": None}
        if in_progress
        else _branch_diff(project_dir, branch, target)
    )
    changed_files = diff["files"]
    certification = _certification_summary(project_dir, record)
    evidence = [serialize_artifact(artifact, index) for index, artifact in enumerate(detail.artifacts)]
    merge_preflight = _merge_preflight(project_dir)
    failure = _failure_summary(project_dir, record, detail.overlay)
    spec_review_pending = _spec_review_pending(record)
    readiness = _review_readiness(
        display_status=display_status,
        merged=merged,
        branch=branch,
        diff_error=diff["error"],
        target=target,
        overlay=detail.overlay,
        merge_preflight=merge_preflight,
        failure=failure,
        spec_review_pending=spec_review_pending,
        certification_only=certification_only,
    )
    next_action = (
        {"label": "No action", "action_key": None, "enabled": False, "reason": f"Already merged into {target}."}
        if merged
        else {
            "label": "No merge action",
            "action_key": None,
            "enabled": False,
            "reason": "Certification-only runs produce proof and do not land code.",
        }
        if certification_only and display_status == "done"
        else _suggested_next_action(display_status, detail.legal_actions, detail.overlay)
    )
    if not merged and next_action.get("action_key") == "m" and readiness.get("state") != "ready":
        reason = (
            "Commit, stash, or revert local project changes before landing."
            if display_status == "done" and merge_preflight.get("merge_blocked")
            else "Resolve review blockers before landing."
        )
        next_action = {
            "label": "Land blocked",
            "action_key": None,
            "enabled": False,
            "reason": reason,
        }
    return {
        "headline": _review_packet_headline(record, display_status, merged=merged, readiness=readiness, target=target),
        "status": "merged" if merged else display_status,
        "summary": _optional_str(record.intent.get("summary")) or record.display_name or record.run_id,
        "readiness": readiness,
        "checks": _review_checks(
            display_status=display_status,
            merged=merged,
            branch=branch,
            target=target,
            diff=diff,
            certification=certification,
            evidence=evidence,
            readiness=readiness,
            failure=failure,
            spec_review_pending=spec_review_pending,
            certification_only=certification_only,
        ),
        "next_action": next_action,
        "certification": certification,
        "changes": {
            "branch": branch,
            "target": target,
            "merged": merged,
            "merge_id": merge_info.get("merge_id") if merge_info else None,
            "file_count": len(changed_files),
            "files": changed_files[:12],
            "truncated": len(changed_files) > 12,
            "diff_command": (
                _optional_str(diff.get("command"))
                if merged
                else None if in_progress else f"git diff {target}...{branch}" if branch and branch != target else None
            ),
            "diff_error": diff["error"],
        },
        "evidence": evidence,
        "failure": failure,
        "product_handoff": _product_handoff(
            project_dir,
            record,
            merged=merged,
            certification=certification,
            changed_files=changed_files,
        ),
    }


def _merge_review_packet(project_dir: Path, detail: DetailView, *, display_status: str, target: str) -> dict[str, Any]:
    record = detail.record
    merge_id = _optional_str(record.identity.get("merge_id")) or record.run_id
    certification = _certification_summary(project_dir, record)
    evidence = [serialize_artifact(artifact, index) for index, artifact in enumerate(detail.artifacts)]
    failure = _failure_summary(project_dir, record, detail.overlay)
    terminal_success = display_status == "done" or record.terminal_outcome == "success"
    needs_attention = display_status in {"failed", "cancelled", "interrupted", "stale"}
    if terminal_success:
        readiness = {
            "state": "merged",
            "label": f"Landed in {target}",
            "tone": "success",
            "blockers": [],
            "next_step": "Audit the landing record, artifacts, and final logs if needed.",
        }
        headline = f"Landed in {target}"
    elif display_status in REVIEW_IN_PROGRESS_STATUSES:
        readiness = {
            "state": "in_progress",
            "label": "Landing in progress",
            "tone": "info",
            "blockers": ["Wait for the landing run to finish."],
            "next_step": "Watch logs or wait for completion.",
        }
        headline = "Landing in progress"
    elif needs_attention:
        reason = detail.overlay.reason if detail.overlay is not None else f"Landing status is {display_status}."
        readiness = {
            "state": "needs_attention",
            "label": "Landing needs action",
            "tone": "danger",
            "blockers": [reason],
            "next_step": "Inspect merge logs and resolve the landing failure.",
        }
        headline = "Landing failed"
    else:
        readiness = {
            "state": "blocked",
            "label": "Landing audit",
            "tone": "warning",
            "blockers": [f"Landing status is {display_status or 'unknown'}."],
            "next_step": "Inspect merge logs before taking further action.",
        }
        headline = "Landing audit"
    return {
        "headline": headline,
        "status": "merged" if terminal_success else display_status,
        "summary": _optional_str(record.intent.get("summary")) or record.display_name or record.run_id,
        "readiness": readiness,
        "checks": _merge_review_checks(
            display_status=display_status,
            target=target,
            certification=certification,
            evidence=evidence,
            readiness=readiness,
        ),
        "next_action": {"label": "No action", "action_key": None, "enabled": False, "reason": "Landing runs are audit records."},
        "certification": certification,
        "changes": {
            "branch": _optional_str(record.git.get("branch")),
            "target": target,
            "merged": terminal_success,
            "merge_id": merge_id,
            "file_count": 0,
            "files": [],
            "truncated": False,
            "diff_command": None,
            "diff_error": None,
        },
        "evidence": evidence,
        "failure": failure,
        "product_handoff": _product_handoff(
            project_dir,
            record,
            merged=terminal_success,
            certification=certification,
            changed_files=[],
        ),
    }


def _review_target(project_dir: Path, record: Any) -> str:
    target = _optional_str(record.git.get("target_branch")) if hasattr(record, "git") else None
    if target:
        return target
    if getattr(record, "domain", None) == "merge":
        merge_id = _optional_str(record.identity.get("merge_id")) if hasattr(record, "identity") else None
        merge_id = merge_id or _optional_str(getattr(record, "run_id", None))
        if merge_id:
            try:
                state = load_merge_state(project_dir, merge_id)
            except Exception:
                state = None
            if state is not None:
                state_target = _optional_str(state.target)
                if state_target:
                    return state_target
    return _merge_target(project_dir)


def _is_certification_only_run(record: Any) -> bool:
    """True when a run's primary purpose is proof, not code production."""
    return _record_command_family(record) == "certify"


def _queue_task_command_family(task: Any) -> str:
    argv = getattr(task, "command_argv", None)
    if isinstance(argv, list) and argv:
        first = str(argv[0] or "").strip().lower()
        if first in {"build", "improve", "certify", "cert", "merge", "land"}:
            return "certify" if first == "cert" else "merge" if first == "land" else first
    return ""


def _is_certification_only_task(task: Any) -> bool:
    return _queue_task_command_family(task) == "certify"


def _merge_review_checks(
    *,
    display_status: str,
    target: str,
    certification: dict[str, Any],
    evidence: list[dict[str, Any]],
    readiness: dict[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    terminal_success = readiness["state"] == "merged"
    in_progress = display_status in REVIEW_IN_PROGRESS_STATUSES
    incomplete_terminal = display_status in {"interrupted", "cancelled", "stale"}
    if terminal_success:
        checks.append(_review_check("run", "Landing run", "pass", f"Landing completed into {target}."))
    elif in_progress:
        checks.append(_review_check("run", "Landing run", "pending", "Landing is still in flight."))
    elif incomplete_terminal:
        detail = "; ".join(str(item) for item in readiness.get("blockers", []) if item) or f"Landing status is {display_status}."
        checks.append(_review_check("run", "Landing run", "warn", detail))
    else:
        checks.append(_review_check("run", "Landing run", "fail", f"Landing status is {display_status or 'unknown'}."))

    stories_tested = _int_or_none(certification.get("stories_tested"))
    stories_passed = _int_or_none(certification.get("stories_passed"))
    evidence_gate = certification.get("evidence_gate") if isinstance(certification.get("evidence_gate"), dict) else {}
    evidence_gate_reason = _optional_str(evidence_gate.get("reason")) if isinstance(evidence_gate, dict) else None
    certification_passed = bool(certification.get("passed"))
    if stories_tested and stories_passed is not None and stories_passed >= stories_tested:
        if certification_passed:
            checks.append(_review_check("certification", "Post-landing certification", "pass", f"{stories_passed}/{stories_tested} stories passed."))
        else:
            detail = f"{stories_passed}/{stories_tested} stories passed, but certification proof is incomplete."
            if evidence_gate_reason:
                detail = f"{detail} {evidence_gate_reason}"
            checks.append(_review_check("certification", "Post-landing certification", "fail", detail))
    elif stories_tested and stories_passed is not None:
        checks.append(_review_check("certification", "Post-landing certification", "fail", f"{stories_passed}/{stories_tested} stories passed."))
    elif in_progress:
        checks.append(_review_check("certification", "Post-landing certification", "pending", "Certification results appear after the landing run finishes."))
    elif incomplete_terminal:
        checks.append(_review_check("certification", "Post-landing certification", "pending", "Certification did not finish because the landing run stopped."))
    else:
        checks.append(_review_check("certification", "Post-landing certification", "info", "No post-landing story count was recorded."))

    existing_evidence = [item for item in evidence if _is_review_evidence_artifact(item) and item.get("exists")]
    if in_progress:
        if existing_evidence:
            checks.append(_review_check(
                "evidence",
                "Artifacts",
                "pending",
                f"{len(existing_evidence)} file{'' if len(existing_evidence) == 1 else 's'} collected so far; final artifacts are pending.",
            ))
        else:
            checks.append(_review_check("evidence", "Artifacts", "pending", "Artifacts are available after the landing run writes them."))
    elif incomplete_terminal:
        if existing_evidence:
            checks.append(_review_check(
                "evidence",
                "Artifacts",
                "pending",
                f"{len(existing_evidence)} partial artifact{'' if len(existing_evidence) == 1 else 's'} available; final artifacts were not completed.",
            ))
        else:
            checks.append(_review_check("evidence", "Artifacts", "pending", "No final artifacts are available because the landing run did not finish."))
    elif existing_evidence:
        checks.append(_review_check("evidence", "Artifacts", "pass", f"{len(existing_evidence)} file{'' if len(existing_evidence) == 1 else 's'} attached."))
    else:
        checks.append(_review_check("evidence", "Artifacts", "warn", "No readable landing artifacts are attached."))

    if terminal_success:
        checks.append(_review_check("landing", "Landing state", "pass", "No further landing action is needed."))
    elif in_progress:
        detail = "; ".join(str(item) for item in readiness.get("blockers", []) if item) or "Landing is still in progress."
        checks.append(_review_check("landing", "Landing state", "pending", detail))
    elif incomplete_terminal:
        detail = "; ".join(str(item) for item in readiness.get("blockers", []) if item) or "Inspect merge logs and recover the landing run."
        checks.append(_review_check("landing", "Landing state", "pending", detail))
    else:
        detail = "; ".join(str(item) for item in readiness.get("blockers", []) if item) or "Landing is not complete."
        checks.append(_review_check("landing", "Landing state", "fail", detail))
    return checks


def _review_readiness(
    *,
    display_status: str,
    merged: bool,
    branch: str | None,
    diff_error: str | None,
    target: str,
    overlay: Any,
    merge_preflight: dict[str, Any],
    failure: dict[str, Any] | None = None,
    spec_review_pending: bool = False,
    certification_only: bool = False,
) -> dict[str, Any]:
    blockers: list[str] = []
    if merged:
        return {
            "state": "merged",
            "label": f"Landed in {target}",
            "tone": "success",
            "blockers": blockers,
            "next_step": "No merge action is needed.",
        }
    if display_status in REVIEW_IN_PROGRESS_STATUSES:
        label = {
            "queued": "Queued",
            "starting": "Starting",
            "initializing": "Initializing",
            "running": "Running",
            "terminating": "Stopping",
        }.get(display_status, "In progress")
        next_step = (
            "Start the queue runner when you want this queued task to run."
            if display_status == "queued"
            else "Watch logs or wait for completion."
        )
        return {
            "state": "in_progress",
            "label": label,
            "tone": "info",
            "blockers": ["Wait for the task to finish before review."],
            "next_step": next_step,
        }
    if spec_review_pending:
        return {
            "state": "needs_attention",
            "label": "Spec review required",
            "tone": "warning",
            "blockers": ["Review the generated spec before build work starts."],
            "next_step": "Open the spec artifact, then approve it or request changes.",
        }
    if display_status == "done":
        if certification_only:
            return {
                "state": "reviewed",
                "label": "Certification complete",
                "tone": "success",
                "blockers": blockers,
                "next_step": "Review the proof packet; no merge action is needed.",
            }
        if not branch:
            blockers.append("No source branch was recorded for this task.")
        if diff_error:
            blockers.append(f"Changed files could not be inspected: {diff_error}")
        if merge_preflight.get("merge_blocked"):
            blockers.append(_merge_preflight_review_blocker(merge_preflight))
        if blockers:
            return {
                "state": "blocked",
                "label": "Review blocked",
                "tone": "danger",
                "blockers": blockers,
                "next_step": "Fix the branch or repository state, then refresh.",
            }
        return {
            "state": "ready",
            "label": f"Ready to land in {target}",
            "tone": "success",
            "blockers": blockers,
            "next_step": "Review evidence and land the task.",
        }
    if display_status in {"failed", "cancelled", "interrupted", "stale"}:
        reason = (
            _optional_str(failure.get("reason")) if failure is not None else None
        ) or (overlay.reason if overlay is not None else f"Run status is {display_status}.")
        return {
            "state": "needs_attention",
            "label": "Needs action",
            "tone": "warning" if display_status in {"interrupted", "stale"} else "danger",
            "blockers": [reason],
            "next_step": "Inspect failure evidence and retry, resume, requeue, or remove.",
        }
    return {
        "state": "blocked",
        "label": "Not ready",
        "tone": "warning",
        "blockers": [f"Run status is {display_status or 'unknown'}."],
        "next_step": "Inspect the run before taking action.",
    }


def _review_checks(
    *,
    display_status: str,
    merged: bool,
    branch: str | None,
    target: str,
    diff: dict[str, Any],
    certification: dict[str, Any],
    evidence: list[dict[str, Any]],
    readiness: dict[str, Any],
    failure: dict[str, Any] | None = None,
    spec_review_pending: bool = False,
    certification_only: bool = False,
) -> list[dict[str, Any]]:
    changed_files = list(diff.get("files") or [])
    diff_error = _optional_str(diff.get("error"))
    review_evidence = [item for item in evidence if _is_review_evidence_artifact(item)]
    existing_evidence = [item for item in review_evidence if item.get("exists")]
    missing_evidence = [item for item in review_evidence if not item.get("exists")]
    stories_tested = _int_or_none(certification.get("stories_tested"))
    stories_passed = _int_or_none(certification.get("stories_passed"))
    evidence_gate = certification.get("evidence_gate") if isinstance(certification.get("evidence_gate"), dict) else {}
    evidence_gate_reason = _optional_str(evidence_gate.get("reason")) if isinstance(evidence_gate, dict) else None
    certification_passed = bool(certification.get("passed"))
    incomplete_terminal = display_status in {"interrupted", "cancelled", "stale"}

    checks: list[dict[str, Any]] = []
    if spec_review_pending:
        checks.append(_review_check("run", "Spec review", "pending", "Build is paused until the generated spec is approved or regenerated."))
    elif merged:
        checks.append(_review_check("run", "Run finished", "pass", f"Already landed in {target}."))
    elif certification_only and display_status == "done":
        checks.append(_review_check("run", "Certification finished", "pass", "Proof completed; no code landing is expected."))
    elif display_status == "done":
        checks.append(_review_check("run", "Run finished", "pass", "Task completed and is ready for human review."))
    elif display_status in REVIEW_IN_PROGRESS_STATUSES:
        run_label = "Waiting to start" if display_status == "queued" else "Run in progress"
        run_detail = (
            "The queue runner has not started this queued task yet."
            if display_status == "queued"
            else "Task is still in flight."
        )
        checks.append(_review_check("run", run_label, "pending", run_detail))
    elif incomplete_terminal:
        reason = (_optional_str(failure.get("reason")) if failure is not None else None) or f"Run status is {display_status}."
        checks.append(_review_check("run", "Run interrupted", "warn", reason))
    else:
        reason = (_optional_str(failure.get("reason")) if failure is not None else None) or f"Run status is {display_status or 'unknown'}."
        checks.append(_review_check("run", "Run finished", "fail", reason))

    if spec_review_pending:
        checks.append(_review_check("certification", "Certification", "pending", "Certification starts after spec approval and build execution."))
    elif display_status in REVIEW_IN_PROGRESS_STATUSES:
        checks.append(_review_check("certification", "Certification", "pending", "Certification is pending until the task finishes."))
    elif incomplete_terminal:
        checks.append(_review_check("certification", "Certification", "pending", "Certification did not finish because the run was interrupted."))
    elif stories_tested and stories_passed is not None and stories_passed >= stories_tested:
        if certification_passed:
            checks.append(_review_check("certification", "Certification", "pass", f"{stories_passed}/{stories_tested} stories passed."))
        else:
            detail = f"{stories_passed}/{stories_tested} stories passed, but certification proof is incomplete."
            if evidence_gate_reason:
                detail = f"{detail} {evidence_gate_reason}"
            checks.append(_review_check("certification", "Certification", "fail", detail))
    elif stories_tested and stories_passed is not None:
        checks.append(_review_check("certification", "Certification", "fail", f"{stories_passed}/{stories_tested} stories passed."))
    else:
        checks.append(_review_check("certification", "Certification", "warn", "No story pass count was recorded. Inspect artifacts before landing."))

    if spec_review_pending:
        checks.append(_review_check("changes", "Changed files", "pending", "No product changes should be present before spec approval."))
    elif display_status in REVIEW_IN_PROGRESS_STATUSES:
        checks.append(_review_check("changes", "Changed files", "pending", "Changed files are available after the task creates its branch."))
    elif incomplete_terminal:
        checks.append(_review_check("changes", "Changed files", "pending", "Final changed-file review is unavailable because the run did not finish."))
    elif diff_error:
        status = "info" if certification_only else "fail"
        detail = (
            "No code diff is required for certification-only runs."
            if certification_only
            else diff_error
        )
        checks.append(_review_check("changes", "Changed files", status, detail))
    elif changed_files:
        detail = (
            f"{len(changed_files)} file{'' if len(changed_files) == 1 else 's'} landed into {target}."
            if merged
            else f"{len(changed_files)} file{'' if len(changed_files) == 1 else 's'} changed on {branch}."
        )
        status = "warn" if certification_only else "pass"
        if certification_only:
            detail = f"{detail} Certification-only runs normally should not change code."
        checks.append(_review_check("changes", "Changed files", status, detail))
    elif merged:
        checks.append(_review_check("changes", "Changed files", "info", "No unlanded diff remains."))
    elif certification_only:
        checks.append(_review_check("changes", "Changed files", "pass", "No code changes expected for a certification-only run."))
    else:
        checks.append(_review_check("changes", "Changed files", "warn", "No changed files were detected. Confirm the task produced the expected artifact."))

    if spec_review_pending and existing_evidence:
        checks.append(_review_check("evidence", "Spec artifact", "pass", f"{len(existing_evidence)} artifact{'' if len(existing_evidence) == 1 else 's'} available."))
    elif spec_review_pending:
        checks.append(_review_check("evidence", "Spec artifact", "warn", "Spec review is pending but no readable spec artifact is attached."))
    elif display_status in REVIEW_IN_PROGRESS_STATUSES:
        if existing_evidence:
            checks.append(_review_check(
                "evidence",
                "Artifacts",
                "pending",
                f"{len(existing_evidence)} file{'' if len(existing_evidence) == 1 else 's'} collected so far; final artifacts are pending.",
            ))
        else:
            checks.append(_review_check("evidence", "Artifacts", "pending", "Artifacts are available after the task writes them."))
    elif incomplete_terminal:
        if existing_evidence:
            checks.append(_review_check(
                "evidence",
                "Artifacts",
                "pending",
                f"{len(existing_evidence)} partial artifact{'' if len(existing_evidence) == 1 else 's'} available; final artifacts were not completed.",
            ))
        else:
            checks.append(_review_check("evidence", "Artifacts", "pending", "No final artifacts are available because the run did not finish."))
    elif existing_evidence and not missing_evidence:
        checks.append(_review_check("evidence", "Artifacts", "pass", f"{len(existing_evidence)} file{'' if len(existing_evidence) == 1 else 's'} attached."))
    elif existing_evidence:
        checks.append(
            _review_check(
                "evidence",
                "Artifacts",
                "warn",
                f"{len(existing_evidence)} available, {len(missing_evidence)} missing.",
            )
        )
    else:
        checks.append(
            _review_check(
                "evidence",
                "Artifacts",
                "warn",
                "No readable artifacts are attached; use stories and changed files as proof before landing.",
            )
        )

    if readiness["state"] == "ready":
        checks.append(_review_check("landing", "Landing action", "pass", f"Safe to land into {target}."))
    elif readiness["state"] == "merged":
        checks.append(_review_check("landing", "Landing action", "pass", "Task is already landed."))
    elif readiness["state"] == "reviewed":
        checks.append(_review_check("landing", "Merge action", "pass", "No merge action is needed for certification-only proof."))
    elif readiness["state"] == "in_progress":
        checks.append(_review_check("landing", "Landing action", "pending", "Landing is disabled until the task completes."))
    elif incomplete_terminal:
        checks.append(_review_check("landing", "Landing action", "pending", "Resume or requeue the run before landing."))
    else:
        detail = (_optional_str(failure.get("reason")) if failure is not None else None) or "; ".join(str(item) for item in readiness.get("blockers", []) if item) or "Landing is disabled."
        checks.append(_review_check("landing", "Landing action", "fail", detail))

    return checks


def _review_check(key: str, label: str, status: str, detail: str) -> dict[str, str]:
    return {"key": key, "label": label, "status": status, "detail": detail}


def _is_review_evidence_artifact(item: dict[str, Any]) -> bool:
    if str(item.get("kind") or "").lower() == "directory":
        return False
    label = str(item.get("label") or "").strip().lower()
    if not item.get("exists") and label in {"intent", "checkpoint"}:
        return False
    return True


def _spec_review_pending(record: Any) -> bool:
    if getattr(record, "status", "") != "paused":
        return False
    artifacts = getattr(record, "artifacts", {}) or {}
    checkpoint_path = _optional_str(artifacts.get("checkpoint_path"))
    if not checkpoint_path:
        return False
    try:
        checkpoint = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(checkpoint, dict):
        return False
    if str(checkpoint.get("phase") or "") != "spec_review":
        return False
    spec_path = _optional_str(checkpoint.get("spec_path"))
    return bool(spec_path and Path(spec_path).exists())


def _review_packet_headline(
    record: Any,
    display_status: str,
    *,
    merged: bool,
    readiness: dict[str, Any],
    target: str,
) -> str:
    if merged:
        return f"Already merged into {target}"
    if readiness.get("state") == "blocked" and display_status == "done":
        blockers = [str(item) for item in readiness.get("blockers", []) if item]
        if any(item.startswith("Repository has local changes") for item in blockers):
            return "Repository cleanup required before landing"
        return "Review blocked before landing"
    if readiness.get("state") == "reviewed" and display_status == "done":
        return "Certification complete"
    return _review_headline(record, display_status)


def _review_headline(record: Any, display_status: str) -> str:
    if display_status == "done":
        return "Ready for review"
    if display_status == "failed":
        return "Failed; review evidence and requeue or remove"
    if display_status == "stale":
        return "Stale; stop or remove the orphaned work"
    if display_status == "queued":
        return "Waiting for queue runner"
    if display_status in REVIEW_IN_PROGRESS_STATUSES:
        return "In progress"
    if display_status == "paused" and _spec_review_pending(record):
        return "Spec review required"
    if display_status == "interrupted":
        return "Interrupted; resume or requeue"
    summary = _optional_str(record.intent.get("summary")) if hasattr(record, "intent") else None
    return summary or str(display_status or "Run detail")


def _suggested_next_action(
    display_status: str,
    actions: list[Any],
    overlay: Any,
) -> dict[str, Any]:
    if display_status == "queued":
        return {
            "label": "Start queue runner",
            "action_key": None,
            "enabled": False,
            "reason": "Use Start queue runner to run queued work.",
        }
    by_key = {action.key: action for action in actions}
    preferred = {
        "failed": ["r", "R", "x"],
        "cancelled": ["r", "R", "x"],
        "interrupted": ["r", "R", "x"],
        "paused": ["a", "g", "r", "x"],
        "stale": ["x", "c"],
        "done": ["m", "x"],
        "running": ["c"],
        "initializing": ["c"],
        "starting": ["c"],
        "queued": ["x"],
    }.get(display_status, [])
    for key in preferred:
        action = by_key.get(key)
        if action is not None and action.enabled:
            return {
                "label": _review_action_label(action.key, action.label),
                "action_key": action.key,
                "enabled": True,
                "reason": action.preview,
            }
    for action in actions:
        if action.key in {"M", "o", "e"}:
            continue
        if action.enabled:
            return {
                "label": _review_action_label(action.key, action.label),
                "action_key": action.key,
                "enabled": True,
                "reason": action.preview,
            }
    reason = overlay.reason if overlay is not None else "No safe action is currently enabled."
    return {"label": "No action", "action_key": None, "enabled": False, "reason": reason}


def _review_action_label(key: str, label: str) -> str:
    return "Land selected" if key == "m" else label


def _apply_landing_context(project_dir: Path, payload: dict[str, Any], detail: DetailView) -> None:
    merge_info = _detail_merge_info(project_dir, detail)
    if merge_info is None:
        payload["landing_state"] = None
        return
    target = str(merge_info.get("target") or _merge_target(project_dir))
    payload["landing_state"] = "merged"
    payload["merge_info"] = merge_info
    for action in payload.get("legal_actions", []):
        if isinstance(action, dict) and action.get("key") == "m":
            action["enabled"] = False
            action["reason"] = f"Already merged into {target}."
            action["preview"] = f"Already merged into {target}."


def _detail_merge_info(project_dir: Path, detail: DetailView) -> dict[str, Any] | None:
    branch = _optional_str(detail.record.git.get("branch"))
    if not branch:
        return None
    target = str(detail.record.git.get("target_branch") or _merge_target(project_dir))
    info = _merged_branch_index(project_dir, target).get(branch)
    if info is None:
        return None
    return {**info, "target": target}


def _merged_task_diff(project_dir: Path, merge_info: dict[str, Any] | None) -> dict[str, Any]:
    base, head = _merged_task_diff_range(merge_info)
    if not base or not head:
        return {"files": [], "error": None, "command": None}
    return _commit_range_diff(project_dir, base, head)


def _merged_task_diff_text(project_dir: Path, merge_info: dict[str, Any] | None) -> dict[str, Any]:
    base, head = _merged_task_diff_range(merge_info)
    if not base or not head:
        return {"text": "", "error": None}
    return _commit_range_diff_text(project_dir, base, head)


def _merged_task_diff_range(merge_info: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not merge_info:
        return None, None
    base = _optional_str(merge_info.get("diff_base")) or _optional_str(merge_info.get("target_head_before"))
    head = _optional_str(merge_info.get("merge_commit"))
    return base, head


def _product_handoff(
    project_dir: Path,
    record: Any,
    *,
    merged: bool,
    certification: dict[str, Any] | None,
    changed_files: list[str],
) -> dict[str, Any]:
    root = _product_root(project_dir, record, merged=merged)
    explicit = _explicit_product_handoff(
        project_dir,
        record,
        root,
        certification=certification,
        changed_files=changed_files,
    )
    if explicit is not None:
        return explicit
    return _detected_product_handoff(root, record, certification=certification, changed_files=changed_files)


def _product_root(project_dir: Path, record: Any, *, merged: bool) -> Path:
    if merged:
        return project_dir
    git = getattr(record, "git", {}) if isinstance(getattr(record, "git", {}), dict) else {}
    for value in (git.get("worktree"), getattr(record, "cwd", None)):
        text = _optional_str(value)
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = project_dir / path
        if path.exists() and path.is_dir():
            return path.resolve(strict=False)
    return project_dir


def _explicit_product_handoff(
    project_dir: Path,
    record: Any,
    root: Path,
    *,
    certification: dict[str, Any] | None,
    changed_files: list[str],
) -> dict[str, Any] | None:
    artifacts = getattr(record, "artifacts", {}) if isinstance(getattr(record, "artifacts", {}), dict) else {}
    session_dir = _optional_str(artifacts.get("session_dir"))
    candidates: list[Path] = []
    for key in ("product_handoff_path", "product_playbook_path"):
        value = _optional_str(artifacts.get(key))
        if value:
            candidates.append(Path(value).expanduser())
    if session_dir:
        session_path = Path(session_dir).expanduser()
        candidates.extend([
            session_path / "product-handoff.json",
            session_path / "product-playbook.json",
        ])
    candidates.extend([
        root / "product-handoff.json",
        root / "product-playbook.json",
        root / ".otto" / "product-handoff.json",
    ])
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else project_dir / candidate
        if not path.exists() or not path.is_file():
            continue
        data = _read_json_object(path)
        if data is None:
            continue
        return _normalize_product_handoff(
            data,
            root=root,
            record=record,
            certification=certification,
            changed_files=changed_files,
            source="artifact",
            source_path=path,
        )
    return None


def _normalize_product_handoff(
    data: dict[str, Any],
    *,
    root: Path,
    record: Any,
    certification: dict[str, Any] | None,
    changed_files: list[str],
    source: str,
    source_path: Path | None,
) -> dict[str, Any]:
    kind = _normalize_product_kind(data.get("kind") or data.get("product_type") or data.get("type"))
    if kind == "unknown":
        kind = _detect_product_kind(root, _read_text(root / "README.md"))
    launch = _normalize_commands(data.get("launch") or data.get("run") or data.get("commands"))
    reset = _normalize_commands(data.get("reset") or data.get("reset_commands"))
    try_flows = _normalize_flows(data.get("try_flows") or data.get("flows") or data.get("journeys"))
    sample_data = _normalize_sample_data(data.get("sample_data") or data.get("sample_users") or data.get("fixtures"))
    urls = _coerce_string_list(data.get("urls") or data.get("links"))
    if not urls:
        urls = _urls_from_text(json.dumps(data, default=str))
    task_context = _task_handoff_context(record, certification=certification, changed_files=changed_files, kind=kind)
    preview = _product_preview_metadata(
        record,
        kind=kind,
        source=source,
        launch=launch,
        urls=urls,
        changed_files=changed_files,
    )
    return {
        "kind": kind,
        "label": _product_kind_label(kind),
        "source": source,
        "source_path": str(source_path) if source_path is not None else None,
        "root": str(root),
        "summary": _optional_str(data.get("summary") or data.get("description")) or _fallback_product_summary(root),
        **preview,
        **task_context,
        "urls": urls[:8],
        "launch": launch[:8],
        "reset": reset[:6],
        "try_flows": try_flows[:12] or _fallback_try_flows(kind),
        "sample_data": sample_data[:12],
        "notes": _coerce_string_list(data.get("notes") or data.get("known_limitations"))[:10],
    }


def _detected_product_handoff(
    root: Path,
    record: Any,
    *,
    certification: dict[str, Any] | None,
    changed_files: list[str],
) -> dict[str, Any]:
    readme = _read_text(root / "README.md")
    kind = _detect_product_kind(root, readme)
    task_context = _task_handoff_context(record, certification=certification, changed_files=changed_files, kind=kind)
    launch = _detect_launch_commands(root, kind, readme)[:8]
    urls = _urls_from_text(readme)[:8]
    preview = _product_preview_metadata(
        record,
        kind=kind,
        source="detected" if kind != "unknown" else "fallback",
        launch=launch,
        urls=urls,
        changed_files=changed_files,
    )
    return {
        "kind": kind,
        "label": _product_kind_label(kind),
        "source": "detected" if kind != "unknown" else "fallback",
        "source_path": str(root / "README.md") if (root / "README.md").exists() else None,
        "root": str(root),
        "summary": _fallback_product_summary(root, readme=readme),
        **preview,
        **task_context,
        "urls": urls,
        "launch": launch,
        "reset": _detect_reset_commands(root, readme)[:6],
        "try_flows": _fallback_try_flows(kind, readme=readme)[:12],
        "sample_data": _sample_data_from_readme(readme)[:12],
        "notes": _fallback_handoff_notes(kind),
    }


def _task_handoff_context(
    record: Any,
    *,
    certification: dict[str, Any] | None,
    changed_files: list[str],
    kind: str,
) -> dict[str, Any]:
    summary = _task_summary(record)
    git = getattr(record, "git", {}) if isinstance(getattr(record, "git", {}), dict) else {}
    status = _optional_str(getattr(record, "status", None))
    return {
        "task_summary": summary,
        "task_status": status,
        "task_branch": _optional_str(git.get("branch")),
        "task_changed_files": [str(path) for path in changed_files[:12]],
        "task_flows": _task_try_flows(summary, certification=certification, kind=kind)[:8],
    }


def _product_preview_metadata(
    record: Any,
    *,
    kind: str,
    source: str,
    launch: list[dict[str, str]],
    urls: list[str],
    changed_files: list[str],
) -> dict[str, Any]:
    """Describe whether Mission Control can offer a real product preview.

    A product handoff is always useful as metadata, but the UI should only
    show a primary "preview/open" action when Otto has something executable:
    a URL or a concrete launch command. Fallback README guesses without either
    should stay under review metadata, not become a misleading button.
    """
    concrete_launch = [
        entry for entry in launch
        if not _is_placeholder_launch_command(entry.get("command", ""))
    ]
    available = bool(urls or concrete_launch)
    suppression_reason = _product_preview_suppression_reason(
        record,
        changed_files=changed_files,
        source=source,
    )
    if available and suppression_reason:
        available = False
    family = _record_command_family(record)
    if family == "certify":
        label = "Open tested product"
    elif family == "merge":
        label = "Open landed product"
    elif kind in {"api", "cli", "library"}:
        label = "Run product"
    else:
        label = "Preview product"
    if suppression_reason:
        reason = suppression_reason
    elif available:
        if urls:
            reason = "A product URL was recorded."
        elif source == "artifact":
            reason = "A product launch command was provided by the run."
        else:
            reason = "A launch command was detected from project files."
    else:
        reason = "No product URL or concrete launch command was recorded for this run."
    return {
        "preview_available": available,
        "preview_label": label,
        "preview_reason": reason,
    }


def _product_preview_suppression_reason(record: Any, *, changed_files: list[str], source: str) -> str:
    """Return why a product preview should not be a primary action.

    README detection says "this repository can launch a product"; it does not
    mean every run produced something useful to try. Test-only smoke tasks and
    certification runs should lead users to proof/results instead.
    """
    if source == "artifact":
        return ""
    family = _record_command_family(record)
    if family == "certify":
        return "This certification run produced proof, not a product preview. Review the result instead."
    paths = [str(path).strip() for path in changed_files if str(path).strip()]
    if paths and all(_is_non_product_change_path(path) for path in paths):
        return "This run changed only tests, docs, or support files. Review the result instead."
    summary = _task_summary(record).lower()
    verification_markers = (
        "smoke test",
        "test-only",
        "add test",
        "add tests",
        "test coverage",
        "certification",
        "verify ",
        "verification",
    )
    if not paths and any(marker in summary for marker in verification_markers):
        return "This run is verification-focused. Review the result instead."
    return ""


def _is_non_product_change_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    if normalized.startswith(("tests/", "test/", "docs/", ".github/", "scripts/")):
        return True
    if name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
        return True
    if name in {"readme.md", "conftest.py", "pytest.ini", "mypy.ini", "ruff.toml"}:
        return True
    return False


def _record_command_family(record: Any) -> str:
    source = getattr(record, "source", {}) if isinstance(getattr(record, "source", {}), dict) else {}
    argv = source.get("argv") if isinstance(source, dict) else None
    if isinstance(argv, list) and argv:
        first = str(argv[0] or "").strip().lower()
        if first in {"build", "improve", "certify", "cert", "merge", "land"}:
            return "certify" if first == "cert" else "merge" if first == "land" else first
    command = str(getattr(record, "command", "") or "").strip().lower()
    first_command = command.split(None, 1)[0] if command else ""
    if first_command in {"build", "improve", "certify", "cert", "merge", "land"}:
        return "certify" if first_command == "cert" else "merge" if first_command == "land" else first_command
    candidates = [
        getattr(record, "run_type", None),
        getattr(record, "domain", None),
    ]
    intent = getattr(record, "intent", {}) if isinstance(getattr(record, "intent", {}), dict) else {}
    candidates.append(intent.get("command"))
    text = " ".join(str(item or "").strip().lower() for item in candidates)
    if any(token in {"certify", "cert"} for token in text.split()):
        return "certify"
    if any(token in {"merge", "land"} for token in text.split()):
        return "merge"
    if "improve" in text.split():
        return "improve"
    if any(token in {"build", "queue"} for token in text.split()):
        return "build"
    return ""


def _is_placeholder_launch_command(command: str) -> bool:
    text = str(command or "").strip().lower()
    if not text:
        return True
    placeholders = ("<module>", "<package>", "${port}", "$port")
    return any(token in text for token in placeholders)


def _task_summary(record: Any) -> str:
    intent = getattr(record, "intent", {}) if isinstance(getattr(record, "intent", {}), dict) else {}
    return (
        _optional_str(intent.get("summary"))
        or _optional_str(getattr(record, "display_name", None))
        or _optional_str(getattr(record, "run_id", None))
        or ""
    )


def _task_try_flows(
    summary: str,
    *,
    certification: dict[str, Any] | None,
    kind: str,
) -> list[dict[str, Any]]:
    stories = certification.get("stories") if isinstance(certification, dict) else None
    flows: list[dict[str, Any]] = []
    if isinstance(stories, list):
        for raw in stories[:6]:
            if not isinstance(raw, dict):
                continue
            title = _first_nonempty(raw.get("title"), raw.get("id"), "Certified story")
            status = _first_nonempty(raw.get("status"))
            detail = _first_nonempty(raw.get("detail"))
            steps = [
                _launch_instruction_for_kind(kind),
                f"Exercise this task story: {title}.",
            ]
            if detail:
                steps.append(f"Compare against certification evidence: {detail}")
            else:
                steps.append("Confirm the behavior matches the task request.")
            if status:
                steps.append(f"Certification recorded this story as {status}.")
            flows.append({"title": title, "steps": steps})
    if flows:
        return flows
    if summary:
        return [
            {
                "title": f"Try this task: {summary[:90]}",
                "steps": [
                    _launch_instruction_for_kind(kind),
                    f"Find the feature or behavior requested by this task: {summary}.",
                    "Exercise the path manually, then compare what you see with the proof and diff tabs.",
                ],
            }
        ]
    return []


def _launch_instruction_for_kind(kind: str) -> str:
    if kind == "web":
        return "Start the app and open it in a browser."
    if kind == "api":
        return "Start the API and use curl or the docs page."
    if kind == "cli":
        return "Run the CLI command from the launch section."
    if kind == "desktop":
        return "Launch the desktop app."
    if kind == "library":
        return "Import the package from a fresh script or REPL."
    if kind in {"service", "worker", "pipeline"}:
        return "Run the product process with a small fixture."
    return "Launch the product using the commands above."


def _normalize_product_kind(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "-").replace("_", "-")
    aliases = {
        "rest": "api",
        "rest-api": "api",
        "http-api": "api",
        "webapp": "web",
        "web-app": "web",
        "website": "web",
        "electron": "desktop",
        "tauri": "desktop",
        "native": "desktop",
        "command": "cli",
        "command-line": "cli",
        "package": "library",
        "lib": "library",
        "daemon": "service",
        "queue": "worker",
        "batch": "pipeline",
    }
    text = aliases.get(text, text)
    return text if text in PRODUCT_HANDOFF_KINDS else "unknown"


def _detect_product_kind(root: Path, readme: str = "") -> str:
    lower = readme.lower()
    package = _read_json_object(root / "package.json")
    if isinstance(package, dict):
        deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
        dep_names = {str(key).lower() for key in deps}
        if "electron" in dep_names or (root / "src-tauri").exists() or (root / "tauri.conf.json").exists():
            return "desktop"
        if any(key in dep_names for key in {"vite", "react", "next", "svelte", "vue", "@angular/core"}):
            return "web"
        if package.get("bin"):
            return "cli"
    if (root / "openapi.json").exists() or "openapi" in lower or "swagger" in lower:
        return "api"
    if "uvicorn" in lower or "fastapi" in lower or "flask --app" in lower or "django" in lower:
        return "web" if any(token in lower for token in ("dashboard", "browser", "page", "web app", "html")) else "api"
    if (root / "index.html").exists() or (root / "templates").exists() or (root / "static").exists():
        return "web"
    pyproject = _read_text(root / "pyproject.toml")
    if "[project.scripts]" in pyproject or "[tool.poetry.scripts]" in pyproject:
        return "cli"
    if "__init__.py" in "\n".join(path.name for path in root.glob("*/__init__.py")) or "import " in lower:
        return "library"
    if "worker" in lower or "queue" in lower:
        return "worker"
    if "pipeline" in lower or "batch" in lower:
        return "pipeline"
    return "unknown"


def _detect_launch_commands(root: Path, kind: str, readme: str) -> list[dict[str, str]]:
    commands: list[dict[str, str]] = []
    for command in _commands_from_readme(readme):
        if _command_matches_kind(command, kind):
            commands.append({"label": _command_label(command, kind), "command": command})
    package = _read_json_object(root / "package.json")
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if isinstance(scripts, dict):
        for script in ("dev", "start", "serve"):
            if isinstance(scripts.get(script), str):
                commands.append({"label": f"npm {script}", "command": f"npm run {script}"})
                break
    if kind in {"web", "api"}:
        if (root / "expense_portal").exists():
            commands.append({"label": "Start Flask app", "command": ".venv/bin/flask --app expense_portal run --host 0.0.0.0 --port ${PORT}"})
        elif (root / "app" / "main.py").exists():
            commands.append({"label": "Start ASGI app", "command": "uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"})
        elif (root / "manage.py").exists():
            commands.append({"label": "Start Django app", "command": ".venv/bin/python manage.py runserver 0.0.0.0:${PORT}"})
    if kind == "cli":
        script = _first_project_script(root)
        if script:
            commands.append({"label": "Show help", "command": f"{script} --help"})
        else:
            commands.append({"label": "Show help", "command": "python -m <module> --help"})
    if not commands and kind == "library":
        commands.append({"label": "Import package", "command": "python - <<'PY'\nimport <package>\nprint(<package>.__name__)\nPY"})
    return _dedupe_command_entries(commands)


def _detect_reset_commands(root: Path, readme: str) -> list[dict[str, str]]:
    commands = [
        {"label": _command_label(command, "reset"), "command": command}
        for command in _commands_from_readme(readme)
        if any(token in command.lower() for token in ("reset", "init-db", "seed", "migrate"))
    ]
    if (root / "expense_portal").exists():
        commands.append({"label": "Reset demo database", "command": ".venv/bin/flask --app expense_portal init-db"})
    return _dedupe_command_entries(commands)


def _fallback_try_flows(kind: str, readme: str = "") -> list[dict[str, Any]]:
    if kind == "web":
        return [
            {"title": "Open the app", "steps": ["Start the web server.", "Open the local URL in a browser.", "Confirm the first screen loads without console-visible errors."]},
            {"title": "Exercise the main workflow", "steps": ["Follow the primary action from the README or page.", "Create or update one record.", "Refresh and confirm the state persists."]},
            {"title": "Review edge states", "steps": ["Try an empty or invalid form.", "Confirm the UI explains what to fix."]},
        ]
    if kind == "api":
        return [
            {"title": "Start and probe the API", "steps": ["Start the service.", "Open `/docs`, `/openapi.json`, or the documented health endpoint.", "Confirm a 2xx response."]},
            {"title": "Run a CRUD path", "steps": ["Create a resource with curl.", "Read or list it back.", "Try one invalid request and inspect the error body."]},
        ]
    if kind == "cli":
        return [
            {"title": "Inspect commands", "steps": ["Run the CLI with `--help`.", "Run the main happy-path command.", "Confirm stdout, stderr, exit code, and generated files."]},
            {"title": "Try a bad input", "steps": ["Run one malformed argument.", "Confirm the CLI exits non-zero with a useful message."]},
        ]
    if kind == "desktop":
        return [
            {"title": "Launch the app", "steps": ["Run the desktop launch command.", "Confirm the primary window appears.", "Exercise the main menu or primary action."]},
            {"title": "Check persistence", "steps": ["Change one setting or record.", "Restart the app.", "Confirm the state is still present."]},
        ]
    if kind == "library":
        return [
            {"title": "Import the public API", "steps": ["Create a fresh script or REPL.", "Import the documented package.", "Call the main function and verify its return value."]},
            {"title": "Check error handling", "steps": ["Call the API with one invalid input.", "Confirm it raises or returns the documented error."]},
        ]
    if kind in {"worker", "service", "pipeline"}:
        return [
            {"title": "Run with a fixture", "steps": ["Start the worker, service, or pipeline.", "Feed a small documented input.", "Verify output files, side effects, logs, or state changes."]},
            {"title": "Try a bad fixture", "steps": ["Feed one malformed input.", "Confirm failure is visible and recoverable."]},
        ]
    if "quick start" in readme.lower():
        return [{"title": "Follow README Quick Start", "steps": ["Run the setup commands from README.", "Run the main example.", "Confirm the expected output."]}]
    return [{"title": "Smoke test the product", "steps": ["Open README or product docs.", "Run the documented setup command.", "Exercise the main user-facing path."]}]


def _fallback_product_summary(root: Path, *, readme: str | None = None) -> str:
    readme = _read_text(root / "README.md") if readme is None else readme
    for line in readme.splitlines():
        text = line.strip(" #\t")
        if text:
            return text[:220]
    return root.name


def _fallback_handoff_notes(kind: str) -> list[str]:
    if kind in {"web", "api", "desktop", "service", "worker"}:
        return ["Use ${PORT} as a placeholder when choosing a free local port."]
    return []


def _normalize_commands(value: Any) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    raw_items = value if isinstance(value, list) else [value] if value else []
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, str):
            command = item.strip()
            if command:
                entries.append({"label": f"Command {index}", "command": command})
        elif isinstance(item, dict):
            command = _optional_str(item.get("command") or item.get("cmd"))
            if not command:
                continue
            entries.append({
                "label": _optional_str(item.get("label") or item.get("name")) or f"Command {index}",
                "command": command,
            })
    return _dedupe_command_entries(entries)


def _normalize_flows(value: Any) -> list[dict[str, Any]]:
    flows: list[dict[str, Any]] = []
    raw_items = value if isinstance(value, list) else [value] if value else []
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, str):
            title = item.strip()
            if title:
                flows.append({"title": title, "steps": []})
        elif isinstance(item, dict):
            title = _optional_str(item.get("title") or item.get("name") or item.get("summary")) or f"Flow {index}"
            steps = _coerce_string_list(item.get("steps") or item.get("actions"))
            flows.append({"title": title, "steps": steps[:12]})
    return flows


def _normalize_sample_data(value: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    raw_items = value if isinstance(value, list) else [value] if value else []
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, str):
            text = item.strip()
            if text:
                items.append({"label": f"Sample {index}", "value": text, "detail": ""})
        elif isinstance(item, dict):
            label = _optional_str(item.get("label") or item.get("name") or item.get("role")) or f"Sample {index}"
            value = _optional_str(item.get("value") or item.get("email") or item.get("username") or item.get("id")) or ""
            detail = _optional_str(item.get("detail") or item.get("description") or item.get("password")) or ""
            items.append({"label": label, "value": value, "detail": detail})
    return items


def _commands_from_readme(readme: str) -> list[str]:
    commands: list[str] = []
    for match in COMMAND_LINE_RE.finditer(readme):
        command = match.group("command").strip()
        if command and not command.startswith(("```", "#")):
            commands.append(command)
    return commands[:24]


def _command_matches_kind(command: str, kind: str) -> bool:
    lower = command.lower()
    if any(token in lower for token in (" init-db", " reset", " seed", " migrate", "pytest", " test")):
        return False
    if kind in {"web", "api"}:
        return any(token in lower for token in ("flask", "uvicorn", "fastapi", "runserver", "npm run dev", "npm start", "pnpm dev", "yarn dev", "bun dev"))
    if kind == "desktop":
        return any(token in lower for token in ("electron", "tauri", "npm start", "cargo tauri"))
    if kind == "cli":
        return "--help" in lower or lower.startswith(("python -m", "uv run", "cargo run", "go run"))
    return True


def _command_label(command: str, kind: str) -> str:
    lower = command.lower()
    if "init-db" in lower or "seed" in lower:
        return "Reset demo data"
    if "flask" in lower or "uvicorn" in lower or "runserver" in lower:
        return "Start server"
    if "npm run dev" in lower or "pnpm dev" in lower or "yarn dev" in lower:
        return "Start dev server"
    if "curl" in lower:
        return "Try request"
    if "--help" in lower:
        return "Show help"
    if kind == "api":
        return "Start API"
    if kind == "desktop":
        return "Launch desktop app"
    if kind == "library":
        return "Try import"
    return "Run product"


def _dedupe_command_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for entry in entries:
        command = entry.get("command", "").strip()
        if not command or command in seen:
            continue
        seen.add(command)
        result.append({"label": entry.get("label", "Command").strip() or "Command", "command": command})
    return result


def _urls_from_text(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,")
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _sample_data_from_readme(readme: str) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for line in readme.splitlines():
        text = line.strip(" -*\t")
        if not text:
            continue
        lower = text.lower()
        if any(token in lower for token in ("manager", "employee", "user", "login", "password", "token")) and len(text) <= 160:
            samples.append({"label": "README", "value": text, "detail": ""})
    return samples[:8]


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _optional_str(item)
        if text:
            result.append(text)
    return result


def _read_text(path: Path, *, limit: int = 24_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _first_project_script(root: Path) -> str | None:
    pyproject = _read_text(root / "pyproject.toml")
    for line in pyproject.splitlines():
        if "=" not in line or line.strip().startswith("["):
            continue
        name = line.split("=", 1)[0].strip()
        if name and re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            return name
    package = _read_json_object(root / "package.json")
    if isinstance(package, dict) and isinstance(package.get("bin"), dict):
        for name in package["bin"]:
            return str(name)
    return None


def _product_kind_label(kind: str) -> str:
    return {
        "web": "Web app",
        "api": "API service",
        "cli": "CLI tool",
        "desktop": "Desktop app",
        "library": "Library",
        "service": "Service",
        "worker": "Worker",
        "pipeline": "Pipeline",
    }.get(kind, "Product")


def _certification_summary(project_dir: Path, record: Any) -> dict[str, Any]:
    summary = _summary_for_record(record)
    proof_report = _proof_report_info(project_dir, record)
    proof_json = _read_json_object(Path(str(proof_report["json_path"]))) if proof_report.get("json_path") else None
    metrics = getattr(record, "metrics", {}) if isinstance(getattr(record, "metrics", {}), dict) else {}
    stories_tested = _int_or_none(metrics.get("stories_tested"))
    stories_passed = _int_or_none(metrics.get("stories_passed"))
    for source in (summary, proof_json):
        if not isinstance(source, dict):
            continue
        stories_tested = stories_tested if stories_tested is not None else _int_or_none(source.get("stories_tested"))
        stories_passed = stories_passed if stories_passed is not None else _int_or_none(source.get("stories_passed"))
        if stories_tested is None:
            stories_tested = _int_or_none(source.get("stories_total_count"))
    stories = _certification_stories(proof_json) or _certification_stories(summary)
    if stories and stories_tested is None:
        stories_tested = len(stories)
    if stories and stories_passed is None:
        stories_passed = sum(1 for story in stories if story.get("status") in {"pass", "warn"})
    proof_outcome = _optional_str(proof_json.get("outcome")) if isinstance(proof_json, dict) else None
    evidence_gate = _certification_evidence_gate(proof_json)
    story_counts_pass = (
        stories_passed is not None
        and stories_tested is not None
        and stories_tested > 0
        and stories_passed >= stories_tested
    )
    proof_packet_pass = proof_outcome != "failed" and not bool(evidence_gate.get("blocks_pass"))
    # Cluster-evidence-trustworthiness #4: Mission Control had been
    # flattening the certification down to final stories + counts, hiding
    # earlier rounds and their per-round evidence. The proof-of-work JSON
    # already carries `round_history` (see otto/certifier/__init__.py
    # `_round_history`); we surface it through the review packet so the
    # client can render a per-round tab strip with verdict, timestamp,
    # cost, and per-round story slices instead of pretending every cert
    # was a single round.
    rounds = _certification_round_history(proof_json)
    return {
        "stories_passed": stories_passed,
        "stories_tested": stories_tested,
        "passed": story_counts_pass and proof_packet_pass,
        "summary_path": _optional_str(getattr(record, "artifacts", {}).get("summary_path")),
        "stories": stories,
        "demo_evidence": _certification_demo_evidence(proof_json),
        "evidence_gate": evidence_gate,
        "proof_report": proof_report,
        "rounds": rounds,
    }


def _certification_evidence_gate(proof_json: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(proof_json, dict):
        return {"schema_version": 1, "status": "not_applicable", "blocks_pass": False, "reason": ""}
    raw = proof_json.get("evidence_gate")
    if not isinstance(raw, dict):
        return {"schema_version": 1, "status": "not_applicable", "blocks_pass": False, "reason": ""}
    return {
        "schema_version": _int_or_none(raw.get("schema_version")) or 1,
        "status": _optional_str(raw.get("status")) or "unknown",
        "blocks_pass": bool(raw.get("blocks_pass")),
        "reason": _optional_str(raw.get("reason")) or "",
    }


def _certification_demo_evidence(proof_json: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(proof_json, dict):
        return {
            "schema_version": 1,
            "app_kind": "unknown",
            "demo_required": False,
            "demo_status": "not_applicable",
            "demo_reason": "No structured proof metadata is available for this legacy run.",
            "primary_demo": None,
            "stories": [],
            "counts": {},
        }
    raw = proof_json.get("demo_evidence")
    if not isinstance(raw, dict):
        return {
            "schema_version": 1,
            "app_kind": "unknown",
            "demo_required": False,
            "demo_status": "not_applicable",
            "demo_reason": "This proof report was generated before structured demo evidence was recorded.",
            "primary_demo": None,
            "stories": [],
            "counts": {},
        }
    primary = raw.get("primary_demo") if isinstance(raw.get("primary_demo"), dict) else None
    stories: list[dict[str, Any]] = []
    for item in raw.get("stories") or []:
        if not isinstance(item, dict):
            continue
        stories.append(
            {
                "id": _optional_str(item.get("id")),
                "title": _optional_str(item.get("title")),
                "status": _optional_str(item.get("status")),
                "needs_visual": bool(item.get("needs_visual")),
                "needs_file_validation": bool(item.get("needs_file_validation")),
                "has_text_evidence": bool(item.get("has_text_evidence")),
                "has_file_validation": bool(item.get("has_file_validation")),
                "proof_level": _optional_str(item.get("proof_level")),
                "visual_items": [
                    {
                        "name": _optional_str(visual.get("name")),
                        "kind": _optional_str(visual.get("kind")),
                        "href": _optional_str(visual.get("href")),
                        "caption": _optional_str(visual.get("caption")),
                    }
                    for visual in (item.get("visual_items") or [])
                    if isinstance(visual, dict)
                ],
            }
        )
    return {
        "schema_version": _int_or_none(raw.get("schema_version")) or 1,
        "app_kind": _optional_str(raw.get("app_kind")) or "unknown",
        "demo_required": bool(raw.get("demo_required")),
        "demo_status": _optional_str(raw.get("demo_status")) or "unknown",
        "demo_reason": _optional_str(raw.get("demo_reason")),
        "primary_demo": (
            {
                "name": _optional_str(primary.get("name")),
                "kind": _optional_str(primary.get("kind")),
                "href": _optional_str(primary.get("href")),
                "caption": _optional_str(primary.get("caption")),
            }
            if primary
            else None
        ),
        "stories": stories[:100],
        "counts": raw.get("counts") if isinstance(raw.get("counts"), dict) else {},
    }


def _certification_round_history(proof_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize ``round_history`` from proof-of-work.json for the UI.

    The certifier writes a richer object per round; the client only needs
    a stable subset. We pass through the fields needed to render a round
    tab (`round`, `verdict`, counts, `diagnosis`, durations, costs) plus
    `failing_story_ids` / `warn_story_ids` so the UI can call out the
    deltas across rounds. ``stories`` (per-round) is omitted here because
    the certifier only stores aggregated story IDs in the history; the
    full per-round story payload remains in the HTML report.
    """
    if not isinstance(proof_json, dict):
        return []
    raw_rounds = proof_json.get("round_history")
    if not isinstance(raw_rounds, list):
        return []
    rounds: list[dict[str, Any]] = []
    for entry in raw_rounds:
        if not isinstance(entry, dict):
            continue
        rounds.append(
            {
                "round": _int_or_none(entry.get("round")),
                "verdict": _optional_str(entry.get("verdict")) or "unknown",
                "stories_tested": _int_or_none(entry.get("stories_tested")),
                "passed_count": _int_or_none(entry.get("passed_count")),
                "failed_count": _int_or_none(entry.get("failed_count")),
                "warn_count": _int_or_none(entry.get("warn_count")),
                "failing_story_ids": [
                    str(item) for item in (entry.get("failing_story_ids") or []) if item
                ],
                "warn_story_ids": [
                    str(item) for item in (entry.get("warn_story_ids") or []) if item
                ],
                "diagnosis": _optional_str(entry.get("diagnosis")),
                "duration_s": entry.get("duration_s") if isinstance(entry.get("duration_s"), (int, float)) else None,
                "duration_human": _optional_str(entry.get("duration_human")),
                "cost_usd": entry.get("cost_usd") if isinstance(entry.get("cost_usd"), (int, float)) else None,
                "cost_estimated": bool(entry.get("cost_estimated")),
                "fix_commits": [str(item) for item in (entry.get("fix_commits") or []) if item],
                "fix_diff_stat": _optional_str(entry.get("fix_diff_stat")),
                "still_failing_after_fix": [
                    str(item) for item in (entry.get("still_failing_after_fix") or []) if item
                ],
                "subagent_errors": [
                    str(item) for item in (entry.get("subagent_errors") or []) if item
                ],
            }
        )
    return rounds


def _certification_stories(source: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(source, dict):
        return []
    raw_stories = source.get("stories") or source.get("stories_ordered") or source.get("story_results")
    if not isinstance(raw_stories, list):
        return []
    stories: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_stories, start=1):
        if not isinstance(raw, dict):
            continue
        story_id = _first_nonempty(raw.get("story_id"), raw.get("id"), raw.get("name"), f"story-{index}")
        title = _first_nonempty(raw.get("claim"), raw.get("title"), raw.get("summary"), raw.get("name"), story_id)
        detail = _first_nonempty(
            raw.get("observed_result"),
            raw.get("key_finding"),
            raw.get("evidence"),
            raw.get("failure_evidence"),
            raw.get("summary"),
        )
        stories.append(
            {
                "id": story_id,
                "title": title,
                "status": _story_status(raw),
                "methodology": _first_nonempty(raw.get("methodology"), raw.get("interaction_method")),
                "surface": _first_nonempty(raw.get("surface"), raw.get("surface_display")),
                "detail": detail,
            }
        )
    return stories[:100]


def _story_status(story: dict[str, Any]) -> str:
    raw = str(story.get("status") or story.get("verdict") or story.get("outcome") or "").strip().lower()
    if raw in {"pass", "passed", "success", "ok"}:
        return "pass"
    if raw in {"warn", "warning", "flag", "flag_for_human", "flag-for-human"}:
        return "warn"
    if raw in {"skip", "skipped"}:
        return "skipped"
    if raw in {"fail", "failed", "failure", "error"}:
        return "fail"
    passed = story.get("passed")
    if passed is True:
        return "pass"
    if passed is False:
        return "fail"
    return raw or "unknown"


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _summary_for_record(record: Any) -> dict[str, Any] | None:
    artifacts = getattr(record, "artifacts", {}) if isinstance(getattr(record, "artifacts", {}), dict) else {}
    candidates = [
        artifacts.get("summary_path"),
        Path(str(artifacts.get("session_dir"))) / "summary.json" if artifacts.get("session_dir") else None,
    ]
    for candidate in candidates:
        text = _optional_str(candidate)
        if not text:
            continue
        value = _read_json_object(Path(text).expanduser())
        if value is not None:
            return value
    return None


def _verification_plan_for_detail(detail: DetailView) -> dict[str, Any] | None:
    """Return the operator-visible verification plan for a run, if present."""
    record = detail.record
    summary = _summary_for_record(record)
    if isinstance(summary, dict):
        for key in ("verification_plan", "merge_verification_plan"):
            value = summary.get(key)
            if isinstance(value, dict):
                return value

    artifacts = getattr(record, "artifacts", {}) if isinstance(getattr(record, "artifacts", {}), dict) else {}
    candidates: list[Path] = []
    for raw in artifacts.get("extra_log_paths") or []:
        text = _optional_str(raw)
        if text:
            candidates.append(Path(text).expanduser())
    for artifact in detail.artifacts:
        text = _optional_str(getattr(artifact, "path", None))
        if text:
            candidates.append(Path(text).expanduser())

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.name != "verification-plan.json":
            continue
        value = _read_json_object(resolved)
        if isinstance(value, dict):
            return value
    return None


def _verification_plan_from_review_packet(detail: DetailView, review_packet: dict[str, Any]) -> dict[str, Any] | None:
    """Build a provisional operator plan when a run has not written one yet."""
    raw_checks = review_packet.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        return None

    checks: list[VerificationCheck] = []
    status_map = {
        "pass": "pass",
        "fail": "fail",
        "warn": "warn",
        "pending": "pending",
        "info": "pending",
        "skipped": "skipped",
    }
    for raw in raw_checks:
        if not isinstance(raw, dict):
            continue
        key = _optional_str(raw.get("key")) or _optional_str(raw.get("label")) or "check"
        status = status_map.get((_optional_str(raw.get("status")) or "pending").lower(), "pending")
        checks.append(
            VerificationCheck(
                id=key,
                label=_optional_str(raw.get("label")) or key,
                action="CHECK",
                status=status,  # type: ignore[arg-type]
                reason=_optional_str(raw.get("detail")) or "",
                source="review-packet",
            )
        )
    if not checks:
        return None

    record = detail.record
    changes = review_packet.get("changes") if isinstance(review_packet.get("changes"), dict) else {}
    target = _optional_str(changes.get("target")) or _optional_str(record.git.get("target_branch")) or ""
    display_status = _optional_str(review_packet.get("status")) or record.status or ""
    policy = "smart"
    if display_status in {"queued", "starting", "initializing", "running", "terminating"}:
        reasons = ["Provisional plan from the current run state; final story checks appear after certification."]
    else:
        reasons = ["Plan reconstructed from the review packet because no verification-plan artifact was recorded."]
    return VerificationPlan(
        scope=f"{record.domain}/{record.run_type}",
        target=target,
        policy=policy,
        risk_level="unknown",
        verification_level="provisional",
        allow_skip=True,
        reasons=reasons,
        checks=checks,
        metadata={
            "run_id": detail.run_id,
            "source": "review-packet",
            "status": display_status,
        },
    ).to_dict()


def _proof_report_info(project_dir: Path, record: Any) -> dict[str, Any]:
    """Return proof-of-work locations + provenance metadata.

    Cluster-evidence-trustworthiness #3: the proof drawer was previously
    cached by artifact index alone and the server accepted the first
    matching proof file without checking that ``run_context.run_id``
    actually matched the run we're rendering. We now thread the
    proof-of-work's own provenance (`generated_at`, `run_id`,
    `session_id`, `branch`, `head_sha`), the file mtime, and a content
    SHA-256 into the response so the client can:

    * invalidate its cached proof content when ``version`` (from
      run_context) changes, and
    * warn the operator when the proof's recorded ``run_id`` does not
      match the run record being viewed (stale or mis-routed file).
    """
    json_path, html_path = _proof_report_paths(project_dir, record)
    run_id = str(getattr(record, "run_id", "") or "")
    payload: dict[str, Any] = {
        "json_path": str(json_path) if json_path is not None else None,
        "html_path": str(html_path) if html_path is not None else None,
        "html_url": f"/api/runs/{quote(run_id, safe='')}/proof-report" if html_path is not None else None,
        "available": html_path is not None,
        # Provenance — populated below from the JSON when present so the
        # UI never has to guess whether the file it just rendered actually
        # belongs to this run.
        "generated_at": None,
        "run_id": None,
        "session_id": None,
        "branch": None,
        "head_sha": None,
        "file_mtime": None,
        "sha256": None,
        "run_id_matches": None,
    }
    if json_path is not None:
        payload.update(_proof_provenance(json_path, expected_run_id=run_id))
    return payload


def _proof_provenance(json_path: Path, *, expected_run_id: str) -> dict[str, Any]:
    """Extract provenance metadata from a proof-of-work.json file."""
    info: dict[str, Any] = {
        "generated_at": None,
        "run_id": None,
        "session_id": None,
        "branch": None,
        "head_sha": None,
        "file_mtime": None,
        "sha256": None,
        "run_id_matches": None,
    }
    try:
        stat = json_path.stat()
        info["file_mtime"] = (
            datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except OSError:
        return info
    try:
        raw_bytes = json_path.read_bytes()
    except OSError:
        return info
    try:
        info["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    except Exception:  # pragma: no cover — defensive
        info["sha256"] = None
    parsed = _read_json_object(json_path)
    if not isinstance(parsed, dict):
        return info
    info["generated_at"] = _optional_str(parsed.get("generated_at"))
    run_context = parsed.get("run_context") if isinstance(parsed.get("run_context"), dict) else {}
    info["run_id"] = _optional_str(run_context.get("run_id"))
    info["session_id"] = _optional_str(run_context.get("session_id"))
    info["branch"] = _optional_str(run_context.get("git_branch"))
    info["head_sha"] = _optional_str(run_context.get("git_commit_sha"))
    expected = (expected_run_id or "").strip()
    if expected and info["run_id"]:
        info["run_id_matches"] = info["run_id"] == expected
    elif expected and not info["run_id"]:
        # No proof-side run_id to compare; treat as unknown rather than
        # outright mismatch so legacy reports don't trip the UI warning.
        info["run_id_matches"] = None
    return info


def _rewrite_proof_report_links(html: str, run_id: str) -> str:
    run_token = quote(run_id, safe="")

    def replace(match: re.Match[str]) -> str:
        url = match.group("url")
        rewritten = _proof_asset_url(run_token, url)
        if rewritten == url:
            return match.group(0)
        return f"{match.group('prefix')}{match.group('quote')}{rewritten}{match.group('quote')}"

    return PROOF_LINK_ATTR_RE.sub(replace, html)


def _proof_asset_url(run_token: str, url: str) -> str:
    if not url or url.startswith("#"):
        return url
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or parts.path.startswith("/"):
        return url
    if not parts.path:
        return url
    rewritten = f"/api/runs/{run_token}/proof-assets/{quote(parts.path, safe='')}"
    if parts.query:
        rewritten = f"{rewritten}?{parts.query}"
    if parts.fragment:
        rewritten = f"{rewritten}#{parts.fragment}"
    return rewritten


def _proof_report_asset_root(record: Any, html_path: Path) -> Path:
    artifacts = getattr(record, "artifacts", {}) if isinstance(getattr(record, "artifacts", {}), dict) else {}
    session_dir = _optional_str(artifacts.get("session_dir"))
    if session_dir:
        candidate = Path(session_dir).expanduser()
        return candidate.resolve(strict=False)
    return html_path.parent.parent.resolve(strict=False)


def _proof_report_paths(project_dir: Path, record: Any) -> tuple[Path | None, Path | None]:
    root = project_dir.resolve(strict=False)
    artifacts = getattr(record, "artifacts", {}) if isinstance(getattr(record, "artifacts", {}), dict) else {}
    record_project = Path(str(getattr(record, "project_dir", "") or project_dir)).expanduser()
    run_id = str(getattr(record, "run_id", "") or "").strip()
    json_candidates: list[Path] = []
    html_candidates: list[Path] = []

    _append_path_candidate(json_candidates, artifacts.get("proof_of_work_path"))
    for manifest_path in _manifest_path_candidates(project_dir, record, artifacts):
        manifest = _read_json_object(manifest_path)
        if manifest is not None:
            _append_path_candidate(json_candidates, manifest.get("proof_of_work_path"))

    summary_path = _optional_str(artifacts.get("summary_path"))
    if summary_path:
        summary_parent = Path(summary_path).expanduser().parent
        _append_path_candidate(json_candidates, summary_parent / "certify" / "proof-of-work.json")
    session_dir = _optional_str(artifacts.get("session_dir"))
    if session_dir:
        _append_path_candidate(json_candidates, Path(session_dir).expanduser() / "certify" / "proof-of-work.json")
    if run_id:
        _append_path_candidate(json_candidates, paths.certify_dir(record_project, run_id) / "proof-of-work.json")
        _append_path_candidate(json_candidates, paths.certify_dir(project_dir, run_id) / "proof-of-work.json")

    extra_paths = artifacts.get("extra_log_paths") if isinstance(artifacts.get("extra_log_paths"), list) else []
    for value in extra_paths:
        path = Path(str(value)).expanduser()
        if path.name == "proof-of-work.html":
            _append_path_candidate(html_candidates, path)
        elif path.name == "proof-of-work.json":
            _append_path_candidate(json_candidates, path)

    for candidate in list(json_candidates):
        _append_path_candidate(html_candidates, candidate.with_name("proof-of-work.html"))

    json_path = _first_existing_project_path(root, json_candidates)
    html_path = _first_existing_project_path(root, html_candidates)
    return json_path, html_path


def _manifest_path_candidates(project_dir: Path, record: Any, artifacts: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    _append_path_candidate(candidates, artifacts.get("manifest_path"))
    _append_path_candidate(candidates, artifacts.get("queue_manifest_path"))
    queue_task_id = _optional_str(getattr(record, "identity", {}).get("queue_task_id")) if isinstance(getattr(record, "identity", {}), dict) else None
    if queue_task_id:
        try:
            manifest = paths.queue_manifest_path(project_dir, queue_task_id)
        except ValueError:
            manifest = None
        _append_path_candidate(candidates, manifest)
    return candidates


def _append_path_candidate(candidates: list[Path], value: Any) -> None:
    if value is None:
        return
    path = value if isinstance(value, Path) else Path(str(value))
    text = str(path).strip()
    if not text:
        return
    candidate = Path(text).expanduser()
    if candidate not in candidates:
        candidates.append(candidate)


def _first_existing_project_path(root: Path, candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        path = candidate.resolve(strict=False)
        if not _is_relative_to_path(path, root):
            continue
        if path.exists() and path.is_file():
            return path
    return None


def _is_relative_to_path(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# MIME / binary detection (cluster-evidence-trustworthiness #6)
# ---------------------------------------------------------------------------

# Magic-byte fingerprints for the binary formats we care about. Order
# matters: PNG/GIF/JPG/PDF/WEBM/MP4/WEBP are checked before we fall back
# to a null-byte sniff. We deliberately don't pull `python-magic` in —
# the dependency is heavy and these prefixes cover every binary the
# certifier emits today.
_BINARY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
    (b"\x1aE\xdf\xa3", "video/webm"),
    (b"RIFF", None),  # follow-up sniff for WEBP / WAV
)


def _detect_mime_type(path: Path) -> str:
    """Return a best-effort MIME type for ``path``.

    Tries (1) magic-byte sniff on the first 16 bytes for the binary
    formats we ship, (2) extension-based ``mimetypes.guess_type``,
    (3) ``application/octet-stream`` as the safe default.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        head = b""
    for prefix, mime in _BINARY_MAGIC:
        if head.startswith(prefix):
            if prefix == b"RIFF" and len(head) >= 12 and head[8:12] == b"WEBP":
                return "image/webp"
            if mime is not None:
                return mime
            break
    guess, _encoding = mimetypes.guess_type(str(path))
    if guess:
        return guess
    return "application/octet-stream"


def _looks_like_text(path: Path, *, mime_type: str | None = None) -> bool:
    """True when ``path`` should be served as a UTF-8-decoded text body.

    A file is treated as text when:
    * the MIME type starts with ``text/`` or is one of a small allowlist
      of structured-text MIMEs (``application/json``, ``...xml``), OR
    * the first 1KB contains no NUL byte and decodes cleanly as UTF-8.

    This deliberately treats unknown empty files (size 0) as text so
    placeholder logs render as "No content" rather than as a download.
    """
    mime = mime_type or _detect_mime_type(path)
    if mime.startswith("text/"):
        return True
    if mime in {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "application/x-sh",
        "application/javascript",
    }:
        return True
    try:
        with path.open("rb") as handle:
            sample = handle.read(1024)
    except OSError:
        return False
    if not sample:
        return True  # empty file: nothing binary to render
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _file_provenance(path: Path) -> dict[str, Any]:
    """Return ``{size_bytes, mtime, sha256}`` for ``path`` (best-effort).

    Cluster-evidence-trustworthiness #7: artifact lists previously only
    exposed label/path/kind/exists. We add size/mtime/sha so the UI can
    render columns + tooltips that let the operator spot stale or
    tampered artifacts. The SHA is computed lazily on each read; for
    the typical certifier output (KB-MB sized JSON/PNG/log files) this
    is cheap. We truncate to 12 hex chars for display elsewhere — the
    full hash is still returned so callers can verify integrity.
    """
    info: dict[str, Any] = {"size_bytes": None, "mtime": None, "sha256": None}
    try:
        stat = path.stat()
    except OSError:
        return info
    info["size_bytes"] = stat.st_size
    info["mtime"] = (
        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    if path.is_file() and stat.st_size <= 16 * 1024 * 1024:
        try:
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    hasher.update(chunk)
            info["sha256"] = hasher.hexdigest()
        except OSError:
            info["sha256"] = None
    return info


def _queue_failure_log_fallback(
    project_dir: Path,
    record: Any,
    *,
    offset: int,
    limit_bytes: int,
) -> LogReadResult | None:
    if getattr(record, "domain", None) != "queue":
        return None
    status = str(getattr(record, "status", "") or "")
    if status not in {"failed", "cancelled", "interrupted", "stale"}:
        return None
    artifacts = getattr(record, "artifacts", {}) if isinstance(getattr(record, "artifacts", {}), dict) else {}
    primary_log = _optional_str(artifacts.get("primary_log_path"))
    if primary_log and Path(primary_log).expanduser().exists():
        return None
    fallback = _queue_missing_primary_log_excerpt(project_dir, record)
    if fallback is None:
        return None
    path, text = fallback
    raw = text.encode("utf-8", errors="replace")
    start = max(0, min(offset, len(raw)))
    chunk = raw[start : start + max(1, limit_bytes)]
    next_offset = start + len(chunk)
    return LogReadResult(
        path=str(path),
        offset=start,
        next_offset=next_offset,
        text=chunk.decode("utf-8", errors="replace"),
        exists=True,
        total_bytes=len(raw),
        eof=next_offset >= len(raw),
    )


def _failure_summary(project_dir: Path, record: Any, overlay: Any) -> dict[str, Any] | None:
    status = str(getattr(record, "status", "") or "")
    overlay_reason = overlay.reason if overlay is not None else None
    last_event = _optional_str(getattr(record, "last_event", None))
    if status not in {"failed", "cancelled", "interrupted"} and not overlay_reason:
        return None
    fallback = _queue_missing_primary_log_excerpt(project_dir, record)
    excerpt = fallback[1] if fallback is not None else None
    reason = overlay_reason or _specific_failure_reason(excerpt) or _friendly_exit_reason(last_event) or last_event or status
    return {
        "reason": reason,
        "last_event": last_event,
        "excerpt": excerpt,
        "source": str(fallback[0]) if fallback is not None else None,
    }


def _friendly_exit_reason(last_event: str | None) -> str | None:
    if last_event and last_event.startswith("exit_code="):
        return f"Process exited with {last_event}; no session artifacts were created. Open logs for the watcher excerpt."
    return None


def _specific_failure_reason(excerpt: str | None) -> str | None:
    if not excerpt:
        return None
    for line in excerpt.splitlines():
        stripped = _strip_queue_log_prefix(line.strip())
        if not stripped or stripped.startswith("Primary session log"):
            continue
        if "Fatal Python error" in stripped:
            return stripped
        if "OSError:" in stripped:
            return stripped
    for line in reversed(excerpt.splitlines()):
        stripped = _strip_queue_log_prefix(line.strip())
        if "exit_code=" in stripped or "failed" in stripped.lower():
            return stripped
    return None


def _queue_missing_primary_log_excerpt(project_dir: Path, record: Any) -> tuple[Path, str] | None:
    if getattr(record, "domain", None) != "queue":
        return None
    artifacts = getattr(record, "artifacts", {}) if isinstance(getattr(record, "artifacts", {}), dict) else {}
    primary_log = _optional_str(artifacts.get("primary_log_path"))
    if primary_log and Path(primary_log).expanduser().is_file():
        return None
    return _queue_failure_excerpt(project_dir, record)


def _strip_queue_log_prefix(line: str) -> str:
    if line.startswith("[") and "] " in line:
        return line.split("] ", 1)[1].strip()
    return line


def _queue_failure_excerpt(project_dir: Path, record: Any, *, max_lines: int = 80) -> tuple[Path, str] | None:
    task_id = _optional_str(getattr(record, "identity", {}).get("queue_task_id")) if isinstance(getattr(record, "identity", {}), dict) else None
    run_id = _optional_str(getattr(record, "run_id", None))
    if not task_id and not run_id:
        return None
    primary_needle = f"[{run_id}]" if run_id else None
    secondary_needle = f"[{task_id}]" if task_id else None
    for path in (
        paths.logs_dir(project_dir) / "web" / "watcher.log",
        paths.queue_dir(project_dir) / "watcher.log",
    ):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        matched = _watcher_excerpt_lines(lines, primary_needle=primary_needle, secondary_needle=secondary_needle, max_lines=max_lines)
        if not matched:
            continue
        excerpt = "\n".join(matched[-max_lines:])
        return (
            path,
            "Primary session log was not created. Showing watcher output for this task.\n\n"
            f"{excerpt}\n",
        )
    return None


def _watcher_excerpt_lines(
    lines: list[str],
    *,
    primary_needle: str | None,
    secondary_needle: str | None,
    max_lines: int,
) -> list[str]:
    for needle in (primary_needle, secondary_needle):
        if not needle:
            continue
        indexes = [index for index, line in enumerate(lines) if needle in line]
        if indexes:
            first_index = last_index = indexes[-1]
            while first_index > 0 and needle in lines[first_index - 1]:
                first_index -= 1
            return lines[first_index : last_index + 1][-max_lines:]
    return []


def _action_key(action: str) -> str:
    mapping = {
        "cancel": "c",
        "resume": "r",
        "retry": "R",
        "requeue": "R",
        "cleanup": "x",
        "remove": "x",
        "merge": "m",
        "open": "e",
        "approve-spec": "a",
        "approve_spec": "a",
        "regenerate-spec": "g",
        "request-spec-changes": "g",
    }
    return mapping.get(action, action)


def _normalize_web_build_spec_args(extra_args: list[str]) -> list[str]:
    args = list(extra_args)
    if "--spec" not in args:
        return args
    if "--yes" in args or "--spec-review-mode" in args:
        return args
    return [*args, "--spec-review-mode", "web"]


def _event_action_name(key: str, *, label: str | None = None, domain: str | None = None) -> str:
    if key == "R":
        normalized = str(label or "").strip().lower()
        if normalized == "retry" or domain in {"atomic", "merge"}:
            return "retry"
        return "requeue"
    return {
        "c": "cancel",
        "r": "resume",
        "a": "approve-spec",
        "g": "regenerate-spec",
        "x": "cleanup",
        "m": "merge",
        "e": "open",
    }.get(key, key)


def _event_severity(payload: dict[str, Any]) -> str:
    if payload.get("ok") is False:
        return "error"
    value = str(payload.get("severity") or "").strip().lower()
    if value == "information":
        return "info"
    if value in {"error", "warning", "info", "success"}:
        return value
    return "success"


def terminate_watcher_blocking(
    project_dir: Path,
    *,
    grace: float = 3.0,
    reason: str = "backend shutdown",
    fallback_pid: int | None = None,
) -> dict[str, Any]:
    """Force-terminate any watcher subprocess this project owns.

    Reads the supervisor metadata, sends ``SIGTERM`` to the watcher's
    process group (the watcher is launched with ``start_new_session=True``
    so its grandchildren — e.g. an in-flight ``otto build`` — share the
    pgid). Waits up to ``grace`` seconds for the leader to exit, then
    escalates to ``SIGKILL`` on the same pgid.

    Idempotent: if no supervisor metadata exists, or the watcher pid is
    already dead, returns a status dict with ``terminated=False`` and
    no side effects beyond a stop-record write.

    Returned dict keys:
    - ``terminated`` (bool): whether we sent any signal
    - ``pid`` (int | None): the watcher leader pid we targeted
    - ``pgid`` (int | None): the process group we signalled
    - ``escalated`` (bool): whether SIGKILL was needed after the grace
    - ``error`` (str | None): unexpected OS error string if any

    Used by:
    - ``MCBackend.stop()`` (test harness) to ensure no orphan survives a
      tempdir teardown.
    - FastAPI shutdown lifespan in ``otto/web/app.py`` for production.
    """

    result: dict[str, Any] = {
        "terminated": False,
        "pid": None,
        "pgid": None,
        "escalated": False,
        "error": None,
        "children": [],
    }
    try:
        metadata, _err = read_supervisor(project_dir)
    except Exception as exc:  # pragma: no cover — defensive
        result["error"] = f"supervisor read failed: {exc}"
        return result
    children = _queue_children_for_termination(project_dir)
    pid = metadata.get("watcher_pid") if metadata else fallback_pid
    if not isinstance(pid, int) or pid <= 0:
        if children:
            _terminate_queue_children(children, result, signal.SIGTERM)
            _wait_for_queue_children(children, grace)
            live_children = [child for child in children if _child_pid_alive(child)]
            if live_children:
                _terminate_queue_children(live_children, result, signal.SIGKILL)
                result["escalated"] = True
                _wait_for_queue_children(live_children, 1.0)
        return result
    result["pid"] = pid
    if not _pid_alive(pid):
        # Already dead — record the stop so health probes stop reporting it.
        if children:
            _terminate_queue_children(children, result, signal.SIGTERM)
            _wait_for_queue_children(children, grace)
            live_children = [child for child in children if _child_pid_alive(child)]
            if live_children:
                _terminate_queue_children(live_children, result, signal.SIGKILL)
                result["escalated"] = True
                _wait_for_queue_children(live_children, 1.0)
        try:
            record_watcher_stop(project_dir, target_pid=pid, reason=f"{reason} (already dead)")
        except Exception:
            pass
        return result

    pgid = _safe_getpgid(pid)
    result["pgid"] = pgid
    target_for_signal = pgid if pgid is not None else pid
    signaller = os.killpg if pgid is not None else os.kill
    try:
        signaller(target_for_signal, signal.SIGTERM)
        result["terminated"] = True
        _terminate_queue_children(children, result, signal.SIGTERM)
    except ProcessLookupError:
        _terminate_queue_children(children, result, signal.SIGTERM)
        try:
            record_watcher_stop(project_dir, target_pid=pid, reason=f"{reason} (lookup miss)")
        except Exception:
            pass
        return result
    except OSError as exc:
        result["error"] = f"SIGTERM failed: {exc}"
        return result

    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if not _pid_alive(pid) and not any(_child_pid_alive(child) for child in children):
            break
        time.sleep(0.05)

    live_children = [child for child in children if _child_pid_alive(child)]
    if _pid_alive(pid) or live_children:
        # Escalate to SIGKILL on the same target.
        if _pid_alive(pid):
            try:
                signaller(target_for_signal, signal.SIGKILL)
                result["escalated"] = True
            except ProcessLookupError:
                pass
            except OSError as exc:
                result["error"] = f"SIGKILL failed: {exc}"
        if live_children:
            _terminate_queue_children(live_children, result, signal.SIGKILL)
            result["escalated"] = True
        # Final brief wait for the kernel to reap.
        kill_deadline = time.monotonic() + 1.0
        while time.monotonic() < kill_deadline:
            if not _pid_alive(pid) and not any(_child_pid_alive(child) for child in children):
                break
            time.sleep(0.05)

    try:
        record_watcher_stop(project_dir, target_pid=pid, reason=reason)
    except Exception:
        pass
    return result


def _queue_children_for_termination(project_dir: Path) -> list[dict[str, Any]]:
    try:
        state = load_queue_state(project_dir)
    except (OSError, ValueError, TypeError):
        return []
    children: list[dict[str, Any]] = []
    for task_id, task_state in (state.get("tasks") or {}).items():
        if not isinstance(task_state, dict):
            continue
        if str(task_state.get("status") or "") not in IN_FLIGHT_STATUSES:
            continue
        child = task_state.get("child")
        if not isinstance(child, dict):
            continue
        normalized = {**child, "task_id": str(task_id)}
        if child_is_alive(normalized):
            children.append(normalized)
    return children


def _terminate_queue_children(
    children: list[dict[str, Any]],
    result: dict[str, Any],
    sig: signal.Signals | int,
) -> None:
    sent_signal = getattr(sig, "name", str(sig))
    for child in children:
        pid = child.get("pid")
        pgid = child.get("pgid")
        error = None
        try:
            sent = kill_child_safely(child, int(sig))
        except OSError as exc:
            sent = False
            error = str(exc)
            result["error"] = result.get("error") or f"child signal failed: {exc}"
        if sent:
            result["terminated"] = True
        child_result = {
            "task_id": child.get("task_id"),
            "pid": pid,
            "pgid": pgid,
            "signal": sent_signal,
            "sent": sent,
        }
        if error:
            child_result["error"] = error
        result["children"].append(child_result)


def _child_pid_alive(child: dict[str, Any]) -> bool:
    pid = child.get("pid")
    return isinstance(pid, int) and _pid_alive(pid)


def _wait_for_queue_children(children: list[dict[str, Any]], grace: float) -> None:
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if not any(_child_pid_alive(child) for child in children):
            return
        time.sleep(0.05)


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is a running, non-zombie process.

    A zombie process still answers ``os.kill(pid, 0)`` until its parent
    reaps it, but it consumes no resources and should not block shutdown.
    We try ``psutil`` first to detect zombies; if psutil isn't available
    or fails, fall back to ``os.kill(pid, 0)`` semantics.
    """

    try:
        import psutil  # local import keeps this helper standalone
    except ImportError:  # pragma: no cover — psutil is a runtime dep
        psutil = None  # type: ignore[assignment]

    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            return proc.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True
        except Exception:
            pass  # fall through to os.kill

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _safe_getpgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def _watcher_stop_identity_issue(project_dir: Path, pid: int, health: dict[str, Any]) -> str | None:
    lock_pid = _int_or_none(health.get("lock_pid"))
    if lock_pid == pid:
        return None
    supervisor, supervisor_error = read_supervisor(project_dir)
    supervisor_pid = _int_or_none(supervisor.get("watcher_pid") if supervisor else None)
    if supervisor_pid == pid:
        return None
    if supervisor_error:
        return f"Refusing to stop watcher pid {pid}; supervisor metadata is unreadable: {supervisor_error}"
    return (
        f"Refusing to stop pid {pid}; Mission Control could not verify that it owns the watcher. "
        "Use a terminal if this process must be stopped manually."
    )


def _merge_target(project_dir: Path) -> str:
    try:
        cfg = load_config(project_dir / "otto.yaml")
    except Exception:
        cfg = {}
    return str(cfg.get("default_branch") or "main")


def _merge_preflight(project_dir: Path) -> dict[str, Any]:
    try:
        issues = repo_preflight_issues(project_dir)
    except Exception as exc:
        return {
            "merge_blocked": True,
            "merge_blockers": [f"merge preflight failed: {exc}"],
            "dirty_files": [],
        }
    # The merge action MUST refuse on user-owned untracked files in the
    # project root (W5-CRITICAL-1). build/improve preflights tolerate
    # untracked-only state via ``ensure_safe_repo_state``; the merge
    # action does not, because landing code while the operator has
    # uncommitted user files is the silent-merge footgun the W5 bench
    # uncovered. ``untracked`` lives in its own preflight category so
    # the merge consumer can opt in without changing build/improve
    # semantics.
    blockers = [
        *issues.get("blocking", []),
        *issues.get("dirty", []),
        *issues.get("untracked", []),
    ]
    return {
        "merge_blocked": bool(blockers),
        "merge_blockers": blockers,
        "dirty_files": list(issues.get("dirty_files", []) or []),
    }


def _ensure_merge_unblocked(project_dir: Path) -> None:
    preflight = _merge_preflight(project_dir)
    if not preflight["merge_blocked"]:
        return
    blockers = "; ".join(preflight["merge_blockers"]) or "repository is not merge-ready"
    dirty_files = list(preflight.get("dirty_files", []) or [])
    suffix = ""
    if dirty_files:
        suffix = f" Affected paths: {', '.join(dirty_files[:5])}"
        if len(dirty_files) > 5:
            suffix += f", ... (+{len(dirty_files) - 5} more)"
        suffix += "."
    raise MissionControlServiceError(
        f"Merge blocked by local repository state: {blockers}.{suffix} "
        "Commit, stash, or revert these project changes before merging.",
        status_code=409,
    )


def _merge_preflight_review_blocker(preflight: dict[str, Any]) -> str:
    dirty_files = list(preflight.get("dirty_files", []) or [])
    if dirty_files:
        preview = ", ".join(str(path) for path in dirty_files[:3])
        if len(dirty_files) > 3:
            preview += f", ... (+{len(dirty_files) - 3} more)"
        return f"Repository has local changes: {preview}."
    blockers = [str(item) for item in preflight.get("merge_blockers", []) or [] if item]
    if blockers:
        return f"Repository is not ready to land: {'; '.join(blockers)}."
    return "Repository is not ready to land."


def _merged_branch_index(project_dir: Path, target: str) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for state_path in sorted(paths.merge_dir(project_dir).glob("*/state.json")):
        try:
            state = load_merge_state(project_dir, state_path.parent.name)
        except Exception:
            continue
        if str(state.target or "") != target:
            continue
        for outcome in state.outcomes:
            if outcome.status not in {"merged", "conflict_resolved"}:
                continue
            if outcome.merge_commit and not _merge_commit_reachable(project_dir, outcome.merge_commit, target):
                continue
            diff_base = (
                _merge_commit_first_parent(project_dir, outcome.merge_commit)
                if outcome.merge_commit
                else _optional_str(state.target_head_before)
            )
            merged[outcome.branch] = {
                "merge_id": state.merge_id,
                "status": outcome.status,
                "merge_run_status": state.status,
                "target_head_before": state.target_head_before,
                "merge_commit": outcome.merge_commit,
                "diff_base": diff_base,
            }
    return merged


def _merge_commit_first_parent(project_dir: Path, merge_commit: str | None) -> str | None:
    commit = _optional_str(merge_commit)
    if not commit:
        return None
    result = git_ops.run_git(project_dir, "rev-parse", "--verify", f"{commit}^1")
    if not result.ok:
        return None
    return result.stdout.strip() or None


def _merge_commit_reachable(project_dir: Path, merge_commit: str, target: str) -> bool:
    result = git_ops.run_git(project_dir, "merge-base", "--is-ancestor", merge_commit, target)
    return result.returncode == 0


def _commit_range_diff(project_dir: Path, base: str, head: str) -> dict[str, Any]:
    result = git_ops.run_git(project_dir, "diff", "--name-only", base, head)
    command = f"git diff {base} {head}"
    if not result.ok:
        detail = (result.stderr or result.stdout or f"git diff exited {result.returncode}").strip()
        return {"files": [], "error": detail, "command": command}
    return {
        "files": sorted(line for line in result.stdout.splitlines() if line),
        "error": None,
        "command": command,
    }


def _commit_range_diff_text(project_dir: Path, base: str, head: str) -> dict[str, Any]:
    result = git_ops.run_git(project_dir, "diff", "--no-ext-diff", "--no-color", base, head)
    if not result.ok:
        detail = (result.stderr or result.stdout or f"git diff exited {result.returncode}").strip()
        return {"text": "", "error": detail}
    return {"text": result.stdout, "error": None}


def _branch_diff(project_dir: Path, branch: str | None, target: str) -> dict[str, Any]:
    branch = str(branch or "").strip()
    target = str(target or "").strip()
    if not branch or not target or branch == target:
        return {"files": [], "error": None}
    target_ref = _git_diff_ref(project_dir, target)
    branch_ref = _git_diff_ref(project_dir, branch)
    result = git_ops.run_git(project_dir, "diff", "--name-only", f"{target_ref}...{branch_ref}")
    if not result.ok:
        detail = (result.stderr or result.stdout or f"git diff exited {result.returncode}").strip()
        return {"files": [], "error": detail}
    return {"files": sorted(line for line in result.stdout.splitlines() if line), "error": None}


def _branch_diff_text(project_dir: Path, branch: str | None, target: str) -> dict[str, Any]:
    branch = str(branch or "").strip()
    target = str(target or "").strip()
    if not branch or not target or branch == target:
        return {"text": "", "error": None}
    target_ref = _git_diff_ref(project_dir, target)
    branch_ref = _git_diff_ref(project_dir, branch)
    result = git_ops.run_git(project_dir, "diff", "--no-ext-diff", "--no-color", f"{target_ref}...{branch_ref}")
    if not result.ok:
        detail = (result.stderr or result.stdout or f"git diff exited {result.returncode}").strip()
        return {"text": "", "error": detail}
    return {"text": result.stdout, "error": None}


def _resolve_sha(project_dir: Path, ref: str) -> tuple[str | None, str | None]:
    """Resolve ``ref`` to a 40-char SHA via ``git rev-parse``.

    Returns ``(sha, error)``. ``error`` is ``None`` on success and a
    short human-readable string when the lookup failed (so the UI can
    distinguish "branch missing" from "git unavailable").
    """
    ref = (ref or "").strip()
    if not ref:
        return None, "ref is empty"
    resolved_ref = _git_diff_ref(project_dir, ref)
    result = git_ops.run_git(project_dir, "rev-parse", "--verify", "--quiet", f"{resolved_ref}^{{commit}}")
    if not result.ok:
        detail = (result.stderr or result.stdout or f"rev-parse exited {result.returncode}").strip()
        return None, detail or f"could not resolve {ref}"
    sha = result.stdout.strip()
    if not sha:
        return None, f"could not resolve {ref}"
    return sha, None


def _resolve_merge_base(project_dir: Path, target: str, branch: str) -> tuple[str | None, str | None]:
    target_ref = _git_diff_ref(project_dir, target)
    branch_ref = _git_diff_ref(project_dir, branch)
    result = git_ops.run_git(project_dir, "merge-base", target_ref, branch_ref)
    if not result.ok:
        detail = (result.stderr or result.stdout or f"merge-base exited {result.returncode}").strip()
        return None, detail or f"could not compute merge-base({target}, {branch})"
    sha = result.stdout.strip()
    if not sha:
        return None, f"could not compute merge-base({target}, {branch})"
    return sha, None


def _diff_freshness_shas(
    project_dir: Path,
    *,
    branch: str | None,
    target: str,
    merge_info: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None, list[str]]:
    """Capture target HEAD, branch HEAD, and merge-base SHAs at fetch time.

    For already-merged tasks we prefer the recorded SHAs from the merge
    state (``target_head_before`` / ``merge_commit``) over re-resolving
    the live refs — those refs may have moved on past the merge.
    Returns ``(target_sha, branch_sha, merge_base, errors)``.
    """
    errors: list[str] = []
    target_sha: str | None = None
    branch_sha: str | None = None
    merge_base: str | None = None
    if merge_info is not None:
        # Merged task: snapshot what was actually merged. The "target_sha"
        # is the target HEAD at merge time; the "branch_sha" is the merge
        # commit's second parent (the tip of the branch as merged); the
        # merge-base is recorded as ``diff_base`` when available.
        target_sha = _optional_str(merge_info.get("target_head_before"))
        merge_commit = _optional_str(merge_info.get("merge_commit"))
        if merge_commit:
            branch_tip = _merge_commit_branch_parent(project_dir, merge_commit)
            branch_sha = branch_tip or merge_commit
        else:
            branch_sha = None
        merge_base = _optional_str(merge_info.get("diff_base")) or target_sha
        if target_sha is None:
            errors.append("target_head_before missing from merge state")
        if branch_sha is None:
            errors.append("merge_commit missing from merge state")
        return target_sha, branch_sha, merge_base, errors

    # Live diff path: rev-parse each ref. Lookups fail independently so the
    # UI can warn the operator about exactly which side is unverifiable.
    if not target:
        errors.append("target ref is empty")
    else:
        target_sha, err = _resolve_sha(project_dir, target)
        if err:
            errors.append(f"target {target}: {err}")
    branch_str = (branch or "").strip()
    if not branch_str or branch_str == target:
        # Same-branch diff is empty; nothing meaningful to report.
        pass
    else:
        branch_sha, err = _resolve_sha(project_dir, branch_str)
        if err:
            errors.append(f"branch {branch_str}: {err}")
    if target and branch_str and branch_str != target and target_sha and branch_sha:
        merge_base, err = _resolve_merge_base(project_dir, target, branch_str)
        if err:
            errors.append(f"merge-base: {err}")
    return target_sha, branch_sha, merge_base, errors


def _merge_commit_branch_parent(project_dir: Path, merge_commit: str) -> str | None:
    """Return the second parent of ``merge_commit`` (tip of merged branch).

    ``merge --no-ff`` produces a commit whose first parent is the target's
    prior HEAD and whose second parent is the merged-in branch tip. For
    fast-forward merges there is only one parent and the branch tip equals
    the merge commit itself.
    """
    result = git_ops.run_git(project_dir, "rev-list", "--parents", "-n", "1", merge_commit)
    if not result.ok:
        return None
    parts = result.stdout.strip().split()
    if len(parts) >= 3:
        return parts[2]
    return None


def _git_diff_ref(project_dir: Path, ref: str) -> str:
    if git_ops.run_git(project_dir, "rev-parse", "--verify", "--quiet", ref).ok:
        return ref
    remote_ref = f"origin/{ref}"
    if git_ops.run_git(project_dir, "rev-parse", "--verify", "--quiet", remote_ref).ok:
        return remote_ref
    return ref


def _branch_changed_files(project_dir: Path, branch: str | None, target: str) -> list[str]:
    return list(_branch_diff(project_dir, branch, target)["files"])


def _landing_collisions(project_dir: Path, ready_tasks: list[Any], target: str) -> list[dict[str, Any]]:
    if len(ready_tasks) < 2:
        return []
    files_by_id: dict[str, set[str]] = {}
    for task in ready_tasks:
        branch = str(getattr(task, "branch", "") or "").strip()
        if not branch:
            continue
        files_by_id[task.id] = set(_branch_diff(project_dir, branch, target)["files"])
    collisions: list[dict[str, Any]] = []
    ids = [task.id for task in ready_tasks if task.id in files_by_id]
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            common = sorted(files_by_id[left] & files_by_id[right])
            if not common:
                continue
            collisions.append(
                {
                    "left": left,
                    "right": right,
                    "files": common[:6],
                    "file_count": len(common),
                }
            )
    return collisions


def _task_run_id(raw_state: Any) -> str | None:
    if isinstance(raw_state, dict):
        value = raw_state.get("attempt_run_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _empty_landing_counts() -> dict[str, int]:
    return {**{key: 0 for key in LANDING_COUNT_KEYS}, "total": 0}


def _landing_task_diff(
    project_dir: Path,
    *,
    target: str,
    branch: str,
    queue_status: str,
    merge_info: dict[str, Any] | None,
    certification_only: bool,
) -> dict[str, Any]:
    if merge_info is not None:
        return _merged_task_diff(project_dir, merge_info)
    if certification_only or queue_status in REVIEW_IN_PROGRESS_STATUSES:
        return {"files": [], "error": None}
    return _branch_diff(project_dir, branch, target)


def _classify_landing_task(
    *,
    queue_status: str,
    branch: str,
    diff: dict[str, Any],
    merge_info: dict[str, Any] | None,
    certification_only: bool,
) -> LandingClassification:
    if merge_info is not None:
        return LandingClassification(state="merged", label="Landed", count_key="merged")
    if queue_status == "done" and certification_only:
        return LandingClassification(state="reviewed", label="Certified", count_key="reviewed")
    if queue_status == "done" and branch and diff.get("error") is None:
        return LandingClassification(
            state="ready",
            label="Ready to land",
            count_key="ready",
            counts_for_collision=True,
        )
    label = "Review blocked" if queue_status == "done" and diff.get("error") else _blocked_landing_label(queue_status, branch)
    return LandingClassification(state="blocked", label=label, count_key="blocked")


def _task_intent(argv: Any) -> str | None:
    if isinstance(argv, list) and len(argv) > 1 and isinstance(argv[1], str):
        return argv[1]
    return None


def _queue_display_status(raw_state: dict[str, Any] | None, queue_state: dict[str, Any]) -> str:
    status = task_display_status(raw_state)
    if status not in IN_FLIGHT_STATUSES:
        return status
    if watcher_alive(queue_state):
        return status
    child = raw_state.get("child") if isinstance(raw_state, dict) else None
    if isinstance(child, dict) and child_is_alive(child):
        return status
    return "stale"


def _blocked_landing_label(queue_status: str, branch: str) -> str:
    if not branch:
        return "No branch"
    if queue_status == "queued":
        return "Queued"
    if queue_status in {"starting", "initializing", "running", "terminating"}:
        return "In progress"
    if queue_status in {"failed", "cancelled", "interrupted", "stale"}:
        return "Needs attention"
    return "Not ready"


def _number_from_mapping(raw_state: Any, key: str) -> int | float | None:
    if not isinstance(raw_state, dict):
        return None
    value = raw_state.get(key)
    return value if isinstance(value, (int, float)) else None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _required_str(value: Any, field: str) -> str:
    text = _optional_str(value)
    if text is None:
        raise MissionControlServiceError(f"{field} is required", status_code=400)
    return text


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MissionControlServiceError("expected a list of strings", status_code=400)
    return [str(item) for item in value if str(item).strip()]


def _landing_recovery_needed(landing: dict[str, Any]) -> bool:
    if not bool(landing.get("merge_blocked")):
        return False
    blockers = landing.get("merge_blockers") if isinstance(landing.get("merge_blockers"), list) else []
    text = " ".join(str(item).lower() for item in blockers)
    return "merge in progress" in text or "unmerged path" in text


def _superseded_failed_task_ids(landing: dict[str, Any]) -> list[str]:
    items = landing.get("items") if isinstance(landing.get("items"), list) else []
    landed_signatures = {
        _summary_signature(item.get("summary"))
        for item in items
        if isinstance(item, dict) and str(item.get("landing_state") or "") == "merged"
    }
    landed_signatures.discard("")
    if not landed_signatures:
        return []
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("queue_status") or "")
        if status not in {"failed", "interrupted", "cancelled", "stale"}:
            continue
        if str(item.get("landing_state") or "") != "blocked":
            continue
        signature = _summary_signature(item.get("summary"))
        task_id = _optional_str(item.get("task_id"))
        if signature and task_id and signature in landed_signatures:
            out.append(task_id)
    return out


def _blocked_attention_task_ids(landing: dict[str, Any]) -> list[str]:
    items = landing.get("items") if isinstance(landing.get("items"), list) else []
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("queue_status") or "")
        task_id = _optional_str(item.get("task_id"))
        if status in {"failed", "interrupted", "cancelled", "stale"} and task_id:
            out.append(task_id)
    return out


def _summary_signature(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return text[:500]


def _validate_inner_command_args(command: str, raw_args: list[str]) -> None:
    """Validate web-queued passthrough args before writing a queue task."""
    try:
        from otto.cli import main

        if command == "improve":
            subcommand = raw_args[0] if raw_args else ""
            improve_group = main.commands["improve"]
            target = improve_group.commands[str(subcommand)]
            argv = raw_args[1:]
            label = f"otto improve {subcommand}"
        else:
            target = main.commands[command]
            argv = raw_args
            label = f"otto {command}"
        ctx = click.Context(target, info_name=target.name)
        target.parse_args(ctx, list(argv))
    except KeyError as exc:
        raise MissionControlServiceError(f"Unsupported queue command: {command}", status_code=400) from exc
    except click.UsageError as exc:
        message = exc.format_message().strip()
        if message.startswith("Error: "):
            message = message[len("Error: "):]
        raise MissionControlServiceError(
            f"Unsupported options for `{label}`: {message}",
            status_code=400,
        ) from exc
    finally:
        if "ctx" in locals():
            ctx.close()


def _tail_text(path: Path, *, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace").strip()
