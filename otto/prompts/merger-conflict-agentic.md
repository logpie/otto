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

## Full merge diff

```diff
{conflict_diff}
```

## Verifying your work

{test_command_section}

## Goal

When you stop, the orchestrator validates:
1. No conflict markers remain anywhere
2. No files outside the conflict list above were modified
3. HEAD has not changed (you should NOT commit; orchestrator commits)

Both the structural validation AND the project's tests should pass.

## Notes

- You have full tool access including Bash. Use the project's test command
  (above) to verify the integration actually works.
- For each conflict region in a file: text above `=======` is from the
  earlier-merged side, text below is from the later-merged side. Decide
  per region — preserve both where compatible, prefer the LATER side on
  direct contradictions, ALWAYS preserve any behavior listed in the
  stories above.
- Don't commit, don't reset, don't run any git command that changes HEAD.
  The orchestrator handles all git state changes.
- Take the most efficient path. You decide when you're done.
