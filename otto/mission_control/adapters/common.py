"""Shared Mission Control adapter helpers."""

from __future__ import annotations

from pathlib import Path

from otto import paths
from otto.mission_control.model import ArtifactRef


def artifact_ref_for_path(path: str, *, fallback_label: str = "artifact") -> ArtifactRef:
    candidate = Path(path)
    label = _artifact_label(candidate, fallback_label=fallback_label)
    kind = _artifact_kind(candidate)
    return ArtifactRef.from_path(label, path, kind=kind)


def expanded_artifact_paths(path: str) -> list[str]:
    candidate = Path(path)
    paths = [path]
    if candidate.name == "proof-of-work.html":
        for sibling in (candidate.with_name("proof-of-work.md"), candidate.with_name("proof-of-work.json")):
            if sibling.exists():
                paths.append(str(sibling))
    return paths


def supplemental_session_artifact_paths(session_dir: str | None) -> list[str]:
    """Return useful session artifacts not always persisted in old run records."""
    if not session_dir:
        return []
    root = Path(session_dir)
    certify_dir = root / "certify"
    candidates = [
        root / "product-handoff.json",
        root / "product-playbook.json",
        certify_dir / "verification-plan.json",
        certify_dir / "proof-of-work.html",
        certify_dir / "narrative.log",
        certify_dir / "messages.jsonl",
    ]
    paths: list[str] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        if candidate.name == "proof-of-work.html":
            paths.extend(expanded_artifact_paths(str(candidate)))
        else:
            paths.append(str(candidate))
    evidence_dir = certify_dir / "evidence"
    if evidence_dir.exists() and evidence_dir.is_dir():
        for candidate in sorted(evidence_dir.iterdir(), key=lambda item: item.name):
            if not candidate.is_file() or candidate.name.endswith(".manifest.json"):
                continue
            paths.append(str(candidate))
    return paths


def durable_session_dir_for_record(record: object) -> Path | None:
    run_id = str(getattr(record, "run_id", "") or "").strip()
    project_dir = str(getattr(record, "project_dir", "") or "").strip()
    domain = str(getattr(record, "domain", "") or "").strip()
    if not run_id or not project_dir or domain == "merge":
        return None
    return paths.session_dir(Path(project_dir), run_id).resolve(strict=False)


def resolve_session_artifact_path(path: str, *, stale_session_dir: str | None, durable_session_dir: Path | None) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    candidate = Path(text).expanduser()
    if candidate.exists():
        return str(candidate)
    if not stale_session_dir or durable_session_dir is None or not durable_session_dir.exists():
        return text
    stale_root = Path(stale_session_dir).expanduser().resolve(strict=False)
    try:
        relative = candidate.resolve(strict=False).relative_to(stale_root)
    except ValueError:
        return text
    fallback = durable_session_dir / relative
    if fallback.exists():
        return str(fallback)
    return text


def session_artifact_roots(stale_session_dir: str | None, durable_session_dir: Path | None) -> list[str]:
    roots: list[str] = []
    if stale_session_dir:
        stale = Path(stale_session_dir).expanduser().resolve(strict=False)
        if stale.exists() and stale.is_dir():
            roots.append(str(stale))
    if durable_session_dir is not None and durable_session_dir.exists() and durable_session_dir.is_dir():
        durable = str(durable_session_dir)
        if durable not in roots:
            roots.append(durable)
    return roots


def _artifact_label(path: Path, *, fallback_label: str) -> str:
    name = path.name
    if name == "proof-of-work.html":
        return "proof report"
    if name == "proof-of-work.md":
        return "proof markdown"
    if name == "proof-of-work.json":
        return "proof json"
    if name == "verification-plan.json":
        return "verification plan"
    if name == "product-handoff.json":
        return "product handoff"
    if name == "product-playbook.json":
        return "product playbook"
    if name == "messages.jsonl":
        if path.parent.name == "certify":
            return "certifier messages"
        return "messages"
    if name == "narrative.log":
        if path.parent.name == "certify":
            return "certifier log"
        return "primary log"
    if name:
        return name
    return fallback_label


def _artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".log", ".jsonl"}:
        return "log"
    if suffix in {".json"}:
        return "json"
    if suffix in {".md", ".markdown", ".txt"}:
        return "text"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"
    if suffix in {".webm", ".mp4", ".mov"}:
        return "video"
    return "file"
