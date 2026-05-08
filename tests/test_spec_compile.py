"""Unit tests for otto.spec_compile — Step 1 of the intent-to-product plan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from otto.spec_compile import (
    ApiProbe,
    BrowserJourney,
    BehaviorJourney,
    BehaviorStep,
    Feature,
    PROJECT_KINDS,
    PytestCheck,
    RepoTestCheck,
    Group,
    SharedContract,
    Spec,
    SpecValidationError,
    StructureDecisions,
    _ensure_webapp_behavior_journeys,
    _ensure_webapp_shared_contracts,
    _normalize_webapp_shared_contract_paths,
    _normalize_webapp_scaffold_scope,
    append_amendment,
    compile_spec,
    infer_feature_group_routes_from_owned_paths,
    load_spec,
    parse_spec_md,
    parse_spec,
    persist_spec,
    render_spec_md,
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
        groups=[
            Group(
                id="shell",
                name="App shell",
                feature_ids=["scaffold the SPA", "add Home route"],
                dependencies=[],
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


def _agent_text_for_spec(spec: Spec) -> str:
    return f"<spec_json>{json.dumps(spec_to_dict(spec))}</spec_json>"


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
    assert len(deserialized.groups) == 1
    slice_a, slice_b = deserialized.groups[0], spec.groups[0]
    assert slice_a.id == slice_b.id
    assert slice_a.feature_ids == slice_b.feature_ids
    assert slice_a.owned_paths == slice_b.owned_paths
    # BrowserJourney command/evidence_globs should round-trip as tuples
    assert slice_a.checks[0].command == slice_b.checks[0].command
    assert slice_a.checks[0].evidence_globs == slice_b.checks[0].evidence_globs
    assert deserialized.amendments == spec.amendments


@pytest.mark.asyncio
async def test_compile_spec_retries_transient_provider_stall_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto.agent import AgentCallError

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    run_dir = project_dir / "otto_logs" / "sessions" / "run-1" / "spec"
    calls: list[Path] = []

    async def flaky_compile_agent(*_args: object, **kwargs: object):
        log_dir = Path(kwargs["log_dir"])
        calls.append(log_dir)
        if len(calls) == 1:
            raise AgentCallError(
                "codex app-server stream stalled after recoverable error: "
                "Reconnecting... 2/5. No provider events arrived for 120s.",
                last_provider_stderr="Reconnecting... 2/5",
            )
        return _agent_text_for_spec(_valid_webapp_spec()), 0.0, "retry-session", {}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", flaky_compile_agent)

    spec = await compile_spec(
        "build a bookmark manager",
        project_dir=project_dir,
        run_dir=run_dir,
        config={"provider": "codex-app-server", "spec_timeout": 30},
        project_kind="webapp",
    )

    assert spec.intent == "a bookmark manager"
    assert [path.name for path in calls] == ["compile-agent", "compile-agent-retry-02"]


def test_spec_roundtrip_supports_all_check_kinds() -> None:
    spec = Spec(
        intent="multi-check fixture",
        project_kind="webapp",
        structure=StructureDecisions(payload=_valid_webapp_payload()),
        groups=[
            Group(
                id="kitchen-sink",
                name="every check kind",
                feature_ids=["t"],
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
    kinds = [c.kind for c in rebuilt.groups[0].checks]
    assert kinds == ["pytest", "api_probe", "browser_journey"]


def test_spec_markdown_roundtrip_preserves_behavior_journeys_and_contracts() -> None:
    spec = _valid_webapp_spec()
    spec.features = [Feature(id="compose-post", name="Compose post", group_id="shell")]
    spec.behavior_journeys = [
        BehaviorJourney(
            id="planned-main-user-flow",
            name="Main user flow",
            steps=[
                BehaviorStep(
                    action="Type and submit a post.",
                    expectation="The post appears in the feed.",
                    assertion="Submitted text remains visible after refresh.",
                    artifact="screenshot",
                    feature_ids=["compose-post"],
                )
            ],
        )
    ]
    spec.shared_contracts = [
        SharedContract(
            id="shared-product-core",
            name="Shared product core",
            owner_id="shell",
            paths=["src/store/**"],
            invariants=["Feed data persists across refresh."],
        )
    ]

    markdown = render_spec_md(spec)
    rebuilt, warnings = parse_spec_md(markdown, base=spec)

    assert warnings == []
    assert "## Planned behavior journeys" in markdown
    assert "## Shared contracts" in markdown
    assert rebuilt.behavior_journeys[0].steps[0].feature_ids == ["compose-post"]
    assert rebuilt.shared_contracts[0].paths == ["src/store/**"]


def test_webapp_behavior_journey_normalization_uses_feature_acceptance() -> None:
    spec = Spec(
        intent="micro feed",
        project_kind="webapp",
        structure=StructureDecisions(payload=_valid_webapp_payload()),
        groups=[
            Group(
                id="feed",
                name="Feed",
                feature_ids=["compose-post"],
                owned_paths=["src/feed/**"],
            )
        ],
        features=[
            Feature(
                id="compose-post",
                name="Compose post",
                acceptance_detail="User can type a post, submit it, and see it in the feed.",
                group_id="feed",
            )
        ],
    )

    warnings = _ensure_webapp_behavior_journeys(spec)

    assert warnings
    assert spec.behavior_journeys[0].id == "planned-main-user-flow"
    assert spec.behavior_journeys[0].steps[0].feature_ids == ["compose-post"]
    assert "type a post" in spec.behavior_journeys[0].steps[0].expectation


def test_webapp_shared_contract_normalization_uses_declared_shared_paths() -> None:
    spec = Spec(
        intent="finance dashboard",
        project_kind="webapp",
        structure=StructureDecisions(payload=_valid_webapp_payload()),
        groups=[
            Group(
                id="foundation",
                name="App foundation",
                feature_ids=["shell"],
                owned_paths=["src/App.tsx"],
            ),
            Group(
                id="reports",
                name="Reports",
                feature_ids=["reports"],
                owned_paths=["src/reports/**"],
            ),
        ],
        shared_paths=["src/store/**", "playwright.config.ts", "docs/notes.md"],
        features=[
            Feature(id="reports", name="Reports", group_id="reports"),
        ],
    )

    warnings = _ensure_webapp_shared_contracts(spec)

    assert warnings
    contract = spec.shared_contracts[0]
    assert contract.id == "shared-product-core"
    assert contract.owner_id == "foundation"
    assert contract.critical is True
    assert contract.paths == ["src/store/**", "playwright.config.ts"]
    assert spec.groups[1].dependencies == ["foundation"]


def test_webapp_browser_contract_does_not_capture_feature_journey_tests() -> None:
    spec = Spec(
        intent="finance dashboard",
        project_kind="webapp",
        structure=StructureDecisions(payload=_valid_webapp_payload()),
        groups=[
            Group(id="foundation", name="Foundation"),
            Group(id="insights", name="Insights", dependencies=["foundation"]),
        ],
        shared_contracts=[
            SharedContract(
                id="browser-quality-contract",
                name="Browser quality and evidence",
                kind="test_runner",
                owner_id="foundation",
                paths=[
                    "tests/run_browser_journey.py",
                    "tests/browser_journeys/**",
                    "tests/browser/**",
                    "otto_artifacts/browser/**",
                    "playwright.config.ts",
                ],
            )
        ],
    )

    warnings = _normalize_webapp_shared_contract_paths(spec)

    assert warnings
    assert spec.shared_contracts[0].paths == [
        "tests/run_browser_journey.py",
        "playwright.config.ts",
    ]
    assert spec.shared_contracts[0].allowed_extension_paths == [
        "tests/browser_journeys/**",
        "tests/browser/**",
        "otto_artifacts/browser/**",
    ]
    assert "Feature groups may add their own behavior journey tests" in (
        spec.shared_contracts[0].extension_policy
    )


def test_validate_spec_warns_on_shared_contract_owned_path_overlap() -> None:
    spec = Spec(
        intent="finance dashboard",
        project_kind="webapp",
        structure=StructureDecisions(payload=_valid_webapp_payload()),
        groups=[
            Group(id="foundation", name="Foundation"),
            Group(
                id="transactions",
                name="Transactions",
                dependencies=["foundation"],
                owned_paths=["tests/browser/test_transactions.*"],
            ),
        ],
        shared_contracts=[
            SharedContract(
                id="browser-quality-contract",
                name="Browser quality",
                kind="test_runner",
                owner_id="foundation",
                paths=["tests/browser/**"],
            )
        ],
    )

    result = validate_spec(spec)

    assert result.valid
    assert any(
        "overlaps non-owner group 'transactions'" in warning
        for warning in result.warnings
    )


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
        "groups": [
            {
                "id": "s",
                "name": "t",
                "tasks": [],
                "deps": [],
                "owned_paths": ["src/**"],
                "checks": [{"kind": "rumor"}],
            }
        ],
    }
    spec, warnings = parse_spec(bad)
    assert spec.groups[0].checks == []
    assert any(w.code == "spec.coerce.unknown_kind" for w in warnings)
    assert any("rumor" in w.message for w in warnings)


def test_parse_spec_coerces_raw_check_strings_to_repo_test() -> None:
    spec, warnings = parse_spec(
        {
            "intent": "fix numeric parsing",
            "project_kind": "library",
            "structure": {
                "payload": {
                    "package_name": "demo",
                    "public_api": [
                        {"symbol": "demo.parse", "kind": "function", "summary": "Parse"}
                    ],
                }
            },
            "groups": [
                {
                    "id": "numeric",
                    "name": "Numeric parsing",
                    "feature_ids": ["parse-numeric"],
                    "owned_paths": ["demo.py", "tests/test_demo.py"],
                    "checks": ["python -m pytest tests/test_demo.py -q"],
                }
            ],
        }
    )

    assert spec.groups[0].checks == [
        RepoTestCheck(command=("python", "-m", "pytest", "tests/test_demo.py", "-q"))
    ]
    assert any(w.code == "spec.coerce.check_string" for w in warnings)


def test_parse_spec_drops_empty_audit_fixture_placeholders() -> None:
    spec, warnings = parse_spec(
        {
            "intent": "document library",
            "project_kind": "library",
            "structure": {
                "payload": {
                    "package_name": "demo",
                    "public_api": [
                        {"symbol": "demo.run", "kind": "function", "summary": "Runs demo"}
                    ],
                }
            },
            "groups": [
                {
                    "id": "core",
                    "name": "Core",
                    "feature_ids": ["demo-run"],
                    "owned_paths": ["demo.py"],
                }
            ],
            "features": [
                {"id": "demo-run", "name": "Demo run", "group_id": "core"}
            ],
            "audit_fixtures": [
                {"kind": "", "payload": {}},
                {"payload": {}},
                {"kind": "data", "payload": {"fixture": "kept"}},
            ],
        }
    )

    assert [(fixture.kind, fixture.payload) for fixture in spec.audit_fixtures] == [
        ("data", {"fixture": "kept"})
    ]
    assert [warning.path for warning in warnings if "audit_fixture" in warning.message] == [
        "audit_fixtures[0]",
        "audit_fixtures[1]",
    ]


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
        groups=[
            Group(
                id="root",
                name="bootstrap",
                feature_ids=["t"],
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
    spec.groups.append(Group(
        id="shell",
        name="dup",
        feature_ids=["t"],
        owned_paths=["src/**"],
        checks=[PytestCheck(selector="tests/test_x.py")],
    ))
    result = validate_spec(spec)
    assert result.valid
    assert any("duplicate group id" in w for w in result.warnings)


def test_validator_warns_unknown_dep() -> None:
    spec = _valid_webapp_spec()
    spec.groups.append(Group(
        id="shell-extra",
        name="bad dep",
        feature_ids=["t"],
        dependencies=["nope"],
        owned_paths=["src/**"],
        checks=[PytestCheck(selector="tests/test_x.py")],
    ))
    result = validate_spec(spec)
    assert result.valid
    assert any("dep" in w and "nope" in w for w in result.warnings)


def test_validator_flags_dep_cycle() -> None:
    """Dep cycles remain hard errors — they would loop the build forever."""
    spec = _valid_webapp_spec()
    spec.groups = [
        Group(id="a", name="a", feature_ids=["t"], dependencies=["b"], owned_paths=["a/**"],
              checks=[PytestCheck(selector="x")]),
        Group(id="b", name="b", feature_ids=["t"], dependencies=["a"], owned_paths=["b/**"],
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
    spec.groups[0].checks = []
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
    spec.groups[0].owned_paths = []
    result = validate_spec(spec)
    assert result.valid, result.errors


def test_validator_warns_unrecommended_slice_id_format() -> None:
    """v2.1 (F4): slice ID regex is advisory. The parser slugifies; the
    validator (when called on a hand-constructed Spec) warns."""
    spec = _valid_webapp_spec()
    spec.groups[0].id = "BadID"
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
    assert (tmp_path / "spec.md").read_text(encoding="utf-8") == render_spec_md(spec)


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


def _group_with_features(features: list[str], group_id: str = "s1") -> Group:
    return Group(
        id=group_id, name="x", dependencies=[],
        owned_paths=["x.txt"], feature_ids=features,
        checks=[PytestCheck(selector="tests/")],
    )


def _spec_with_group(slice_: Group) -> Spec:
    return Spec(
        intent="test", project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[slice_],
    )


def test_validator_warns_on_empty_tasks() -> None:
    spec = _spec_with_group(_group_with_features([]))
    result = validate_spec(spec)
    assert any(
        "feature_ids field empty" in w for w in result.warnings
    ), f"expected empty-feature_ids warning; got {result.warnings}"


def test_validator_warns_on_vague_short_tasks() -> None:
    spec = _spec_with_group(_group_with_features(["build it", "fix"]))
    result = validate_spec(spec)
    vague_warnings = [w for w in result.warnings if "too vague" in w]
    assert len(vague_warnings) == 2, (
        f"expected 2 vague-task warnings, got {vague_warnings}"
    )


def test_validator_accepts_concrete_tasks() -> None:
    spec = _spec_with_group(_group_with_features([
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
        groups=[
            _group_with_features(["Add foo to app.py"], group_id="a"),
            _group_with_features(["Add bar to app.py"], group_id="b"),
        ],
        cross_group_checks=[],
    )
    result = validate_spec(spec)
    assert any("cross_group_checks" in w for w in result.warnings)


def test_validator_silent_on_single_slice_without_cross_checks() -> None:
    spec = _spec_with_group(_group_with_features(["Add /api/foo endpoint"]))
    result = validate_spec(spec)
    # Single-slice spec shouldn't trigger the multi-slice integration warning.
    assert not any("cross_group_checks" in w for w in result.warnings)


def test_validator_warns_when_cross_group_runner_path_has_no_owner() -> None:
    spec = Spec(
        intent="test", project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            _group_with_features(["Add foo to app.py"], group_id="a"),
            _group_with_features(["Add bar to app.py"], group_id="b"),
        ],
        cross_group_checks=[
            BrowserJourney(
                command=("npm", "run", "test:browser", "--", "tests/browser/full-workflow.spec.ts"),
                evidence_globs=("otto_artifacts/browser/full-workflow/*.png",),
            )
        ],
    )

    result = validate_spec(spec)

    assert any(
        "tests/browser/full-workflow.spec.ts" in warning
        and "not covered by any group/component owned_paths" in warning
        for warning in result.warnings
    )


def test_validator_accepts_cross_group_runner_path_with_owner() -> None:
    spec = Spec(
        intent="test", project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(
                id="foundation",
                name="Foundation",
                dependencies=[],
                owned_paths=["tests/browser/full-workflow.spec.ts"],
                feature_ids=["Create integrated full-workflow browser journey"],
                checks=[PytestCheck(selector="tests/")],
            ),
            _group_with_features(["Add bar to app.py"], group_id="b"),
        ],
        cross_group_checks=[
            BrowserJourney(
                command=("npm", "run", "test:browser", "--", "tests/browser/full-workflow.spec.ts"),
                evidence_globs=("otto_artifacts/browser/full-workflow/*.png",),
            )
        ],
    )

    result = validate_spec(spec)

    assert not any(
        "tests/browser/full-workflow.spec.ts" in warning
        and "not covered by any group/component owned_paths" in warning
        for warning in result.warnings
    )


def test_validator_warns_on_routine_webapp_browser_journey_without_agent_browser_runner() -> None:
    spec = Spec(
        intent="test",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(
                id="filtering",
                name="Filtering",
                feature_ids=["filter transactions by text"],
                checks=[
                    BrowserJourney(
                        command=("npm", "run", "test:browser", "--", "tests/browser/filtering.spec.ts"),
                        evidence_globs=("otto_artifacts/browser/filtering/*.png",),
                    )
                ],
            )
        ],
    )

    result = validate_spec(spec)

    assert any(
        "Otto-owned agent-browser runner" in warning
        and "tests/run_browser_journey.py" in warning
        for warning in result.warnings
    )


def test_validator_accepts_webapp_browser_journey_agent_browser_runner() -> None:
    spec = Spec(
        intent="test",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(
                id="filtering",
                name="Filtering",
                feature_ids=["filter transactions by text"],
                owned_paths=["tests/run_browser_journey.py"],
                checks=[
                    BrowserJourney(
                        command=("python3", "tests/run_browser_journey.py", "--journey", "filtering"),
                        evidence_globs=("otto_artifacts/browser/filtering/*.png",),
                    )
                ],
            )
        ],
    )

    result = validate_spec(spec)

    assert not any(
        "Otto-owned agent-browser runner" in warning
        for warning in result.warnings
    )


# ---------------------------------------------------------------------------
# S5: parser warns on missing/wrong-type tasks/deps/owned_paths
# ---------------------------------------------------------------------------


def test_parse_warns_on_missing_tasks_field() -> None:
    from otto.spec_compile import parse_spec
    bad = {
        "intent": "x", "project_kind": "webapp",
        "structure": {"payload": {}},
        "groups": [{"id": "s1", "name": "x"}],  # NO tasks/deps/owned_paths
    }
    spec, warnings = parse_spec(bad)
    codes = [w.code for w in warnings]
    paths = [w.path for w in warnings]
    assert "spec.coerce.field" in codes
    assert "groups[0].feature_ids" in paths
    assert "groups[0].dependencies" in paths
    assert "groups[0].owned_paths" in paths


def test_parse_warns_on_wrong_type_tasks() -> None:
    from otto.spec_compile import parse_spec
    bad = {
        "intent": "x", "project_kind": "webapp",
        "structure": {"payload": {}},
        "groups": [{"id": "s1", "name": "x", "feature_ids": "build it"}],  # str instead of list
    }
    spec, warnings = parse_spec(bad)
    assert any(
        w.path == "groups[0].feature_ids" and "should be a list" in w.message
        for w in warnings
    )


# ---------------------------------------------------------------------------
# S2: append_amendment threads trigger_event_id and tier
# ---------------------------------------------------------------------------


def test_append_amendment_records_trigger_event_id_and_tier() -> None:
    spec = _spec_with_group(_group_with_features(["Add /api/foo endpoint"]))
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
    spec = _spec_with_group(_group_with_features(["Add /api/foo endpoint"]))
    amended = append_amendment(spec, reason="x", actor="user")
    assert amended.amendments[0].trigger_event_id == ""
    assert amended.amendments[0].tier == 0


# ---------------------------------------------------------------------------
# Round-3 audit gap 4 — schema_version bump v1 → v2 + deprecation window
# ---------------------------------------------------------------------------


def test_parse_v1_spec_emits_legacy_read_warning() -> None:
    """A v1 spec.json (schema_version=1, legacy `slices` key) reads
    cleanly under the v2 parser but emits one
    `spec.deprecated.schema_v1_read` advisory warning.
    """
    from otto.spec_compile import parse_spec
    v1_payload = {
        "intent": "tiny webapp",
        "project_kind": "webapp",
        "structure": {"payload": {}},
        "schema_version": 1,
        # Legacy v1 top-level key — read-fallback active for one cycle.
        "slices": [
            {"id": "s1", "title": "Shell", "tasks": ["scaffold"], "deps": []},
        ],
    }
    spec, warnings = parse_spec(v1_payload)
    codes = {w.code for w in warnings}
    assert "spec.deprecated.schema_v1_read" in codes
    # The legacy `slices` key was consumed into `spec.groups`.
    assert len(spec.groups) == 1
    assert spec.groups[0].id == "s1"


def test_parse_v2_spec_with_no_legacy_keys_has_no_deprecation_warning() -> None:
    """A clean v2 spec.json should not emit any deprecation warning."""
    from otto.spec_compile import parse_spec, SCHEMA_VERSION
    assert SCHEMA_VERSION >= 2
    v2_payload = {
        "intent": "tiny webapp",
        "project_kind": "webapp",
        "structure": {"payload": {}},
        "schema_version": SCHEMA_VERSION,
        "groups": [
            {"id": "s1", "name": "Shell", "feature_ids": ["scaffold"],
             "dependencies": []},
        ],
    }
    spec, warnings = parse_spec(v2_payload)
    codes = {w.code for w in warnings}
    assert not any(c.startswith("spec.deprecated.schema_") for c in codes)
    assert spec.schema_version == SCHEMA_VERSION
    assert len(spec.groups) == 1


def test_parse_feature_group_alias_resolves_to_group_id() -> None:
    """Compile agents often emit module/group labels instead of Group ids."""
    from otto.spec_compile import parse_spec

    payload = {
        "intent": "library improvement",
        "project_kind": "library",
        "structure": {"payload": {}},
        "groups": [
            {
                "id": "group_0",
                "name": "Number",
                "features": ["intword"],
                "owned_paths": ["src/humanize/number.py"],
            }
        ],
        "features": [
            {
                "name": "intword",
                "module": "Number",
            }
        ],
    }

    spec, warnings = parse_spec(payload)

    assert spec.features[0].id == "intword"
    assert spec.features[0].group_id == "group_0"
    codes = {w.code for w in warnings}
    assert "spec.coerce.feature_id" in codes
    assert "spec.coerce.feature_group_id" in codes
    assert "spec.deprecated.group_field" in codes


def test_parse_feature_ids_backfill_names_and_group_feature_ids() -> None:
    from otto.spec_compile import parse_spec

    payload = {
        "intent": "library improvement",
        "project_kind": "library",
        "structure": {"payload": {}},
        "groups": [
            {
                "id": "number",
                "name": "Number",
                "owned_paths": ["src/humanize/number.py"],
            }
        ],
        "features": [
            {
                "id": "intword",
                "group_id": "number",
                "description": "parse comma strings",
            }
        ],
    }

    spec, warnings = parse_spec(payload)

    assert spec.features[0].name == "intword"
    assert spec.groups[0].feature_ids == ["intword"]
    codes = {w.code for w in warnings}
    assert "spec.coerce.feature_name" in codes
    assert "spec.coerce.group_feature_ids" in codes


def test_compile_normalization_synthesizes_features_from_group_only_feature_text() -> None:
    from otto.spec_compile import (
        _synthesize_features_from_group_feature_ids,
        parse_spec,
        spec_to_dict,
    )
    from otto.spec_warnings import WarningCollector

    spec, _warnings = parse_spec({
        "intent": "micro twitter",
        "project_kind": "webapp",
        "structure": {"payload": {}},
        "groups": [
            {
                "id": "timeline",
                "name": "Timeline",
                "feature_ids": [
                    "create posts for the current identity",
                    "render a newest-first timeline",
                ],
                "dependencies": [],
                "owned_paths": ["src/features/timeline/**"],
                "checks": [],
            }
        ],
        "features": [],
    })
    collector = WarningCollector()
    spec.features = _synthesize_features_from_group_feature_ids(
        spec.groups,
        project_kind=spec.project_kind,
        collector=collector,
    )

    assert [feature.id for feature in spec.features] == [
        "create-posts-for-the-current-identity",
        "render-a-newest-first-timeline",
    ]
    assert [feature.name for feature in spec.features] == [
        "create posts for the current identity",
        "render a newest-first timeline",
    ]
    assert [feature.group_id for feature in spec.features] == ["timeline", "timeline"]
    assert spec.groups[0].feature_ids == [
        "create-posts-for-the-current-identity",
        "render-a-newest-first-timeline",
    ]
    assert spec_to_dict(spec)["features"][0]["group_id"] == "timeline"
    assert "spec.coerce.features_from_group_feature_ids" in {w.code for w in collector.warnings}


def test_validate_spec_warns_for_unroutable_feature_group() -> None:
    """Layer 2 cannot repair Features without a real owning Group."""
    spec = Spec(
        intent="x",
        groups=[Group(id="g", name="G", feature_ids=["build feature"])],
        features=[Feature(id="f", name="F", group_id="missing")],
    )

    result = validate_spec(spec)

    assert any("group_id 'missing' not in spec groups" in w for w in result.warnings)


def test_compile_normalizes_missing_webapp_scaffold_scope() -> None:
    spec = Spec(
        intent="build a local kanban webapp",
        project_kind="webapp",
        groups=[
            Group(
                id="foundation_shell_state",
                name="React app shell and shared state",
                feature_ids=["scaffold the local only SPA"],
                owned_paths=["README.md", "src/state/**", "src/styles/**"],
            ),
            Group(
                id="board_crud_movement",
                name="Board CRUD and movement",
                dependencies=["foundation_shell_state"],
                feature_ids=["create columns and cards"],
                owned_paths=["src/features/board/**"],
            ),
        ],
    )

    warnings = _normalize_webapp_scaffold_scope(spec)

    foundation_paths = spec.groups[0].owned_paths
    assert "package.json" in foundation_paths
    assert "vite.config.*" in foundation_paths
    assert "playwright.config.*" in foundation_paths
    assert "tsconfig*.json" in foundation_paths
    assert "src/App.*" in foundation_paths
    assert spec.groups[1].owned_paths == ["src/features/board/**"]
    assert any("webapp scaffold scope normalized" in warning for warning in warnings)


def test_infer_feature_group_routes_from_owned_paths(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "number.py").write_text(
        "def intword(value):\n    return value\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "pkg" / "filesize.py").write_text(
        "def naturalsize(value):\n    return value\n",
        encoding="utf-8",
    )
    spec = Spec(
        intent="support numeric strings",
        project_kind="library",
        groups=[
            Group(
                id="number",
                name="Number",
                feature_ids=[],
                owned_paths=["src/pkg/number.py"],
            ),
            Group(
                id="filesize",
                name="File Size",
                feature_ids=[],
                owned_paths=["src/pkg/filesize.py"],
            ),
        ],
        features=[
            Feature(id="intword", name="Intword"),
            Feature(id="naturalsize", name="Natural size"),
        ],
    )

    warnings = infer_feature_group_routes_from_owned_paths(spec, tmp_path)

    assert spec.features[0].group_id == "number"
    assert spec.features[1].group_id == "filesize"
    assert spec.groups[0].feature_ids == ["intword"]
    assert spec.groups[1].feature_ids == ["naturalsize"]
    assert len(warnings) == 2


def test_infer_feature_group_routes_leaves_ambiguous_symbol_unrouted(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def search():\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("def search():\n    pass\n", encoding="utf-8")
    spec = Spec(
        intent="x",
        project_kind="library",
        groups=[
            Group(id="a", name="A", owned_paths=["src/a.py"]),
            Group(id="b", name="B", owned_paths=["src/b.py"]),
        ],
        features=[Feature(id="search", name="Search")],
    )

    warnings = infer_feature_group_routes_from_owned_paths(spec, tmp_path)

    assert spec.features[0].group_id == ""
    assert spec.groups[0].feature_ids == []
    assert spec.groups[1].feature_ids == []
    assert warnings == []


def test_parse_v2_spec_with_leftover_legacy_top_keys_warns_loudly() -> None:
    """A v2 spec that still carries legacy v1 top-level keys must emit
    `spec.deprecated.schema_v2_legacy_top_keys` so operators clean up
    before the next bump drops the read-fallback.
    """
    from otto.spec_compile import parse_spec, SCHEMA_VERSION
    payload = {
        "intent": "tiny webapp",
        "project_kind": "webapp",
        "structure": {"payload": {}},
        "schema_version": SCHEMA_VERSION,
        "groups": [
            {"id": "s1", "name": "Shell", "feature_ids": ["scaffold"]},
        ],
        # Leftover legacy key while schema_version says v2 — loud warning.
        "cross_slice_checks": [],
    }
    _spec, warnings = parse_spec(payload)
    codes = {w.code for w in warnings}
    assert "spec.deprecated.schema_v2_legacy_top_keys" in codes


def test_parse_v2_spec_with_leftover_legacy_group_fields_warns() -> None:
    """Leftover per-group legacy fields (title, tasks, deps) on a v2
    spec also emit a louder `schema_v2_legacy_group_fields` warning."""
    from otto.spec_compile import parse_spec, SCHEMA_VERSION
    payload = {
        "intent": "tiny webapp",
        "project_kind": "webapp",
        "structure": {"payload": {}},
        "schema_version": SCHEMA_VERSION,
        "groups": [
            {
                "id": "s1", "name": "Shell",
                "feature_ids": ["scaffold"],
                # Leftover legacy field on a v2 spec.
                "tasks": ["scaffold"],
            },
        ],
    }
    _spec, warnings = parse_spec(payload)
    codes = {w.code for w in warnings}
    assert "spec.deprecated.schema_v2_legacy_group_fields" in codes
