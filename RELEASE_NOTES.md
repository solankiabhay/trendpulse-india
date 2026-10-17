# Release 1.2.0
- Thin digests (fewer than 3 publishers and under 160 chars of summary) are now `noindex` and left out of the sitemap.
- Feed teaser debris ("Read more at: ...", URLs, mid-word cuts) is cleaned, including in already-stored posts.
- Fixed: the language check only detected Devanagari, so Telugu/Malayalam/Gujarati titles were accepted as English. Now enforced per script.
- New: `--unpublish ID` and an `unpublish_ids` input on the Auto Blog workflow; removed posts are backed up in `unpublished.json`.
- Auto Blog now regenerates pages from stored posts before running tests.
- Tests: 68 -> 74.

# Release 1.1.0 (previous)
- Article pages restructured: What happened / What each source reports (publisher + link) / What is not confirmed, in English and Hindi.
- Thin topics now go to `review.json` (7-day expiry) instead of being dropped; `--list-review` and `--approve ID` added.
- No invented background or facts: sections only contain what feeds actually provide.
- Tests: 61 -> 68.

# Release 1.0.0 (previous)
Initial from-scratch build (no prior project files were provided).
- Pipeline: RSS/Atom + Google Trends parsing, XXE/entity-bomb rejection, retries with timeouts, quality gate, dedupe, atomic writes.
- Site: bilingual home/English/Hindi pages, search + category filter, About/Contact/Privacy/Editorial pages, sitemap, robots, canonicals, hreflang, JSON-LD Article.
- Automation: `auto-blog.yml` (4x daily IST, concurrency-safe, minimal permissions, Pages deploy), `ci.yml`.
- Fixed during build: temp-file permissions (0600 → 0644) on generated files; JSON-LD now escapes `<`, `>`, `&`.
- Placeholders you must replace: `site_url`, `contact_email` in `config.json`.
