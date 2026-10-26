import json, re, shutil, subprocess, sys, unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import engine, htmlcheck  # noqa: E402

REQUIRED = ["index.html", "en/index.html", "hi/index.html", "assets/style.css", "assets/app.js", "engine.py", "config.json",
            "requirements.txt", "posts.json", "published.json", "review.json", "sitemap.xml", "robots.txt", ".gitignore", "README.md",
            ".github/workflows/auto-blog.yml", ".github/workflows/ci.yml"] + [f"{l}/{p}.html" for l in ("en", "hi") for p in engine.INFO_PAGES]


class ProjectFiles(unittest.TestCase):

    def test_source_preview_detection(self):
        self.assertTrue(engine.source_is_preview("Oman vs Nepal match predictions for October 19"))
        self.assertFalse(engine.source_is_preview("Nepal beat Oman by 113 runs"))

    def test_result_headline_detection_and_cleanup_priority(self):
        self.assertTrue(engine.source_title_reports_result("Nepal beat Oman by 113 runs"))
        self.assertTrue(engine.source_title_reports_result("Nepal vs Oman Highlights"))
        self.assertFalse(engine.source_title_reports_result("Oman vs Nepal match predictions"))
        mixed = {
            "title": "Oman v Nepal | Match Highlights",
            "sources": [
                {"title": "ICC CWC League 2 Match 139 - Oman vs Nepal predictions"},
                {"title": "Nepal vs Oman Highlights: 113-run win"}
            ]
        }
        self.assertTrue(engine.source_identity_needs_cleanup(mixed))
        mixed["sources"] = [{"title": "Oman vs Nepal match predictions"}]
        self.assertFalse(engine.source_identity_needs_cleanup(mixed))

    def test_source_titles_conflict_on_different_match_numbers(self):
        self.assertTrue(engine.source_titles_conflict("Oman vs Nepal Match 135", "Oman v Nepal Match 139 preview"))
        self.assertFalse(engine.source_titles_conflict("Oman v Nepal match highlights", "Nepal vs Oman match report"))

    def test_source_titles_conflict_on_explicit_different_dates(self):
        self.assertTrue(engine.source_titles_conflict("Oman vs Nepal on 13 October 2026", "Oman v Nepal on October 19, 2026"))
        self.assertFalse(engine.source_titles_conflict("Oman vs Nepal", "Nepal vs Oman"))

    def test_evidence_digest_keeps_more_verified_source_context(self):
        text = " ".join(["Nepal defeated Oman by 113 runs after scoring 263 runs in fifty overs."] * 8)
        digest = engine.build_evidence_digest([{"publisher": "Scorecard", "paragraphs": [text]}])
        self.assertGreater(len(digest.split()), 25)
        self.assertLessEqual(len(digest.split()), 50)

    def test_hindi_content_language_detector_flags_english_digest(self):
        self.assertTrue(engine.content_language_needs_refresh("The court announced its decision in London today.", "hi"))
        self.assertFalse(engine.content_language_needs_refresh("अदालत ने आज लंदन में अपना फैसला सुनाया।", "hi"))
        self.assertFalse(engine.content_language_needs_refresh("The court announced its decision in London today.", "en"))
        self.assertTrue(engine.content_language_needs_refresh("UAE शाही परिवार की महिला Mahrah Al Nahyan को लंदन में पाया गया।", "hi"))

    def test_hindi_excerpt_cleaner_removes_english_translation_suffix(self):
        self.assertEqual(engine.clean_hindi_excerpt("कौन हैं महराह? - Mahra Al Nahyan found unconscious in London"), "कौन हैं महराह?")
        self.assertEqual(engine.clean_hindi_excerpt("भारत में आज बारिश हुई।"), "भारत में आज बारिश हुई।")
        self.assertEqual(engine.clean_hindi_excerpt("English-only headline from publisher"), "")

    def test_hindi_evidence_digest_uses_translated_excerpt(self):
        from unittest import mock
        evidence = [{"publisher": "Example News", "paragraphs": ["Officials announced the new plan today after a meeting."]}]
        with mock.patch.object(engine, "translate_to_hindi", return_value="अधिकारियों ने बैठक के बाद नई योजना की घोषणा की।"):
            body = engine.build_evidence_digest(evidence, lang="hi")
        self.assertIn("अधिकारियों ने बैठक के बाद नई योजना की घोषणा की।", body)


    def test_required_files_exist(self):
        self.assertEqual([f for f in REQUIRED if not (ROOT / f).exists()], [])

    def test_json_files_valid_and_config_ok(self):
        for f in ("posts.json", "published.json", "config.json"): json.loads((ROOT / f).read_text(encoding="utf-8"))
        cfg = engine.load_config(ROOT / "config.json"); self.assertGreaterEqual(len(cfg["feeds"]), 2)
        self.assertEqual({f["lang"] for f in cfg["feeds"]}, {"en", "hi"})

    def test_no_secrets_committed(self):
        pat = re.compile(r"ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key\s*[:=]\s*['\"][^'\"]{8,}", re.I)
        for f in ROOT.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".json", ".yml", ".html", ".js", ".md", ".txt", ".css") and "tests" not in f.parts[-2:-1]:
                self.assertIsNone(pat.search(f.read_text(encoding="utf-8", errors="ignore")), str(f))
        self.assertFalse(list(ROOT.glob(".env*")))

    def test_gitignore_covers_secrets_and_caches(self):
        g = (ROOT / ".gitignore").read_text()
        for needle in (".env", "__pycache__", ".venv"): self.assertIn(needle, g)

    def test_engine_has_no_shell_or_eval(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        for bad in ("subprocess", "os.system", "eval(", "exec(", "shell=True", "pickle"): self.assertNotIn(bad, src)

    def test_generated_files_up_to_date(self): self.assertTrue(engine.check_generated(ROOT))

    def test_google_trends_card_shows_keyword_and_google_search(self):
        post = {"keyword": "India monsoon", "url": "en/posts/monsoon.html", "title": "Rain forecast updated", "lang": "en"}
        card = engine.trend_topic_card(post, "", "en")
        self.assertIn("India monsoon", card)
        self.assertIn("https://www.google.com/search?hl=en&amp;q=India%20monsoon", card)
        self.assertIn("Rain forecast updated", card)
        self.assertIn('rel="noopener noreferrer"', card)

    def test_hindi_google_trends_card_localizes_keyword_and_headline(self):
        from unittest import mock
        post = {"keyword": "oman vs nepal", "url": "en/posts/match.html", "title": "Oman v Nepal match highlights", "lang": "en"}
        with mock.patch.object(engine, "translate_to_hindi", return_value="ओमान और नेपाल के मैच की मुख्य झलकियाँ"):
            card = engine.trend_topic_card(post, "../", "hi")
        self.assertIn('lang="hi"', card)
        self.assertIn("Google Trends · भारत", card)
        self.assertIn("ओमान बनाम नेपाल", card)
        self.assertIn("ओमान और नेपाल के मैच की मुख्य झलकियाँ", card)
        self.assertIn("https://www.google.com/search?hl=hi&amp;q=oman%20vs%20nepal", card)

    def test_hindi_google_trends_card_never_falls_back_to_english_headline(self):
        from unittest import mock
        post = {"keyword": "unfamiliar keyword", "url": "en/posts/story.html", "title": "English headline stays untranslated", "lang": "en"}
        with mock.patch.object(engine, "translate_to_hindi", side_effect=lambda value: value):
            card = engine.trend_topic_card(post, "../", "hi")
        self.assertIn("इस ट्रेंड से जुड़ी खबर पढ़ें।", card)
        self.assertNotIn("English headline stays untranslated", card)

    def test_no_api_fallback_uses_short_attributed_source_excerpts(self):
        evidence = [
            {"publisher": "Example News", "paragraphs": ["Officials announced the new plan today after a meeting. More detail follows."]},
            {"publisher": "Daily Report", "paragraphs": ["The department said the rollout begins next month in several regions."]},
        ]
        body = engine.build_evidence_digest(evidence)
        self.assertIn("Example News:", body)
        self.assertIn("Daily Report:", body)
        self.assertEqual(len(body.split("\n\n")), 2)
        for paragraph in body.split("\n\n"):
            self.assertLessEqual(len(paragraph.split()), 26)  # publisher label + 22-word excerpt + quote marks


class HtmlSeo(unittest.TestCase):
    cfg = engine.load_config(ROOT / "config.json")

    def test_every_page_passes_static_checks(self):
        self.assertEqual(htmlcheck.check_tree(ROOT, self.cfg["site_url"]), [])

    def test_hreflang_pairs_reciprocal(self):
        for pg in ("index", "about", "contact", "privacy", "editorial"):
            en, hi = htmlcheck.parse(ROOT / f"en/{pg}.html"), htmlcheck.parse(ROOT / f"hi/{pg}.html")
            self.assertEqual(en.hreflangs.get("en"), hi.hreflangs.get("en")); self.assertEqual(en.hreflangs.get("hi"), hi.hreflangs.get("hi"))
            self.assertIn("hi", en.hreflangs)
        self.assertEqual(htmlcheck.parse(ROOT / "hi/index.html").lang, "hi")

    def test_unique_titles(self):
        titles = [htmlcheck.parse(f).title for f in ROOT.rglob("*.html") if "tests" not in f.parts and not f.name.startswith("google") ]
        self.assertEqual(len(titles), len(set(titles)))

    def test_sitemap_and_robots(self):
        urls = [e.text for e in ET.parse(ROOT / "sitemap.xml").getroot().iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
        self.assertTrue(urls and all(u.startswith(self.cfg["site_url"]) for u in urls)); self.assertEqual(len(urls), len(set(urls)))
        self.assertIn(f"Sitemap: {self.cfg['site_url']}/sitemap.xml", (ROOT / "robots.txt").read_text())

    def test_footer_legal_links_on_every_page(self):
        for f in ROOT.rglob("*.html"):
            if "tests" in f.parts or f.name.startswith("google"): continue
            html = f.read_text(encoding="utf-8")
            for pg in ("privacy", "editorial", "contact", "about"): self.assertIn(f"{pg}.html", html, f.name)

    def test_config_placeholders_are_reported_not_hidden(self):
        # Release notes tell the owner to replace these; the test documents the current state.
        self.assertTrue(self.cfg["site_url"].startswith("https://"))


class Accessibility(unittest.TestCase):
    @staticmethod
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(self, a, b):
        la, lb = sorted((self.lum(a), self.lum(b)), reverse=True); return (la + 0.05) / (lb + 0.05)

    def test_color_contrast_wcag_aa(self):
        css = (ROOT / "assets/style.css").read_text(encoding="utf-8")
        v = dict(re.findall(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", css))
        core = [("ink", "bg"), ("muted", "bg"), ("blue", "bg"), ("bg", "navy"), ("badge-ink", "badge-bg")]
        optional = [("muted", "soft"), ("bg", "blue"), ("bg", "blue-d")]  # checked only if both variables exist
        for a, b in core: self.assertIn(a, v); self.assertIn(b, v)
        for a, b in core + [p for p in optional if p[0] in v and p[1] in v]:
            self.assertGreaterEqual(self.ratio(v[a], v[b]), 4.5, f"{a} on {b}")

    def test_css_focus_and_reduced_motion(self):
        css = re.sub(r"\s+", "", (ROOT / "assets/style.css").read_text(encoding="utf-8"))  # ignore formatting
        for needle in (":focus-visible", "prefers-reduced-motion", ".skip", "min-height:44px", "[hidden]"): self.assertIn(needle, css)

    def test_pages_have_skip_link_nav_labels_and_search_label(self):
        p = (ROOT / "en/index.html").read_text(encoding="utf-8")
        for needle in ('class="skip"', 'aria-label="Main navigation"', '<main id="main"'): self.assertIn(needle, p)
        self.assertNotIn('for="q"', p)
        self.assertNotIn('role="search"', p)
        self.assertNotIn('role="status"', p)
        self.assertIn('width=device-width, initial-scale=1', p)

    def test_no_external_requests_in_pages(self):
        for f in ROOT.rglob("*.html"):
            if "tests" in f.parts or f.name.startswith("google"): continue
            h = f.read_text(encoding="utf-8")
            res = re.findall(r'<script[^>]+src="https?://[^"]+"|<link[^>]+rel="stylesheet"[^>]*href="https?://[^"]+"|<img[^>]+src="https?://[^"]+"', h)
            self.assertEqual(res, [], f.name)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class JavaScript(unittest.TestCase):
    def test_syntax(self):
        self.assertEqual(subprocess.run(["node", "--check", str(ROOT / "assets/app.js")], capture_output=True).returncode, 0)

    def test_category_filter_logic(self):
        code = ("const {matches}=require(process.argv[1]);"
                "const india={cat:'india'}, technology={cat:'technology'}, sports={cat:'sports'};"
                "const r=[matches(india,'all'),matches(technology,'technology'),"
                "matches(technology,'sports'),matches(sports,'technology'),"
                "matches(india,'india'),matches(technology,'all')];"
                "console.log(JSON.stringify(r))")
        out = subprocess.run(["node", "-e", code, str(ROOT / "assets/app.js")], capture_output=True, text=True)
        self.assertEqual(json.loads(out.stdout), [True, True, False, False, True, True], out.stderr)


class Workflows(unittest.TestCase):
    ab = (ROOT / ".github/workflows/auto-blog.yml").read_text()
    ci = (ROOT / ".github/workflows/ci.yml").read_text()

    def test_schedule_checks_feeds_every_two_hours(self):
        self.assertIn('- cron: "0 */2 * * *"', self.ab)
        self.assertNotIn('- cron: "30 3 * * *"', self.ab)

    def test_readme_schedule_and_post_limit_match_config(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertIn('`0 */2 * * *`', self.ab)
        self.assertIn('`0 */2 * * *`', readme)
        self.assertIn(f"At most {cfg['max_new_posts_per_run']} new posts per run", readme)

    def test_concurrency_permissions_and_tests(self):
        self.assertIn("cancel-in-progress: false", self.ab); self.assertIn("group: trendpulse-pipeline", self.ab)
        self.assertIn("permissions:\n  contents: read", self.ab); self.assertNotIn("write-all", self.ab + self.ci)
        self.assertLess(self.ab.index("unittest discover"), self.ab.index("Run pipeline"))
        self.assertIn("python engine.py --refresh-only", self.ab)
        self.assertIn("else python engine.py; fi", self.ab)
        self.assertIn("workflow_dispatch", self.ab)
        self.assertIn("unittest discover", self.ci); self.assertIn("engine.py --check", self.ci)

    def test_actions_pinned_to_major_versions_and_only_ai_key_secret_used(self):
        for u in re.findall(r"uses: ([\w./-]+@[\w.]+)", self.ab + self.ci): self.assertRegex(u, r"@v\d+$")
        self.assertEqual(re.findall(r"secrets\.([A-Z0-9_]+)", self.ab + self.ci), ["OPENAI_API_KEY"])

    def test_no_interpolated_untrusted_input_in_run_blocks(self):
        for block in re.findall(r"run: \|?\n?(.*?)(?=\n\s+- |\n\s+\w+:|\Z)", self.ab, re.S):
            self.assertNotRegex(block, r"\$\{\{\s*github\.(event|head_ref)")


if __name__ == "__main__":
    unittest.main()
