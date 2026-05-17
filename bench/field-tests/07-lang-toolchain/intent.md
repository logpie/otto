# Mini Language Toolchain

Build a small but real toolchain for a tiny expression language ("Calc"):
integer/float literals, identifiers, `+ - * /`, parentheses, `let name =
expr;` bindings, and a final expression. Example program:

    let x = 3; let y = x * (2 + 4); y / 3

This is a FORCED-RECURSION field-test scenario. The toolchain is a CLI plus
a shared core, plus a **language-analysis engine** that is irreducibly
layered: lexer, parser, semantic analyzer, and diagnostics each own a
distinct stage with its own data structures and its own tests. One inline
leaf cannot coherently own the whole engine; the child that owns the
analysis engine MUST itself decompose into one nested leaf per stage.
Recursive sub-decomposition is required.

## Top-level shape (must be honored)

- A shared-contracts/scaffold child: token type enum, AST node schema,
  diagnostic record shape, error-code conventions, `CHARTER.md`.
- A language-analysis-engine child that is itself multi-stage and MUST emit
  a nested subtree — one leaf per stage below.
- A CLI child: `calc run <file>`, `calc tokens <file>`, `calc ast <file>`,
  `calc check <file>` (diagnostics only). Wires the engine stages together.

## Analysis engine — four nested stages (each its own nested leaf)

1. **Lexer**: source text -> token stream. Tracks line/column. Emits a
   lexical diagnostic for unknown characters; never throws.
2. **Parser**: token stream -> AST per the shared schema. Recursive-descent;
   emits a syntax diagnostic with location on malformed input; recovers
   enough to keep parsing where reasonable.
3. **Semantic analyzer**: AST -> resolved AST. Checks: use-before-`let`,
   duplicate `let`, division by literal zero. Emits semantic diagnostics.
4. **Evaluator + diagnostics aggregator**: walks the resolved AST to a
   numeric result; aggregates lexer/parser/semantic diagnostics into one
   ordered, deduplicated report keyed by (line, col, code).

Stages communicate only through the shared schemas from the scaffold child;
no stage imports another stage's internals.

## Required deliverables

- A CLI entry point runnable as `python -m calc ...` or `./calc`.
- `start.sh` at repo root: NOT a server. It runs the acceptance suite and
  prints a one-line PASS/FAIL summary (this scenario has no HTTP surface).
- `tests/run_acceptance.py` that, from a clean checkout, asserts: a valid
  program evaluates to the correct number; `calc tokens` and `calc ast`
  produce the documented shapes; and three broken programs (lexical,
  syntax, semantic) each produce the correct diagnostic code and location
  with a zero crash count.
- `CHARTER.md` documenting the shared schemas, the four-stage ownership, and
  the nested decomposition.

Keep each stage deliberately small but real. No external parser generators;
hand-written lexer/parser only.
