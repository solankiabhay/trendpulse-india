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


def item(title, pub="Sat, 17 Oct 2026 04:00:00 GMT", publisher="The Hindu", related=(), link="https://news.example/a1", extra=""):
    li = "".join(f'<li><a href="{u}">{t}</a>&nbsp;&nbsp;<font color="#6f6f6f">{p}</font></li>' for t, u, p in related)
    d = f"<description><![CDATA[<ol>{li}</ol>]]></description>" if related else ""
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
        self.assertIn("no_publication_date", engine.check_quality(c, self.cfg, NOW))
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
        self.assertIn("/en/posts/", (self.root / "sitemap.xml").read_text(encoding="utf-8"))

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

    def test_differing_headlines_are_flagged_as_unconfirmed(self):
        odd = item("Parliament budget session to begin next week", related=[
            ("Parliament budget session to begin next week", "https://news.example/a1", "The Hindu"),
            ("Opposition plans walkout over farm laws", "https://other.example/b2", "Mint")])
        self.go([odd])
        self.assertIn("word the story differently", self.page_of("en")); self.assertIn("Mint", self.page_of("en"))

    def test_single_source_flagged_when_summary_carries_value(self):
        long = "Detailed summary text from the feed about the session timetable and agenda. " * 3
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


if __name__ == "__main__":
    unittest.main()
