# TrendPulse India

Bilingual (English / हिंदी) trending-topics site. A Python pipeline reads public RSS feeds, applies a
content-quality gate, and writes **source-linked digests** as static HTML. Hosted on GitHub Pages;
GitHub Actions runs the pipeline every two hours. Hindi summaries can use the optional local Argos English→Hindi model; no paid translation API or database is required.

**What the articles are:** automatic digests of headlines and feed summaries, each linking to the original
publishers. They are not original reporting and are labelled as unverified on every page. The pipeline never
invents sources, dates, quotes, statistics or authors. Thin digests without a substantive summary are kept out of the public home/language listings, marked `noindex,follow`, and excluded from the XML sitemap. Their direct URLs remain accessible and can reappear in listings after a later refresh adds a substantive summary.

## Layout
| Path | Purpose |
|---|---|
| `engine.py` | feed fetch/parse, quality gate, dedupe, safe JSON writes, HTML/sitemap/robots rendering |
| `config.json` | site URL, contact email, thresholds, feed list |
| `posts.json` / `published.json` / `review.json` | article store / publication history / thin topics waiting for your review (committed by the workflow) |
| `index.html`, `en/`, `hi/` | **generated** pages (do not hand-edit; edit `engine.py`) |
| `assets/style.css`, `assets/app.js` | hand-written styles; search + category filter (no network requests) |
| `tests/` | 74 unittest cases (mocked network) |
| `.github/workflows/ci.yml`, `auto-blog.yml` | CI; scheduled pipeline + Pages deploy |

## Before first deploy (required edits)
1. `config.json` → `site_url`: your real Pages URL, e.g. `https://USER.github.io/REPO` (no trailing slash). Canonicals, hreflang and the sitemap use it.
2. `config.json` → `contact_email`: a real address (shown on the Contact pages).
3. Run `python engine.py --render-only` and commit the regenerated files (CI fails if generated files are stale).

## Local use
```
python -m unittest discover -s tests -v     # all tests, no internet needed
python engine.py --dry-run                   # fetch live feeds, evaluate, write nothing
python engine.py                             # fetch, publish, regenerate pages
python engine.py --render-only               # regenerate pages from posts.json
python engine.py --check                     # fail if generated files are out of date
python -m http.server 8000                   # preview at http://localhost:8000/
```
Exit codes: `0` ok · `2` every feed failed (nothing written; workflow fails visibly) · `3` bad config.

## Deploy (GitHub Pages)
1. Create a GitHub repo, push this folder to branch `main`.
2. Settings → Pages → **Source: GitHub Actions**.
3. Settings → Actions → General → Workflow permissions: allow **Read and write** (the workflow also declares its own minimal permissions).
4. Actions → **Auto Blog** → *Run workflow*. This runs tests, the pipeline, commits any new posts, and deploys.
5. After the run, open the URL shown on the deploy job. Empty feeds/first run simply shows the "No articles yet" message.

Schedule (UTC cron → IST): `30 3` → 09:00, `30 7` → 13:00, `30 11` → 17:00, `30 15` → 21:00.
GitHub may delay scheduled runs, and disables schedules on repos with no activity for 60 days (re-enable under Actions).
A push to `main` re-renders and redeploys without fetching feeds. Pushes made by the workflow itself use `[skip ci]` and do not loop.

Custom domain: Settings → Pages → Custom domain, add the DNS record GitHub shows, then update `site_url` and re-render.
Search Console: add the property, submit `<site_url>/sitemap.xml`. Indexing and ranking are not guaranteed.

## Article structure and manual review
Each article page has: **What happened** (headline plus the feed's own summary, if it has one) → **What each source reports**
(publisher name + link to the original report) → **What is not confirmed** (states that only headlines/short summaries were
available, flags single-publisher topics, and flags sources whose wording differs). There is deliberately **no "Background" or
"Key facts" section**: feeds do not supply them, and the pipeline will not invent them. Real background needs full-text sources
or an optional LLM/human step, which this release does not include.

Topics rejected *only* for being too thin (fewer than 2 publishers and little summary text) are saved in `review.json`
instead of being dropped. Entries expire after 7 days. To publish one after checking its sources yourself:
```
python engine.py --list-review
python engine.py --approve en-1234567890
git add -A && git commit -m "Approve reviewed topic" && git push
```

## Search-engine hygiene
- **Discovery and indexing rule:** a digest appears in public home/language listings and is offered to search engines only if its source summary is substantive (160+ characters and not generic boilerplate). Thin digests are hidden from public listings, marked `noindex,follow`, and excluded from `sitemap.xml`; their direct URLs remain accessible and may reappear after enrichment. Publisher count alone does not qualify a thin digest.
  Listing pages are always indexable. This lowers the risk of the site being judged as thin auto-generated content. It does not promise rankings.
- **Teaser clean-up:** "Read more at: <url>" lines, tracking URLs and mid-word cuts (`..`) are removed from feed text, including text stored earlier.
- **Language gate:** English posts must be mostly Latin script and Hindi posts mostly Devanagari. Other scripts (Telugu, Gujarati, ...) are rejected.
- **Remove a post on purpose:** Actions → Auto Blog → Run workflow → paste IDs (e.g. `en-0123456789`) into *unpublish_ids*. Or locally:
  `python engine.py --unpublish en-0123456789`. A copy is kept in `unpublished.json` and the pipeline will not recreate it.

## How selection works
- Score = recency (0–40) + 10 per distinct publisher (max 4) + 10 if the feed gives distinct summary text + feed `weight`. No traffic or search-volume numbers are used or claimed.
- Rejected: misleading/clickbait titles, rumours/unconfirmed wording, no valid source URL, missing or old (>72 h) date, wrong language, fewer than 2 publishers *and* thin summary, near-duplicates (title similarity ≥ 0.6, or same id).
- At most 4 new posts per run (`max_new_posts_per_run`). Skipping beats publishing weak content.
- Write order: pages → `posts.json` → `published.json`. A crash never marks content published that isn't saved; rerunning is idempotent.
- Hindi pages prefer Hindi feeds (`hl=hi`). When source excerpts are English, the workflow attempts local English→Hindi translation with the open-source Argos model; the model is cached between runs. If model download/translation fails, English-only excerpts are omitted rather than mislabelled as Hindi. English and Hindi news articles remain separate (no hreflang between them); only section/info pages are paired.

## Status: what is verified where
- **A. Automated tests:** GitHub Actions CI passed on 2026-10-19; 108 unit/integration/site tests passed, plus static checks and generated-file consistency.
- **B. Hindi translation:** the GitHub Actions run successfully installed the open-source Argos English→Hindi model and refreshed stored summaries to refresh version 16. No paid API key is required for translation.
- **C. Deployment:** GitHub Pages deployment passed on 2026-10-19. Check the latest Actions run after content-refresh commits to confirm the live site has the newest generated files.
- **D. Needs domain/DNS:** only if you use a custom domain.
- **E. Needs external credentials:** none. (AdSense later needs your own account; update the Privacy Policy before enabling ads or analytics.)
- **F. Post-deployment verification:** live Google News / Trends feed responses (Google may change or rate-limit them; the Trends URL is unverified), Lighthouse / PageSpeed / Core Web Vitals, real-device mobile layout, Search Console sitemap acceptance, structured-data validation.

## Troubleshooting
- *Workflow fails "every enabled feed failed":* feeds blocked or URLs changed; run `python engine.py --dry-run -v` locally; edit `config.json`.
- *CI fails "generated file out of date":* run `python engine.py --render-only` and commit.
- *Refresh existing thin articles without adding new headlines:* run `python engine.py --refresh-only`.
- *`posts.json.corrupt-*` appears:* a file was invalid JSON and was backed up, not deleted. Restore the backup, fix, rerun.
- *Pages show stale content:* check the deploy job ran; Pages source must be "GitHub Actions".
- *No new posts for days:* normal if feeds fail the quality gate; see `rejected:` log lines in the run.

## Rollback
`git revert <commit>` for a bad content commit (or restore `posts.json`/`published.json` from history) then `python engine.py --render-only`, push. Published article files are never deleted by the pipeline.

## Known limitations
No JS-driven live data; digests are headline-level, not full articles; relevance is heuristic; Hindi quality depends on source feeds; no browser-automation tests were run; no Lighthouse numbers exist yet.
