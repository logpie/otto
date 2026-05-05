"""Seed 3 synthetic Mission Control sessions for the A4 RUA exercise.

Creates a project dir layout:

    <project_dir>/
        otto_logs/
            sessions/
                <sid-passed>/
                    spec/spec.json
                    spec/spec.md
                    spec/lifecycle.json (draft)
                    proof-packet.json
                    spec-state.jsonl
                <sid-partial>/
                    ...
                <sid-blocked>/
                    ...

The three sessions cover:
    a) all features passed
    b) one feature partial (warn) + one missing
    c) one feature blocked / failed (audit failure)

Run:
    python scripts/rua/seed_fixture_sessions.py /tmp/rua-fixture-project
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from otto.spec_compile import (
    Feature,
    Group,
    Guardrail,
    Spec,
    lock_intent,
    render_spec_md,
    spec_to_dict,
)


def _session_id(slug: str, suffix: str) -> str:
    # session id format: <yyyy-mm-dd>-<HHMMSS>-<6hex>
    return f"2026-05-04-{slug}-{suffix}"


def _make_spec_passed() -> Spec:
    raw = Spec(
        intent="A small markdown note-taking webapp with login, list, and edit views.",
        project_kind="webapp",
        groups=[
            Group(id="auth", title="Authentication"),
            Group(id="notes", title="Notes"),
        ],
        features=[
            Feature(
                id="login",
                name="Email + password login",
                description="Users sign in with email and password and land on /notes.",
                evidence_kinds=["BrowserJourney"],
                group_id="auth",
            ),
            Feature(
                id="list-notes",
                name="List user notes",
                description="GET /notes shows the signed-in user's notes in reverse chronological order.",
                evidence_kinds=["BrowserJourney"],
                group_id="notes",
            ),
            Feature(
                id="edit-note",
                name="Edit a note",
                description="Clicking a note opens an editor that saves on blur.",
                evidence_kinds=["BrowserJourney"],
                group_id="notes",
            ),
        ],
        guardrails=[
            Guardrail(id="no-oauth", text="No OAuth — email + password only.", applies_to="*"),
        ],
    )
    return lock_intent(raw)


def _make_spec_partial() -> Spec:
    raw = Spec(
        intent="A real-time chat app with rooms, DMs, and presence indicators.",
        project_kind="webapp",
        groups=[
            Group(id="rooms", title="Rooms"),
            Group(id="dm", title="Direct messages"),
            Group(id="presence", title="Presence"),
        ],
        features=[
            Feature(
                id="join-room",
                name="Join a room",
                description="User picks a room from the sidebar and sees the message log.",
                evidence_kinds=["BrowserJourney"],
                group_id="rooms",
            ),
            Feature(
                id="send-dm",
                name="Send a direct message",
                description="User opens a DM thread and posts a message; recipient sees it.",
                evidence_kinds=["BrowserJourney"],
                group_id="dm",
                multi_actor_required=True,
            ),
            Feature(
                id="presence",
                name="Online presence",
                description="A green dot shows next to online users in the room sidebar.",
                evidence_kinds=["BrowserJourney"],
                group_id="presence",
            ),
        ],
        guardrails=[
            Guardrail(id="no-mobile", text="Desktop browser only — mobile out of scope.", applies_to="*"),
        ],
    )
    return lock_intent(raw)


def _make_spec_blocked() -> Spec:
    raw = Spec(
        intent="A mini e-commerce store with product list, cart, and Stripe checkout.",
        project_kind="webapp",
        groups=[
            Group(id="catalog", title="Catalog"),
            Group(id="cart", title="Cart"),
            Group(id="checkout", title="Checkout"),
        ],
        features=[
            Feature(
                id="browse",
                name="Browse products",
                description="Anonymous user sees a grid of products with name + price.",
                evidence_kinds=["BrowserJourney"],
                group_id="catalog",
            ),
            Feature(
                id="add-to-cart",
                name="Add to cart",
                description="Clicking 'Add' appends item to cart; cart count badge updates.",
                evidence_kinds=["BrowserJourney"],
                group_id="cart",
            ),
            Feature(
                id="checkout",
                name="Stripe checkout",
                description="Submitting cart redirects to a Stripe-hosted checkout flow.",
                evidence_kinds=["BrowserJourney"],
                group_id="checkout",
            ),
        ],
        guardrails=[
            Guardrail(id="no-stripe-keys", text="Use Stripe test keys only.", applies_to="checkout"),
        ],
    )
    return lock_intent(raw)


def _proof_passed(spec: Spec) -> dict:
    return {
        "schema_version": 1,
        "intent": spec.intent,
        "project_kind": spec.project_kind,
        "verdict": "passed",
        "wall_s": 312.5,
        "cost_usd": 1.84,
        "groups": [
            {
                "id": g.id,
                "name": g.title,
                "title": g.title,
                "feature_ids": [f.id for f in spec.features if f.group_id == g.id],
                "status": "passing",
                "branch": f"{g.id}-branch",
                "owned_paths": [],
                "cost_usd": 0.6,
                "wall_s": 95.0,
            }
            for g in spec.groups
        ],
        "features": [
            {
                "feature_id": f.id,
                "name": f.name,
                "verdict": "passed",
                "group_id": f.group_id,
                "evidence_completeness": "full",
                "coverage_confidence": "high",
                "description": f.description,
            }
            for f in spec.features
        ],
        "components": [],
        "guardrails": [
            {"id": g.id, "text": g.text, "applies_to": g.applies_to, "verified": True}
            for g in spec.guardrails
        ],
        "quality_findings": [
            {
                "severity": "polish",
                "text": "Login page uses default fonts; could match brand voice.",
                "feature_id": "login",
            }
        ],
    }


def _proof_partial(spec: Spec) -> dict:
    feature_outcomes = {
        "join-room": ("passed", "full", "high"),
        "send-dm": ("partial", "proxy_only", "medium"),
        "presence": ("missing", "partial", "low"),
    }
    return {
        "schema_version": 1,
        "intent": spec.intent,
        "project_kind": spec.project_kind,
        "verdict": "partial",
        "wall_s": 421.0,
        "cost_usd": 2.91,
        "groups": [
            {
                "id": g.id,
                "name": g.title,
                "title": g.title,
                "feature_ids": [f.id for f in spec.features if f.group_id == g.id],
                "status": "passing" if g.id == "rooms" else "warn",
                "branch": f"{g.id}-branch",
                "owned_paths": [],
                "cost_usd": 0.9,
                "wall_s": 120.0,
            }
            for g in spec.groups
        ],
        "features": [
            {
                "feature_id": f.id,
                "name": f.name,
                "verdict": feature_outcomes[f.id][0],
                "group_id": f.group_id,
                "evidence_completeness": feature_outcomes[f.id][1],
                "coverage_confidence": feature_outcomes[f.id][2],
                "multi_actor_required": f.multi_actor_required,
                "description": f.description,
            }
            for f in spec.features
        ],
        "components": [
            {
                "id": "ws-hub",
                "name": "WebSocket hub",
                "owned_paths": ["realtime/"],
                "consumed_by": ["dm-delivery", "presence"],
                "status": "warn",
            }
        ],
        "guardrails": [
            {"id": g.id, "text": g.text, "applies_to": g.applies_to, "verified": True}
            for g in spec.guardrails
        ],
        "quality_findings": [
            {
                "severity": "blocking",
                "text": "Presence indicator never refreshed without a manual reload.",
                "feature_id": "presence",
            },
            {
                "severity": "polish",
                "text": "DM notifications could include sender avatar.",
                "feature_id": "send-dm",
            },
        ],
    }


def _proof_blocked(spec: Spec) -> dict:
    feature_outcomes = {
        "browse": ("passed", "full", "high"),
        "add-to-cart": ("partial", "partial", "medium"),
        "checkout": ("blocked", "proxy_only", "low"),
    }
    return {
        "schema_version": 1,
        "intent": spec.intent,
        "project_kind": spec.project_kind,
        "verdict": "blocked",
        "wall_s": 287.0,
        "cost_usd": 2.05,
        "groups": [
            {
                "id": g.id,
                "name": g.title,
                "title": g.title,
                "feature_ids": [f.id for f in spec.features if f.group_id == g.id],
                "status": "passing" if g.id == "catalog" else ("warn" if g.id == "cart" else "blocked"),
                "branch": f"{g.id}-branch",
                "owned_paths": [],
                "cost_usd": 0.7,
                "wall_s": 90.0,
            }
            for g in spec.groups
        ],
        "features": [
            {
                "feature_id": f.id,
                "name": f.name,
                "verdict": feature_outcomes[f.id][0],
                "group_id": f.group_id,
                "evidence_completeness": feature_outcomes[f.id][1],
                "coverage_confidence": feature_outcomes[f.id][2],
                "description": f.description,
            }
            for f in spec.features
        ],
        "components": [],
        "guardrails": [
            {
                "id": g.id,
                "text": g.text,
                "applies_to": g.applies_to,
                "verified": g.id != "no-stripe-keys",
            }
            for g in spec.guardrails
        ],
        "quality_findings": [
            {
                "severity": "blocking",
                "text": "Stripe checkout fails — env var STRIPE_SECRET_KEY missing in test harness.",
                "feature_id": "checkout",
            },
            {
                "severity": "blocking",
                "text": "Cart count badge not updating after add (stale state).",
                "feature_id": "add-to-cart",
            },
        ],
    }


def _state_events(verdict: str) -> list[dict]:
    base = "2026-05-04T20:00:00Z"
    events = [
        {"event": "run.started", "ts": base},
        {"event": "stage.compile.started", "ts": "2026-05-04T20:00:01Z"},
        {"event": "stage.compile.finished", "ts": "2026-05-04T20:00:30Z", "duration_s": 29.0},
        {"event": "stage.spec_review.started", "ts": "2026-05-04T20:00:31Z"},
        {"event": "stage.spec_review.finished", "ts": "2026-05-04T20:01:05Z", "duration_s": 34.0},
        {"event": "stage.build.started", "ts": "2026-05-04T20:01:06Z"},
        {"event": "stage.build.finished", "ts": "2026-05-04T20:04:30Z", "duration_s": 204.0},
        {"event": "stage.audit.started", "ts": "2026-05-04T20:04:31Z"},
        {"event": "stage.audit.finished", "ts": "2026-05-04T20:05:30Z", "duration_s": 59.0},
        {"event": "stage.render.started", "ts": "2026-05-04T20:05:31Z"},
        {"event": "stage.render.finished", "ts": "2026-05-04T20:06:00Z", "duration_s": 29.0},
    ]
    if verdict == "passed":
        events.append({"event": "stage.land.started", "ts": "2026-05-04T20:06:01Z"})
        events.append({"event": "stage.land.finished", "ts": "2026-05-04T20:06:30Z", "duration_s": 29.0})
        events.append({"event": "run.finished", "ts": "2026-05-04T20:06:31Z", "verdict": "passed"})
    else:
        events.append({"event": "run.finished", "ts": "2026-05-04T20:06:01Z", "verdict": verdict})
    return events


def _write_session(
    project: Path,
    sid: str,
    spec: Spec,
    proof: dict,
    state_events: list[dict],
) -> Path:
    session = project / "otto_logs" / "sessions" / sid
    spec_d = session / "spec"
    spec_d.mkdir(parents=True, exist_ok=True)
    (spec_d / "spec.json").write_text(
        json.dumps(spec_to_dict(spec), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (spec_d / "spec.md").write_text(render_spec_md(spec), encoding="utf-8")
    (spec_d / "lifecycle.json").write_text(
        json.dumps(
            {
                "lifecycle": "draft",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (session / "proof-packet.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    (session / "spec-state.jsonl").write_text(
        "\n".join(json.dumps(e) for e in state_events) + "\n",
        encoding="utf-8",
    )
    # Also write summary.json for completeness
    (session / "summary.json").write_text(
        json.dumps(
            {
                "session_id": sid,
                "intent": spec.intent,
                "verdict": proof["verdict"],
                "cost_usd": proof["cost_usd"],
                "wall_s": proof["wall_s"],
                "status": "completed",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return session


def main(project_dir: Path) -> list[str]:
    project_dir = project_dir.resolve()
    project_dir.mkdir(parents=True, exist_ok=True)

    specs = [
        ("passed", _make_spec_passed(), _proof_passed),
        ("partial", _make_spec_partial(), _proof_partial),
        ("blocked", _make_spec_blocked(), _proof_blocked),
    ]

    sids: list[str] = []
    for slug, spec, proof_fn in specs:
        sid = _session_id(slug, "abc123")
        proof = proof_fn(spec)
        events = _state_events(proof["verdict"])
        path = _write_session(project_dir, sid, spec, proof, events)
        print(f"seeded {slug:8s} -> {path}")
        sids.append(sid)

    return sids


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "/tmp/rua-fixture-project"
    sids = main(Path(arg))
    print("\nSession IDs (line per session):")
    for sid in sids:
        print(sid)
