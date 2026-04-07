"""Benchmark intents with fixed acceptance stories.

Each intent has a fixed set of stories that both otto and bare CC products
are tested against. Stories are derived from the intent, not from the code.
This ensures apples-to-apples comparison.
"""

INTENTS = [
    {
        "id": "cli-simple",
        "type": "CLI",
        "name": "Password generator",
        "intent": (
            "Python CLI password generator: generate passwords with configurable "
            "length (--length, default 16), character sets (--uppercase, --lowercase, "
            "--digits, --symbols flags, all on by default), exclude ambiguous chars "
            "(--no-ambiguous removes 0O1lI), copy to clipboard (--copy). "
            "Multiple passwords at once (--count N). Use argparse."
        ),
        "stories": [
            {"id": "S1-defaults", "test": "Run with no flags. Verify output is 16 chars, contains uppercase+lowercase+digits+symbols."},
            {"id": "S2-length", "test": "Run with --length 32. Verify output is 32 chars. Run with --length 4, verify 4 chars."},
            {"id": "S3-charsets", "test": "Run with --no-uppercase --no-symbols. Verify output has only lowercase+digits. Run with --digits only (disable others). Verify only digits."},
            {"id": "S4-ambiguous", "test": "Run with --no-ambiguous. Generate 100 passwords. Verify none contain 0, O, 1, l, or I."},
            {"id": "S5-count", "test": "Run with --count 5. Verify 5 different passwords are output, each on its own line."},
            {"id": "S6-edge", "test": "Run with all charsets disabled — should error. Run with --length 0 — should error. Run with no command — should show help."},
        ],
    },
    {
        "id": "cli-complex",
        "type": "CLI",
        "name": "Log analyzer",
        "intent": (
            "Python CLI tool: log analyzer. Reads web server log files (Apache combined "
            "format) from stdin or file argument. Commands: analyze (summary: total requests, "
            "unique IPs, top 10 URLs, top 10 IPs, requests per method/status/hour, avg "
            "response size), filter (by --status, --method, --ip, --url-contains, "
            "--after/--before datetime, --min-size, AND logic), top (top N by "
            "ip|url|status|user-agent|hour, --reverse). Handle malformed lines gracefully. "
            "Support .gz files. Use argparse. Comprehensive pytest tests."
        ),
        "stories": [
            {"id": "S1-analyze", "test": "Create a sample log file (10+ entries, mixed methods/status). Run analyze. Verify total requests, unique IPs, top URLs, top IPs, method breakdown, status breakdown are all present and correct."},
            {"id": "S2-filter", "test": "Run filter --status 404 on sample file. Verify only 404 lines shown. Run filter --method POST --ip 1.2.3.4. Verify AND logic (both conditions met). Run filter --after '2024-01-15' --before '2024-01-16'. Verify date range works."},
            {"id": "S3-top", "test": "Run top --by ip. Verify top 10 IPs shown in descending order. Run top --by url -n 3. Verify only 3 shown. Run top --by status --reverse. Verify ascending order."},
            {"id": "S4-stdin-gz", "test": "Pipe log file via stdin: cat sample.log | tool analyze. Verify same output as file arg. Create gzipped file. Run analyze on .gz file. Verify works."},
            {"id": "S5-malformed", "test": "Add malformed lines to sample file (empty line, truncated line, binary garbage). Run analyze. Verify malformed lines are skipped with warnings to stderr, valid lines still analyzed correctly."},
            {"id": "S6-edge", "test": "Run on nonexistent file — should print error, not crash. Run on empty file — should handle gracefully. Run analyze with no file and no stdin pipe — should show help or error."},
        ],
    },
    {
        "id": "api-auth",
        "type": "API",
        "name": "URL shortener with auth",
        "intent": (
            "Express.js REST API for a URL shortener: create short URLs (POST /api/shorten "
            "with {url}), redirect short code to original (GET /:code), list user's URLs "
            "(GET /api/urls), delete URL (DELETE /api/urls/:id), click tracking (each "
            "redirect increments count, GET /api/urls/:id/stats returns click count). "
            "JWT auth required for create/list/delete. SQLite with better-sqlite3."
        ),
        "stories": [
            {"id": "S1-auth", "test": "Register a user (POST /api/auth/register). Login (POST /api/auth/login). Verify JWT token returned. Use token for subsequent requests."},
            {"id": "S2-crud", "test": "Create short URL (POST /api/shorten {url:'https://example.com'}). List URLs (GET /api/urls). Verify it appears. Delete it (DELETE /api/urls/:id). Verify gone from list."},
            {"id": "S3-redirect", "test": "Create a short URL. GET /:code (the short code). Verify redirect (301/302) to original URL. Do it 3 times. Check stats — click count should be 3."},
            {"id": "S4-isolation", "test": "Register user A and user B. User A creates a URL. User B lists URLs — should NOT see A's URL. User B tries to delete A's URL — should fail (403/404)."},
            {"id": "S5-access", "test": "Try POST /api/shorten without auth token — should get 401. Try GET /api/urls without auth — should get 401. GET /:code (redirect) should work without auth."},
            {"id": "S6-edge", "test": "POST /api/shorten with invalid URL (empty, missing field) — should get 400. GET /:nonexistent — should get 404. Register duplicate username — should fail."},
        ],
    },
    {
        "id": "api-complex",
        "type": "API",
        "name": "Project management",
        "intent": (
            "Express.js project management API: Users (register/login JWT, profiles with "
            "avatar+bio). Projects (CRUD, owner+members many-to-many, only owner deletes). "
            "Tasks (belong to project, title/description/status/priority/assignee/due-date, "
            "filter by status+priority+assignee, sort by due-date+priority). "
            "Comments (belong to task, author+body+timestamp, any member can comment). "
            "SQLite with better-sqlite3. Proper foreign keys and cascading deletes."
        ),
        "stories": [
            {"id": "S1-auth-profile", "test": "Register user, login, update profile (avatar_url, bio). Verify profile read returns updated fields."},
            {"id": "S2-projects", "test": "Create project. List projects (should appear). Update project name. Delete project (owner). Verify non-owner cannot delete (403)."},
            {"id": "S3-members", "test": "Create project. Add member (another user). Verify member can see project. Remove member. Verify member can no longer access project."},
            {"id": "S4-tasks", "test": "Create task in project with all fields (title, description, status=todo, priority=high, assignee=member, due_date). List tasks. Update status to done. Delete task."},
            {"id": "S5-filter-sort", "test": "Create 3 tasks with different status/priority/assignee. Filter by status=todo. Filter by priority=high. Sort by due_date. Verify correct results."},
            {"id": "S6-comments", "test": "Create comment on task (as project member). List comments. Verify author and timestamp. Non-member should not be able to comment."},
            {"id": "S7-cascade", "test": "Create project with tasks and comments. Delete the project. Verify tasks and comments are also deleted (cascading delete)."},
        ],
    },
    {
        "id": "library",
        "type": "Library",
        "name": "Rate limiter",
        "intent": (
            "Python library: a rate limiter with sliding window algorithm. "
            "RateLimiter(max_requests, window_seconds), limiter.allow(key) returns True/False, "
            "limiter.remaining(key) returns remaining requests, limiter.reset(key) clears a key, "
            "limiter.wait(key) blocks until allowed (async version: await limiter.wait_async(key)). "
            "Thread-safe. Include comprehensive pytest tests."
        ),
        "stories": [
            {"id": "S1-basic", "test": "Create RateLimiter(3, 1.0). Call allow('k') 3 times — all True. Call 4th time — False. Check remaining('k') is 0."},
            {"id": "S2-expiry", "test": "Create RateLimiter(2, 0.5). Exhaust limit. Sleep 0.6s. Call allow('k') — should be True again (window expired)."},
            {"id": "S3-reset", "test": "Exhaust limit. Call reset('k'). Call allow('k') — should be True. remaining('k') should be max_requests - 1."},
            {"id": "S4-isolation", "test": "Exhaust limit for key 'a'. allow('b') should still be True — different keys are independent."},
            {"id": "S5-threading", "test": "Create RateLimiter(100, 10). Spawn 20 threads, each calling allow('shared') 10 times. Total True count should be exactly 100."},
            {"id": "S6-wait", "test": "Create RateLimiter(1, 0.5). Call allow('k') once (True). Call wait('k') — should block ~0.5s then return. Test wait_async similarly."},
        ],
    },
    {
        "id": "webapp",
        "type": "Web app",
        "name": "Todo list with UI",
        "intent": (
            "Express.js web app: personal todo list with HTML UI. Add tasks with "
            "title+priority (high/medium/low), view on homepage with color-coded badges, "
            "mark complete (strikethrough), delete, filter by priority. EJS templates, "
            "SQLite, custom CSS. Homepage has form at top, task list below, counter "
            "'X of Y completed'. No auth."
        ),
        "stories": [
            {"id": "S1-empty", "test": "GET /. Verify HTML contains form (input+select+button), empty state message or empty list, counter shows '0 of 0'."},
            {"id": "S2-crud", "test": "POST to create task with title='Test' priority='high'. GET /. Verify task appears with title and 'high' badge. Mark complete. Verify strikethrough class. Delete. Verify gone."},
            {"id": "S3-counter", "test": "Create 3 tasks. Verify counter shows '0 of 3'. Mark 1 complete. Verify '1 of 3'. Delete 1 incomplete. Verify '1 of 2'."},
            {"id": "S4-filter", "test": "Create tasks with high/medium/low priority. GET /?priority=high. Verify only high-priority tasks shown. GET /?priority=low. Verify only low shown. GET / (no filter). Verify all shown."},
            {"id": "S5-edge", "test": "POST with empty title — should reject. POST with invalid priority — should reject. Verify special chars in title are HTML-escaped (no XSS)."},
            {"id": "S6-persist", "test": "Create a task. Restart the server. GET /. Verify task still exists (SQLite persistence)."},
        ],
    },
]
