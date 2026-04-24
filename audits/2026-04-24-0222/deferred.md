# Deferred Notes

1. Cleanup action copy needs a sharper explanation of what cleanup removes.
   - Current behavior is correct enough for this release pass, but the UI should
     distinguish queue bookkeeping cleanup from worktree cleanup.

2. Long-running provider work needs budget and time affordances.
   - The overview shows active work and elapsed/cost rows, but a production
     operator will eventually need budget caps, projected cost, timeout, and
     provider-specific warnings.

3. Claude visual certification quality remains a provider prompt/integrity
   follow-up.
   - The real E2E Claude path completed, but the due-date certification relied
     partly on source review for an overdue UI callout rather than a live browser
     rendering of an overdue card.
