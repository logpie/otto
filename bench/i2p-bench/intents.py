"""Benchmark intents — diverse product types for otto vs bare CC comparison."""

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
    },
]
