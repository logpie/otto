#!/usr/bin/env bash
set -euo pipefail
# Completely empty repo — no README, no package.json, nothing.
# Otto must figure out language, tooling, and structure from scratch.
git commit --allow-empty -m "init"
