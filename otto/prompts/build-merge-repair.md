## Merge repair mode

You are repairing this group so it integrates cleanly with the current target
branch. Treat this as an integration task, not a branch-winner choice.

- Understand both the group behavior and the already-landed target behavior before editing.
- Do not resolve by blindly choosing one side, the newer side, or the larger diff.
- Compose both sides where compatible so this group's accepted tasks and already-integrated product behavior both survive.
- If this worktree has conflict markers or unmerged paths, resolve them directly in the files. Otto will stage and commit the repair; do not run git mutation commands yourself.
- If the conflict is an incompatible product decision, make the smallest safe integration and call out the remaining decision in your final response instead of silently erasing behavior.
