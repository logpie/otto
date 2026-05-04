# P8 — `jobq` job queue + worker CLI (T4 second project) — judgment: **PASS**

Session: `/tmp/otto-e2e/p8-jobq/otto_logs/sessions/2026-05-04-162602-8c1cce`

| Phase | Result | Cost | Wall |
|---|---|---|---|
| Compile | 4-slice linear chain (foundation→core→worker→cli), 1 validator warning | – | – |
| Build | 4/4 passing | $2.01 | 1001s |
| Merge | **4 landed, 0 blocked** | $0.00 | – |
| Audit | **verdict: passed** | $0.32 | 139s |
| **Total** | | **$2.33** | ~19 min |

## Per-rubric-dimension verdicts — all PASS

### Dim 5 — Product quality: PASS

```
$ pytest tests/ -q
31 passed in 0.12s

$ jobq init
Database initialized at /tmp/jobq-test/jobq.db

$ jobq enqueue send_email --payload '{"to":"a@x"}'  → 1
$ jobq enqueue send_email --payload '{"to":"b@x"}'  → 2
$ jobq enqueue send_email --payload '{"to":"c@x"}'  → 3

$ jobq jobs list
ID  NAME       STATUS  CREATED_AT
1   send_email queued  ...
2   send_email queued  ...
3   send_email queued  ...

$ jobq worker --once  (×3)

$ jobq jobs list
1  send_email done
2  send_email done
3  send_email done

$ jobq stats
{
  "counts": {"queued": 0, "running": 0, "done": 3, "failed": 0, "cancelled": 0},
  "oldest_queued_age_seconds": null,
  "schedules_count": 0,
  "last_scheduler_tick": null
}
```

State machine, queue claim, CLI, stats — all working as specified.

### Overall: **PASS — second T4 project, different shape from P7.**

## T4 progress: **PASSED** (P7 webapp+auth+admin + P8 cli+state-machine).

T1 ✓ (P1, P2). T2 ✓ (P3, P4). T3 ✓ (P5, P6). T4 ✓ (P7, P8).
Four tiers tier-passed.
