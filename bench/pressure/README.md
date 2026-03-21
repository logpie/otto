# Otto Pressure Test Projects

Canonical set of 28 projects for pressure testing otto. Each project has a `setup.sh`
(prepares the workdir) and `tasks.txt` (one task per line, or multi-line for multi-task projects).

Uses the same format as `bench/smoke/` — compatible with the smoke test runner.

## Project Categories

### Python Greenfield (5)
| Project | Tasks | What it tests |
|---------|-------|---------------|
| py-rate-limiter | 1 | Threading, concurrency, stdlib-only |
| py-data-pipeline | 1 | Multi-module, data validation, edge cases in sample data |
| py-cli-task-manager | 1 | argparse, SQLite, env vars, subprocess testing |
| py-key-value-store | 1 | File I/O, TTL, compaction, file locking, threading |
| py-markdown-parser | 1 | Lexer/parser/AST architecture, nested formatting |

### Node.js Greenfield (5)
| Project | Tasks | What it tests |
|---------|-------|---------------|
| node-rest-api | 1 | Express, validation, pagination, rate limiting, XSS |
| node-websocket-chat | 1 | WebSocket, rooms, heartbeat, message history |
| node-task-queue | 1 | Priority queue, concurrency, retry/backoff, events |
| node-file-processor | 1 | Streams, backpressure, schema validation, JSONL |
| node-rate-limiter-middleware | 1 | Three algorithms, headers, middleware patterns |

### TypeScript Greenfield (4)
| Project | Tasks | What it tests |
|---------|-------|---------------|
| ts-schema-validator | 1 | Generics, type inference, chainable API (mini-zod) |
| ts-event-emitter | 1 | Type-safe generics, async handlers, mediator pattern |
| ts-result-type | 1 | Monads, type inference, async chaining |
| ts-dependency-injector | 1 | DI container, scopes, circular detection, proxies |

### Bug Fix — Pre-Seeded (3)
| Project | Tasks | Bugs | What it tests |
|---------|-------|------|---------------|
| bugfix-inventory | 1 | 6 | Race condition, validation, logic errors, sort order |
| bugfix-scheduler | 1 | 6 | Heap indexing, this-binding, Error serialization |
| bugfix-csv-parser | 1 | 6 | \r\n handling, ragged rows, quoting, round-trip |

### Bug Fix — Real Repos (3)
| Project | Tasks | Source | What it tests |
|---------|-------|--------|---------------|
| real-cachetools-bugfix | 1 | tkem/cachetools @ 3b3167a | Cache stampede threading bug |
| real-semver-bugfix | 1 | npm/node-semver @ 2677f2a | Regex ordering in prerelease parsing |
| real-box-bugfix | 1 | cdgriffith/Box @ 91cc956 | box_dots get() regression |

### Feature Add — Real Repos (3)
| Project | Tasks | Source | What it tests |
|---------|-------|--------|---------------|
| real-tinydb-feature | 1 | msiemens/tinydb @ 9394412 | Add persist_empty table parameter |
| real-radash-feature | 1 | sodiray/radash @ 32a3de4 | Add inRange() utility function |
| real-citty-feature | 1 | unjs/citty @ 69252d4 | Add subcommand alias support |

### Multi-Task (2)
| Project | Tasks | What it tests |
|---------|-------|---------------|
| multi-blog-engine | 3 | Data → logic → CLI layering (Python) |
| multi-expense-tracker | 3 | Data → analytics → API layering (Node.js) |

### Edge Cases (3)
| Project | Tasks | What it tests |
|---------|-------|---------------|
| edge-greenfield-complex | 1 | Empty repo, must set up everything from scratch |
| edge-large-spec | 1 | 16 acceptance criteria, dense spec |
| edge-conflicting-tasks | 3 | Three tasks modifying the same file |

## Totals
- **28 projects**, **34 tasks**
- Python: 11 projects, Node.js: 10, TypeScript: 7
- Greenfield: 14, Bug fix: 6, Feature add: 3, Multi-task: 2, Edge: 3

## Running

These use the same format as smoke tests. The pressure test skill (`/pressure-test`)
reads from this directory. Failed projects are accumulated into `bench/bad-cases.yaml`.
