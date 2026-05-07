"""Tests for A1a — Feature, Component, Guardrail, Finding, AuditFixture
dataclasses + extended Spec fields. Per research §2 vocabulary, §2.6, §4.

These dataclasses live alongside Group during the A0/A1 transition.
Compile starts populating them in A1a; user-facing surfaces (MC, proof
packet, spec.md) read them in A3/A4. Group remains the dispatch unit;
Feature is the value/verdict unit.
"""

from __future__ import annotations

from otto.spec_compile import (
    AuditFixture,
    Component,
    FINDING_SEVERITIES,
    Feature,
    Finding,
    Group,
    Guardrail,
    Spec,
)


# ---------------------------------------------------------------------------
# Feature
# ---------------------------------------------------------------------------


def test_feature_minimum_construction() -> None:
    f = Feature(id="auth", name="Auth (register/login)")
    assert f.id == "auth"
    assert f.name == "Auth (register/login)"
    assert f.description == ""
    assert f.acceptance_detail == ""
    assert f.evidence_kinds == []
    assert f.group_id == ""
    assert f.verdict is None
    assert f.evidence_completeness == "full"
    assert f.coverage_confidence == "high"
    assert f.multi_actor_required is False
    assert f.audit_pre_merge is False


def test_feature_full_construction() -> None:
    f = Feature(
        id="dm-delivery",
        name="Direct message delivery",
        description="User A → User B DM in <2s with notification",
        acceptance_detail="A sends DM; B sees in side panel within 2s",
        evidence_kinds=["BrowserJourney", "ApiProbe", "StateInvariant"],
        group_id="messaging",
        verdict="partial",
        evidence_completeness="proxy_only",
        coverage_confidence="medium",
        multi_actor_required=True,
        audit_pre_merge=True,
    )
    assert f.id == "dm-delivery"
    assert f.evidence_completeness == "proxy_only"
    assert f.coverage_confidence == "medium"
    assert f.multi_actor_required is True
    assert f.audit_pre_merge is True


def test_feature_id_stable_across_renames() -> None:
    """Renaming `name` must not alter `id` — research §2 vocabulary rule."""
    f = Feature(id="auth", name="Auth (register/login)")
    f.name = "User accounts"  # user rename
    assert f.id == "auth"  # id unchanged


# ---------------------------------------------------------------------------
# Component
# ---------------------------------------------------------------------------


def test_component_minimum_construction() -> None:
    c = Component(id="websocket-hub", name="WebSocket hub")
    assert c.id == "websocket-hub"
    assert c.owned_paths == []
    assert c.dependencies == []
    assert c.checks == []
    assert c.consumed_by == []


def test_component_full_construction() -> None:
    c = Component(
        id="search-index",
        name="Search index",
        description="Full-text search backend over messages",
        owned_paths=["search/", "indexer.py"],
        dependencies=["foundation"],
        checks=[],
        consumed_by=["search-feature", "mention-feature"],
    )
    assert c.consumed_by == ["search-feature", "mention-feature"]
    assert "foundation" in c.dependencies


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------


def test_guardrail_minimum_construction() -> None:
    g = Guardrail(id="no-video", text="No video upload")
    assert g.id == "no-video"
    assert g.text == "No video upload"
    assert g.applies_to == "*"  # default = whole product


def test_guardrail_scoped_to_group() -> None:
    g = Guardrail(id="auth-no-oauth", text="No OAuth", applies_to="auth")
    assert g.applies_to == "auth"


# ---------------------------------------------------------------------------
# Finding (severity ladder)
# ---------------------------------------------------------------------------


def test_finding_severity_ladder() -> None:
    assert FINDING_SEVERITIES == ("critical", "important", "polish")


def test_finding_construction() -> None:
    f = Finding(severity="critical", text="Page load >8s", feature_id="home")
    assert f.severity == "critical"
    assert f.feature_id == "home"


def test_finding_whole_product() -> None:
    f = Finding(severity="polish", text="Color palette is generic")
    assert f.feature_id == ""  # whole-product finding


# ---------------------------------------------------------------------------
# AuditFixture
# ---------------------------------------------------------------------------


def test_audit_fixture_user() -> None:
    fx = AuditFixture(kind="user", payload={"username": "alice", "role": "admin"})
    assert fx.kind == "user"
    assert fx.payload["username"] == "alice"


def test_audit_fixture_follow() -> None:
    fx = AuditFixture(kind="follow", payload={"follower": "alice", "followed": "bob"})
    assert fx.kind == "follow"
    assert fx.payload["follower"] == "alice"


# ---------------------------------------------------------------------------
# Spec — extended fields
# ---------------------------------------------------------------------------


def test_spec_default_new_fields_empty() -> None:
    s = Spec()
    assert s.features == []
    assert s.components == []
    assert s.guardrails == []
    assert s.shared_paths == []
    assert s.audit_fixtures == []


def test_spec_with_features() -> None:
    s = Spec(
        intent="doc editor",
        features=[
            Feature(id="md-render", name="Markdown rendering", group_id="editor"),
            Feature(id="image-upload", name="Image upload", group_id="editor"),
        ],
        groups=[Group(id="editor", name="Editor surface")],
    )
    assert len(s.features) == 2
    assert s.features[0].id == "md-render"
    assert s.features[0].group_id == "editor"
    assert len(s.groups) == 1


def test_spec_with_components() -> None:
    s = Spec(
        intent="slack clone",
        components=[
            Component(
                id="websocket-hub",
                name="WebSocket hub",
                consumed_by=["dm-delivery", "presence"],
            ),
        ],
    )
    assert len(s.components) == 1
    assert s.components[0].id == "websocket-hub"


def test_spec_with_guardrails() -> None:
    s = Spec(
        intent="video chat",
        guardrails=[
            Guardrail(id="no-recording", text="No video recording"),
            Guardrail(id="no-screen-share", text="No screen share", applies_to="video"),
        ],
    )
    assert len(s.guardrails) == 2
    assert s.guardrails[1].applies_to == "video"


def test_spec_with_shared_paths() -> None:
    s = Spec(
        intent="multi-feature webapp",
        shared_paths=["models.py", "app.py", "requirements.txt"],
    )
    assert "models.py" in s.shared_paths


def test_spec_with_audit_fixtures() -> None:
    s = Spec(
        intent="multi-user IM",
        audit_fixtures=[
            AuditFixture(kind="user", payload={"username": "alice"}),
            AuditFixture(kind="user", payload={"username": "bob"}),
            AuditFixture(
                kind="channel",
                payload={"name": "general", "members": ["alice", "bob"]},
            ),
        ],
    )
    assert len(s.audit_fixtures) == 3
    assert s.audit_fixtures[2].kind == "channel"


def test_spec_extended_fields_independent_of_groups() -> None:
    """Group remains dispatch unit; Feature is value unit. They're orthogonal."""
    s = Spec(
        intent="webapp",
        groups=[Group(id="g1", name="G1")],
        features=[Feature(id="f1", name="F1", group_id="g1")],
    )
    assert s.groups[0].id == "g1"
    assert s.features[0].group_id == "g1"


# ---------------------------------------------------------------------------
# JSON round-trip — the new fields persist through serialise/parse
# ---------------------------------------------------------------------------


def test_round_trip_with_features() -> None:
    from otto.spec_compile import parse_spec, spec_to_dict

    original = Spec(
        intent="doc editor",
        groups=[Group(id="editor", name="Editor surface")],
        features=[
            Feature(
                id="md-render",
                name="Markdown rendering",
                description="Render .md as HTML",
                acceptance_detail="all CommonMark elements render correctly",
                evidence_kinds=["BrowserJourney", "RepoTestCheck"],
                group_id="editor",
                evidence_completeness="full",
                coverage_confidence="high",
            ),
        ],
    )
    serialised = spec_to_dict(original)
    parsed, warnings = parse_spec(serialised)
    assert len(parsed.features) == 1
    f = parsed.features[0]
    assert f.id == "md-render"
    assert f.name == "Markdown rendering"
    assert f.description == "Render .md as HTML"
    assert f.acceptance_detail == "all CommonMark elements render correctly"
    assert f.evidence_kinds == ["BrowserJourney", "RepoTestCheck"]
    assert f.group_id == "editor"
    assert f.evidence_completeness == "full"
    assert f.coverage_confidence == "high"
    assert f.multi_actor_required is False
    assert warnings == [] or all(w.code != "spec.coerce.field" for w in warnings)


def test_round_trip_with_components() -> None:
    from otto.spec_compile import parse_spec, spec_to_dict

    original = Spec(
        intent="slack-clone",
        groups=[Group(id="messages", name="Messages")],
        components=[
            Component(
                id="websocket-hub",
                name="WebSocket hub",
                description="Pub/sub layer for live updates",
                owned_paths=["realtime/", "ws_server.py"],
                dependencies=["foundation"],
                consumed_by=["dm-delivery", "presence"],
            ),
        ],
    )
    serialised = spec_to_dict(original)
    parsed, _ = parse_spec(serialised)
    assert len(parsed.components) == 1
    c = parsed.components[0]
    assert c.id == "websocket-hub"
    assert c.consumed_by == ["dm-delivery", "presence"]
    assert "foundation" in c.dependencies


def test_round_trip_with_guardrails_and_shared_paths() -> None:
    from otto.spec_compile import parse_spec, spec_to_dict

    original = Spec(
        intent="webapp",
        groups=[Group(id="g1", name="G1")],
        guardrails=[
            Guardrail(id="no-video", text="No video upload", applies_to="*"),
            Guardrail(id="no-cdn", text="No external CDN", applies_to="g1"),
        ],
        shared_paths=["models.py", "app.py", "requirements.txt"],
    )
    serialised = spec_to_dict(original)
    parsed, _ = parse_spec(serialised)
    assert len(parsed.guardrails) == 2
    assert parsed.guardrails[0].applies_to == "*"
    assert parsed.guardrails[1].applies_to == "g1"
    assert parsed.shared_paths == ["models.py", "app.py", "requirements.txt"]


def test_round_trip_with_audit_fixtures() -> None:
    from otto.spec_compile import parse_spec, spec_to_dict

    original = Spec(
        intent="multi-user IM",
        groups=[Group(id="g", name="g")],
        audit_fixtures=[
            AuditFixture(kind="user", payload={"username": "alice", "role": "admin"}),
            AuditFixture(
                kind="follow",
                payload={"follower": "alice", "followed": "bob"},
            ),
        ],
    )
    serialised = spec_to_dict(original)
    parsed, _ = parse_spec(serialised)
    assert len(parsed.audit_fixtures) == 2
    assert parsed.audit_fixtures[0].kind == "user"
    assert parsed.audit_fixtures[0].payload["username"] == "alice"
    assert parsed.audit_fixtures[1].kind == "follow"


def test_legacy_spec_without_new_fields_parses_clean() -> None:
    """A spec.json that predates A1a additions must still parse — defaults to []."""
    from otto.spec_compile import parse_spec

    legacy_payload = {
        "schema_version": 1,
        "intent": "legacy webapp",
        "project_kind": "webapp",
        "structure": {"payload": {"framework": "flask"}},
        "groups": [{"id": "g1", "title": "G1", "tasks": [], "deps": [], "owned_paths": [], "checks": []}],
        # NO features, components, guardrails, shared_paths, audit_fixtures keys
    }
    parsed, warnings = parse_spec(legacy_payload)
    assert parsed.features == []
    assert parsed.components == []
    assert parsed.guardrails == []
    assert parsed.shared_paths == []
    assert parsed.audit_fixtures == []
    # Legacy slices read fine
    assert len(parsed.groups) == 1
    assert parsed.groups[0].id == "g1"


# ---------------------------------------------------------------------------
# Per-kind structure schemas + default evidence kinds (research §2.7)
# ---------------------------------------------------------------------------


def test_per_kind_evidence_defaults_exist_for_each_project_kind() -> None:
    from otto.spec_compile import DEFAULT_EVIDENCE_KINDS_PER_KIND, PROJECT_KINDS

    for kind in PROJECT_KINDS:
        assert kind in DEFAULT_EVIDENCE_KINDS_PER_KIND, (
            f"project_kind {kind!r} missing from DEFAULT_EVIDENCE_KINDS_PER_KIND"
        )
        kinds = DEFAULT_EVIDENCE_KINDS_PER_KIND[kind]
        assert len(kinds) > 0, f"empty evidence kinds for {kind}"


def test_per_kind_evidence_defaults_match_research_spec() -> None:
    """Per research §2.7 — exact evidence-kind sets per project_kind."""
    from otto.spec_compile import DEFAULT_EVIDENCE_KINDS_PER_KIND

    assert set(DEFAULT_EVIDENCE_KINDS_PER_KIND["webapp"]) == {
        "BrowserJourney", "ApiProbe", "StateInvariant", "RepoTestCheck",
    }
    assert set(DEFAULT_EVIDENCE_KINDS_PER_KIND["api"]) == {
        "ApiProbe", "StateInvariant", "RepoTestCheck",
    }
    assert set(DEFAULT_EVIDENCE_KINDS_PER_KIND["library"]) == {
        "ImportCheck", "TypeCheck", "RepoTestCheck",
    }
    assert set(DEFAULT_EVIDENCE_KINDS_PER_KIND["cli"]) == {
        "CLIProbe", "RepoTestCheck",
    }


def test_default_evidence_kinds_for_helper() -> None:
    from otto.spec_compile import default_evidence_kinds_for

    webapp_defaults = default_evidence_kinds_for("webapp")
    assert "BrowserJourney" in webapp_defaults
    cli_defaults = default_evidence_kinds_for("cli")
    assert "CLIProbe" in cli_defaults
    assert "BrowserJourney" not in cli_defaults  # cli should not default to browser


def test_unknown_kind_falls_back_to_webapp_defaults() -> None:
    """Per docstring contract: unknown project_kind → webapp defaults."""
    from otto.spec_compile import default_evidence_kinds_for

    unknown = default_evidence_kinds_for("nonexistent-kind")
    webapp = default_evidence_kinds_for("webapp")
    assert unknown == webapp


def test_per_kind_schemas_exist_on_disk() -> None:
    """Each project_kind must have a JSON schema file (research §2.7)."""
    from otto.spec_compile import PROJECT_KINDS, SCHEMAS_DIR

    for kind in PROJECT_KINDS:
        schema_path = SCHEMAS_DIR / f"{kind}.json"
        assert schema_path.exists(), f"missing schema file for {kind}: {schema_path}"


# ---------------------------------------------------------------------------
# A1b: New Check kinds (CLIProbe, ImportCheck, TypeCheck) + Evidence.feature_id
# ---------------------------------------------------------------------------


def test_cli_probe_construction() -> None:
    from otto.spec_compile import CLIProbe

    c = CLIProbe(
        command=("./mytool", "--help"),
        expect_exit_code=0,
        expect_stdout_substring="Usage:",
    )
    assert c.kind == "cli_probe"
    assert c.command == ("./mytool", "--help")
    assert c.expect_exit_code == 0
    assert c.expect_stdout_substring == "Usage:"
    assert c.expect_stderr_substring == ""
    assert c.timeout_s == 60


def test_import_check_construction() -> None:
    from otto.spec_compile import ImportCheck

    c = ImportCheck(package_name="retryable", expect_version="0.1.0")
    assert c.kind == "import_check"
    assert c.package_name == "retryable"
    assert c.expect_version == "0.1.0"
    assert c.timeout_s == 30


def test_import_check_no_version_required() -> None:
    from otto.spec_compile import ImportCheck

    c = ImportCheck(package_name="my_pkg")
    assert c.expect_version == ""  # default = no version assertion


def test_type_check_construction() -> None:
    from otto.spec_compile import TypeCheck

    c = TypeCheck(paths=("src/", "tests/"), tool="mypy")
    assert c.kind == "type_check"
    assert c.paths == ("src/", "tests/")
    assert c.tool == "mypy"


def test_type_check_pyright_default() -> None:
    from otto.spec_compile import TypeCheck

    c = TypeCheck(paths=("src/",), tool="pyright")
    assert c.tool == "pyright"


def test_check_kind_union_includes_new_kinds() -> None:
    """CheckKind union must include all 8 kinds (5 legacy + 3 A1b)."""
    from otto.spec_compile import (
        CLIProbe, ImportCheck, TypeCheck, _CHECK_TYPES,
    )
    expected_keys = {
        "pytest", "repo_test", "api_probe", "browser_journey",
        "state_invariant", "cli_probe", "import_check", "type_check",
    }
    assert set(_CHECK_TYPES.keys()) == expected_keys
    # Each value is the corresponding dataclass
    assert _CHECK_TYPES["cli_probe"] is CLIProbe
    assert _CHECK_TYPES["import_check"] is ImportCheck
    assert _CHECK_TYPES["type_check"] is TypeCheck


def test_evidence_feature_id_default_empty() -> None:
    from otto.checks import Evidence

    e = Evidence(passed=True, started_at="2026-05-04T20:00:00Z", duration_s=1.0, detail="ok")
    assert e.feature_id == ""


def test_evidence_feature_id_attribution() -> None:
    from otto.checks import Evidence

    e = Evidence(
        passed=True,
        started_at="2026-05-04T20:00:00Z",
        duration_s=2.5,
        detail="auth route returned 200",
        feature_id="auth-login",
    )
    assert e.feature_id == "auth-login"


def test_check_round_trip_serialization_includes_new_kinds() -> None:
    """spec_to_dict and parse_spec must round-trip new Check kinds."""
    from otto.spec_compile import (
        CLIProbe, ImportCheck, TypeCheck, Group, Spec, parse_spec, spec_to_dict,
    )

    original = Spec(
        intent="multi-kind project",
        groups=[
            Group(
                id="cli-bin",
                name="CLI",
                checks=[
                    CLIProbe(command=("./bin", "list"), expect_exit_code=0),
                    ImportCheck(package_name="my_lib"),
                    TypeCheck(paths=("src/",), tool="mypy"),
                ],
            )
        ],
    )
    serialised = spec_to_dict(original)
    parsed, _ = parse_spec(serialised)
    checks = parsed.groups[0].checks
    assert len(checks) == 3
    # Order preserved; types preserved
    assert isinstance(checks[0], CLIProbe)
    assert checks[0].command == ("./bin", "list")
    assert isinstance(checks[1], ImportCheck)
    assert checks[1].package_name == "my_lib"
    assert isinstance(checks[2], TypeCheck)
    assert checks[2].paths == ("src/",)


# ---------------------------------------------------------------------------
# A1b: ComponentResult + BuildResult component accessors (research §2.6)
# ---------------------------------------------------------------------------


def test_component_status_enum_values() -> None:
    from otto.build import ComponentStatus

    assert ComponentStatus.PENDING.value == "pending"
    assert ComponentStatus.IN_PROGRESS.value == "in_progress"
    assert ComponentStatus.PASSING.value == "passing"
    assert ComponentStatus.BLOCKED.value == "blocked"
    assert ComponentStatus.LANDED.value == "landed"


def test_component_result_construction() -> None:
    from pathlib import Path
    from otto.build import ComponentResult, ComponentStatus

    r = ComponentResult(
        component_id="websocket-hub",
        status=ComponentStatus.PASSING,
        attempts=1,
        branch="comp/websocket-hub",
        worktree=Path("/tmp/wt"),
    )
    assert r.component_id == "websocket-hub"
    assert r.status == ComponentStatus.PASSING
    assert r.attempts == 1
    assert r.cost_usd == 0.0
    assert r.last_evidence == []
    assert r.failure_narrative == ""


def test_build_result_component_accessors() -> None:
    from pathlib import Path
    from otto.build import BuildResult, ComponentResult, ComponentStatus

    result = BuildResult(
        spec_session_dir=Path("/tmp/session"),
        component_results=[
            ComponentResult(
                component_id="ws-hub",
                status=ComponentStatus.PASSING,
                attempts=1,
                branch="b1",
                worktree=Path("/tmp/wt1"),
            ),
            ComponentResult(
                component_id="search-idx",
                status=ComponentStatus.BLOCKED,
                attempts=3,
                branch="b2",
                worktree=Path("/tmp/wt2"),
            ),
            ComponentResult(
                component_id="notify-fanout",
                status=ComponentStatus.PASSING,
                attempts=2,
                branch="b3",
                worktree=Path("/tmp/wt3"),
            ),
        ],
    )
    assert result.passing_component_ids == ["ws-hub", "notify-fanout"]
    assert result.blocked_component_ids == ["search-idx"]
    assert result.all_components_passing is False


def test_build_result_all_components_passing_vacuously_true_when_empty() -> None:
    from pathlib import Path
    from otto.build import BuildResult

    result = BuildResult(spec_session_dir=Path("/tmp/session"))
    assert result.all_components_passing is True
    assert result.component_results == []


def test_build_result_all_components_passing_when_all_pass() -> None:
    from pathlib import Path
    from otto.build import BuildResult, ComponentResult, ComponentStatus

    result = BuildResult(
        spec_session_dir=Path("/tmp/session"),
        component_results=[
            ComponentResult(
                component_id=f"c{i}",
                status=ComponentStatus.PASSING,
                attempts=1,
                branch=f"b{i}",
                worktree=Path(f"/tmp/wt{i}"),
            )
            for i in range(3)
        ],
    )
    assert result.all_components_passing is True


def test_build_result_slices_and_components_independent() -> None:
    """Slices and components are orthogonal — research §2.6."""
    from pathlib import Path
    from otto.build import (
        BuildResult, ComponentResult, ComponentStatus, GroupResult,
        GroupStatus,
    )

    result = BuildResult(
        spec_session_dir=Path("/tmp/session"),
        group_results=[
            GroupResult(
                group_id="g1",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="g1",
                worktree=Path("/tmp/g1"),
            )
        ],
        component_results=[
            ComponentResult(
                component_id="c1",
                status=ComponentStatus.BLOCKED,
                attempts=3,
                branch="c1",
                worktree=Path("/tmp/c1"),
            )
        ],
    )
    # Group passing + Component blocked → all_passing returns True (slices only)
    # but all_components_passing returns False
    assert result.all_passing is True
    assert result.all_components_passing is False
    assert "g1" in result.passing_ids
    assert "c1" in result.blocked_component_ids


# ---------------------------------------------------------------------------
# A1c: merge queue Component eligibility + shared_paths (research §2.6)
# ---------------------------------------------------------------------------


def test_eligible_components_basic() -> None:
    from otto.merge_queue import eligible_components

    s = Spec(
        intent="multi-component",
        groups=[Group(id="g1", name="G1")],
        components=[
            Component(id="ws-hub", name="WebSocket hub"),
            Component(id="search", name="Search index"),
        ],
    )
    out = eligible_components(s, passing_ids=["ws-hub", "search"], landed_ids=[])
    assert [c.id for c in out] == ["ws-hub", "search"]


def test_eligible_components_dep_ordering() -> None:
    from otto.merge_queue import eligible_components

    s = Spec(
        intent="component deps",
        groups=[],
        components=[
            Component(id="db", name="DB"),
            Component(
                id="search", name="Search", dependencies=["db"],
            ),
            Component(
                id="notifier", name="Notifier", dependencies=["search"],
            ),
        ],
    )
    # First pass: only db is eligible (no deps); search/notifier blocked
    out = eligible_components(
        s, passing_ids=["db", "search", "notifier"], landed_ids=[]
    )
    assert [c.id for c in out] == ["db"]

    # Second pass: db landed → search now eligible
    out = eligible_components(
        s, passing_ids=["db", "search", "notifier"], landed_ids=["db"]
    )
    assert [c.id for c in out] == ["search"]

    # Third pass: db + search landed → notifier eligible
    out = eligible_components(
        s, passing_ids=["db", "search", "notifier"],
        landed_ids=["db", "search"],
    )
    assert [c.id for c in out] == ["notifier"]


def test_eligible_components_skips_already_landed_or_blocked() -> None:
    from otto.merge_queue import eligible_components

    s = Spec(
        intent="status filter",
        components=[
            Component(id="a", name="A"),
            Component(id="b", name="B"),
            Component(id="c", name="C"),
        ],
    )
    out = eligible_components(
        s,
        passing_ids=["a", "b", "c"],
        landed_ids=["a"],   # a already landed
        blocked_ids=["b"],  # b terminally failed
    )
    assert [c.id for c in out] == ["c"]


def test_eligible_components_cross_kind_dependency() -> None:
    """A Component can depend on a Group id (and vice versa) — the
    eligibility caller passes the union of landed Group + Component ids
    in landed_ids."""
    from otto.merge_queue import eligible_components

    s = Spec(
        intent="cross-kind deps",
        groups=[Group(id="auth-group", name="Auth")],
        components=[
            # search-index depends on auth-group landing first
            Component(id="search", name="Search", dependencies=["auth-group"]),
        ],
    )
    # auth-group not yet landed → search not eligible
    out = eligible_components(
        s, passing_ids=["search"], landed_ids=[]
    )
    assert out == []

    # auth-group landed → search eligible
    out = eligible_components(
        s, passing_ids=["search"], landed_ids=["auth-group"]
    )
    assert [c.id for c in out] == ["search"]


def test_eligible_components_skips_non_passing() -> None:
    from otto.merge_queue import eligible_components

    s = Spec(
        intent="passing filter",
        components=[
            Component(id="a", name="A"),
            Component(id="b", name="B"),
        ],
    )
    # Only a is passing; b not yet built
    out = eligible_components(s, passing_ids=["a"], landed_ids=[])
    assert [c.id for c in out] == ["a"]


def test_shared_paths_set() -> None:
    from otto.merge_queue import shared_paths_set

    s = Spec(
        intent="shared scaffold",
        groups=[Group(id="g1", name="G1")],
        shared_paths=["models.py", "app.py", "requirements.txt"],
    )
    paths = shared_paths_set(s)
    assert paths == {"models.py", "app.py", "requirements.txt"}


def test_shared_paths_empty_when_unset() -> None:
    from otto.merge_queue import shared_paths_set

    s = Spec(intent="no shared", groups=[Group(id="g", name="g")])
    assert shared_paths_set(s) == set()


def test_eligible_groups_unchanged_by_component_addition() -> None:
    """Adding eligible_components must not regress eligible_candidates
    behavior for Groups (research §2.6 orthogonality)."""
    from otto.merge_queue import eligible_candidates, eligible_components

    s = Spec(
        intent="orthogonality",
        groups=[
            Group(id="g1", name="G1"),
            Group(id="g2", name="G2", dependencies=["g1"]),
        ],
        components=[
            Component(id="c1", name="C1"),
            Component(id="c2", name="C2", dependencies=["c1"]),
        ],
    )
    # First pass — no landed yet; only items without deps are eligible
    g_eligible = eligible_candidates(
        s, passing_ids=["g1", "g2"], landed_ids=[]
    )
    c_eligible = eligible_components(
        s, passing_ids=["c1", "c2"], landed_ids=[]
    )
    assert [g.id for g in g_eligible] == ["g1"]
    assert [c.id for c in c_eligible] == ["c1"]


# ---------------------------------------------------------------------------
# A2: Audit Feature-tagging foundation (research §4 + §2.7)
# ---------------------------------------------------------------------------


def test_walkthrough_action_kinds_defined() -> None:
    from otto.spec_compile import WALKTHROUGH_ACTION_KINDS

    assert "exploration" in WALKTHROUGH_ACTION_KINDS
    assert "browser_navigation" in WALKTHROUGH_ACTION_KINDS
    assert "api_request" in WALKTHROUGH_ACTION_KINDS
    assert "cli_invoke" in WALKTHROUGH_ACTION_KINDS
    assert "import_check" in WALKTHROUGH_ACTION_KINDS
    assert "type_check" in WALKTHROUGH_ACTION_KINDS


def test_walkthrough_entry_construction() -> None:
    from otto.spec_compile import WalkthroughEntry

    e = WalkthroughEntry(
        t="00:42.13",
        feature_ids=["auth"],
        action_kind="browser_navigation",
        narrative="GET /register",
        extras={"screenshot": "assets/audit-001.png", "url": "/register"},
    )
    assert e.t == "00:42.13"
    assert e.feature_ids == ["auth"]
    assert e.action_kind == "browser_navigation"
    assert e.extras["screenshot"] == "assets/audit-001.png"


def test_parse_walkthrough_entry_happy_path() -> None:
    from otto.spec_compile import parse_walkthrough_entry

    spec = Spec(
        intent="webapp",
        features=[Feature(id="auth", name="Auth")],
    )
    payload = {
        "t": "00:42.13",
        "feature_ids": ["auth"],
        "action_kind": "browser_navigation",
        "narrative": "GET /register",
        "url": "/register",
        "screenshot": "assets/audit-001.png",
    }
    entry, warnings = parse_walkthrough_entry(payload, spec)
    assert entry is not None
    assert entry.feature_ids == ["auth"]
    assert entry.action_kind == "browser_navigation"
    assert entry.extras["url"] == "/register"
    assert warnings == []


def test_parse_walkthrough_entry_unknown_feature_id_warns() -> None:
    from otto.spec_compile import parse_walkthrough_entry

    spec = Spec(
        intent="webapp",
        features=[Feature(id="auth", name="Auth")],
    )
    payload = {
        "t": "00:42",
        "feature_ids": ["nonexistent-feature"],
        "action_kind": "browser_navigation",
    }
    entry, warnings = parse_walkthrough_entry(payload, spec)
    assert entry is not None
    assert any("unknown_feature_id" in w for w in warnings)


def test_parse_walkthrough_entry_untagged_non_exploration_warns() -> None:
    from otto.spec_compile import parse_walkthrough_entry

    spec = Spec(intent="webapp", features=[Feature(id="auth", name="Auth")])
    payload = {
        "t": "00:42",
        "feature_ids": [],
        "action_kind": "browser_navigation",
        "narrative": "untagged action",
    }
    entry, warnings = parse_walkthrough_entry(payload, spec)
    assert entry is not None
    assert "untagged_non_exploration" in warnings


def test_parse_walkthrough_entry_exploration_no_warning() -> None:
    from otto.spec_compile import parse_walkthrough_entry

    spec = Spec(intent="webapp", features=[Feature(id="auth", name="Auth")])
    payload = {
        "t": "00:00",
        "feature_ids": [],
        "action_kind": "exploration",
        "narrative": "GET /favicon.ico",
    }
    entry, warnings = parse_walkthrough_entry(payload, spec)
    assert entry is not None
    assert "untagged_non_exploration" not in warnings


def test_parse_walkthrough_entry_unknown_action_kind_warns() -> None:
    from otto.spec_compile import parse_walkthrough_entry

    spec = Spec(intent="webapp", features=[Feature(id="auth", name="Auth")])
    payload = {
        "t": "00:42",
        "feature_ids": ["auth"],
        "action_kind": "invalid-kind",
    }
    entry, warnings = parse_walkthrough_entry(payload, spec)
    assert entry is not None
    assert any("unknown_action_kind" in w for w in warnings)


def test_parse_walkthrough_entry_malformed_returns_none() -> None:
    from otto.spec_compile import parse_walkthrough_entry

    spec = Spec(intent="webapp", features=[])
    entry, warnings = parse_walkthrough_entry("not a dict", spec)  # type: ignore[arg-type]
    assert entry is None
    assert warnings


def test_validate_walkthrough_coverage_meets_threshold() -> None:
    from otto.spec_compile import (
        WalkthroughEntry, validate_walkthrough_coverage,
    )

    spec = Spec(
        intent="webapp",
        features=[
            Feature(id="auth", name="Auth"),
            Feature(id="profile", name="Profile"),
        ],
    )
    entries = [
        WalkthroughEntry(t="00:00", action_kind="exploration"),
        WalkthroughEntry(t="00:01", feature_ids=["auth"], action_kind="browser_navigation"),
        WalkthroughEntry(t="00:02", feature_ids=["auth"], action_kind="api_request"),
        WalkthroughEntry(t="00:03", feature_ids=["profile"], action_kind="browser_navigation"),
    ]
    report = validate_walkthrough_coverage(entries, spec)
    assert report.total_entries == 4
    assert report.exploration_entries == 1
    assert report.tagged_entries == 3
    assert report.untagged_entries == 0
    assert report.coverage_ratio == 1.0
    assert report.meets_threshold() is True


def test_validate_walkthrough_coverage_below_threshold() -> None:
    from otto.spec_compile import (
        WalkthroughEntry, validate_walkthrough_coverage,
    )

    spec = Spec(intent="webapp", features=[Feature(id="auth", name="Auth")])
    entries = [
        WalkthroughEntry(t="00:00", action_kind="exploration"),
        WalkthroughEntry(t="00:01", feature_ids=["auth"], action_kind="browser_navigation"),
        # 9 untagged non-exploration → 1/10 tagged = 10% coverage
        *[
            WalkthroughEntry(t=f"00:0{i}", feature_ids=[], action_kind="browser_navigation")
            for i in range(2, 11)
        ],
    ]
    report = validate_walkthrough_coverage(entries, spec)
    assert report.untagged_entries == 9
    assert report.tagged_entries == 1
    assert 0.05 < report.coverage_ratio < 0.15  # 10%
    assert report.meets_threshold(0.90) is False


def test_validate_walkthrough_coverage_per_feature_count() -> None:
    from otto.spec_compile import (
        WalkthroughEntry, validate_walkthrough_coverage,
    )

    spec = Spec(
        intent="webapp",
        features=[
            Feature(id="auth", name="Auth"),
            Feature(id="profile", name="Profile"),
        ],
    )
    entries = [
        WalkthroughEntry(t="0", feature_ids=["auth"], action_kind="api_request"),
        WalkthroughEntry(t="1", feature_ids=["auth"], action_kind="browser_navigation"),
        WalkthroughEntry(t="2", feature_ids=["auth"], action_kind="browser_navigation"),
        # profile gets zero evidence — surfaced in the report
    ]
    report = validate_walkthrough_coverage(entries, spec)
    assert report.per_feature_evidence_count["auth"] == 3
    assert report.per_feature_evidence_count["profile"] == 0


def test_validate_walkthrough_coverage_unknown_feature_ids_listed() -> None:
    from otto.spec_compile import (
        WalkthroughEntry, validate_walkthrough_coverage,
    )

    spec = Spec(intent="webapp", features=[Feature(id="auth", name="Auth")])
    entries = [
        WalkthroughEntry(
            t="0",
            feature_ids=["auth", "nonexistent"],
            action_kind="api_request",
        ),
    ]
    report = validate_walkthrough_coverage(entries, spec)
    assert "nonexistent" in report.unknown_feature_id_refs


def test_validate_walkthrough_coverage_vacuous_truth_for_setup_only() -> None:
    """If walkthrough is purely exploration (no feature-tagged actions),
    coverage_ratio is 1.0 (vacuously). Audit may still flag a separate
    "no-tagged-evidence" warning but the threshold isn't violated."""
    from otto.spec_compile import (
        WalkthroughEntry, validate_walkthrough_coverage,
    )

    spec = Spec(intent="webapp", features=[Feature(id="auth", name="Auth")])
    entries = [
        WalkthroughEntry(t="0", action_kind="exploration"),
        WalkthroughEntry(t="1", action_kind="exploration"),
    ]
    report = validate_walkthrough_coverage(entries, spec)
    assert report.coverage_ratio == 1.0  # vacuous
    assert report.meets_threshold() is True


# ---------------------------------------------------------------------------
# A2: audit_loop.py — Layer 2 retries (research §4)
# ---------------------------------------------------------------------------


def test_select_failing_features_picks_repair_candidates() -> None:
    from otto.audit_loop import select_failing_features

    verdicts = [
        {"feature_id": "auth", "verdict": "passed", "detail": "ok"},
        {"feature_id": "profile", "verdict": "failed", "detail": "404"},
        {"feature_id": "search", "verdict": "partial", "detail": "slow"},
        {"feature_id": "comments", "verdict": "blocked", "detail": "no merge"},
        {"feature_id": "polish", "verdict": "missing", "detail": "no walkthrough"},
    ]
    failing = select_failing_features(verdicts)
    ids = [f.feature_id for f in failing]
    assert "auth" not in ids   # passed
    assert "profile" in ids    # failed
    assert "search" in ids     # partial
    assert "comments" in ids   # blocked
    assert "polish" in ids     # missing


def test_select_failing_features_skips_malformed() -> None:
    from otto.audit_loop import select_failing_features

    failing = select_failing_features(
        [
            "not-a-dict",  # type: ignore[list-item]
            {"feature_id": "a", "verdict": "failed"},
            None,  # type: ignore[list-item]
        ]
    )
    assert len(failing) == 1
    assert failing[0].feature_id == "a"


def test_group_for_feature_finds_owner() -> None:
    from otto.audit_loop import group_for_feature

    spec = Spec(
        intent="webapp",
        groups=[Group(id="auth-group", name="Auth")],
        features=[Feature(id="auth", name="Auth", group_id="auth-group")],
    )
    g = group_for_feature(spec, "auth")
    assert g is not None
    assert g.id == "auth-group"


def test_group_for_feature_returns_none_for_orphan() -> None:
    from otto.audit_loop import group_for_feature

    spec = Spec(
        intent="webapp",
        groups=[Group(id="g1", name="G1")],
        features=[Feature(id="orphan", name="Orphan", group_id="")],
    )
    assert group_for_feature(spec, "orphan") is None
    assert group_for_feature(spec, "unknown-feature") is None


def test_features_to_repair_coalesces_same_group_before_default_cap() -> None:
    from otto.audit_loop import features_to_repair

    spec = Spec(
        intent="webapp",
        groups=[Group(id="g1", name="G1")],
        features=[
            Feature(id=f"f{i}", name=f"F{i}", group_id="g1") for i in range(10)
        ],
    )
    verdicts = [
        {"feature_id": f"f{i}", "verdict": "failed", "detail": "x"}
        for i in range(10)
    ]
    candidates = features_to_repair(spec, verdicts)
    assert len(candidates) == 1
    assert candidates[0].feature_id == "f0"
    assert candidates[0].related_feature_ids == [f"f{i}" for i in range(10)]
    assert "Multiple actionable audit failures share group `g1`" in candidates[0].detail


def test_features_to_repair_respects_explicit_cap() -> None:
    from otto.audit_loop import features_to_repair

    spec = Spec(
        intent="webapp",
        groups=[Group(id=f"g{i}", name=f"G{i}") for i in range(5)],
        features=[
            Feature(id=f"f{i}", name=f"F{i}", group_id=f"g{i}") for i in range(5)
        ],
    )
    verdicts = [
        {"feature_id": f"f{i}", "verdict": "failed", "detail": "x"}
        for i in range(5)
    ]
    candidates = features_to_repair(spec, verdicts, max_attempts_per_run=3)
    assert len(candidates) == 3


def test_features_to_repair_excludes_orphans() -> None:
    from otto.audit_loop import features_to_repair

    spec = Spec(
        intent="webapp",
        groups=[Group(id="g1", name="G1")],
        features=[
            Feature(id="owned", name="Owned", group_id="g1"),
            Feature(id="orphan", name="Orphan", group_id=""),
            Feature(
                id="bad-group",
                name="Bad group",
                group_id="nonexistent-group",
            ),
        ],
    )
    verdicts = [
        {"feature_id": "owned", "verdict": "failed", "detail": "x"},
        {"feature_id": "orphan", "verdict": "failed", "detail": "x"},
        {"feature_id": "bad-group", "verdict": "failed", "detail": "x"},
    ]
    candidates = features_to_repair(spec, verdicts, max_attempts_per_run=10)
    ids = [c.feature_id for c in candidates]
    assert "owned" in ids
    assert "orphan" not in ids   # no group_id → no repair routing
    assert "bad-group" not in ids  # group_id refers to non-existent group


def test_can_run_another_audit_pass_within_cap() -> None:
    from otto.audit_loop import can_run_another_audit_pass

    # Default max_audit_passes = 4; original counts as 1
    assert can_run_another_audit_pass(audit_passes_run=1) is True
    assert can_run_another_audit_pass(audit_passes_run=3) is True
    assert can_run_another_audit_pass(audit_passes_run=4) is False


def test_can_run_another_audit_pass_explicit_cap() -> None:
    from otto.audit_loop import can_run_another_audit_pass

    assert can_run_another_audit_pass(audit_passes_run=1, max_audit_passes=3) is True
    assert can_run_another_audit_pass(audit_passes_run=3, max_audit_passes=3) is False


def test_repair_result_accessors() -> None:
    from otto.audit_loop import RepairAttempt, RepairResult

    result = RepairResult(
        attempts=[
            RepairAttempt(
                feature_id="auth",
                group_id="auth-group",
                attempt_number=1,
                succeeded=True,
                new_verdict="passed",
            ),
            RepairAttempt(
                feature_id="search",
                group_id="search-group",
                attempt_number=1,
                succeeded=False,
                new_verdict="failed",
            ),
        ],
        audit_passes_run=2,
    )
    assert result.repaired_feature_ids == ["auth"]
    assert result.still_failing_feature_ids == ["search"]


# ---------------------------------------------------------------------------
# A3: Per-Feature proof packet renderer foundation (research §7)
# ---------------------------------------------------------------------------


def test_filter_walkthrough_by_feature_basic() -> None:
    from otto.spec_compile import filter_walkthrough_by_feature, WalkthroughEntry

    entries = [
        WalkthroughEntry(t="0", feature_ids=["auth"], action_kind="api_request"),
        WalkthroughEntry(t="1", feature_ids=["profile"], action_kind="api_request"),
        WalkthroughEntry(t="2", feature_ids=["auth"], action_kind="browser_navigation"),
    ]
    auth_slice = filter_walkthrough_by_feature(entries, "auth")
    assert len(auth_slice) == 2
    assert auth_slice[0].t == "0"
    assert auth_slice[1].t == "2"

    profile_slice = filter_walkthrough_by_feature(entries, "profile")
    assert len(profile_slice) == 1


def test_filter_walkthrough_by_feature_excludes_exploration() -> None:
    from otto.spec_compile import filter_walkthrough_by_feature, WalkthroughEntry

    entries = [
        WalkthroughEntry(t="0", action_kind="exploration"),
        WalkthroughEntry(t="1", feature_ids=["auth"], action_kind="api_request"),
    ]
    auth_slice = filter_walkthrough_by_feature(entries, "auth")
    assert len(auth_slice) == 1
    assert auth_slice[0].t == "1"


def test_slice_walkthrough_multi_feature_cross_link() -> None:
    """Research §7: multi-Feature entries appear in EACH relevant
    Feature's slice — don't double-store, cross-link."""
    from otto.spec_compile import filter_walkthrough_by_feature, WalkthroughEntry

    entries = [
        # This entry evidences both upload AND comment
        WalkthroughEntry(
            t="00:42",
            feature_ids=["image-upload", "comment"],
            action_kind="browser_navigation",
            narrative="user uploads image, then comments on it",
        ),
        WalkthroughEntry(
            t="00:50",
            feature_ids=["comment"],
            action_kind="browser_navigation",
            narrative="user posts comment text",
        ),
    ]
    upload_slice = filter_walkthrough_by_feature(entries, "image-upload")
    comment_slice = filter_walkthrough_by_feature(entries, "comment")
    # Multi-Feature entry appears in both slices (cross-link)
    assert len(upload_slice) == 1
    assert len(comment_slice) == 2  # multi-Feature entry + comment-only entry


def test_feature_proof_block_construction() -> None:
    from otto.spec_compile import FeatureProofBlock, WalkthroughEntry

    block = FeatureProofBlock(
        feature_id="auth",
        name="Auth (register/login)",
        description="user can register",
        group_id="auth-group",
        verdict="passed",
        detail="all auth flows pass",
        walkthrough_entries=[
            WalkthroughEntry(
                t="0", feature_ids=["auth"], action_kind="api_request"
            ),
        ],
    )
    assert block.feature_id == "auth"
    assert block.verdict == "passed"
    assert block.evidence_completeness == "full"  # default
    assert block.coverage_confidence == "high"  # default
    assert block.shared_with == []
    assert block.findings == []


def test_build_feature_proof_blocks_basic() -> None:
    from otto.spec_compile import build_feature_proof_blocks, WalkthroughEntry

    spec = Spec(
        intent="webapp",
        groups=[Group(id="auth-group", name="Auth", owned_paths=["routes/auth.py"])],
        features=[
            Feature(
                id="auth", name="Auth", description="register+login",
                group_id="auth-group",
            ),
        ],
    )
    walkthrough = [
        WalkthroughEntry(t="0", feature_ids=["auth"], action_kind="api_request"),
        WalkthroughEntry(t="1", feature_ids=["auth"], action_kind="browser_navigation"),
    ]
    verdicts = [
        {
            "feature_id": "auth",
            "verdict": "passed",
            "detail": "register+login both work",
            "evidence_refs": ["walkthrough.jsonl#L1-L2"],
        }
    ]
    blocks = build_feature_proof_blocks(spec, walkthrough, verdicts)
    assert len(blocks) == 1
    block = blocks[0]
    assert block.feature_id == "auth"
    assert block.verdict == "passed"
    assert block.detail == "register+login both work"
    assert len(block.walkthrough_entries) == 2
    assert block.check_evidence_refs == ["walkthrough.jsonl#L1-L2"]


def test_build_feature_proof_blocks_includes_files_per_group() -> None:
    from otto.spec_compile import build_feature_proof_blocks

    spec = Spec(
        intent="webapp",
        groups=[Group(id="auth", name="Auth")],
        features=[Feature(id="login", name="Login", group_id="auth")],
    )
    blocks = build_feature_proof_blocks(
        spec,
        walkthrough_entries=[],
        feature_verdicts=[],
        files_per_group={"auth": ["routes/auth.py", "templates/login.html"]},
    )
    assert blocks[0].files_changed == ["routes/auth.py", "templates/login.html"]


def test_build_feature_proof_blocks_attaches_findings_by_feature_id() -> None:
    from otto.spec_compile import (
        Finding, build_feature_proof_blocks,
    )

    spec = Spec(
        intent="webapp",
        groups=[Group(id="g", name="g")],
        features=[
            Feature(id="home", name="Home", group_id="g"),
            Feature(id="auth", name="Auth", group_id="g"),
        ],
    )
    findings = [
        Finding(severity="critical", text="page load >8s", feature_id="home"),
        Finding(severity="polish", text="palette generic", feature_id=""),  # whole-product
        Finding(severity="important", text="auth flow", feature_id="auth"),
    ]
    blocks = build_feature_proof_blocks(
        spec, walkthrough_entries=[], feature_verdicts=[], findings=findings,
    )
    home_block = next(b for b in blocks if b.feature_id == "home")
    auth_block = next(b for b in blocks if b.feature_id == "auth")
    assert len(home_block.findings) == 1
    assert home_block.findings[0].severity == "critical"
    assert len(auth_block.findings) == 1
    # Whole-product findings (feature_id="") don't attach to any per-Feature block
    for block in blocks:
        for f in block.findings:
            assert f.feature_id == block.feature_id


def test_build_feature_proof_blocks_missing_walkthrough_for_feature() -> None:
    """Per research §4: a Feature with 0 walkthrough lines tagged to it
    returns verdict=missing — never passed. Test that the proof block
    surfaces the empty walkthrough_entries list honestly so the renderer
    can flag verdict=missing."""
    from otto.spec_compile import build_feature_proof_blocks

    spec = Spec(
        intent="webapp",
        groups=[Group(id="g", name="g")],
        features=[
            Feature(id="audited", name="Audited", group_id="g"),
            Feature(id="unaudited", name="Unaudited", group_id="g"),
        ],
    )
    walkthrough = []  # empty — no Feature was actually walked
    verdicts = [
        {"feature_id": "audited", "verdict": "passed"},
        # Note: no entry for "unaudited" — its block should default to missing
    ]
    blocks = build_feature_proof_blocks(spec, walkthrough, verdicts)
    audited_block = next(b for b in blocks if b.feature_id == "audited")
    unaudited_block = next(b for b in blocks if b.feature_id == "unaudited")
    assert audited_block.walkthrough_entries == []
    assert unaudited_block.walkthrough_entries == []
    # Audited block has explicit verdict; unaudited has None (renderer
    # must treat as missing)
    assert audited_block.verdict == "passed"
    assert unaudited_block.verdict is None


def test_build_feature_proof_blocks_multi_feature_cross_link() -> None:
    """A walkthrough entry tagged ['a', 'b'] appears in BOTH blocks,
    and each block lists the other in shared_with (research §7)."""
    from otto.spec_compile import build_feature_proof_blocks, WalkthroughEntry

    spec = Spec(
        intent="webapp",
        groups=[Group(id="g", name="g")],
        features=[
            Feature(id="a", name="A", group_id="g"),
            Feature(id="b", name="B", group_id="g"),
        ],
    )
    walkthrough = [
        WalkthroughEntry(
            t="0", feature_ids=["a", "b"], action_kind="api_request",
            narrative="evidences both A and B at once",
        ),
    ]
    blocks = build_feature_proof_blocks(spec, walkthrough, feature_verdicts=[])
    a_block = next(b for b in blocks if b.feature_id == "a")
    b_block = next(b for b in blocks if b.feature_id == "b")
    # Multi-Feature entry appears in both
    assert len(a_block.walkthrough_entries) == 1
    assert len(b_block.walkthrough_entries) == 1
    # Cross-link surfaces the other Feature
    assert "b" in a_block.shared_with
    assert "a" in b_block.shared_with


# ---------------------------------------------------------------------------
# A3: per-Feature proof JSON emission (research §7)
# ---------------------------------------------------------------------------


def test_walkthrough_entry_to_dict_round_trip() -> None:
    from otto.spec_compile import (
        parse_walkthrough_entry, walkthrough_entry_to_dict,
    )
    spec = Spec(intent="webapp", features=[Feature(id="auth", name="Auth")])
    payload = {
        "t": "00:42",
        "feature_ids": ["auth"],
        "action_kind": "browser_navigation",
        "narrative": "GET /register",
        "url": "/register",
        "screenshot": "assets/audit-001.png",
    }
    entry, _ = parse_walkthrough_entry(payload, spec)
    assert entry is not None
    out = walkthrough_entry_to_dict(entry)
    # Core fields preserved
    assert out["t"] == "00:42"
    assert out["feature_ids"] == ["auth"]
    assert out["action_kind"] == "browser_navigation"
    assert out["narrative"] == "GET /register"
    # Kind-specific extras flattened back
    assert out["url"] == "/register"
    assert out["screenshot"] == "assets/audit-001.png"


def test_feature_proof_block_to_dict_full() -> None:
    from otto.spec_compile import (
        FeatureProofBlock, Finding, WalkthroughEntry, feature_proof_block_to_dict,
    )

    block = FeatureProofBlock(
        feature_id="auth",
        name="Auth",
        description="register+login",
        group_id="auth-group",
        verdict="passed",
        detail="all flows pass",
        walkthrough_entries=[
            WalkthroughEntry(
                t="00:42",
                feature_ids=["auth"],
                action_kind="api_request",
                narrative="POST /register",
                extras={"method": "POST", "path": "/register", "response_status": 201},
            ),
        ],
        shared_with=["profile"],
        evidence_completeness="full",
        coverage_confidence="high",
        check_evidence_refs=["walkthrough.jsonl#L1"],
        files_changed=["routes/auth.py", "templates/login.html"],
        repair_history=[{"attempt": 1, "succeeded": True}],
        audit_narrative_excerpt="The audit walked register and login flows.",
        findings=[
            Finding(severity="polish", text="form spacing tight", feature_id="auth"),
        ],
    )
    d = feature_proof_block_to_dict(block)
    assert d["feature_id"] == "auth"
    assert d["verdict"] == "passed"
    assert d["evidence_completeness"] == "full"
    assert d["coverage_confidence"] == "high"
    assert d["shared_with"] == ["profile"]
    assert len(d["walkthrough_entries"]) == 1
    assert d["walkthrough_entries"][0]["method"] == "POST"
    assert d["check_evidence_refs"] == ["walkthrough.jsonl#L1"]
    assert d["files_changed"] == ["routes/auth.py", "templates/login.html"]
    assert d["repair_history"] == [{"attempt": 1, "succeeded": True}]
    assert d["audit_narrative_excerpt"] == "The audit walked register and login flows."
    assert d["findings"][0]["severity"] == "polish"
    assert d["findings"][0]["feature_id"] == "auth"


def test_feature_proof_block_to_dict_minimum_defaults() -> None:
    from otto.spec_compile import FeatureProofBlock, feature_proof_block_to_dict

    block = FeatureProofBlock(feature_id="x", name="X")
    d = feature_proof_block_to_dict(block)
    assert d["feature_id"] == "x"
    assert d["verdict"] is None
    assert d["evidence_completeness"] == "full"
    assert d["walkthrough_entries"] == []
    assert d["findings"] == []


def test_feature_proof_blocks_to_dicts_preserves_order() -> None:
    from otto.spec_compile import (
        FeatureProofBlock, feature_proof_blocks_to_dicts,
    )
    blocks = [
        FeatureProofBlock(feature_id=f"f{i}", name=f"F{i}") for i in range(5)
    ]
    dicts = feature_proof_blocks_to_dicts(blocks)
    assert [d["feature_id"] for d in dicts] == ["f0", "f1", "f2", "f3", "f4"]


def test_build_and_serialise_per_feature_proof_end_to_end() -> None:
    """Spec + walkthrough + verdicts → FeatureProofBlock list → dicts.
    The shape that proof/features/<id>/proof.json holds."""
    from otto.spec_compile import (
        WalkthroughEntry, build_feature_proof_blocks,
        feature_proof_blocks_to_dicts,
    )

    spec = Spec(
        intent="webapp",
        groups=[Group(id="auth-group", name="Auth", owned_paths=["routes/auth.py"])],
        features=[
            Feature(
                id="auth",
                name="Auth",
                description="register+login",
                group_id="auth-group",
                evidence_kinds=["BrowserJourney", "ApiProbe"],
            ),
        ],
    )
    walkthrough = [
        WalkthroughEntry(
            t="00:42",
            feature_ids=["auth"],
            action_kind="api_request",
            narrative="POST /register",
            extras={"path": "/register", "response_status": 201},
        ),
    ]
    verdicts = [
        {
            "feature_id": "auth",
            "verdict": "passed",
            "detail": "auth flows verified",
            "evidence_refs": ["walkthrough.jsonl#L1"],
            "evidence_completeness": "full",
            "coverage_confidence": "high",
        },
    ]
    blocks = build_feature_proof_blocks(
        spec, walkthrough, verdicts,
        files_per_group={"auth-group": ["routes/auth.py"]},
    )
    dicts = feature_proof_blocks_to_dicts(blocks)
    assert len(dicts) == 1
    d = dicts[0]
    assert d["feature_id"] == "auth"
    assert d["verdict"] == "passed"
    assert d["walkthrough_entries"][0]["narrative"] == "POST /register"
    assert d["walkthrough_entries"][0]["response_status"] == 201
    assert d["files_changed"] == ["routes/auth.py"]


# ---------------------------------------------------------------------------
# A3: render.py ProofPacket integration (research §7)
# ---------------------------------------------------------------------------


def test_proof_packet_has_features_field() -> None:
    """ProofPacket.features must exist and default to empty list."""
    from otto.render import ProofPacket

    p = ProofPacket(
        schema_version=1,
        intent="test",
        project_kind="webapp",
        verdict="passed",
        wall_s=10.0,
        cost_usd=1.0,
        structure={},
        non_goals=[],
        done_means=[],
        groups=[],
        audit_narrative="",
        walkthrough_artifacts=[],
        blocked_group_ids=[],
        landed_group_ids=[],
    )
    assert p.features == []


def test_proof_packet_with_features_serialises() -> None:
    """ProofPacket with features[] populated must emit them in JSON."""
    import json
    from otto.render import ProofPacket, render_json

    p = ProofPacket(
        schema_version=1,
        intent="test",
        project_kind="webapp",
        verdict="passed",
        wall_s=10.0,
        cost_usd=1.0,
        structure={},
        non_goals=[],
        done_means=[],
        groups=[],
        audit_narrative="",
        walkthrough_artifacts=[],
        blocked_group_ids=[],
        landed_group_ids=[],
        features=[
            {
                "feature_id": "auth",
                "name": "Auth",
                "verdict": "passed",
                "walkthrough_entries": [],
            },
        ],
    )
    data = json.loads(render_json(p))
    assert "features" in data
    assert len(data["features"]) == 1
    assert data["features"][0]["feature_id"] == "auth"


def test_proof_packet_legacy_emission_includes_empty_features() -> None:
    """Legacy proof packets (no features kwarg) get empty features[] in JSON."""
    import json
    from otto.render import ProofPacket, render_json

    p = ProofPacket(
        schema_version=1,
        intent="legacy",
        project_kind="webapp",
        verdict="passed",
        wall_s=1.0,
        cost_usd=0.0,
        structure={},
        non_goals=[],
        done_means=[],
        groups=[],
        audit_narrative="",
        walkthrough_artifacts=[],
        blocked_group_ids=[],
        landed_group_ids=[],
    )
    data = json.loads(render_json(p))
    assert data.get("features") == []


def test_proof_packet_features_round_trip_with_block_serializer() -> None:
    """End-to-end: build_feature_proof_blocks → to_dicts → ProofPacket → JSON."""
    import json
    from otto.spec_compile import (
        WalkthroughEntry, build_feature_proof_blocks,
        feature_proof_blocks_to_dicts,
    )
    from otto.render import ProofPacket, render_json

    spec = Spec(
        intent="webapp",
        groups=[Group(id="auth-group", name="Auth")],
        features=[
            Feature(id="auth", name="Auth", group_id="auth-group"),
        ],
    )
    walkthrough = [
        WalkthroughEntry(
            t="00:42",
            feature_ids=["auth"],
            action_kind="api_request",
            narrative="POST /register",
        ),
    ]
    verdicts = [{"feature_id": "auth", "verdict": "passed", "detail": "ok"}]
    blocks = build_feature_proof_blocks(spec, walkthrough, verdicts)
    feature_dicts = feature_proof_blocks_to_dicts(blocks)

    p = ProofPacket(
        schema_version=1,
        intent="webapp",
        project_kind="webapp",
        verdict="passed",
        wall_s=10.0,
        cost_usd=1.0,
        structure={},
        non_goals=[],
        done_means=[],
        groups=[],
        audit_narrative="",
        walkthrough_artifacts=[],
        blocked_group_ids=[],
        landed_group_ids=[],
        features=feature_dicts,
    )
    data = json.loads(render_json(p))
    assert len(data["features"]) == 1
    f = data["features"][0]
    assert f["feature_id"] == "auth"
    assert f["verdict"] == "passed"
    assert f["walkthrough_entries"][0]["narrative"] == "POST /register"


# ---------------------------------------------------------------------------
# A3: per-Feature HTML rendering (research §7 + §2.7 per-kind branches)
# ---------------------------------------------------------------------------


def test_feature_proof_block_to_html_basic_webapp() -> None:
    from otto.spec_compile import (
        FeatureProofBlock, WalkthroughEntry, feature_proof_block_to_html,
    )

    block = FeatureProofBlock(
        feature_id="auth",
        name="Auth (register/login)",
        description="user can register and log in",
        group_id="auth-group",
        verdict="passed",
        detail="all auth flows pass",
        walkthrough_entries=[
            WalkthroughEntry(
                t="00:42",
                feature_ids=["auth"],
                action_kind="browser_navigation",
                narrative="GET /register",
                extras={
                    "url": "/register",
                    "method": "GET",
                    "screenshot": "assets/audit-001.png",
                    "dom_snapshot": "assets/audit-001.html",
                },
            ),
        ],
    )
    html = feature_proof_block_to_html(block, project_kind="webapp")
    # Structural assertions
    assert 'id="feature-auth"' in html
    assert "Auth (register/login)" in html
    assert 'verdict ok' in html  # passed verdict gets ok class
    assert 'GET /register' in html
    assert 'screenshot' in html
    assert 'audit-001.png' in html


def test_feature_proof_block_to_html_api_kind() -> None:
    from otto.spec_compile import (
        FeatureProofBlock, WalkthroughEntry, feature_proof_block_to_html,
    )

    block = FeatureProofBlock(
        feature_id="create-task",
        name="Create task",
        verdict="passed",
        walkthrough_entries=[
            WalkthroughEntry(
                t="00:01",
                feature_ids=["create-task"],
                action_kind="api_request",
                narrative="POST /tasks created task",
                extras={
                    "method": "POST",
                    "path": "/tasks",
                    "response_status": 201,
                },
            ),
        ],
    )
    html = feature_proof_block_to_html(block, project_kind="api")
    # API variant uses request/response table
    assert "<table class='walkthrough api'>" in html
    assert "POST" in html
    assert "/tasks" in html
    assert "201" in html


def test_feature_proof_block_to_html_cli_kind() -> None:
    from otto.spec_compile import (
        FeatureProofBlock, WalkthroughEntry, feature_proof_block_to_html,
    )

    block = FeatureProofBlock(
        feature_id="help",
        name="Help flag",
        verdict="passed",
        walkthrough_entries=[
            WalkthroughEntry(
                t="00:01",
                feature_ids=["help"],
                action_kind="cli_invoke",
                narrative="./tool --help",
                extras={
                    "command": ["./tool", "--help"],
                    "exit_code": 0,
                    "stdout": "Usage: tool [OPTIONS]",
                },
            ),
        ],
    )
    html = feature_proof_block_to_html(block, project_kind="cli")
    # CLI variant uses terminal-style transcript
    assert "<div class='walkthrough cli'>" in html
    assert "$ ./tool --help" in html
    assert "exit=0" in html
    assert "Usage: tool [OPTIONS]" in html


def test_feature_proof_block_to_html_library_kind() -> None:
    from otto.spec_compile import (
        FeatureProofBlock, WalkthroughEntry, feature_proof_block_to_html,
    )

    block = FeatureProofBlock(
        feature_id="importable",
        name="Package importable",
        verdict="passed",
        walkthrough_entries=[
            WalkthroughEntry(
                t="00:01",
                feature_ids=["importable"],
                action_kind="import_check",
                narrative="package imports cleanly",
                extras={
                    "package": "retryable",
                    "version": "0.1.0",
                    "import_succeeded": True,
                },
            ),
        ],
    )
    html = feature_proof_block_to_html(block, project_kind="library")
    # Library variant uses import-status table
    assert "<table class='walkthrough library'>" in html
    assert "retryable" in html
    assert "ok" in html  # import_succeeded → ok status


def test_feature_proof_block_to_html_unknown_kind_falls_back_to_webapp() -> None:
    from otto.spec_compile import (
        FeatureProofBlock, WalkthroughEntry, feature_proof_block_to_html,
    )

    block = FeatureProofBlock(
        feature_id="x",
        name="X",
        walkthrough_entries=[
            WalkthroughEntry(t="0", feature_ids=["x"], action_kind="browser_navigation"),
        ],
    )
    html = feature_proof_block_to_html(block, project_kind="unknown-kind")
    assert "<div class='walkthrough webapp'>" in html  # fallback


def test_feature_proof_block_to_html_empty_walkthrough() -> None:
    from otto.spec_compile import FeatureProofBlock, feature_proof_block_to_html

    block = FeatureProofBlock(feature_id="missing", name="Missing", verdict="missing")
    html = feature_proof_block_to_html(block, project_kind="webapp")
    assert "No walkthrough entries tagged" in html


def test_feature_proof_block_to_html_renders_findings() -> None:
    from otto.spec_compile import (
        FeatureProofBlock, Finding, feature_proof_block_to_html,
    )

    block = FeatureProofBlock(
        feature_id="home",
        name="Home page",
        verdict="partial",
        findings=[
            Finding(severity="critical", text="page load >8s", feature_id="home"),
            Finding(severity="polish", text="generic palette", feature_id="home"),
        ],
    )
    html = feature_proof_block_to_html(block, project_kind="webapp")
    assert "Quality findings" in html
    assert "[critical]" in html
    assert "page load &gt;8s" in html  # > escaped
    assert "[polish]" in html
    assert "verdict warn" in html  # partial verdict badge


def test_feature_proof_block_to_html_renders_repair_history() -> None:
    from otto.spec_compile import FeatureProofBlock, feature_proof_block_to_html

    block = FeatureProofBlock(
        feature_id="auth",
        name="Auth",
        verdict="passed",
        repair_history=[
            {"attempt": 1, "succeeded": False},
            {"attempt": 2, "succeeded": True},
        ],
    )
    html = feature_proof_block_to_html(block)
    assert "Repair history" in html
    assert "Attempt 1" in html
    assert "failed" in html
    assert "Attempt 2" in html
    assert "succeeded" in html


def test_feature_proof_block_to_html_cross_link_section() -> None:
    from otto.spec_compile import FeatureProofBlock, feature_proof_block_to_html

    block = FeatureProofBlock(
        feature_id="upload",
        name="Image upload",
        verdict="passed",
        shared_with=["comment", "edit"],
    )
    html = feature_proof_block_to_html(block)
    assert "Cross-linked features" in html
    assert 'href="#feature-comment"' in html
    assert 'href="#feature-edit"' in html


def test_feature_proof_block_to_html_html_escape_safe() -> None:
    """User-provided narrative must be escaped to prevent XSS / breakage."""
    from otto.spec_compile import (
        FeatureProofBlock, WalkthroughEntry, feature_proof_block_to_html,
    )

    block = FeatureProofBlock(
        feature_id="x",
        name="X<script>alert(1)</script>",
        verdict="passed",
        walkthrough_entries=[
            WalkthroughEntry(
                t="0",
                feature_ids=["x"],
                action_kind="browser_navigation",
                narrative="<b>not bold</b>",
            ),
        ],
    )
    html = feature_proof_block_to_html(block)
    assert "<script>alert(1)</script>" not in html  # raw script not present
    assert "&lt;script&gt;" in html  # but escaped form is
    assert "&lt;b&gt;not bold&lt;/b&gt;" in html


# ---------------------------------------------------------------------------
# A5: render_spec_md (research §2.1 — human-readable Spec output)
# ---------------------------------------------------------------------------


def test_render_spec_md_minimal_spec() -> None:
    from otto.spec_compile import render_spec_md

    s = Spec(intent="A doc editor", project_kind="webapp")
    md = render_spec_md(s)
    assert "# A doc editor" in md
    assert "## Project kind" in md
    assert "webapp" in md


def test_render_spec_md_multi_line_intent() -> None:
    """First line becomes H1; remaining lines render as body."""
    from otto.spec_compile import render_spec_md

    s = Spec(
        intent="A doc editor\n\nFor engineering teams. Markdown rendering and inline comments.",
        project_kind="webapp",
    )
    md = render_spec_md(s)
    assert "# A doc editor" in md
    assert "For engineering teams" in md
    assert "Markdown rendering and inline comments" in md


def test_render_spec_md_features_grouped() -> None:
    from otto.spec_compile import render_spec_md

    s = Spec(
        intent="A webapp",
        project_kind="webapp",
        groups=[
            Group(id="editor", name="Editor surface"),
            Group(id="comments", name="Comments"),
        ],
        features=[
            Feature(
                id="md-render",
                name="Markdown rendering",
                description="Pages render .md files",
                evidence_kinds=["BrowserJourney", "RepoTestCheck"],
                group_id="editor",
            ),
            Feature(
                id="line-comment",
                name="Line-anchored comments",
                description="Click any line to comment",
                evidence_kinds=["BrowserJourney"],
                group_id="comments",
            ),
        ],
    )
    md = render_spec_md(s)
    assert "## Features" in md
    assert "### Editor surface" in md
    assert "<!-- group: editor -->" in md
    assert "#### Markdown rendering" in md
    assert "<!-- feature: md-render | evidence: BrowserJourney, RepoTestCheck -->" in md
    assert "Pages render .md files" in md
    assert "### Comments" in md
    assert "<!-- group: comments -->" in md
    assert "#### Line-anchored comments" in md


def test_render_spec_md_group_feature_ids_when_features_empty() -> None:
    from otto.spec_compile import render_spec_md

    s = Spec(
        intent="A webapp",
        project_kind="webapp",
        groups=[
            Group(
                id="sla-aging-data",
                name="SLA aging data",
                feature_ids=[
                    "derive pending expenses submitted more than 7 days ago",
                    "derive pending expenses submitted more than 7 days ago",
                    "support dashboard links",
                ],
            )
        ],
        features=[],
    )

    md = render_spec_md(s)

    assert "### SLA aging data" in md
    assert "#### derive pending expenses submitted more than 7 days ago" in md
    assert (
        "<!-- feature: derive-pending-expenses-submitted-more-than-7-days-ago -->"
        in md
    )
    assert (
        "<!-- feature: derive-pending-expenses-submitted-more-than-7-days-ago-2 -->"
        in md
    )
    assert "<!-- feature: support-dashboard-links -->" in md


def test_render_spec_md_acceptance_detail_emitted() -> None:
    from otto.spec_compile import render_spec_md

    s = Spec(
        intent="webapp",
        groups=[Group(id="g", name="G")],
        features=[
            Feature(
                id="f",
                name="F",
                acceptance_detail="Click X; observe Y.",
                group_id="g",
            ),
        ],
    )
    md = render_spec_md(s)
    assert "**Acceptance:** Click X; observe Y." in md


def test_render_spec_md_omits_empty_optional_fields() -> None:
    from otto.spec_compile import render_spec_md

    s = Spec(
        intent="webapp",
        groups=[Group(id="g", name="G")],
        features=[Feature(id="f", name="F", group_id="g")],  # no description, no acceptance
    )
    md = render_spec_md(s)
    assert "**Acceptance:**" not in md
    # No empty description paragraph
    assert "#### F\n<!-- feature: f -->\n\n\n" not in md


def test_render_spec_md_evidence_kinds_optional() -> None:
    from otto.spec_compile import render_spec_md

    s = Spec(
        intent="webapp",
        groups=[Group(id="g", name="G")],
        features=[
            Feature(id="f1", name="F1", group_id="g"),  # no evidence_kinds
            Feature(id="f2", name="F2", group_id="g", evidence_kinds=["RepoTestCheck"]),
        ],
    )
    md = render_spec_md(s)
    assert "<!-- feature: f1 -->" in md
    assert "<!-- feature: f2 | evidence: RepoTestCheck -->" in md


def test_render_spec_md_guardrails() -> None:
    from otto.spec_compile import render_spec_md

    s = Spec(
        intent="webapp",
        guardrails=[
            Guardrail(id="no-video", text="No video upload"),
            Guardrail(id="no-cdn", text="No external CDN", applies_to="static-assets"),
        ],
    )
    md = render_spec_md(s)
    assert "## Guardrails" in md
    assert "- ⊘ No video upload" in md
    assert "- ⊘ No external CDN" in md
    assert "_(applies to: static-assets)_" in md


def test_render_spec_md_orphan_features_render_under_ungrouped() -> None:
    from otto.spec_compile import render_spec_md

    s = Spec(
        intent="webapp",
        groups=[Group(id="g", name="G")],
        features=[
            Feature(id="grouped", name="Grouped", group_id="g"),
            Feature(id="orphan", name="Orphan feature", group_id=""),
        ],
    )
    md = render_spec_md(s)
    assert "### Ungrouped" in md
    assert "#### Orphan feature" in md
    assert "<!-- feature: orphan -->" in md


def test_render_spec_md_empty_spec() -> None:
    """Even an empty Spec must produce well-formed Markdown without crashing."""
    from otto.spec_compile import render_spec_md

    s = Spec()
    md = render_spec_md(s)
    assert "# Untitled" in md
    assert "## Project kind" in md


def test_render_spec_md_multiple_groups_preserves_spec_order() -> None:
    from otto.spec_compile import render_spec_md

    s = Spec(
        intent="x",
        groups=[
            Group(id="z", name="Z first"),
            Group(id="a", name="A second"),
        ],
        features=[
            Feature(id="fa", name="FA", group_id="a"),
            Feature(id="fz", name="FZ", group_id="z"),
        ],
    )
    md = render_spec_md(s)
    z_pos = md.index("Z first")
    a_pos = md.index("A second")
    assert z_pos < a_pos  # spec.groups order preserved, not alphabetical


def test_render_spec_md_surfaces_planned_checks() -> None:
    from otto.spec_compile import BrowserJourney, RepoTestCheck, render_spec_md

    s = Spec(
        intent="x",
        groups=[
            Group(
                id="g",
                name="G",
                checks=[RepoTestCheck(command=("npm", "test"), timeout_s=120)],
            )
        ],
        cross_group_checks=[
            BrowserJourney(
                command=("python3", "tests/run_browser_journey.py"),
                evidence_globs=("otto_artifacts/browser/*.png",),
                timeout_s=600,
            )
        ],
    )
    md = render_spec_md(s)
    assert "## Planned checks" in md
    assert "<!-- planned-checks-group: g -->" in md
    assert '"kind": "repo_test"' in md
    assert "<!-- planned-checks: cross_group_checks -->" in md
    assert '"kind": "browser_journey"' in md


# ---------------------------------------------------------------------------
# A5: parse_spec_md + round-trip property test (research §2.1)
# ---------------------------------------------------------------------------


def test_parse_spec_md_minimal() -> None:
    from otto.spec_compile import parse_spec_md

    md = "# Doc editor\n\n## Project kind\n\nwebapp\n"
    spec, warnings = parse_spec_md(md)
    assert spec.intent.startswith("Doc editor")
    assert spec.project_kind == "webapp"
    assert spec.features == []
    assert spec.groups == []


def test_parse_spec_md_features_with_metadata_comments() -> None:
    from otto.spec_compile import parse_spec_md

    md = """# Doc editor

## Project kind

webapp

## Features

### Editor surface
<!-- group: editor-surface -->

#### Markdown rendering
<!-- feature: md-render | evidence: BrowserJourney, RepoTestCheck -->

Pages render .md as HTML.

**Acceptance:** Open fixture; verify rendering.

#### Save / load
<!-- feature: save-load | evidence: ApiProbe -->

Persist drafts.
"""
    spec, _ = parse_spec_md(md)
    assert len(spec.groups) == 1
    assert spec.groups[0].id == "editor-surface"
    assert spec.groups[0].name == "Editor surface"
    assert len(spec.features) == 2
    f1 = spec.features[0]
    assert f1.id == "md-render"
    assert f1.name == "Markdown rendering"
    assert f1.evidence_kinds == ["BrowserJourney", "RepoTestCheck"]
    assert f1.group_id == "editor-surface"
    assert "Pages render .md as HTML" in f1.description
    assert f1.acceptance_detail == "Open fixture; verify rendering."
    f2 = spec.features[1]
    assert f2.id == "save-load"
    assert f2.evidence_kinds == ["ApiProbe"]
    assert spec.groups[0].feature_ids == ["md-render", "save-load"]


def test_parse_spec_md_guardrails() -> None:
    from otto.spec_compile import parse_spec_md

    md = """# x

## Guardrails

- ⊘ No video upload
- ⊘ No external CDN _(applies to: static-assets)_
"""
    spec, _ = parse_spec_md(md)
    assert len(spec.guardrails) == 2
    assert spec.guardrails[0].text == "No video upload"
    assert spec.guardrails[0].applies_to == "*"
    assert spec.guardrails[1].text == "No external CDN"
    assert spec.guardrails[1].applies_to == "static-assets"


def test_round_trip_render_parse_minimal() -> None:
    """parse_spec_md(render_spec_md(s)) recovers the surface fields."""
    from otto.spec_compile import parse_spec_md, render_spec_md

    original = Spec(
        intent="A doc editor",
        project_kind="webapp",
    )
    md = render_spec_md(original)
    parsed, _ = parse_spec_md(md, base=original)
    assert parsed.intent.startswith("A doc editor")
    assert parsed.project_kind == "webapp"


def test_round_trip_render_parse_full() -> None:
    """Full round-trip: spec → md → spec preserves features/groups/guardrails."""
    from otto.spec_compile import parse_spec_md, render_spec_md

    original = Spec(
        intent="Doc editor for engineering teams",
        project_kind="webapp",
        groups=[
            Group(id="editor-surface", name="Editor surface"),
            Group(id="comments", name="Comments"),
        ],
        features=[
            Feature(
                id="md-render",
                name="Markdown rendering",
                description="Pages render .md as HTML.",
                acceptance_detail="Open fixture; verify rendering.",
                evidence_kinds=["BrowserJourney", "RepoTestCheck"],
                group_id="editor-surface",
            ),
            Feature(
                id="line-comment",
                name="Line-anchored comments",
                description="Click any line to add a comment.",
                evidence_kinds=["BrowserJourney"],
                group_id="comments",
            ),
        ],
        guardrails=[
            Guardrail(id="no-video", text="No video upload"),
            Guardrail(id="no-cdn", text="No external CDN", applies_to="static-assets"),
        ],
    )
    md = render_spec_md(original)
    parsed, _ = parse_spec_md(md, base=original)

    # Surface fields preserved
    assert parsed.intent.startswith("Doc editor for engineering teams")
    assert parsed.project_kind == "webapp"
    assert len(parsed.groups) == 2
    assert parsed.groups[0].id == "editor-surface"
    assert parsed.groups[1].id == "comments"
    assert len(parsed.features) == 2
    assert parsed.features[0].id == "md-render"
    assert parsed.features[0].name == "Markdown rendering"
    assert parsed.features[0].evidence_kinds == ["BrowserJourney", "RepoTestCheck"]
    assert parsed.features[0].group_id == "editor-surface"
    assert "Pages render .md as HTML" in parsed.features[0].description
    assert parsed.features[0].acceptance_detail == "Open fixture; verify rendering."
    assert len(parsed.guardrails) == 2
    assert parsed.guardrails[0].text == "No video upload"
    assert parsed.guardrails[1].applies_to == "static-assets"


def test_round_trip_preserves_id_stability_after_rename() -> None:
    """Editing a feature's name in markdown keeps its id stable when base
    is provided (research §2.1: ids never change on rename)."""
    from otto.spec_compile import parse_spec_md, render_spec_md

    original = Spec(
        intent="x",
        groups=[Group(id="g1", name="G1")],
        features=[Feature(id="auth", name="Auth (register/login)", group_id="g1")],
    )
    md = render_spec_md(original)
    # User renames the feature
    edited_md = md.replace("Auth (register/login)", "User accounts")
    parsed, _ = parse_spec_md(edited_md, base=original)
    # ID preserved (read from <!-- feature: auth --> comment)
    assert parsed.features[0].id == "auth"
    # Name updated to user's edit
    assert parsed.features[0].name == "User accounts"


def test_round_trip_preserves_mechanical_fields_via_base() -> None:
    """Group.owned_paths and similar non-markdown fields preserve via base."""
    from otto.spec_compile import parse_spec_md, render_spec_md

    original = Spec(
        intent="x",
        groups=[
            Group(
                id="g1",
                name="G1",
                owned_paths=["routes/g1.py", "templates/g1.html"],
                dependencies=["foundation"],
            ),
        ],
    )
    md = render_spec_md(original)
    parsed, _ = parse_spec_md(md, base=original)
    # Mechanical fields survive
    assert parsed.groups[0].owned_paths == ["routes/g1.py", "templates/g1.html"]
    assert parsed.groups[0].dependencies == ["foundation"]


def test_parse_spec_md_updates_planned_checks_from_markdown() -> None:
    from otto.spec_compile import BrowserJourney, RepoTestCheck, parse_spec_md, render_spec_md

    original = Spec(
        intent="x",
        groups=[
            Group(
                id="g",
                name="G",
                checks=[RepoTestCheck(command=("npm", "test"), timeout_s=120)],
            )
        ],
        cross_group_checks=[
            BrowserJourney(
                command=("python3", "tests/old_browser.py"),
                evidence_globs=("old/*.png",),
                timeout_s=600,
            )
        ],
    )
    md = render_spec_md(original)
    edited = md.replace('"timeout_s": 120', '"timeout_s": 90').replace(
        '"tests/old_browser.py"', '"tests/new_browser.py"'
    )

    parsed, warnings = parse_spec_md(edited, base=original)

    assert warnings == []
    assert parsed.groups[0].checks == [
        RepoTestCheck(command=("npm", "test"), timeout_s=90)
    ]
    assert parsed.cross_group_checks == [
        BrowserJourney(
            command=("python3", "tests/new_browser.py"),
            evidence_globs=("old/*.png",),
            timeout_s=600,
        )
    ]


def test_parse_spec_md_orphan_features_under_ungrouped() -> None:
    from otto.spec_compile import parse_spec_md, render_spec_md

    original = Spec(
        intent="x",
        groups=[Group(id="g", name="G")],
        features=[
            Feature(id="grouped", name="Grouped", group_id="g"),
            Feature(id="orphan", name="Orphan", group_id=""),
        ],
    )
    md = render_spec_md(original)
    parsed, _ = parse_spec_md(md, base=original)
    # Orphan feature recovered
    orphan = next((f for f in parsed.features if f.id == "orphan"), None)
    assert orphan is not None
    assert orphan.group_id == ""


def test_parse_spec_md_tolerates_missing_metadata_comment() -> None:
    """Feature without an explicit <!-- feature: id --> comment is dropped
    with no crash (warnings list reflects this in stricter parsers)."""
    from otto.spec_compile import parse_spec_md

    md = """# x

## Features

### G
<!-- group: g -->

#### A feature with no id comment

This feature has no metadata comment.
"""
    spec, _ = parse_spec_md(md)
    # Group is recovered; orphan-feature without id is silently dropped
    assert len(spec.groups) == 1
    assert spec.groups[0].id == "g"
    assert spec.features == []


def test_round_trip_idempotent() -> None:
    """spec_to_dict → parse_spec → spec_to_dict produces same dict."""
    from otto.spec_compile import parse_spec, spec_to_dict

    original = Spec(
        intent="full spec",
        groups=[Group(id="g", name="G")],
        features=[Feature(id="f1", name="F1", group_id="g")],
        components=[Component(id="c1", name="C1")],
        guardrails=[Guardrail(id="r1", text="r1")],
        shared_paths=["x.py"],
        audit_fixtures=[AuditFixture(kind="user", payload={"u": "a"})],
    )
    s1 = spec_to_dict(original)
    parsed, _ = parse_spec(s1)
    s2 = spec_to_dict(parsed)
    # All A1a-related keys round-trip identically
    for k in ("features", "components", "guardrails", "shared_paths", "audit_fixtures"):
        assert s1[k] == s2[k], f"key {k} did not round-trip"
