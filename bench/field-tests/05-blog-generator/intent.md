# Blog Generator

Build a Python static blog generator.

Required behavior:

- Source posts live under `content/` as Markdown files with YAML frontmatter:
  `title`, `date`, `tags`, and `summary`.
- Provide a CLI entry point: `python -m blog build` or `python build_site.py`.
- Generate:
  - `output/index.html` with newest-first posts.
  - `output/posts/<slug>.html` for each post.
  - `output/tags/<tag>.html` for each tag.
  - `output/rss.xml`.
  - `output/search.json` with title, slug, summary, tags, and date.
- Use one shared base template for generated pages.
- Seed three example posts so the generated output can be inspected immediately.
- Include `start.sh` at the repo root. It must build the site if needed and
  serve `output/` on `$PORT`.
- Include `tests/run_acceptance.py` that verifies output structure, post
  ordering, tag pages, RSS XML, search JSON, and idempotent rebuilds.

Keep dependencies light and document how to add a post.
