import json, os, shutil, sys, tempfile, unittest, urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
FEED_EN = {"url": "https://news.example/en", "lang": "en", "category": "india", "name": "t-en"}
FEED_HI = {"url": "https://news.example/hi", "lang": "hi", "category": "india", "name": "t-hi"}


def rss(items):
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:ht="https://trends.google.com/trending/rss">'
            "<channel><title>x</title>" + "".join(items) + "</channel></rss>").encode("utf-8")


def item(title, pub="Sat, 17 Oct 2026 04:00:00 GMT", publisher="The Hindu", related=(), link="https://news.example/a1", extra="", include_detail=True):
    li = "".join(f'<li><a href="{u}">{t}</a>&nbsp;&nbsp;<font color="#6f6f6f">{p}</font></li>' for t, u, p in related)
    # Synthetic detail is test-only fixture data; production summaries must come from feeds or publisher pages.
    fixture_detail = (" Test-only fixture detail: the report describes the event and gives readers additional context, "
                      "timing, and reported developments beyond the headline. This synthetic text exists only to exercise "
                      "the content-quality pipeline in automated tests. The report also outlines the background, the "
                      "main points raised by the sources, the next expected steps, and the timeline mentioned in the "
                      "coverage. These extra words are fictional test fixture text and are never used in production.") if include_detail and related else ""
    d = f"<description><![CDATA[<ol>{li}</ol>{fixture_detail}]]></description>" if related else ""
    dt = f"<pubDate>{pub}</pubDate>" if pub else ""
    return f'<item><title>{title} - {publisher}</title><link>{link}</link>{dt}{d}<source url="https://x.example">{publisher}</source>{extra}</item>'


GOOD_EN = item("Parliament budget session to begin next week", related=[
    ("Parliament budget session to begin next week", "https://news.example/a1", "The Hindu"),
    ("Budget session dates announced by ministry", "https://other.example/b2", "Mint")])
GOOD_HI = item("संसद का बजट सत्र अगले सप्ताह से शुरू होगा", publisher="नवभारत टाइम्स", link="https://hi.example/a1", related=[
    ("संसद का बजट सत्र अगले सप्ताह से शुरू होगा", "https://hi.example/a1", "नवभारत टाइम्स"),
    ("बजट सत्र की तारीखों का ऐलान", "https://hi2.example/b2", "अमर उजाला")])


def make_root(feeds=None):
    d = Path(tempfile.mkdtemp())
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["feeds"] = feeds if feeds is not None else [FEED_EN, FEED_HI]
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    shutil.copytree(ROOT / "assets", d / "assets")
    return d


def fetcher_for(mapping):
    def f(url):
        v = mapping[url]
        if isinstance(v, Exception): raise v
        return v
    return f


class ParsingTests(unittest.TestCase):
    def test_valid_rss_with_related_sources(self):
        c = engine.parse_feed(rss([GOOD_EN]), FEED_EN)[0]
        self.assertEqual(c["title"], "Parliament budget session to begin next week")
        self.assertEqual({s["publisher"] for s in c["sources"]}, {"The Hindu", "Mint"})
        self.assertEqual(c["published"], datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc))

    def test_atom_feed(self):
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom headline here</title><link href="https://a.example/x"/><updated>2026-10-17T03:00:00Z</updated><summary>s</summary></entry></feed>'
        c = engine.parse_feed(atom, FEED_EN)
        self.assertEqual(len(c), 1); self.assertEqual(c[0]["sources"][0]["url"], "https://a.example/x")

    def test_trends_item_uses_news_headline(self):
        extra = ('<ht:news_item><ht:news_item_title>Team wins final in Mumbai</ht:news_item_title><ht:news_item_url>https://s1.example/1</ht:news_item_url><ht:news_item_source>S1</ht:news_item_source></ht:news_item>'
                 '<ht:news_item><ht:news_item_title>Final won by home side</ht:news_item_title><ht:news_item_url>https://s2.example/2</ht:news_item_url><ht:news_item_source>S2</ht:news_item_source></ht:news_item>')
        x = rss([f'<item><title>ipl final</title><pubDate>Sat, 17 Oct 2026 04:00:00 GMT</pubDate>{extra}</item>'])
        c = engine.parse_feed(x, FEED_EN)[0]
        self.assertEqual(c["title"], "Team wins final in Mumbai"); self.assertEqual(c["keyword"], "ipl final")
        self.assertEqual(len(c["sources"]), 2)

    def test_empty_feed(self):
        self.assertEqual(engine.parse_feed(rss([]), FEED_EN), [])

    def test_malformed_and_wrong_root(self):
        with self.assertRaises(engine.FeedError): engine.parse_feed(b"<rss><channel><item>", FEED_EN)
        with self.assertRaises(engine.FeedError): engine.parse_feed(b"<html></html>", FEED_EN)
        with self.assertRaises(engine.FeedError): engine.parse_feed(b"not xml at all", FEED_EN)

    def test_entity_expansion_rejected(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>'
        with self.assertRaises(engine.FeedError): engine.parse_feed(bomb, FEED_EN)

    def test_missing_elements_are_tolerated(self):
        x = rss(["<item><link>https://a.example/</link></item>", "<item><title>No link no date headline</title></item>"])
        c = engine.parse_feed(x, FEED_EN)
        self.assertEqual(len(c), 1); self.assertIsNone(c[0]["published"]); self.assertEqual(c[0]["sources"], [])

    def test_unsafe_urls(self):
        for bad in ("javascript:alert(1)", "ftp://x.example/a", "data:text/html,x", "", "//nohost", "https://"):
            self.assertIsNone(engine.safe_url(bad), bad)
        self.assertEqual(engine.safe_url("https://a.example/p?utm_source=x&id=3#frag"), "https://a.example/p?id=3")

    def test_hindi_unicode(self):
        c = engine.parse_feed(rss([GOOD_HI]), FEED_HI)[0]
        self.assertEqual(c["title"], "संसद का बजट सत्र अगले सप्ताह से शुरू होगा")
        self.assertIn("बजट", engine.tokens(c["title"]))  # matras preserved, words not split
        self.assertGreater(engine.deva_ratio(c["title"]), 0.9)
        self.assertRegex(engine.make_slug("hi", c["title"]), r"^[0-9a-f]{10}$")

    def test_date_handling(self):
        self.assertEqual(engine.parse_date("2026-10-17T09:30:00+05:30"), datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc))
        self.assertIsNone(engine.parse_date("garbage")); self.assertIsNone(engine.parse_date(""))


class FetchTests(unittest.TestCase):
    def test_network_error_retries_then_fails(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")) as m:
            with self.assertRaises(engine.FetchError): engine.fetch_url("https://a.example/", retries=2, sleep=lambda s: None)
        self.assertEqual(m.call_count, 3)

    def test_timeout(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("t")):
            with self.assertRaises(engine.FetchError): engine.fetch_url("https://a.example/", retries=0)

    def test_http_404_not_retried_500_retried(self):
        e404 = urllib.error.HTTPError("https://a.example/", 404, "nf", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=e404) as m:
            with self.assertRaises(engine.FetchError): engine.fetch_url("https://a.example/", retries=2, sleep=lambda s: None)
        self.assertEqual(m.call_count, 1)
        e500 = urllib.error.HTTPError("https://a.example/", 500, "err", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=e500) as m:
            with self.assertRaises(engine.FetchError): engine.fetch_url("https://a.example/", retries=2, sleep=lambda s: None)
        self.assertEqual(m.call_count, 3)

    def test_unsafe_scheme_and_oversize(self):
        with self.assertRaises(engine.FetchError): engine.fetch_url("file:///etc/passwd")
        resp = mock.MagicMock(); resp.__enter__.return_value.read.return_value = b"x" * (engine.MAX_FEED_BYTES + 1)
        with mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(engine.FetchError): engine.fetch_url("https://a.example/", retries=0)

    def test_success(self):
        resp = mock.MagicMock(); resp.__enter__.return_value.read.return_value = b"<rss/>"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            self.assertEqual(engine.fetch_url("https://a.example/"), b"<rss/>")


class QualityTests(unittest.TestCase):
    cfg = {**engine.DEFAULTS}

    def cand(self, xml=GOOD_EN, feed=FEED_EN):
        return engine.parse_feed(rss([xml]), feed)[0]

    def test_good_candidates_pass(self):
        self.assertEqual(engine.check_quality(self.cand(), self.cfg, NOW), [])
        self.assertEqual(engine.check_quality(self.cand(GOOD_HI, FEED_HI), self.cfg, NOW), [])

    def test_rejections(self):
        c = self.cand(); c["published"] = None
        self.assertIn("no_publication_date", engine.check_quality(c, engine.load_config(ROOT / "config.json"), NOW))
        c = self.cand(item("Old story that nobody needs now", pub="Mon, 01 Jun 2026 04:00:00 GMT", related=[("a", "https://o.example/1", "O1"), ("b", "https://o.example/2", "O2")]))
        self.assertIn("too_old", engine.check_quality(c, self.cfg, NOW))
        c = self.cand(item("You won't believe what happened next!!"))
        self.assertTrue({"clickbait_title", "insufficient_value"} <= set(engine.check_quality(c, self.cfg, NOW)))
        c = self.cand(item("Unconfirmed rumour about minister resigning", related=[("a", "https://o.example/1", "O1"), ("b", "https://o.example/2", "O2")]))
        self.assertIn("unsupported_speculation", engine.check_quality(c, self.cfg, NOW))
        c = self.cand(item("Short"))
        self.assertIn("title_length", engine.check_quality(c, self.cfg, NOW))
        c = self.cand(GOOD_HI, FEED_EN)
        self.assertIn("language_mismatch", engine.check_quality(c, self.cfg, NOW))
        c = self.cand(item("Single source story without any detail", link="javascript:x"))
        self.assertIn("no_valid_source", engine.check_quality(c, self.cfg, NOW))

    def test_duplicate_detection(self):
        c = self.cand(); known = [{"id": "x", "lang": "en", "title": "Parliament budget session to begin next week today"}]
        self.assertTrue(engine.is_duplicate(c, known, 0.6))
        self.assertFalse(engine.is_duplicate(c, [{"id": "y", "lang": "en", "title": "Cricket final in Mumbai"}], 0.6))
        self.assertFalse(engine.is_duplicate(c, [{"id": "z", "lang": "hi", "title": c["title"]}], 0.6))  # other language: not a duplicate


class JsonSafetyTests(unittest.TestCase):
    def setUp(self): self.d = Path(tempfile.mkdtemp())

    def test_missing_returns_default(self):
        self.assertEqual(engine.load_posts(self.d / "nope.json"), {"version": 1, "posts": []})

    def test_corrupt_json_backed_up_not_lost(self):
        p = self.d / "posts.json"; p.write_text("{broken", encoding="utf-8")
        self.assertEqual(engine.load_posts(p)["posts"], [])
        self.assertEqual(len(list(self.d.glob("posts.json.corrupt-*"))), 1)

    def test_wrong_structure_treated_invalid(self):
        p = self.d / "published.json"; p.write_text('["a"]', encoding="utf-8")
        self.assertEqual(engine.load_published(p)["items"], [])

    def test_atomic_write_failure_keeps_original(self):
        p = self.d / "f.json"; engine.write_json(p, {"a": 1})
        with mock.patch("os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError): engine.write_json(p, {"a": 2})
        self.assertEqual(json.loads(p.read_text()), {"a": 1}); self.assertEqual(list(self.d.glob("*.tmp")), [])

    def test_unicode_roundtrip_and_permissions(self):
        p = self.d / "u.json"; engine.write_json(p, {"t": "हिंदी"})
        self.assertIn("हिंदी", p.read_text(encoding="utf-8")); self.assertEqual(os.stat(p).st_mode & 0o777, 0o644)

    def test_bad_config(self):
        (self.d / "config.json").write_text("{nope", encoding="utf-8")
        with self.assertRaises(engine.ConfigError): engine.load_config(self.d / "config.json")
        (self.d / "config.json").write_text(json.dumps({"feeds": [{"url": "ftp://x", "lang": "en", "category": "india"}]}))
        with self.assertRaises(engine.ConfigError): engine.load_config(self.d / "config.json")
        with self.assertRaises(engine.ConfigError): engine.load_config(self.d / "missing.json")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.fx = fetcher_for({FEED_EN["url"]: rss([GOOD_EN]), FEED_HI["url"]: rss([GOOD_HI])})

    def run_(self, fx=None, **kw): return engine.run(self.root, fetcher=fx or self.fx, now=NOW, **kw)

    def test_end_to_end_pipeline(self):
        s = self.run_()
        self.assertEqual(len(s["published"]), 2)
        posts = json.loads((self.root / "posts.json").read_text(encoding="utf-8"))["posts"]
        pub = json.loads((self.root / "published.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual({p["lang"] for p in posts}, {"en", "hi"}); self.assertEqual({p["id"] for p in posts}, {i["id"] for i in pub})
        for p in posts:
            self.assertTrue(engine.valid_post(p)); page = (self.root / p["url"]).read_text(encoding="utf-8")
            self.assertIn(p["sources"][0]["url"], page); self.assertIn('application/ld+json', page)
        listing = (self.root / "en/index.html").read_text(encoding="utf-8")
        self.assertIn("Parliament budget session", listing); self.assertIn("data-cat=\"india\"", listing)
        self.assertIn("बजट", (self.root / "hi/index.html").read_text(encoding="utf-8"))
        self.assertIn("/en/posts/", (self.root / "sitemap.xml").read_text(encoding="utf-8"))  # substantive test fixtures are indexable
        self.assertNotIn('content="noindex,follow"', self.root.joinpath(posts[0]["url"]).read_text(encoding="utf-8"))

    def test_idempotent_rerun(self):
        self.run_(); before = {p: (self.root / p).read_bytes() for p in ("posts.json", "published.json", "sitemap.xml")}
        s = self.run_()
        self.assertEqual(s["published"], []); self.assertGreaterEqual(s["duplicates"], 2)
        self.assertEqual(before, {p: (self.root / p).read_bytes() for p in before})

    def test_existing_posts_preserved(self):
        self.run_()
        first = json.loads((self.root / "posts.json").read_text(encoding="utf-8"))["posts"]
        other = item("Chandrayaan team shares new lunar images", related=[("Chandrayaan team shares new lunar images", "https://n.example/1", "NDTV"), ("New lunar images from Chandrayaan", "https://n2.example/2", "WION")], link="https://n.example/1", publisher="NDTV")
        fx = fetcher_for({FEED_EN["url"]: rss([other]), FEED_HI["url"]: rss([])})
        self.assertEqual(len(self.run_(fx)["published"]), 1)
        after = json.loads((self.root / "posts.json").read_text(encoding="utf-8"))["posts"]
        self.assertEqual(after[: len(first)], first); self.assertEqual(len(after), len(first) + 1)

    def test_failed_feed_does_not_stop_others(self):
        fx = fetcher_for({FEED_EN["url"]: engine.FetchError("boom"), FEED_HI["url"]: rss([GOOD_HI])})
        s = self.run_(fx); self.assertEqual((s["feeds_ok"], s["feeds_failed"], len(s["published"])), (1, 1, 1))

    def test_malformed_feed_does_not_stop_others(self):
        fx = fetcher_for({FEED_EN["url"]: b"<rss><broken", FEED_HI["url"]: rss([GOOD_HI])})
        self.assertEqual(len(self.run_(fx)["published"]), 1)

    def test_all_feeds_fail_changes_nothing_and_exits_nonzero(self):
        fx = fetcher_for({FEED_EN["url"]: engine.FetchError("x"), FEED_HI["url"]: engine.FetchError("y")})
        s = self.run_(fx); self.assertTrue(s["all_feeds_failed"]); self.assertEqual(s["published"], [])
        self.assertFalse((self.root / "posts.json").exists()); self.assertFalse((self.root / "published.json").exists())
        cfg = json.loads((self.root / "config.json").read_text()); cfg["retries"] = 0
        (self.root / "config.json").write_text(json.dumps(cfg))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            self.assertEqual(engine.main(["--root", str(self.root)]), 2)  # CI must fail visibly

    def test_bad_config_exit_code(self):
        (self.root / "config.json").write_text("{bad")
        self.assertEqual(engine.main(["--root", str(self.root)]), 3)

    def test_failed_quality_check_blocks_publication(self):
        bad = item("You won't believe this shocking result!!")
        fx = fetcher_for({FEED_EN["url"]: rss([bad]), FEED_HI["url"]: rss([])})
        s = self.run_(fx); self.assertEqual(s["published"], []); self.assertTrue(s["rejected"])
        self.assertFalse((self.root / "published.json").exists())

    def test_one_failing_topic_does_not_destroy_batch(self):
        second = item("Chandrayaan team shares new lunar images", related=[("x", "https://n.example/1", "NDTV"), ("y", "https://n2.example/2", "WION")], link="https://n.example/1", publisher="NDTV")
        fx = fetcher_for({FEED_EN["url"]: rss([GOOD_EN, second]), FEED_HI["url"]: rss([])})
        real = engine.build_post
        def flaky(c, now):
            if "Chandrayaan" in c["title"]: raise RuntimeError("generation bug")
            return real(c, now)
        with mock.patch.object(engine, "build_post", flaky):
            s = self.run_(fx)
        self.assertEqual(s["errors"], 1); self.assertEqual(len(s["published"]), 1)

    def test_interrupted_run_is_recoverable(self):
        real = engine.write_json
        def crash_on_history(path, data):
            if Path(path).name == "published.json": raise OSError("killed")
            return real(path, data)
        with mock.patch.object(engine, "write_json", crash_on_history):
            with self.assertRaises(OSError): self.run_()
        self.assertEqual(len(json.loads((self.root / "posts.json").read_text(encoding="utf-8"))["posts"]), 2)
        self.assertFalse((self.root / "published.json").exists())  # history not falsely marked
        s = self.run_(); self.assertEqual(s["published"], [])  # posts.json alone prevents duplicates
        self.assertEqual(len(json.loads((self.root / "posts.json").read_text(encoding="utf-8"))["posts"]), 2)

    def test_max_new_posts_cap_and_dry_run(self):
        cfg = json.loads((self.root / "config.json").read_text()); cfg["max_new_posts_per_run"] = 1
        (self.root / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(len(self.run_(dry_run=True)["published"]), 1); self.assertFalse((self.root / "posts.json").exists())
        self.assertEqual(len(self.run_()["published"]), 1)

    def test_xss_in_titles_is_escaped(self):
        evil = item("<script>alert(1)</script> budget session begins", related=[("a", "https://o.example/1", "O1"), ("b", "https://o.example/2", "O2")])
        fx = fetcher_for({FEED_EN["url"]: rss([evil.replace("<script>", "&lt;script&gt;").replace("</script>", "&lt;/script&gt;")]), FEED_HI["url"]: rss([])})
        self.run_(fx)
        import re
        for f in (self.root / "en").rglob("*.html"):
            h = f.read_text(encoding="utf-8"); self.assertNotIn("<script>alert(1)", h)
            for blob in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, re.S): self.assertNotIn("<", blob)
            self.assertEqual(h.count("<script"), h.count("</script>"))

    def test_generated_site_passes_html_checks(self):
        import htmlcheck
        self.run_(); cfg = engine.load_config(self.root / "config.json")
        self.assertEqual(htmlcheck.check_tree(self.root, cfg["site_url"]), [])

    def test_check_detects_stale_files(self):
        self.run_(); self.assertTrue(engine.check_generated(self.root))
        (self.root / "en/index.html").write_text("stale", encoding="utf-8"); self.assertFalse(engine.check_generated(self.root))

    def test_path_traversal_in_stored_post_is_ignored(self):
        bad = {"id": "en-1", "lang": "en", "slug": "../../evil", "url": "en/posts/../../evil.html", "title": "t", "summary": "s", "category": "india",
               "published_at": NOW.isoformat(), "sources": [{"url": "https://a.example/", "title": "t", "publisher": "p"}]}
        self.assertFalse(engine.valid_post(bad))



class StructureAndReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = make_root()

    def go(self, en, hi=None):
        fx = fetcher_for({FEED_EN["url"]: rss(en), FEED_HI["url"]: rss(hi or [])})
        return engine.run(self.root, fetcher=fx, now=NOW)

    def page_of(self, lang):
        posts = json.loads((self.root / "posts.json").read_text(encoding="utf-8"))["posts"]
        p = next(p for p in posts if p["lang"] == lang)
        return (self.root / p["url"]).read_text(encoding="utf-8")

    def test_article_sections_en_and_hi(self):
        self.go([GOOD_EN], [GOOD_HI])
        en, hi = self.page_of("en"), self.page_of("hi")
        for h in ("What happened", "What each source reports", "What is not confirmed"): self.assertIn(f"<h2>{h}</h2>", en)
        for h in ("क्या हुआ", "हर स्रोत क्या रिपोर्ट करता है", "क्या पुष्टि नहीं हुई है"): self.assertIn(f"<h2>{h}</h2>", hi)
        self.assertIn("<strong>Mint</strong>", en); self.assertIn("https://other.example/b2", en)  # publisher name + original link
        self.assertNotIn("Background", en)  # no invented background section

    def test_snippet_relevance_rejects_unrelated_publisher_text(self):
        title = "Parliament budget session to begin next week"
        self.assertTrue(engine._snippet_matches_story(
            "Parliament session timetable and budget agenda were discussed by officials.", title))
        self.assertFalse(engine._snippet_matches_story(
            "Weather update predicts heavy rainfall across coastal districts this weekend.", title))

        candidate = {
            "title": title,
            "summary": "",
            "sources": [{"title": title, "url": "https://publisher.example/story", "publisher": "Publisher"}],
        }
        cfg = {**engine.DEFAULTS, "timeout_seconds": 5}
        unrelated = (
            b'<html><head><meta name="description" content="Weather update predicts heavy rainfall '
            b'across coastal districts this weekend and authorities issued a public advisory."/></head></html>'
        )
        with mock.patch.object(engine, "fetch_source_page",
                               return_value=("https://publisher.example/story", unrelated)):
            engine.enrich_candidate_summary(candidate, cfg)
        self.assertEqual(candidate["summary"], "")

    def test_enrichment_collects_multiple_relevant_details_but_skips_unrelated_paragraphs(self):
        candidate = {
            "title": "Parliament budget session to begin next week",
            "summary": "",
            "sources": [{"title": "Parliament budget session to begin next week",
                         "url": "https://publisher.example/story", "publisher": "Publisher"}],
        }
        page = b"""<html><head>
        <meta name="description" content="Parliament budget session timetable was confirmed by officials for next week.">
        </head><body>
        <p>Officials said Parliament budget session will begin next week after the committee finalises the agenda.</p>
        <p>Weather update predicts heavy rainfall across coastal districts this weekend and authorities issued a public advisory.</p>
        <p>The budget session agenda is expected to include questions on public spending and ministry allocations.</p>
        </body></html>"""
        cfg = {**engine.DEFAULTS, "timeout_seconds": 5}
        with mock.patch.object(engine, "fetch_source_page",
                               return_value=("https://publisher.example/story", page)):
            engine.enrich_candidate_summary(candidate, cfg)
        self.assertIn("committee finalises the agenda", candidate["summary"])
        self.assertNotIn("Weather update", candidate["summary"])
        # The most relevant article paragraph wins over the meta description.
        # Only one short excerpt per publisher is retained.
        self.assertLessEqual(len(candidate["summary"].split()), 30)

    def test_enrichment_finds_relevant_paragraph_after_first_eight(self):
        candidate = {
            "title": "Inter Kashi Odisha FC seek first points",
            "summary": "",
            "sources": [{"title": "Odisha FC remains unbeaten against Inter Kashi",
                         "url": "https://publisher.example/story", "publisher": "Sportstar"}],
        }
        unrelated = "".join(
            f"<p>Unrelated cricket update number {i} reports a separate match and team result in another tournament.</p>"
            for i in range(12)
        )
        page = ("<html><head><meta name='description' content='A short unrelated sports homepage description that is not about this fixture.'></head>"
                "<body>" + unrelated +
                "<p>Odisha FC remains unbeaten against Inter Kashi across all competitions, having won three nil in the Super Cup and drawn goalless in the previous meeting.</p>"
                "</body></html>").encode()
        cfg = {**engine.DEFAULTS, "timeout_seconds": 5}
        with mock.patch.object(engine, "fetch_source_page",
                               return_value=("https://publisher.example/story", page)):
            engine.enrich_candidate_summary(candidate, cfg)
        self.assertIn("Odisha FC remains unbeaten against Inter Kashi", candidate["summary"])
        self.assertNotIn("Unrelated cricket update", candidate["summary"])

    def test_attributed_source_snippets_render_next_to_their_source_links(self):
        c = engine.parse_feed(rss([GOOD_EN]), FEED_EN)[0]
        post = engine.build_post(c, NOW)
        post["source_summary"] = (
            "The Hindu: Officials announced the dates on Tuesday, and the session is scheduled to begin next week. "
            "The agenda will be shared ahead of the opening day. " + "\n\n" +
            "Mint: The ministry released a separate notice confirming the timetable for the upcoming session."
        )
        cfg = engine.load_config(ROOT / "config.json")
        html = engine.article_page(cfg, post, [post])
        self.assertIn('<p class="source-detail">Officials announced the dates on Tuesday', html)
        self.assertIn('<p class="source-detail">The ministry released a separate notice', html)
        self.assertLess(html.index("<strong>The Hindu</strong>", html.index("What each source reports")), html.index("Officials announced the dates", html.index("What each source reports")))
        self.assertIn("What each source reports", html)

    def test_differing_headlines_are_flagged_as_unconfirmed(self):
        odd = item("Parliament budget session to begin next week", related=[
            ("Parliament budget session to begin next week", "https://news.example/a1", "The Hindu"),
            ("Opposition plans walkout over farm laws", "https://other.example/b2", "Mint")])
        self.go([odd])
        self.assertIn("word the story differently", self.page_of("en")); self.assertIn("Mint", self.page_of("en"))

    def test_single_source_flagged_when_summary_carries_value(self):
        long = "Detailed summary text from the feed about the session timetable and agenda. " * 7
        one = f'<item><title>Parliament budget session to begin next week - The Hindu</title><link>https://news.example/a1</link><pubDate>Sat, 17 Oct 2026 04:00:00 GMT</pubDate><description>{long}</description><source url="https://x.example">The Hindu</source></item>'
        self.assertEqual(len(self.go([one])["published"]), 1)
        self.assertIn("Only one publisher is cited", self.page_of("en"))

    def test_insufficient_info_goes_to_review_not_published(self):
        thin = item("Minister inaugurates new bridge in district")
        s = self.go([thin])
        self.assertEqual(s["published"], []); self.assertEqual(s["queued_for_review"], 1)
        rev = json.loads((self.root / "review.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(len(rev), 1); self.assertEqual(rev[0]["reasons"], ["insufficient_value"])
        self.assertFalse((self.root / "posts.json").exists())
        self.assertEqual(self.go([thin])["queued_for_review"], 0)  # rerun does not duplicate the queue entry
        self.assertEqual(len(json.loads((self.root / "review.json").read_text())["items"]), 1)

    def test_multi_source_headlines_without_substantive_summary_go_to_review(self):
        thin = item(
            "Minister announces a new transport project today",
            related=[
                ("Minister announces a new transport project today", "https://a.example/1", "Publisher A"),
                ("Transport project announced by officials", "https://b.example/2", "Publisher B"),
                ("Officials outline transport project", "https://c.example/3", "Publisher C"),
            ],
            include_detail=False,
        )
        stats = self.go([thin])
        self.assertEqual(stats["published"], [])
        self.assertEqual(stats["queued_for_review"], 1)
        self.assertIn("insufficient_value", stats["rejected"][0]["reasons"])
        self.assertFalse((self.root / "posts.json").exists())

    def test_other_rejections_are_not_queued(self):
        self.assertEqual(self.go([item("You won't believe this shocking result!!")])["queued_for_review"], 0)
        self.assertFalse((self.root / "review.json").exists())

    def test_approve_publishes_and_clears_queue(self):
        self.go([item("Minister inaugurates new bridge in district")])
        rid = json.loads((self.root / "review.json").read_text(encoding="utf-8"))["items"][0]["id"]
        post = engine.approve(self.root, rid, now=NOW)
        self.assertTrue((self.root / post["url"]).exists()); self.assertEqual(json.loads((self.root / "review.json").read_text())["items"], [])
        self.assertIn(rid, (self.root / "published.json").read_text())
        with self.assertRaises(KeyError): engine.approve(self.root, rid, now=NOW)
        self.assertEqual(engine.main(["--root", str(self.root), "--approve", "en-missing"]), 4)

    def test_stale_review_items_expire(self):
        self.go([item("Minister inaugurates new bridge in district")])
        later = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        fx = fetcher_for({FEED_EN["url"]: rss([item("Another thin story about a village fair", pub="Fri, 30 Oct 2026 04:00:00 GMT")]), FEED_HI["url"]: rss([])})
        engine.run(self.root, fetcher=fx, now=later)
        titles = [i["title"] for i in json.loads((self.root / "review.json").read_text(encoding="utf-8"))["items"]]
        self.assertEqual(titles, ["Another thin story about a village fair"])



class TeaserAndIndexingTests(unittest.TestCase):
    def test_google_news_boilerplate_is_not_treated_as_a_story_summary(self):
        filler = "Comprehensive up-to-date news coverage, aggregated from sources all over the world by Google News."
        xml = item("Cyberleek shares new GTA 6 footage today", extra=f"<description>{filler}</description>")
        c = engine.parse_feed(rss([xml]), FEED_EN)[0]
        self.assertEqual(c["summary"], "")
        self.assertTrue(c["boilerplate_summary"])
        self.assertIn("insufficient_value", engine.check_quality(c, engine.load_config(ROOT / "config.json"), NOW))

    def test_tidy_removes_read_more_and_cut_words(self):
        raw = "Tesla's long-delayed next-generation Roadster has taken centr ..\n\nRead more at:\nhttp://timesofindia.indiatimes.com/articleshow/1.cms?utm_source=x&utm_medium=text"
        self.assertEqual(engine.tidy(raw), "Tesla's long-delayed next-generation Roadster has taken…")
        self.assertEqual(engine.tidy("Plain text stays. https://x.example/a"), "Plain text stays.")
        self.assertEqual(engine.tidy(engine.tidy(raw)), engine.tidy(raw))  # idempotent
        self.assertEqual(engine.tidy("भारत ने मैच जीता। Read more at: https://a.example"), "भारत ने मैच जीता।")

    def test_feed_teaser_is_cleaned_at_parse_time(self):
        d = ("Roadster demo expected to draw crowds this month with new battery details announced by the maker today .. "
             "Read more at: http://t.example/1?utm_source=a")
        x = f"<item><title>Roadster demo draws crowds - ToI</title><link>https://t.example/1</link><pubDate>Sat, 17 Oct 2026 04:00:00 GMT</pubDate><description>{d}</description><source url='https://t.example'>ToI</source></item>"
        c = engine.parse_feed(rss([x]), FEED_EN)[0]
        self.assertNotIn("Read more", c["summary"]); self.assertNotIn("http", c["summary"]); self.assertTrue(c["summary"].endswith("…"))

    def test_only_substantive_pages_are_indexable(self):
        root = make_root(); cfg = engine.load_config(root / "config.json")
        three = item("Chandrayaan team shares new lunar images", related=[("a", "https://n.example/1", "NDTV"), ("b", "https://n2.example/2", "WION"), ("c", "https://n3.example/3", "Mint")], link="https://n.example/1", publisher="NDTV")
        long = "Detailed summary text from the feed about the session timetable and agenda. " * 7
        one = f'<item><title>Parliament budget session to begin next week - The Hindu</title><link>https://news.example/a1</link><pubDate>Sat, 17 Oct 2026 04:00:00 GMT</pubDate><description>{long}</description><source url="https://x.example">The Hindu</source></item>'
        fx = fetcher_for({FEED_EN["url"]: rss([GOOD_EN, three, one.replace("Parliament budget session to begin next week", "Rain disrupts metro services across the city")]), FEED_HI["url"]: rss([])})
        engine.run(root, fetcher=fx, now=NOW)
        posts = {p["title"]: p for p in json.loads((root / "posts.json").read_text(encoding="utf-8"))["posts"]}
        smap = (root / "sitemap.xml").read_text(encoding="utf-8")
        for title, want in (("Chandrayaan team shares new lunar images", True), ("Rain disrupts metro services across the city", True), ("Parliament budget session to begin next week", True)):
            p = posts[title]; html = (root / p["url"]).read_text(encoding="utf-8")
            self.assertEqual(engine.indexable(p, cfg), want, title); self.assertEqual(p["url"] in smap, want, title)
            self.assertEqual('content="noindex,follow"' not in html, want, title)
        self.assertNotIn('noindex', (root / "en/index.html").read_text(encoding="utf-8"))  # listing pages stay indexable

    def test_thin_digests_are_hidden_from_public_lists_but_keep_noindex_page(self):
        root = make_root()
        cfg = engine.load_config(root / "config.json")
        post = engine.build_post(engine.parse_feed(rss([GOOD_EN]), FEED_EN)[0], NOW)
        post["source_summary"] = "Coverage from 2 sources: The Hindu, Mint."
        post["summary"] = "Coverage from 2 sources: The Hindu, Mint."
        files = engine.render_site(root, cfg, [post], write=False)
        self.assertNotIn(post["title"], files["index.html"])
        self.assertNotIn(post["title"], files["en/index.html"])
        article = files[post["url"]]
        self.assertIn('content="noindex,follow"', article)
        self.assertIn(post["title"], article)

    def test_multiple_publishers_do_not_make_a_thin_page_indexable(self):
        cfg = {**engine.DEFAULTS}
        post = {
            "source_summary": "Coverage from 3 sources: NDTV, WION, Mint.",
            "sources": [
                {"publisher": "NDTV"}, {"publisher": "WION"}, {"publisher": "Mint"}
            ],
        }
        self.assertFalse(engine.indexable(post, cfg))

    def test_long_looking_but_under_60_word_teaser_is_not_indexable(self):
        cfg = {**engine.DEFAULTS}
        teaser = (
            "ISL Official: Inter Kashi, Odisha FC seek first points after opening defeats. "
            "Sportstar: Odisha FC remains unbeaten against Inter Kashi across all competitions, "
            "having won 3-0 in the Super Cup and drawn goalless in the…"
        )
        self.assertGreater(len(teaser), cfg["min_summary_chars"])
        self.assertLess(len(teaser.split()), 60)
        self.assertFalse(engine.indexable({"source_summary": teaser}, cfg))

    def test_sixty_word_quality_gate_requires_more_than_a_headline_pair(self):
        cfg = {**engine.DEFAULTS}
        summary = ("Indian Super League: Inter Kashi and Odisha FC look for their first points. "
                   "Sportstar: Odisha FC have previously faced Inter Kashi in the Super Cup. " * 2)
        self.assertGreater(len(summary), cfg["min_summary_chars"])
        self.assertLess(len(summary.split()), 60)
        self.assertFalse(engine.indexable({"source_summary": summary}, cfg))

    def test_ai_article_generation_skips_without_key_or_two_sources(self):
        evidence = [{"publisher": "One", "paragraphs": ["Reported fact goes here."]}]
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(engine.generate_ai_article("Headline", "en", evidence), "")
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                self.assertEqual(engine.generate_ai_article("Headline", "en", evidence), "")

    def test_ai_article_generation_allows_one_substantial_publisher_source(self):
        source_text = (
            "The club confirmed the head coach changed the starting lineup and training schedule. "
            "The team lost its opening match, and the coach said the squad is preparing for the next fixture. "
            "Training has focused on defensive shape, transitions, fitness, and set-piece routines. "
            "The club described the preparation timetable and explained which players joined the session. "
        ) * 3
        evidence = [{"publisher": "Official Club", "title": "Team prepares for next fixture",
                     "url": "https://club.example/story", "paragraphs": [source_text]}]
        paragraph = (
            "The club said the squad has been preparing for its next fixture after the opening match. "
            "The coach outlined training priorities and the team schedule. "
        ) * 5
        fake_body = {"choices": [{"message": {"content": "\n\n".join([paragraph] * 3)}}]}
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _limit): return json.dumps(fake_body).encode("utf-8")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
             mock.patch.object(engine.urllib.request, "urlopen", return_value=FakeResponse()):
            result = engine.generate_ai_article("Team prepares for next fixture", "en", evidence)
        self.assertGreaterEqual(len(result.split()), 180)
        self.assertEqual(len(result.split("\n\n")), 3)

    def test_ai_article_generation_uses_grounded_prompt_and_accepts_long_paragraphs(self):
        evidence = [
            {"publisher": "Source One", "title": "Headline one", "url": "https://one.example/story",
             "paragraphs": ["The club confirmed the coach changed the starting lineup and training schedule."]},
            {"publisher": "Source Two", "title": "Headline two", "url": "https://two.example/story",
             "paragraphs": ["The team lost its opening match and the coach said the squad is preparing for the next fixture."]},
        ]
        paragraph = "The coach said the squad is working on its defensive shape before the next fixture. " * 25
        article = paragraph + "\n\n" + paragraph + "\n\n" + paragraph
        fake_body = {"choices": [{"message": {"content": article}}]}
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _limit): return json.dumps(fake_body).encode("utf-8")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
             mock.patch.object(engine.urllib.request, "urlopen", return_value=FakeResponse()) as mocked:
            result = engine.generate_ai_article("Team prepares for next fixture", "en", evidence)
        self.assertGreaterEqual(len(result.split("\n\n")), 3)
        self.assertGreaterEqual(len(result.split("\n\n")), 3)
        payload = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
        self.assertIn("ONLY facts explicitly present", payload["messages"][1]["content"])
        self.assertEqual(payload["model"], "gpt-4.1-mini")
    def test_attributed_excerpt_cap_allows_more_context_and_repairs_joined_location(self):
        excerpt = engine._short_attributed_text(
            "Early-Season MomentumPune: Inter Kashi FC and Odisha FC will look to improve."
        )
        self.assertIn("Momentum Pune:", excerpt)
        self.assertLessEqual(len(excerpt.removesuffix("…").split()), 30)

    def test_google_news_boilerplate_never_makes_a_page_indexable(self):
        cfg = {**engine.DEFAULTS}
        post = {
            "source_summary": "Comprehensive up-to-date news coverage, aggregated from sources all over the world by Google News.",
            "sources": [{"publisher": "A"}, {"publisher": "B"}, {"publisher": "C"}],
        }
        self.assertFalse(engine.indexable(post, cfg))

    def test_substantive_summary_remains_indexable(self):
        cfg = {**engine.DEFAULTS}
        summary = "Officials announced the project timeline and described the next steps for implementation. " * 6
        post = {"source_summary": summary, "sources": [{"publisher": "A"}]}
        self.assertTrue(engine.indexable(post, cfg))

    def test_stored_posts_with_teaser_debris_render_clean(self):
        root = make_root(); cfg = engine.load_config(root / "config.json")
        post = engine.build_post(engine.parse_feed(rss([GOOD_EN]), FEED_EN)[0], NOW)
        post["summary"] = post["source_summary"] = "Old stored teaser cut centr .. Read more at: http://t.example/x?utm_source=a"
        files = engine.render_site(root, cfg, [post], write=False)
        for page_html in (files["en/index.html"], files[post["url"]]):
            self.assertNotIn("Read more at", page_html); self.assertNotIn("utm_source", page_html)


class LanguageAndUnpublishTests(unittest.TestCase):
    def test_other_scripts_are_rejected_for_en_and_hi(self):
        cfg = {**engine.DEFAULTS}
        for title in ("పూలతేరుగా మూలవాగు కథనం ఇక్కడ", "વાંચો તમારું રાશિ ભવિષ્ય આજે", "പ്രിയദർശിനി ബസുകൾ യാത്രക്കാർ"):
            for feed in (FEED_EN, FEED_HI):
                c = engine.parse_feed(rss([item(title, related=[("a", "https://o.example/1", "O1"), ("b", "https://o.example/2", "O2")])]), feed)[0]
                self.assertIn("language_mismatch", engine.check_quality(c, cfg, NOW), (title, feed["lang"]))
        self.assertTrue(engine.language_ok("en", "IND vs WI 2026/27, 5th T20I Match Preview")); self.assertTrue(engine.language_ok("hi", "BCCI नहीं, खुद खर्च उठाएंगे शुभमन गिल"))
        self.assertFalse(engine.language_ok("en", "संसद का बजट सत्र")); self.assertFalse(engine.language_ok("hi", "Budget session next week"))

    def test_unpublish_backs_up_removes_and_prevents_regeneration(self):
        root = make_root(); fx = fetcher_for({FEED_EN["url"]: rss([GOOD_EN]), FEED_HI["url"]: rss([GOOD_HI])})
        engine.run(root, fetcher=fx, now=NOW)
        posts = json.loads((root / "posts.json").read_text(encoding="utf-8"))["posts"]; victim = next(p for p in posts if p["lang"] == "en")
        engine.unpublish(root, victim["id"])
        self.assertFalse((root / victim["url"]).exists())
        left = json.loads((root / "posts.json").read_text(encoding="utf-8"))["posts"]
        self.assertEqual([p["id"] for p in left], [p["id"] for p in posts if p["id"] != victim["id"]])
        self.assertEqual(json.loads((root / "unpublished.json").read_text(encoding="utf-8"))["items"][0]["id"], victim["id"])
        self.assertNotIn(victim["url"], (root / "sitemap.xml").read_text(encoding="utf-8"))
        self.assertEqual(engine.run(root, fetcher=fx, now=NOW)["published"], [])  # not regenerated
        for bad in ("../../etc/passwd", "en-zzz", "", "en-0123456789; rm -rf"):
            with self.assertRaises(ValueError): engine.unpublish(root, bad)
        with self.assertRaises(KeyError): engine.unpublish(root, "en-0123456789")
        self.assertEqual(engine.main(["--root", str(root), "--unpublish", "bad id"]), 4)

class SourceExtractionTests(unittest.TestCase):
    def test_google_news_token_embedded_publisher_url(self):
        raw = b"\x12https://publisher.example/news/story"
        token = engine.base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        wrapped = f"https://news.google.com/rss/articles/{token}?oc=5"
        self.assertEqual(engine.embedded_source_url(wrapped), "https://publisher.example/news/story")

    def test_source_parser_extracts_metadata_and_article_paragraphs_only(self):
        parser = engine._MetaDescription()
        parser.feed("""
            <html><head><meta property="og:description" content="A sufficiently detailed publisher description about this reported event and its key context."></head>
            <body><nav><p>Navigation text that must not appear in the summary because it is not story content.</p></nav>
            <article><p>This is a detailed article paragraph with enough words to qualify as a useful snippet from a publisher page.</p>
            <p>Another paragraph contains additional details about the event and what officials said during the briefing.</p></article>
            <script><p>Ignore this hidden script content because it is not a readable article paragraph.</p></script></body></html>
        """)
        self.assertTrue(parser.descriptions)
        self.assertEqual(len(parser.paragraphs), 2)
        self.assertNotIn("Navigation text", " ".join(parser.paragraphs))
        self.assertNotIn("hidden script", " ".join(parser.paragraphs))

    def test_source_parser_extracts_jsonld_article_body_when_paragraphs_are_missing(self):
        parser = engine._MetaDescription()
        parser.feed("""
            <html><head>
            <script type="application/ld+json">
            {"@context":"https://schema.org","@type":"NewsArticle",
             "headline":"A reported event",
             "description":"A publisher description with enough detail to be useful for readers following this event.",
             "articleBody":"Officials announced the project on Monday. The department said construction will begin in November and the first phase will connect three districts. The next review meeting is scheduled for Friday."}
            </script></head><body><div class="paywall">Subscribe to continue reading</div></body></html>
        """)
        self.assertTrue(any("construction will begin in November" in p for p in parser.paragraphs))
        self.assertTrue(any("publisher description" in d for d in parser.descriptions))
        self.assertNotIn("Subscribe to continue", " ".join(parser.paragraphs))

    def test_source_excerpt_is_limited_to_22_words(self):
        long_text = " ".join(f"word{i}" for i in range(40))
        excerpt = engine._short_attributed_text(long_text)
        self.assertLessEqual(len(excerpt.removesuffix("…").split()), 30)
        self.assertTrue(excerpt.endswith("…"))

    def test_snippet_matches_publisher_headline_when_cluster_title_differs(self):
        # Same match story, but the publisher headline uses the team name rather
        # than the clustered headline's wording.
        snippet = "Odisha FC remains unbeaten against Inter Kashi across all competitions."
        self.assertTrue(engine._snippet_matches_story(
            snippet,
            "Inter Kashi, Odisha FC seek first points after opening defeats",
            "Odisha FC remain unbeaten against Inter Kashi",
        ))

    def test_unrelated_snippet_still_rejected_with_publisher_headline(self):
        self.assertFalse(engine._snippet_matches_story(
            "Heavy rain disrupts train services across the city.",
            "Inter Kashi, Odisha FC seek first points after opening defeats",
            "Odisha FC remain unbeaten against Inter Kashi",
        ))

    def test_google_news_boilerplate_tolerates_punctuation_variants(self):
        self.assertTrue(engine.is_boilerplate_summary("  Comprehensive up to date news coverage, aggregated from sources all over the world by Google News!  "))
    def test_boilerplate_is_not_accepted_as_source_excerpt(self):
        self.assertTrue(engine.is_boilerplate_summary(
            "Comprehensive up-to-date news coverage, aggregated from sources all over the world by Google News."
        ))


class RefreshOnlyTests(unittest.TestCase):
    def test_refresh_only_retries_long_but_thin_attributed_summary(self):
        root = make_root()
        post = engine.build_post(engine.parse_feed(rss([GOOD_EN]), FEED_EN)[0], NOW)
        # More than the character minimum, but only two short source excerpts.
        post["source_summary"] = (
            "ISL Official: Inter Kashi, Odisha FC seek first points after opening defeats. "
            "Sportstar: Odisha FC remains unbeaten against Inter Kashi across all competitions, "
            "having won three nil in the Super Cup and drawn goalless in the previous meeting."
        )
        post["summary"] = post["source_summary"][:200]
        engine.write_json(root / "posts.json", {"version": 1, "posts": [post]})
        engine.write_json(root / "published.json", {"version": 1, "items": []})
        self.assertGreater(len(post["source_summary"]), engine.DEFAULTS["min_summary_chars"])
        self.assertTrue(engine.summary_needs_refresh(post["source_summary"]))

        def fake_enrich(candidate, cfg, max_sources=5):
            candidate["summary"] = (
                "ISL Official: Inter Kashi and Odisha FC are looking for their first points "
                "after losing their opening matches. Sportstar: Odisha FC have previously "
                "beaten Inter Kashi in the Super Cup and played out a goalless draw."
            )

        with mock.patch.object(engine, "collect", side_effect=AssertionError("refresh-only must not collect feeds")), \
             mock.patch.object(engine, "enrich_candidate_summary", side_effect=fake_enrich):
            engine.run(root, now=NOW, refresh_only=True)
        stored = engine.load_posts(root / "posts.json")["posts"][0]
        self.assertIn("looking for their first points", stored["source_summary"])
        self.assertEqual(stored["summary_refresh_version"], engine.SUMMARY_REFRESH_VERSION)

    def test_refresh_priority_does_not_starve_never_attempted_thin_post(self):
        root = make_root()
        posts = []
        for i in range(20):
            post = engine.build_post(engine.parse_feed(rss([GOOD_EN]), FEED_EN)[0], NOW)
            post["id"] = f"en-{1000000000 + i}"
            post["source_summary"] = "Short"
            post["summary_refresh_version"] = engine.SUMMARY_REFRESH_VERSION
            post["summary_refresh_attempted_at"] = NOW.isoformat()
            post["published_at"] = f"2026-10-{i + 1:02d}T00:00:00+00:00"
            posts.append(post)
        untried = engine.build_post(engine.parse_feed(rss([GOOD_EN]), FEED_EN)[0], NOW)
        untried["id"] = "en-1999999999"
        untried["source_summary"] = "This is a still thin but longer teaser " * 5
        untried["summary"] = untried["source_summary"][:200]
        untried.pop("summary_refresh_version", None)
        untried.pop("summary_refresh_attempted_at", None)
        posts.append(untried)
        engine.write_json(root / "posts.json", {"version": 1, "posts": posts})
        engine.write_json(root / "published.json", {"version": 1, "items": []})
        attempted = []

        def fake_enrich(candidate, cfg, max_sources=5):
            attempted.append(candidate["title"])
            candidate["summary"] = "Publisher: Officials shared a detailed update and explained the next steps for the project timeline. " * 2

        with mock.patch.object(engine, "collect", side_effect=AssertionError("refresh-only must not collect feeds")), \
             mock.patch.object(engine, "enrich_candidate_summary", side_effect=fake_enrich):
            engine.run(root, now=NOW, refresh_only=True)
        self.assertEqual(attempted, [untried["title"]])

    def test_new_refresh_version_prioritizes_near_threshold_over_empty_recent_attempts(self):
        root = make_root()
        posts = []
        for i in range(20):
            post = engine.build_post(engine.parse_feed(rss([GOOD_EN]), FEED_EN)[0], NOW)
            post["id"] = f"en-{2000000000 + i}"
            post["source_summary"] = ""
            post["summary_refresh_version"] = 8
            post["summary_refresh_attempted_at"] = NOW.isoformat()
            post["published_at"] = f"2026-10-{i + 1:02d}T00:00:00+00:00"
            posts.append(post)
        target = engine.build_post(engine.parse_feed(rss([GOOD_EN]), FEED_EN)[0], NOW)
        target["id"] = "en-2999999999"
        target["source_summary"] = (
            "Indian Super League: Vaz Pinto and Purushothaman want improvements as both sides "
            "build early-season momentum in Pune. Sportstar: Inter Kashi and Odisha FC seek "
            "their first points after opening defeats, with Odisha FC previously unbeaten "
            "against Inter Kashi after a Super Cup win and goalless draw."
        )
        target["summary"] = target["source_summary"][:200]
        target["summary_refresh_version"] = 8
        target["summary_refresh_attempted_at"] = NOW.isoformat()
        posts.append(target)
        engine.write_json(root / "posts.json", {"version": 1, "posts": posts})
        engine.write_json(root / "published.json", {"version": 1, "items": []})
        attempted = []

        def fake_enrich(candidate, cfg, max_sources=5):
            attempted.append(candidate["title"])
            candidate["summary"] = (
                "Indian Super League: Coaches described the planned improvements and preparation. "
                "Sportstar: Both clubs are seeking their first points after opening defeats. "
            ) * 3

        with mock.patch.object(engine, "collect", side_effect=AssertionError("refresh-only must not collect feeds")), \
             mock.patch.object(engine, "enrich_candidate_summary", side_effect=fake_enrich):
            engine.run(root, now=NOW, refresh_only=True)
        self.assertEqual(attempted[0], target["title"])

    def test_refresh_only_updates_stored_posts_without_collecting_new_stories(self):
        root = make_root()
        post = engine.build_post(engine.parse_feed(rss([GOOD_EN]), FEED_EN)[0], NOW)
        post["summary"] = "Coverage from 2 sources: The Hindu, Mint."
        post["source_summary"] = ""
        engine.write_json(root / "posts.json", {"version": 1, "posts": [post]})
        engine.write_json(root / "published.json", {"version": 1, "items": []})

        def fake_enrich(candidate, cfg, max_sources=3):
            candidate["summary"] = "The Hindu: Officials published a detailed update about the session and confirmed the next meeting date."

        with mock.patch.object(engine, "collect", side_effect=AssertionError("refresh-only must not collect feeds")), \
             mock.patch.object(engine, "enrich_candidate_summary", side_effect=fake_enrich):
            stats = engine.run(root, now=NOW, refresh_only=True)

        stored = engine.load_posts(root / "posts.json")["posts"]
        self.assertEqual(stats["published"], [])
        self.assertEqual(len(stored), 1)
        self.assertIn("Officials published a detailed update", stored[0]["source_summary"])
        self.assertEqual(stored[0]["summary_refresh_version"], engine.SUMMARY_REFRESH_VERSION)


if __name__ == "__main__":
    unittest.main()
