# Release 1.0.0
Initial from-scratch build (no prior project files were provided).
- Pipeline: RSS/Atom + Google Trends parsing, XXE/entity-bomb rejection, retries with timeouts, quality gate, dedupe, atomic writes.
- Site: bilingual home/English/Hindi pages, search + category filter, About/Contact/Privacy/Editorial pages, sitemap, robots, canonicals, hreflang, JSON-LD Article.
- Automation: `auto-blog.yml` (4x daily IST, concurrency-safe, minimal permissions, Pages deploy), `ci.yml`.
- Fixed during build: temp-file permissions (0600 → 0644) on generated files; JSON-LD now escapes `<`, `>`, `&`.
- Placeholders you must replace: `site_url`, `contact_email` in `config.json`.
