# Hunter: Dead/Dedup/Slop

Findings received from read-only subagent Raman.

- Medium: diff failures were shown as 0 changed files. Fixed by surfacing diff_error in landing and review packet.
- Medium: event write failures were silently lost. Fixed by logging warning from _record_event.
- Medium: retry/requeue action mapping was inconsistent. Fixed event naming using legal action label/domain.
- Medium: backend action severity was downgraded in UI. Fixed action toast/banner severity mapping.
- Low: event polling read whole append-only log. Fixed bounded tail reader and truncated flag.
- Low: supervisor payload unused/overclaimed. Fixed UI start/stop to use runtime.supervisor and renamed lock field.
- Low: outcome filter drift. Fixed backend filter tuple, TS type, dropdown.

