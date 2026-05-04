"""Unit tests for otto.spec_compile — Step 1 of the intent-to-product plan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from otto.spec_compile import (
    ApiProbe,
    BrowserJourney,
    PROJECT_KINDS,
    PytestCheck,
    Slice,
    Spec,
    SpecValidationError,
    StructureDecisions,
    append_amendment,
    load_spec,
    persist_spec,
    spec_content_sha256,
    spec_from_dict,
    spec_to_dict,
    validate_spec,
)


def _valid_webapp_payload() -> dict[str, object]:
    """Minimal valid `structure.payload` for a webapp."""
    return {
        "routes": [
            {"path": "/", "component": "Home", "key_text": "Bookmark Manager"},
        ],
        "components": [
            {"name": "Home", "key_text": "Welcome to Bookmark Manager"},
        ],
    }


def _valid_webapp_spec() -> Spec:
    return Spec(
        intent="a bookmark manager",
        project_kind="webapp",
        structure=StructureDecisions(payload=_valid_webapp_payload()),
        slices=[
            Slice(
                id="shell",
                title="App shell",
                tasks=["scaffold the SPA", "add Home route"],
                deps=[],
                owned_paths=["src/App.*", "src/components/Home.*"],
                checks=[
                    BrowserJourney(
                        command=("pytest", "tests/browser/test_shell.py"),
                        evidence_globs=("evidence/shell/*.png",),
                    ),
                ],
            ),
        ],
        non_goals=["multi-user accounts"],
        done_means=["user navigates to / and sees Bookmark Manager"],
    )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_spec_roundtrip_through_json_preserves_structure() -> None:
    spec = _valid_webapp_spec()
    spec = append_amendment(spec, reason="initial post-approval edit", actor="tester")
    serialized = json.dumps(spec_to_dict(spec))
    deserialized = spec_from_dict(json.loads(serialized))

    assert deserialized.intent == spec.intent
    assert deserialized.project_kind == spec.project_kind
    assert deserialized.structure.payload == spec.structure.payload
    assert len(deserialized.slices) == 1
    slice_a, slice_b = deserialized.slices[0], spec.slices[0]
    assert slice_a.id == slice_b.id
    assert slice_a.tasks == slice_b.tasks
    assert slice_a.owned_paths == slice_b.owned_paths
    # BrowserJourney command/evidence_globs should round-trip as tuples
    assert slice_a.checks[0].command == slice_b.checks[0].command
    assert slice_a.checks[0].evidence_globs == slice_b.checks[0].evidence_globs
    assert deserialized.amendments == spec.amendments


def test_spec_roundtrip_supports_all_check_kinds() -> None:
    spec = Spec(
        intent="multi-check fixture",
        project_kind="webapp",
        structure=StructureDecisions(payload=_valid_webapp_payload()),
        slices=[
            Slice(
                id="kitchen-sink",
                title="every check kind",
                tasks=["t"],
                owned_paths=["src/**/*"],
                checks=[
                    PytestCheck(selector="tests/test_x.py::test_y"),
                    ApiProbe(method="GET", path="/health", expect_status=200),
                    BrowserJourney(command=("pytest",), evidence_globs=("e/*.png",)),
                ],
            ),
        ],
    )
    rebuilt = spec_from_dict(spec_to_dict(spec))
    kinds = [c.kind for c in rebuilt.slices[0].checks]
    assert kinds == ["pytest", "api_probe", "browser_journey"]


def test_unknown_check_kind_is_dropped_with_warning() -> None:
    """v2.1: unknown check kinds parse permissively. The check is dropped
    from the slice and a warning is recorded; parsing does not raise.
    Real damage is caught by other checks + audit's contract gate.
    """
    from otto.spec_compile import parse_spec

    bad = {
        "intent": "x",
        "project_kind": "webapp",
        "structure": {"payload": _valid_webapp_payload()},
        "slices": [
            {
                "id": "s",
                "title": "t",
                "tasks": [],
                "deps": [],
                "owned_paths": ["src/**"],
                "checks": [{"kind": "rumor"}],
            }
        ],
    }
    spec, warnings = parse_spec(bad)
    assert spec.slices[0].checks == []
    assert any(w.code == "spec.coerce.unknown_kind" for w in warnings)
    assert any("rumor" in w.message for w in warnings)


# ---------------------------------------------------------------------------
# Validator — schema concreteness gates
# ---------------------------------------------------------------------------


def test_validator_passes_on_valid_webapp() -> None:
    result = validate_spec(_valid_webapp_spec())
    assert result.valid, result.errors


def test_validator_warns_webapp_missing_routes() -> None:
    """v2.1: missing recommended fields are warnings, not errors."""
    spec = _valid_webapp_spec()
    payload = dict(spec.structure.payload)
    payload.pop("routes")
    spec.structure = StructureDecisions(payload=payload)
    result = validate_spec(spec)
    assert result.valid  # spec is still usable
    assert any("routes" in w for w in result.warnings)


def test_validator_warns_webapp_component_without_key_text() -> None:
    spec = _valid_webapp_spec()
    payload = dict(spec.structure.payload)
    payload["components"] = [{"name": "Home"}]
    spec.structure = StructureDecisions(payload=payload)
    result = validate_spec(spec)
    assert result.valid
    assert any("key_text" in w for w in result.warnings)


def test_validator_warns_route_without_key_text() -> None:
    spec = _valid_webapp_spec()
    payload = dict(spec.structure.payload)
    payload["routes"] = [{"path": "/", "component": "Home"}]
    spec.structure = StructureDecisions(payload=payload)
    result = validate_spec(spec)
    assert result.valid
    assert any("key_text" in w for w in result.warnings)


def test_validator_warns_cli_missing_entrypoint() -> None:
    spec = Spec(
        intent="a CLI",
        project_kind="cli",
        structure=StructureDecisions(payload={
            "commands": [{"name": "build", "summary": "build the thing"}],
        }),
        slices=[
            Slice(
                id="root",
                title="bootstrap",
                tasks=["t"],
                owned_paths=["src/**"],
                checks=[PytestCheck(selector="tests/test_x.py")],
            ),
        ],
    )
    result = validate_spec(spec)
    assert result.valid
    assert any("entrypoint" in w for w in result.warnings)


def test_validator_warns_duplicate_slice_ids() -> None:
    """v2.1: duplicate IDs warn at the validator level. The parser's
    `_coerce_slice_id` auto-suffixes duplicates, so this only fires
    when callers construct a Spec by hand bypassing the parser.
    """
    spec = _valid_webapp_spec()
    spec.slices.append(Slice(
        id="shell",
        title="dup",
        tasks=["t"],
        owned_paths=["src/**"],
        checks=[PytestCheck(selector="tests/test_x.py")],
    ))
    result = validate_spec(spec)
    assert result.valid
    assert any("duplicate slice id" in w for w in result.warnings)


def test_validator_warns_unknown_dep() -> None:
    spec = _valid_webapp_spec()
    spec.slices.append(Slice(
        id="shell-extra",
        title="bad dep",
        tasks=["t"],
        deps=["nope"],
        owned_paths=["src/**"],
        checks=[PytestCheck(selector="tests/test_x.py")],
    ))
    result = validate_spec(spec)
    assert result.valid
    assert any("dep" in w and "nope" in w for w in result.warnings)


def test_validator_flags_dep_cycle() -> None:
    """Dep cycles remain hard errors — they would loop the build forever."""
    spec = _valid_webapp_spec()
    spec.slices = [
        Slice(id="a", title="a", tasks=["t"], deps=["b"], owned_paths=["a/**"],
              checks=[PytestCheck(selector="x")]),
        Slice(id="b", title="b", tasks=["t"], deps=["a"], owned_paths=["b/**"],
              checks=[PytestCheck(selector="x")]),
    ]
    result = validate_spec(spec)
    assert not result.valid
    assert any("cycle" in err for err in result.errors)


def test_validator_warns_unknown_project_kind() -> None:
    """v2.1: project_kind is open-enum. Unknown values warn but don't reject."""
    spec = _valid_webapp_spec()
    spec.project_kind = "alien"
    result = validate_spec(spec)
    assert result.valid
    assert any("project_kind" in w for w in result.warnings)


def test_validator_allows_slice_with_no_checks() -> None:
    """v2.1 (F5): a slice with no checks vacuously passes. Audit's
    contract gate verifies the integrated product."""
    spec = _valid_webapp_spec()
    spec.slices[0].checks = []
    result = validate_spec(spec)
    assert result.valid
    assert any("no checks declared" in w for w in result.warnings)


def test_validator_allows_empty_owned_paths() -> None:
    """A slice may have empty owned_paths if it only adds new files anywhere
    or only modifies files owned by transitive deps. Round-5 Microfeed
    bench learning: the original strict rule was over-restrictive once
    the dep-transitivity scope rule landed.
    """
    spec = _valid_webapp_spec()
    spec.slices[0].owned_paths = []
    result = validate_spec(spec)
    assert result.valid, result.errors


def test_validator_warns_unrecommended_slice_id_format() -> None:
    """v2.1 (F4): slice ID regex is advisory. The parser slugifies; the
    validator (when called on a hand-constructed Spec) warns."""
    spec = _valid_webapp_spec()
    spec.slices[0].id = "BadID"
    result = validate_spec(spec)
    assert result.valid
    assert any("BadID" in w for w in result.warnings)


def test_project_kinds_constant_matches_shipped_schemas() -> None:
    """Each supported project_kind has a schema file and the validator finds it."""
    schemas_dir = Path(__file__).parent.parent / "otto" / "spec_schemas"
    for kind in PROJECT_KINDS:
        assert (schemas_dir / f"{kind}.json").exists(), f"missing schema for {kind}"


# ---------------------------------------------------------------------------
# Amendments — immutability semantics
# ---------------------------------------------------------------------------


def test_amendment_append_updates_hash_chain() -> None:
    spec = _valid_webapp_spec()
    h0 = spec_content_sha256(spec)

    spec_v1 = append_amendment(spec, reason="initial review edit", actor="tester")
    assert len(spec_v1.amendments) == 1
    a1 = spec_v1.amendments[0]
    assert a1.diff_sha256_before == ""    # no prior hash for the very first amendment
    assert a1.diff_sha256_after == h0     # content unchanged in this call

    # Now mutate, then append another amendment
    spec_v2 = Spec(**{**spec_v1.__dict__, "non_goals": ["additional non-goal"]})
    h1 = spec_content_sha256(spec_v2)
    spec_v2 = append_amendment(spec_v2, reason="add non-goal", actor="tester", prior_sha256=h0)
    assert len(spec_v2.amendments) == 2
    a2 = spec_v2.amendments[-1]
    assert a2.diff_sha256_before == h0
    assert a2.diff_sha256_after == h1


def test_amendment_requires_non_empty_reason_and_actor() -> None:
    spec = _valid_webapp_spec()
    with pytest.raises(ValueError):
        append_amendment(spec, reason="", actor="tester")
    with pytest.raises(ValueError):
        append_amendment(spec, reason="x", actor="   ")


def test_persist_spec_initial_write_requires_allow_initial(tmp_path: Path) -> None:
    spec = _valid_webapp_spec()
    target = tmp_path / "spec.json"
    with pytest.raises(SpecValidationError):
        persist_spec(spec, target)
    persist_spec(spec, target, allow_initial=True)
    assert target.exists()


def test_persist_spec_allow_initial_overwrites_pre_existing_file(tmp_path: Path) -> None:
    """Round-12 Microfeed bench learning: the compile agent writes spec.json
    itself per the prompt, then compile_spec parses + canonicalizes and
    calls persist_spec(allow_initial=True). The on-disk file already
    exists with the agent's formatting; the parsed-and-canonicalized
    version differs in JSON formatting (key order, spacing). Without
    this branch, fresh runs cheap-fail with "spec content changed but
    no new amendment" because the immutability check fired.

    `allow_initial=True` semantics: this is the initial write — overwrite
    directly without the immutability gate, even if the file already
    exists.
    """
    spec = _valid_webapp_spec()
    target = tmp_path / "spec.json"
    # Simulate the agent having written its own (slightly different)
    # serialization.
    target.write_text(
        '{"intent": "agent-formatted", "schema_version": 1}\n', encoding="utf-8"
    )
    # persist_spec(allow_initial=True) should NOT raise — it's the first
    # canonical write.
    persist_spec(spec, target, allow_initial=True)
    # File now contains the canonical form.
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk["intent"] == spec.intent
    assert on_disk["schema_version"] == spec.schema_version


def test_persist_spec_idempotent_rewrite_no_op(tmp_path: Path) -> None:
    spec = _valid_webapp_spec()
    target = tmp_path / "spec.json"
    persist_spec(spec, target, allow_initial=True)
    mtime_before = target.stat().st_mtime
    # Same content, no amendments needed: must succeed and skip rewrite.
    persist_spec(spec, target)
    # Rewrite would change mtime; idempotent path leaves the file alone.
    # Allow equality up to filesystem resolution by re-loading and comparing.
    assert load_spec(target) == spec
    assert target.stat().st_mtime == mtime_before


def test_persist_spec_rejects_content_change_without_amendment(tmp_path: Path) -> None:
    spec = _valid_webapp_spec()
    target = tmp_path / "spec.json"
    persist_spec(spec, target, allow_initial=True)

    spec_changed = Spec(**{**spec.__dict__, "non_goals": ["new"]})
    with pytest.raises(SpecValidationError):
        persist_spec(spec_changed, target)


def test_persist_spec_accepts_content_change_with_matching_amendment(tmp_path: Path) -> None:
    spec = _valid_webapp_spec()
    target = tmp_path / "spec.json"
    persist_spec(spec, target, allow_initial=True)
    h0 = spec_content_sha256(spec)

    spec_changed = Spec(**{**spec.__dict__, "non_goals": ["new"]})
    amended = append_amendment(spec_changed, reason="user added a non-goal", actor="user", prior_sha256=h0)
    persist_spec(amended, target)

    on_disk = load_spec(target)
    assert on_disk.non_goals == ["new"]
    assert len(on_disk.amendments) == 1
    assert on_disk.amendments[0].diff_sha256_before == h0


def test_persist_spec_rejects_amendment_with_wrong_prior_hash(tmp_path: Path) -> None:
    spec = _valid_webapp_spec()
    target = tmp_path / "spec.json"
    persist_spec(spec, target, allow_initial=True)

    spec_changed = Spec(**{**spec.__dict__, "non_goals": ["new"]})
    # Append an amendment with a bogus prior hash
    amended = append_amendment(spec_changed, reason="bad", actor="tester", prior_sha256="deadbeef")
    with pytest.raises(SpecValidationError):
        persist_spec(amended, target)


# ---------------------------------------------------------------------------
# S1: validator warns on empty/vague tasks
# ---------------------------------------------------------------------------


def _slice_with_tasks(tasks: list[str], slice_id: str = "s1") -> Slice:
    return Slice(
        id=slice_id, title="x", deps=[],
        owned_paths=["x.txt"], tasks=tasks,
        checks=[PytestCheck(selector="tests/")],
    )


def _spec_with_slice(slice_: Slice) -> Spec:
    return Spec(
        intent="test", project_kind="webapp",
        structure=StructureDecisions(payload={}),
        slices=[slice_],
    )


def test_validator_warns_on_empty_tasks() -> None:
    spec = _spec_with_slice(_slice_with_tasks([]))
    result = validate_spec(spec)
    assert any(
        "tasks field empty" in w for w in result.warnings
    ), f"expected empty-tasks warning; got {result.warnings}"


def test_validator_warns_on_vague_short_tasks() -> None:
    spec = _spec_with_slice(_slice_with_tasks(["build it", "fix"]))
    result = validate_spec(spec)
    vague_warnings = [w for w in result.warnings if "too vague" in w]
    assert len(vague_warnings) == 2, (
        f"expected 2 vague-task warnings, got {vague_warnings}"
    )


def test_validator_accepts_concrete_tasks() -> None:
    spec = _spec_with_slice(_slice_with_tasks([
        "Add GET /api/items returning [{id, name}]",
        "Wire register_blueprint(items_bp) in app.py",
    ]))
    result = validate_spec(spec)
    assert not any("too vague" in w for w in result.warnings)
    assert not any("tasks field empty" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# S4: validator warns on multi-slice spec without cross_slice_checks
# ---------------------------------------------------------------------------


def test_validator_warns_multi_slice_without_cross_checks() -> None:
    spec = Spec(
        intent="test", project_kind="webapp",
        structure=StructureDecisions(payload={}),
        slices=[
            _slice_with_tasks(["Add foo to app.py"], slice_id="a"),
            _slice_with_tasks(["Add bar to app.py"], slice_id="b"),
        ],
        cross_slice_checks=[],
    )
    result = validate_spec(spec)
    assert any("cross_slice_checks" in w for w in result.warnings)


def test_validator_silent_on_single_slice_without_cross_checks() -> None:
    spec = _spec_with_slice(_slice_with_tasks(["Add /api/foo endpoint"]))
    result = validate_spec(spec)
    # Single-slice spec shouldn't trigger the multi-slice integration warning.
    assert not any("cross_slice_checks" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# S5: parser warns on missing/wrong-type tasks/deps/owned_paths
# ---------------------------------------------------------------------------


def test_parse_warns_on_missing_tasks_field() -> None:
    from otto.spec_compile import parse_spec
    bad = {
        "intent": "x", "project_kind": "webapp",
        "structure": {"payload": {}},
        "slices": [{"id": "s1", "title": "x"}],  # NO tasks/deps/owned_paths
    }
    spec, warnings = parse_spec(bad)
    codes = [w.code for w in warnings]
    paths = [w.path for w in warnings]
    assert "spec.coerce.field" in codes
    assert "slices[0].tasks" in paths
    assert "slices[0].deps" in paths
    assert "slices[0].owned_paths" in paths


def test_parse_warns_on_wrong_type_tasks() -> None:
    from otto.spec_compile import parse_spec
    bad = {
        "intent": "x", "project_kind": "webapp",
        "structure": {"payload": {}},
        "slices": [{"id": "s1", "title": "x", "tasks": "build it"}],  # str instead of list
    }
    spec, warnings = parse_spec(bad)
    assert any(
        w.path == "slices[0].tasks" and "should be a list" in w.message
        for w in warnings
    )


# ---------------------------------------------------------------------------
# S2: append_amendment threads trigger_event_id and tier
# ---------------------------------------------------------------------------


def test_append_amendment_records_trigger_event_id_and_tier() -> None:
    spec = _spec_with_slice(_slice_with_tasks(["Add /api/foo endpoint"]))
    amended = append_amendment(
        spec,
        reason="user fixed a typo",
        actor="user",
        trigger_event_id="ev-000042",
        tier=1,
    )
    assert len(amended.amendments) == 1
    assert amended.amendments[0].trigger_event_id == "ev-000042"
    assert amended.amendments[0].tier == 1


def test_append_amendment_defaults_remain_for_back_compat() -> None:
    spec = _spec_with_slice(_slice_with_tasks(["Add /api/foo endpoint"]))
    amended = append_amendment(spec, reason="x", actor="user")
    assert amended.amendments[0].trigger_event_id == ""
    assert amended.amendments[0].tier == 0
