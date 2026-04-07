# i2p Benchmark: Otto vs Bare CC

## Design

Same intent → both paths → same certifier judges both.

```
For each intent:
  1. otto build "intent"  → product A  → certifier scores A
  2. bare CC "intent"     → product B  → certifier scores B
  Compare: stories passed, total stories, cost, time
```

## Intents (diverse product types)

| # | Type | Intent |
|---|------|--------|
| 1 | CLI simple | Password generator with flags |
| 2 | CLI complex | Log analyzer with 4 commands |
| 3 | API simple | URL shortener with JWT auth |
| 4 | API complex | Project management (4 entities, roles) |
| 5 | Library | Rate limiter with threading + async |
| 6 | Web app | Todo list with HTML UI |

## Metrics

- **Stories passed / total** — product completeness + correctness
- **Cost ($)** — total LLM spend
- **Time (s)** — wall clock
- **Fix rounds** — how many certifier iterations (otto only)
- **Bugs found** — certifier failures that were fixed

## Running

```bash
python bench/i2p-bench/run.py           # run all intents, both paths
python bench/i2p-bench/run.py --otto    # otto only
python bench/i2p-bench/run.py --bare    # bare CC only
python bench/i2p-bench/run.py --intent 1  # single intent
```

Results written to `bench/i2p-bench/results/`.
