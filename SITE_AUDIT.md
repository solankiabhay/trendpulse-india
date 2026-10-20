# TrendPulse India — Site Audit

**Audit date:** 2026-10-20  
**Repository:** https://github.com/solankiabhay/trendpulse-india  
**Scope:** Static source review of the generator, stored posts, generated English/Hindi homepages, CSS/JavaScript, SEO metadata, sitemap, robots file, and existing automated checks.

## Findings

### Content quality — needs improvement
- The stored post set contains 77 entries: 56 English and 21 Hindi.
- A local reproduction of the current indexing gate classified 59 posts as eligible and 18 as too thin or otherwise unsuitable for indexing. The generated sitemap contains 70 URLs, consistent with 59 eligible article URLs plus the homepage/language and information pages.
- 29 stored posts have no dedicated `article_body`; those pages rely on source-feed summaries. This is transparent but limits depth and differentiation.
- Some Hindi headlines naturally retain product names, acronyms, and English words. These should be reviewed for readability rather than mechanically translated.
- One stored trend headline was explicit ticket-sales copy (“Get your tickets to watch …”). This audit adds a shared promotional-title gate so matching sales/affiliate-style headlines are rejected on ingestion, excluded from public listings and the sitemap, and not shown as trend cards.

### SEO — baseline is in place, not independently validated
- Generated listing pages include canonical URLs, English/Hindi hreflang alternates, meta descriptions, Open Graph/Twitter metadata, and a viewport declaration.
- `robots.txt` points to the generated XML sitemap. The sitemap contains the expected top-level/language/info pages and eligible article URLs; no unexpected URL paths were found in the repository snapshot.
- Article pages have structured data and thin summaries are intended to receive `noindex,follow`.
- Search Console acceptance, real Google indexing/canonical selection, rich-result validation, and live sitemap fetch have **not** been verified in this audit.

### Mobile layout and accessibility — source-level checks only
- CSS has a mobile breakpoint at 42rem, flexible navigation/card grids, wrapping for long titles, 44px minimum category-button height, visible keyboard focus, skip links, and reduced-motion support.
- The audit found low contrast on small source labels and orange text/actions against white cards. The stylesheet now uses a darker orange and the standard muted text color; a WCAG AA contrast regression test was added for those key colors.
- Mobile styles remove the sticky/blurred header effect and decorative animation at small widths.
- No real-device/browser viewport test was run, so actual overflow, tap-target behavior, and screen-reader behavior still need browser testing.

### Performance — likely lightweight, but no measured score
- The site is static HTML/CSS with a small deferred JavaScript file for local search/filtering; that script makes no network requests.
- No images or third-party analytics are loaded by the listing pages reviewed.
- No Lighthouse/PageSpeed/Core Web Vitals measurements were available. Performance is therefore **not scored** rather than guessed.

## Changes in this audit branch
- Add a shared promotional headline rule to the content-quality gate, indexability gate, and trend-card relevance check.
- Add regression tests for promotional headlines.
- Correct stale README claims about test counts, publishing cadence, and the per-run publishing limit.

## Remaining verification
1. Run the complete Python unit/integration/site test suite and generated-file consistency check in GitHub Actions.
2. Confirm the Pages deployment completes and verify the live English and Hindi homepages and representative article URLs.
3. Run Lighthouse/PageSpeed on desktop and mobile; inspect layout at 320px, 375px, 768px, and desktop widths.
4. Submit the sitemap in Google Search Console and review indexing/canonical reports.
5. Manually review a sample of English and Hindi digests for source relevance, translation quality, clipped headlines, and sufficient reader value.

**Important:** This is a code/repository audit, not a claim that the live deployment, mobile browser rendering, or real-world SEO/performance metrics have passed.
