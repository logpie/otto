# Production Bug Hunter Notes

Finding fixed:

- `otto/mission_control/service.py` had two `_string_list` helpers. The product-handoff normalizer intended to coerce scalar strings into single-item lists, but a later strict request-validation helper with the same name replaced the global at call time. Explicit product handoff JSON such as `"urls": "http://..."`, scalar `"notes"`, or flow `"steps": "..."` raised `MissionControlServiceError("expected a list of strings")`.

Fix:

- Renamed the permissive handoff helper to `_coerce_string_list`.
- Left the strict request payload helper as `_string_list`.
- Added regression coverage in `tests/test_web_mission_control.py`.

Verification:

- `uv run pytest -q tests/test_test_tiers.py tests/test_web_mission_control.py::test_web_review_packet_includes_explicit_product_handoff --maxfail=1`
- `uv run python scripts/test_tiers.py web -- --maxfail=1`
- full default pytest.
