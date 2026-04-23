You're integrating multiple feature branches into `{target}`. The
orchestrator has done sequential `git merge --no-ff` of each branch and
committed the results — but some files contain unresolved `<<<<<<<` /
`=======` / `>>>>>>>` markers. Your job: produce a clean, working
integration where every branch's behaviors survive.

## Branches integrated

{branches_listing}

## Each branch's intent

{branch_intents_section}

## Stories that must survive across the integration

{stories_section}

## Files needing resolution

{conflict_files_listing}

## Additional files you MAY edit if needed for coherence

{secondary_files_listing}

## Full merge diff

```diff
{conflict_diff}
```

## Verifying your work

{test_command_section}

## Goal

When you stop, the orchestrator validates:
1. No conflict markers remain anywhere
2. You may edit the conflict files above freely
3. You may also edit the additional files above only when needed to keep imports, helpers, or tests coherent with the merged behavior
4. Any edit outside both lists fails validation
5. HEAD has not changed (you should NOT commit; orchestrator commits)

Both the structural validation AND the project's tests should pass.

## Notes

- You have full tool access including Bash. Use the project's test command
  (above) to verify the integration actually works.
- Prefer resolving the marker-bearing files first. Touch secondary files only
  when the merge would otherwise leave adjacent code or tests incoherent.
- For each conflict region in a file: text above `=======` is from the
  earlier-merged side, text below is from the later-merged side. Decide
  per region — preserve both where compatible, prefer the LATER side on
  direct contradictions, ALWAYS preserve any behavior listed in the
  stories above.
- Don't commit, don't reset, don't run any git command that changes HEAD.
  The orchestrator handles all git state changes.
- Take the most efficient path. You decide when you're done.
