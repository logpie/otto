---
name: otto-as-user
description: "Simulate real users using Otto end to end through Mission Control Web first, using the canonical Otto as-user protocol from this repo."
---

# Otto As User

This project-level Claude skill intentionally delegates to the canonical Otto
as-user skill in this repository:

`/Users/yuxuan/work/cc-autonomous/.codex/skills/otto-as-user/SKILL.md`

Before running an Otto as-user test, read that file and follow it as the source
of truth. Do not maintain a separate copy of the protocol here; the canonical
repo skill owns the current rules for:

- Mission Control Web as the default true-user surface
- real browser interactions with no UI shortcuts
- expectation/observation/verdict checking after each user action
- realistic wait-time behavior such as logs, diffs, proof, refresh,
  back/forward, project round trips, keyboard paths, scrolling, and layout
  checks
- generated-product verification with real browser behavior
- evidence collection, artifacts, logs, concurrency checks, and final verdicts
- root-cause reporting when Otto fails

If the canonical file is missing, stop and report that the Otto as-user skill
cannot be applied reliably instead of inventing a replacement workflow.
