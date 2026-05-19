"""Regression: the framework-conventions pack exists, is version-pinned,
and is wired into EVERY build/foundation/feature agent prompt.

Root cause (2026-05-18, user-directed protocol fix): otto's scaffold is
100% agent-guessed prose with ZERO version pins anywhere (`spec-light.md`
explicitly: "the build agent picks that later"). Each run reinvents
`package.json`/`vite.config.ts`/`tsconfig`/the zustand store and emits
non-strict-tsc-clean code (Linkboard fgate2-163718: untyped zustand v5
store TS2345, `vite.config.ts` TS2580 missing `@types/node`). The
foundation-gate probe only *detects* this late and pays a ~20min repair +
architect-reentry cascade → merge_blocked. The protocol-level fix is a
pinned, copy-paste framework-conventions reference the build agent consults
instead of guessing.

These tests lock the contract (not the registry):
  (a) the pack renders non-empty and carries the pinned anchor versions;
  (b) it pins (not floats) versions — no ``"latest"``/``"*"``/``^latest``;
  (c) it directly addresses the two Linkboard failure signatures
      (curried ``create<AppStore>()`` zustand store; ``tsconfig.node.json``
      + ``@types/node`` for the ``vite.config.ts`` TS2580);
  (d) it is appended by ``_build_agent_prompt`` AFTER
      ``build-agent-static-policy.md`` so every builder receives it.
"""

from __future__ import annotations

from pathlib import Path

from otto.prompts import render_prompt

PACK = "build-agent-framework-conventions.md"
_REPO = Path(__file__).resolve().parents[1]


def _pack() -> str:
    return render_prompt(PACK)


def test_pack_renders_non_empty() -> None:
    body = _pack()
    assert body.strip(), "framework-conventions pack must render non-empty"
    assert len(body) > 2000, "pack unexpectedly small — assembly regressed"


def test_pack_carries_pinned_anchor_versions() -> None:
    """The reconciled anchor matrix must be present verbatim — these are
    the single source of truth other sections defer to."""
    body = _pack()
    for token in (
        '"react": "^18.3.1"',
        '"vite": "^6.0.5"',
        '"typescript": "~5.7.2"',
        '"@types/node": "^22.10.2"',
        '"zustand": "^5.0.13"',
        '"@tanstack/react-query": "^5.100.11"',
        '"react-router-dom": "^6.30.3"',
        '"tailwindcss": "3.4.19"',
        "fastapi>=0.115,<0.116",
        "SQLAlchemy>=2.0,<2.1",
        "pydantic>=2.13,<3",
        "alembic>=1.13,<1.14",
        "pytest>=8.4,<9",
        'requires-python = ">=3.12"',
    ):
        assert token in body, f"anchor pin missing from pack: {token}"


def test_pack_pins_not_floats() -> None:
    """Drift-proof: never emit unreproducible ranges in the pinned
    manifests (mirrors + supersedes the weak static-policy rule)."""
    body = _pack()
    # Target JSON manifest *values* (`"pkg": "<float>"`), not the preamble
    # prose that legitimately quotes `"latest"`/`"*"` to PROHIBIT them.
    for forbidden in (': "latest"', ': "^latest"', ': "*"', ': "^*"'):
        assert forbidden not in body, f"pack must not float versions: {forbidden}"


def test_pack_fixes_linkboard_failure_signatures() -> None:
    """The two concrete strict-tsc failures that caused the merge_blocked
    must be explicitly prevented by the templates."""
    body = _pack()
    # zustand TS2345 'never' fix = mandatory curried generic create
    assert "create<AppStore>()(" in body
    assert "MANDATORY" in body
    # vite.config.ts TS2580 'process' fix = node-scoped tsconfig + @types/node
    assert "tsconfig.node.json" in body
    assert '"types": ["node"]' in body
    # single-Base fix-11 invariant reinforced, not weakened
    assert "DeclarativeBase" in body
    assert "never declarative_base()" in body.lower() or "NEVER `declarative_base()`" in body
    # port hermeticity (fix-12 class)
    assert "${PORT:-8000}" in body
    assert "pkill" in body  # the "no pkill/killall" invariant


def test_pack_is_wired_into_build_agent_prompt() -> None:
    """It must be appended by _build_agent_prompt AFTER the static-policy
    snippet so every build/foundation/feature child receives it."""
    src = (_REPO / "otto" / "build.py").read_text()
    static = '_append_prompt_snippet(lines, "build-agent-static-policy.md")'
    pack = '_append_prompt_snippet(lines, "build-agent-framework-conventions.md")'
    assert static in src, "static-policy wiring vanished — anchor for ordering"
    assert pack in src, "framework-conventions pack is not wired into build.py"
    assert src.index(static) < src.index(pack), (
        "framework-conventions must be appended AFTER static-policy"
    )


def test_pack_pointer_in_lead_prompt() -> None:
    """lead.md must point the architect at the pinned stack so
    decomposition stays consistent with the conventions."""
    lead = render_prompt("lead.md")
    assert "pinned" in lead and "framework conventions" in lead, (
        "lead.md missing the pinned-stack pointer"
    )


def test_pack_has_enforced_pre_return_self_check() -> None:
    """ENFORCEMENT (not merely doc-appended): the e2e (linkboard-fwconv-174858)
    proved a wired pack is ignored as prose. The preamble must carry an
    explicit pre-return SCAFFOLD CONFORMANCE SELF-CHECK that names the exact
    deviations that failed clean-boot, so the agent (which optimizes for
    checks, not buried prose) must self-verify before returning."""
    body = _pack()
    assert "SCAFFOLD CONFORMANCE SELF-CHECK" in body
    assert "BEFORE you confirm completion" in body
    low = body.lower()
    # the precise fwconv-174858 violations must be called out as fail bullets
    assert "bespoke `backend_port`" in low or "not a bespoke" in low
    assert "--strictport" in low
    assert "never bare" in low and "python: command not found" in low
    assert "tsc -b && vite build" in body and "tsc --noEmit && vite build" in body
    assert 'vite `^6`' in body.lower() or "vite `^6`" in body.lower()
    assert 'do not return' in low


def test_final_instruction_points_at_scaffold_self_check() -> None:
    """build-final-instruction.md is the LAST snippet the agent sees
    (appended after everything). It must gate completion on the scaffold
    self-check so the contract is reinforced at maximum salience."""
    fin = render_prompt("build-final-instruction.md")
    assert "SCAFFOLD CONFORMANCE\nSELF-CHECK" in fin or "SCAFFOLD CONFORMANCE SELF-CHECK" in fin.replace(
        "\n", " "
    )
    assert "Before you confirm completion" in fin
    assert "$PORT" in fin and "--strictPort" in fin
    src = (_REPO / "otto" / "build.py").read_text()
    assert '_append_prompt_snippet(lines, "build-final-instruction.md")' in src, (
        "build-final-instruction.md must remain the final appended snippet"
    )
