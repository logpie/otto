# Running the Certifier on Ubuntu

## Prerequisites

```bash
# 1. Clone and checkout
git clone https://github.com/logpie/otto.git && cd otto
git checkout worktree-i2p

# 2. Python + uv
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]" --python .venv/bin/python

# 3. Node.js (for Next.js projects)
# Ensure node 18+ and npm are installed
node --version && npm --version

# 4. agent-browser (for browser testing + video recording)
npm install -g agent-browser
# Needs Chromium:
sudo apt install -y chromium-browser
# Headless display (if no X server):
sudo apt install -y xvfb
export DISPLAY=:99
Xvfb :99 -screen 0 1280x1024x24 &

# 5. lsof (for port cleanup)
sudo apt install -y lsof
```

## Test Commands

### Quick smoke test — single story, no browser (fastest signal)
```bash
PYTHONUNBUFFERED=1 .venv/bin/python -c "
from pathlib import Path
from otto.certifier import run_unified_certifier
from otto.certifier.stories import load_or_compile_stories

PROJECT = Path('bench/certifier-stress-test/task-manager')
INTENT = 'task manager with user auth, CRUD tasks with title/description/status/due date, user isolation, filter by status, sort by due date'

# Install deps first
import subprocess; subprocess.run(['npm', 'install', '--no-audit', '--no-fund'], cwd=str(PROJECT), capture_output=True)

story_set, _, _, _ = load_or_compile_stories(PROJECT, INTENT, config={})
skip = {s.id for s in story_set.stories[1:]}
config = {'certifier_parallel_stories': 1, 'certifier_skip_break': True, 'certifier_app_start_timeout': 90}

report = run_unified_certifier(intent=INTENT, project_dir=PROJECT, config=config, skip_story_ids=skip)
print(f'Outcome: {report.outcome.value}, {report.duration_s:.0f}s, \${report.cost_usd:.2f}')
"
```

### Full parallel run — 7 stories, parallel=3
```bash
PYTHONUNBUFFERED=1 .venv/bin/python -c "
from pathlib import Path
from otto.certifier import run_unified_certifier

PROJECT = Path('bench/certifier-stress-test/task-manager')
INTENT = 'task manager with user auth, CRUD tasks with title/description/status/due date, user isolation, filter by status, sort by due date'

config = {'certifier_parallel_stories': 3, 'certifier_skip_break': True, 'certifier_app_start_timeout': 90}
report = run_unified_certifier(intent=INTENT, project_dir=PROJECT, config=config)
print(f'Outcome: {report.outcome.value}, {report.duration_s:.0f}s, \${report.cost_usd:.2f}')

tier4 = next((t for t in report.tiers if t.tier == 4), None)
if tier4 and hasattr(tier4, '_cert_result'):
    for r in tier4._cert_result.results:
        s = 'PASS' if r.passed else 'FAIL'
        print(f'  [{s}] {r.story_title} ({r.duration_s:.0f}s, \${r.cost_usd:.3f})')
"
```

### With browser + video recording
```bash
# Ensure Xvfb is running for headless browser
export DISPLAY=:99

PYTHONUNBUFFERED=1 .venv/bin/python -c "
from pathlib import Path
from otto.certifier import run_unified_certifier
from otto.certifier.stories import load_or_compile_stories

PROJECT = Path('bench/certifier-stress-test/task-manager')
INTENT = 'task manager with user auth, CRUD tasks with title/description/status/due date, user isolation, filter by status, sort by due date'

story_set, _, _, _ = load_or_compile_stories(PROJECT, INTENT, config={})
skip = {s.id for s in story_set.stories[1:]}
config = {'certifier_parallel_stories': 1, 'certifier_skip_break': True, 'certifier_browser': True, 'certifier_app_start_timeout': 90}

report = run_unified_certifier(intent=INTENT, project_dir=PROJECT, config=config, skip_story_ids=skip)
print(f'Outcome: {report.outcome.value}, {report.duration_s:.0f}s, \${report.cost_usd:.2f}')

# Check evidence
import glob
for d in sorted(glob.glob(str(PROJECT / 'otto_logs' / 'certifier' / 'evidence-*'))):
    from pathlib import Path as P
    files = sorted(P(d).iterdir())
    print(f'{P(d).name}/')
    for f in files:
        print(f'  {f.name} ({f.stat().st_size:,} bytes)')
"
```

## What to Compare (Mac Mini vs Ubuntu)

| Metric | Mac Mini | Ubuntu |
|--------|----------|--------|
| Single story (no browser) | ~40s | ? |
| Single story (with browser) | ~100s | ? |
| 7 stories parallel=3 | ~313s | ? |
| Cost per story | ~$0.19 | should be same |
| Worker copy time | <1s (APFS clone) | ?s (copytree) |
| npm install per worker | ~15s | ? |
| Next.js startup | ~15s | ? |
| Evidence files generated | ✓ | ? |
| HTML report generated | ✓ | ? |
| Video recording works | ✓ | ? (needs Xvfb) |

## Platform Differences

- **Worker copy**: Mac uses APFS clone (`cp -c`, instant). Ubuntu uses `shutil.copytree` (slower, copies everything). Worker copy time will be higher.
- **Browser**: Mac has Chrome installed. Ubuntu needs `chromium-browser` + `Xvfb` for headless.
- **lsof**: May need `apt install lsof`. Used for port cleanup — wrapped in try/except so non-critical.
- **Date commands**: The LLM agent generates `date` commands in curl. macOS and Linux `date` have different syntax. The agent should adapt but may need a retry.

## Reports

After any run, check:
```bash
# PoW reports
cat PROJECT/otto_logs/certifier/proof-of-work.md
open PROJECT/otto_logs/certifier/proof-of-work.html  # or xdg-open on Ubuntu

# Evidence
ls PROJECT/otto_logs/certifier/evidence-*/

# Story run logs (parallel mode)
ls PROJECT/otto_logs/certifier/stories/
```
