# Hunter: Web UI

Findings received from read-only subagent Pauli.

- High: stale run detail actions could target the wrong selected run. Fixed by clearing detail on selection, ignoring stale async detail responses, and binding actions to detail.run_id.
- Medium: missing state looked healthy. Fixed workflowHealth unknown/loading state for missing data.
- Medium: outcome filter omitted other. Fixed.
- Medium: action result contract not surfaced. Fixed persistent result banner for modal fields and severity mapping.
- Medium: selectable rows mouse-only. Fixed keyboard role/tabIndex/Enter/Space handling.
- Low: stale artifact/log content. Fixed explicit log reset bypass and clear artifact content before fetch.

