#!/usr/bin/env python3
"""TrendPulse India pipeline (Python standard library only).

RSS/Atom discovery -> normalisation -> quality gate -> duplicate check ->
digest generation -> safe JSON writes -> static HTML/sitemap/robots rendering.

Articles are automatically compiled DIGESTS of source headlines and feed
summaries. They are not original reporting and are never presented as such.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("trendpulse")

MAX_FEED_BYTES = 5_000_000
SUMMARY_REFRESH_VERSION = 21
IST = timezone(timedelta(hours=5, minutes=30))
LANGS = ("en", "hi")
CATEGORIES = {
    "india": ("India", "भारत"), "world": ("World", "दुनिया"),
    "business": ("Business", "कारोबार"), "technology": ("Technology", "तकनीक"),
    "entertainment": ("Entertainment", "मनोरंजन"), "sports": ("Sports", "खेल"),
    "science": ("Science", "विज्ञान"), "health": ("Health", "स्वास्थ्य"),
    "trending": ("Trending", "ट्रेंडिंग"),
}
DEFAULTS = {
    "site_name": "TrendPulse India",
    "site_url": "https://example.github.io/trendpulse-india",
    "contact_email": "contact@example.com",
    "timeout_seconds": 15, "retries": 2, "retry_backoff_seconds": 2,
    "max_new_posts_per_run": 4, "max_age_hours": 72, "duplicate_similarity": 0.6,
    "min_title_chars": 12, "min_sources": 2, "min_summary_chars": 160,
    "max_list_items": 60, "index_min_publishers": 3, "feeds": [],
}
POST_URL_RE = re.compile(r"^(en|hi)/posts/[A-Za-z0-9-]+\.html$")
CLICKBAIT_RE = re.compile(
    r"you won'?t believe|shocking|must[- ]see|!{2,}|\?{2,}|चौंकाने वाला|आप यकीन नहीं करेंगे", re.I)
SPECULATION_RE = re.compile(r"\b(rumou?rs?|unconfirmed|unverified claim)\b|अफवाह|अफ़वाह|अपुष्ट", re.I)


class FeedError(Exception):
    pass


class FetchError(Exception):
    pass


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- text utils
def normalize_text(s) -> str:
    s = unicodedata.normalize("NFKC", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def tokens(text: str) -> set:
    """Word tokens; keeps Devanagari combining marks so Hindi words stay whole."""
    out, cur = [], []
    for ch in normalize_text(text).casefold():
        if unicodedata.category(ch)[0] in "LMN":
            cur.append(ch)
        elif cur:
            out.append("".join(cur)); cur = []
    if cur:
        out.append("".join(cur))
    return {t for t in out if len(t) > 1}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def safe_url(u):
    """Return a normalised http(s) URL or None (rejects javascript:, ftp:, data: ...)."""
    try:
        p = urllib.parse.urlsplit((u or "").strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not (k.lower().startswith("utm_") or k.lower() in ("fbclid", "gclid"))]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def host_of(u: str) -> str:
    return urllib.parse.urlsplit(u).netloc.removeprefix("www.")


URL_RE = re.compile(r"https?://\S+")


def tidy(text) -> str:
    """Remove feed teaser debris: 'Read more at: <url>', bare URLs and a mid-word cut before '..'/'…'."""
    ws = lambda x: re.sub(r"\s+", " ", x).strip()  # no NFKC here: it would turn "…" into "..." and break idempotency
    t = ws(html.unescape(text or ""))
    t = re.sub(r"Read more at:?\s*\S*", "", t, flags=re.I)
    t = ws(URL_RE.sub("", t))
    m = re.search(r"\s*\.{2,}\s*$", t)  # ".." means the feed cut mid-word; an existing "…" is left alone (idempotent)
    if m:
        t = t[: m.start()]
        t = (t.rsplit(" ", 1)[0] if " " in t else t).rstrip(" ,;:-") + "…"
    return t


def is_boilerplate_summary(text) -> bool:
    """Recognize generic Google News filler despite minor punctuation or whitespace changes."""
    t = normalize_text(text).casefold().strip(" .!:-")
    if not t:
        return False
    generic = (
        "comprehensive up-to-date news coverage, aggregated from sources all over the world by google news",
        "comprehensive up to date news coverage, aggregated from sources all over the world by google news",
    )
    return any(t == phrase or t.startswith(phrase + " ") for phrase in generic)

def summary_needs_refresh(text, min_words=60) -> bool:
    """Flag a long-looking but still thin digest for another source-enrichment attempt.

    Publisher labels and headlines can make a summary pass a character-count
    threshold while providing very little actual story context.
    """
    clean = tidy(text)
    if not clean or is_boilerplate_summary(clean):
        return True
    return len(clean.split()) < min_words

def indexable(p, cfg) -> bool:
    """Only summaries with enough readable context may enter search indexes.

    Character count alone is misleading when publisher names and headlines
    inflate a short teaser. Keep weak article pages noindex until enrichment
    adds at least 60 words of non-boilerplate source-attributed context.
    """
    summary = tidy(p.get("source_summary", "") or "")
    if len(summary) < cfg["min_summary_chars"] or summary_needs_refresh(summary):
        return False
    # Avoid indexing a page in the wrong language when its digest is clearly dominated by another script.
    lang = p.get("lang")
    if lang == "hi" and latin_ratio(summary) >= 0.40 and deva_ratio(summary) < 0.40:
        return False
    if lang == "en" and deva_ratio(summary) >= 0.40 and latin_ratio(summary) < 0.40:
        return False
    return True


def seo_description(text, max_chars=155) -> str:
    """Create a concise, non-stuffed search/social description from source text."""
    parts = [tidy(part) for part in re.split(r"\n\s*\n", text or "") if tidy(part)]
    if not parts:
        return "Source-linked news digest from TrendPulse India."
    value = parts[0]
    # Source summaries often start with "Publisher: headline"; the publisher is
    # already shown beside the source link and adds little to the search snippet.
    value = re.sub(r"^[^:]{2,70}:\s*", "", value)
    value = normalize_text(value)
    if len(value) > max_chars:
        value = value[:max_chars + 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return value

def clean_title(title: str, publisher: str = "") -> str:
    t = normalize_text(title)
    if publisher:
        for sep in (" - ", " | ", " – "):
            suf = sep + publisher
            if t.casefold().endswith(suf.casefold()):
                return t[: -len(suf)].strip()
    return t


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def deva_ratio(t: str) -> float:
    letters = [c for c in t if unicodedata.category(c)[0] in "LM"]
    return sum("\u0900" <= c <= "\u097f" for c in letters) / len(letters) if letters else 0.0


def latin_ratio(t: str) -> float:
    letters = [c for c in t if unicodedata.category(c)[0] == "L"]
    return sum(unicodedata.name(c, "").startswith("LATIN") for c in letters) / len(letters) if letters else 0.0

def paragraph_language(text: str, default_lang: str) -> str:
    """Identify clearly single-script excerpts so mixed-language pages remain accessible."""
    deva, latin = deva_ratio(text), latin_ratio(text)
    if default_lang == "hi" and latin >= 0.55 and deva < 0.20:
        return "en"
    if default_lang == "en" and deva >= 0.55 and latin < 0.20:
        return "hi"
    return default_lang


def render_paragraph(text: str, default_lang: str) -> str:
    """Render a source paragraph with a language override only when script evidence is strong."""
    part_lang = paragraph_language(text, default_lang)
    lang_attr = f' lang="{part_lang}"' if part_lang != default_lang else ""
    return f"<p{lang_attr}>{esc(text)}</p>"


def language_ok(lang: str, title: str) -> bool:
    """English titles must be mostly Latin script, Hindi titles mostly Devanagari (rejects Telugu, Gujarati, etc.)."""
    return latin_ratio(title) >= 0.7 if lang == "en" else deva_ratio(title) >= 0.4



def content_language_needs_refresh(text: str, lang: str) -> bool:
    """Detect a digest labelled Hindi that remains mostly Latin-script."""
    text = normalize_text(text)
    if not text:
        return lang == "hi"
    return lang == "hi" and deva_ratio(text) < 0.85 and latin_ratio(text) > 0.10


def translate_to_hindi(text: str) -> str:
    """Translate English text with the optional local Argos en→hi model.

    No remote translation API or paid key is used. If the model is unavailable,
    keep the original text so callers can safely omit it rather than inventing
    a translation.
    """
    text = normalize_text(text)
    if not text or deva_ratio(text) >= 0.35:
        return text
    try:
        import argostranslate.translate as argos_translate
        translated = normalize_text(argos_translate.translate(text, "en", "hi"))
        if translated and deva_ratio(translated) >= 0.20:
            return translated
    except Exception as exc:
        log.debug("local English-to-Hindi translation unavailable: %s", exc)
    return text


def clean_hindi_excerpt(text: str) -> str:
    """Keep Hindi-script source text; remove appended English headline translations.

    The no-key translation endpoint is not reliably reachable from GitHub Actions.
    Prefer genuine Hindi-source material to silently publishing English as Hindi.
    """
    text = normalize_text(text)
    if not text:
        return ""
    parts = re.split(r"\s*[-–—]\s*", text)
    while len(parts) > 1:
        tail = parts[-1]
        if deva_ratio(tail) < 0.20 and latin_ratio(tail) > 0.20:
            parts.pop()
        else:
            break
    text = " – ".join(parts).strip()
    return text if deva_ratio(text) >= 0.20 else ""



def make_id(lang: str, title: str) -> str:
    h = hashlib.sha1(" ".join(sorted(tokens(title))).encode("utf-8")).hexdigest()[:10]
    return f"{lang}-{h}"


def make_slug(lang: str, title: str) -> str:
    h = make_id(lang, title).split("-", 1)[1]
    asc = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    asc = re.sub(r"[^a-z0-9]+", "-", asc.lower()).strip("-")[:60].rstrip("-")
    return f"{asc}-{h}" if asc else h


# ---------------------------------------------------------------- file safety
def atomic_write(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp defaults to 0600; web files must be readable
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json(path, data) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_json(path, default, validator=None):
    """Load JSON. Corrupt/invalid files are copied to *.corrupt-<ts> and `default` is used."""
    p = Path(path)
    if not p.exists():
        return default
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if validator and not validator(data):
            raise ValueError("unexpected structure")
        return data
    except (ValueError, UnicodeDecodeError) as e:
        backup = p.with_name(f"{p.name}.corrupt-{int(time.time())}")
        shutil.copy2(p, backup)
        log.error("%s invalid (%s); backup saved as %s", p.name, e, backup.name)
        return default


def load_posts(path):
    ok = lambda d: isinstance(d, dict) and isinstance(d.get("posts"), list)
    return load_json(path, {"version": 1, "posts": []}, ok)


def load_published(path):
    ok = lambda d: isinstance(d, dict) and isinstance(d.get("items"), list)
    return load_json(path, {"version": 1, "items": []}, ok)


def load_config(path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config not found: {p}")
    try:
        user = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ConfigError(f"config is not valid JSON: {e}")
    cfg = {**DEFAULTS, **user}
    cfg["site_url"] = str(cfg["site_url"]).rstrip("/")
    if not cfg["site_url"].startswith(("http://", "https://")):
        raise ConfigError("site_url must start with http:// or https://")
    for f in cfg["feeds"]:
        if not safe_url(f.get("url")):
            raise ConfigError(f"feed has invalid url: {f}")
        if f.get("lang") not in LANGS or f.get("category") not in CATEGORIES:
            raise ConfigError(f"feed needs lang in {LANGS} and a known category: {f}")
    return cfg


# ---------------------------------------------------------------- fetching
def fetch_url(url, timeout=15, retries=2, backoff=2, sleep=time.sleep) -> bytes:
    if not safe_url(url):
        raise FetchError(f"refusing unsafe URL: {url!r}")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TrendPulseIndia/1.0 (+RSS reader)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(MAX_FEED_BYTES + 1)
            if len(data) > MAX_FEED_BYTES:
                raise FetchError("feed too large")
            return data
        except FetchError:
            raise
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if attempt < retries:
            sleep(backoff * (attempt + 1))
    raise FetchError(f"{url}: {last}")


# ---------------------------------------------------------------- parsing
def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el, name) -> str:
    for c in el:
        if _local(c.tag) == name and (c.text or "").strip():
            return c.text.strip()
    return ""


def _link(el) -> str:
    for c in el:
        if _local(c.tag) == "link":
            href = c.get("href")
            if href and c.get("rel", "alternate") == "alternate":
                return href
            if (c.text or "").strip():
                return c.text.strip()
    return ""


class _Desc(HTMLParser):
    """Extracts text and (url, title, publisher) anchors from feed description HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.anchors = [], []
        self._href, self._atext, self._font, self._ftext = None, [], False, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._atext = dict(attrs).get("href"), []
        elif tag == "font":
            self._font, self._ftext = True, []

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.anchors.append({"url": self._href, "title": normalize_text("".join(self._atext)), "publisher": ""})
            self._href = None
        elif tag == "font" and self._font:
            self._font = False
            if self.anchors and not self.anchors[-1]["publisher"]:
                self.anchors[-1]["publisher"] = normalize_text("".join(self._ftext))

    def handle_data(self, d):
        self.text.append(d)
        if self._href is not None:
            self._atext.append(d)
        if self._font:
            self._ftext.append(d)


def _parse_item(it, feed):
    raw_title = normalize_text(_text(it, "title"))
    if not raw_title:
        return None
    publisher = ""
    for ch in it:
        if _local(ch.tag) == "source":
            publisher = normalize_text(ch.text or "")
    pub = parse_date(_text(it, "pubDate") or _text(it, "published") or _text(it, "updated") or _text(it, "date"))
    d = _Desc()
    d.feed(_text(it, "description") or _text(it, "summary") or _text(it, "content"))
    sources, seen = [], set()

    def add(t, u, p):
        nu = safe_url(u)
        if nu and nu not in seen:
            seen.add(nu)
            sources.append({"title": normalize_text(t), "url": nu, "publisher": normalize_text(p) or host_of(nu)})

    news = [c for c in it if _local(c.tag) == "news_item"]
    keyword, title = None, clean_title(raw_title, publisher)
    if news:  # Google Trends item: the keyword is not a headline, use its first news item
        keyword = raw_title
        n0_pub = normalize_text(_text(news[0], "news_item_source"))
        title = clean_title(_text(news[0], "news_item_title"), n0_pub) or raw_title
        for n in news:
            add(_text(n, "news_item_title"), _text(n, "news_item_url"), _text(n, "news_item_source"))
    else:
        add(title, _link(it), publisher)
    for a in d.anchors:
        add(clean_title(a["title"], a["publisher"]), a["url"], a["publisher"])
    text = normalize_text("".join(d.text))
    for s in sorted([raw_title, publisher] + [a["title"] for a in d.anchors] + [a["publisher"] for a in d.anchors],
                    key=len, reverse=True):
        if s:
            text = text.replace(s, " ")
    text = tidy(normalize_text(text).strip(" -–—|:·,"))
    boilerplate_summary = is_boilerplate_summary(text)
    return {"title": title, "keyword": keyword, "published": pub,
            "summary": text if len(text) >= 40 and not boilerplate_summary else "",
            "boilerplate_summary": boilerplate_summary,
            "sources": sources[:6], "lang": feed["lang"], "category": feed["category"],
            "weight": feed.get("weight", 0), "feed": feed.get("name", feed["url"])}



class _MetaDescription(HTMLParser):
    """Extract publisher metadata and a few short, attributed article snippets."""

    SKIP_TAGS = {"script", "style", "nav", "footer", "aside", "form", "svg", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.descriptions = []
        self.paragraphs = []
        self.links = []
        self._paragraph = None
        self._anchor_href = None
        self._anchor_text = []
        self._skip_depth = 0
        self._jsonld_script = False
        self._script_data = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {str(k).lower(): normalize_text(v or "") for k, v in attrs}
        if tag == "meta":
            key = (values.get("name") or values.get("property") or "").casefold()
            if key in ("description", "og:description", "twitter:description"):
                value = values.get("content", "")
                if len(value) >= 40 and value not in self.descriptions:
                    self.descriptions.append(value[:500])
        if tag == "script" and values.get("type", "").casefold().split(";")[0].strip() == "application/ld+json":
            self._jsonld_script = True
            self._script_data = []
        if tag == "a" and self._skip_depth == 0:
            self._anchor_href = safe_url(dict(attrs).get("href", ""))
            self._anchor_text = []
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "p" and self._skip_depth == 0:
            self._paragraph = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "script" and self._jsonld_script:
            raw_json = "".join(self._script_data).strip()
            self._jsonld_script = False
            self._script_data = []
            try:
                data = json.loads(raw_json)
            except (ValueError, TypeError):
                data = None
            def visit(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        normalized_key = str(key).casefold().replace("_", "")
                        if isinstance(value, str) and len(value.strip()) >= 40:
                            clean = normalize_text(value)
                            if normalized_key in ("articlebody", "articlebodytext", "text"):
                                if clean not in self.paragraphs:
                                    self.paragraphs.append(clean[:1200])
                            elif normalized_key in ("description", "abstract"):
                                if clean not in self.descriptions:
                                    self.descriptions.append(clean[:500])
                        elif isinstance(value, (dict, list)):
                            visit(value)
                elif isinstance(node, list):
                    for child in node:
                        visit(child)
            if data is not None:
                visit(data)
        if tag == "a" and self._anchor_href:
            label = normalize_text(" ".join(self._anchor_text))
            self.links.append({"url": self._anchor_href, "text": label})
            self._anchor_href, self._anchor_text = None, []
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "p" and self._paragraph is not None:
            value = normalize_text(" ".join(self._paragraph))
            if len(value) >= 55 and value not in self.paragraphs:
                self.paragraphs.append(value[:1200])
            self._paragraph = None

    def handle_data(self, data):
        if self._jsonld_script:
            self._script_data.append(data)
        if self._skip_depth == 0:
            if self._paragraph is not None:
                self._paragraph.append(data)
            if self._anchor_href:
                self._anchor_text.append(data)


def fetch_source_page(url, timeout=8):
    """Fetch a publisher page and retain its final URL after redirects."""
    if not safe_url(url):
        raise FetchError(f"refusing unsafe source URL: {url!r}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; TrendPulseIndia/1.1; +https://solankiabhay.github.io/trendpulse-india/)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            final_url = safe_url(response.geturl()) or url
            data = response.read(MAX_FEED_BYTES + 1)
        if len(data) > MAX_FEED_BYTES:
            raise FetchError("publisher page too large")
        return final_url, data
    except FetchError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        raise FetchError(f"{url}: {e}") from e


def embedded_source_url(url):
    """Recover a publisher URL when a Google News RSS token contains it."""
    try:
        parsed = urllib.parse.urlsplit(url or "")
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if key.casefold() in ("url", "u", "target"):
                candidate = safe_url(value)
                if candidate and not host_of(candidate).casefold().endswith("google.com"):
                    return candidate
        match = re.search(r"/articles/([^/?#]+)", parsed.path)
        if match:
            token = match.group(1)
            token += "=" * ((4 - len(token) % 4) % 4)
            decoded = base64.urlsafe_b64decode(token.encode("ascii"))
            for found in re.findall(rb"https?://[^\x00-\x20\"'<>]+", decoded):
                candidate = safe_url(found.decode("utf-8", errors="ignore").rstrip(".,;)]}"))
                if candidate and not host_of(candidate).casefold().endswith("google.com"):
                    return candidate
    except (ValueError, UnicodeError, base64.binascii.Error):
        return None
    return None


def _short_attributed_text(text, max_words=30):
    """Keep a short, attributed excerpt from one publisher; never mirror a full article."""
    text = tidy(text)
    # Some publisher markup joins a headline and its location/update label.
    # Add a separator at clear lower-to-title-case boundaries before a colon.
    text = re.sub(r"([a-z]{2,})([A-Z][a-z]{2,}:)", r"\1 \2", text)
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(" ,;:-") + "…"
    return text


def _snippet_matches_story(snippet, title, source_title=""):
    """Reject unrelated excerpts, matching against both the clustered headline and publisher headline.

    Publishers often phrase the same story differently; source-title overlap helps
    retain relevant context without accepting arbitrary text from the page.
    """
    stop_words = {
        "about", "after", "amid", "among", "and", "are", "been", "before",
        "being", "from", "have", "into", "more", "over", "said", "says",
        "that", "the", "their", "there", "these", "they", "this", "those",
        "under", "was", "were", "what", "when", "where", "which", "while",
        "with", "will", "would", "your", "today", "latest", "update",
    }
    def meaningful_terms(text):
        return {word for word in tokens(text) if len(word) >= 4 and word not in stop_words}
    snippet_terms = meaningful_terms(snippet)
    story_terms = meaningful_terms(title) | meaningful_terms(source_title)
    return bool(story_terms & snippet_terms)


def source_is_preview(title: str) -> bool:
    """Whether a source headline is explicitly a preview or prediction."""
    return bool(re.search(r"\b(preview|predictions?|prediction)\b", normalize_text(title), re.I))


def source_title_reports_result(title: str) -> bool:
    """Whether a headline explicitly looks like match-result reporting."""
    return bool(re.search(r"\b(won|wins|beat|defeated|victory|result|scorecard|highlights)\b", normalize_text(title), re.I))


def is_generic_sports_boilerplate(text: str) -> bool:
    """Reject generic sports-organisation descriptions that contain no event reporting."""
    t = normalize_text(text).casefold().strip(" .!:-")
    patterns = (
        r"the international cricket council \(icc\) is the global governing body of cricket",
        r"the icc is the global governing body of cricket",
        r"the international cricket council is the global governing body of cricket",
        r"official website of the international cricket council",
        r"providing news, fixtures, rankings, videos, and live cricket updates",
    )
    return any(re.search(pattern, t) for pattern in patterns)


def source_is_unanchored_result_for_highlights(story_title: str, source_title: str) -> bool:
    """Avoid attaching an outcome report to a generic highlights page without event identity."""
    story = normalize_text(story_title)
    source = normalize_text(source_title)
    highlights_story = bool(re.search(r"\bhighlights?\b", story, re.I))
    story_has_result = bool(re.search(r"\b(won|wins|beat|defeated|victory|result|scorecard|\d+[- ]run win)\b", story, re.I))
    source_has_specific_outcome = bool(re.search(r"\b\d+[- ]run win\b|\bwon by \d+\b|\bdefeated\b", source, re.I))
    return highlights_story and not story_has_result and source_has_specific_outcome


def source_identity_needs_cleanup(item: dict) -> bool:
    """Prioritise digests that mix a preview with result/highlights sources."""
    sources = item.get("sources", [])
    return (
        not source_is_preview(item.get("title", ""))
        and any(source_is_preview(s.get("title", "")) for s in sources)
        and any(source_title_reports_result(s.get("title", "")) for s in sources)
    )


def source_titles_conflict(left: str, right: str) -> bool:
    """Detect explicit event-identity conflicts; shared team names alone are not enough."""
    a, b = normalize_text(left), normalize_text(right)
    match_re = re.compile(r"\bmatch\s*(?:no\.?\s*)?(\d{1,3})\b", re.I)
    ma, mb = match_re.search(a), match_re.search(b)
    if ma and mb and ma.group(1) != mb.group(1):
        return True
    # Compare dates only when both headlines state a date explicitly.
    date_re = re.compile(r"\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(20\d{2})\b|\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b", re.I)
    def date_key(text):
        m = date_re.search(text)
        if not m:
            return None
        if m.group(1):
            day, month, year = m.group(1), m.group(2), m.group(3)
        else:
            month, day, year = m.group(4), m.group(5), m.group(6)
        month_key = month[:3].casefold()
        return (int(year), month_key, int(day))
    da, db = date_key(a), date_key(b)
    return bool(da and db and da != db)


def build_evidence_digest(source_evidence, max_sources=5, max_words=45, lang="en"):
    """Build a clearly attributed extractive digest when no language model is configured.

    Keep each publisher's copied excerpt short; do not pad with invented context.
    """
    paragraphs = []
    for item in source_evidence[:max_sources]:
        available = [tidy(x) for x in item.get("paragraphs", []) if tidy(x)]
        if not available:
            continue
        publisher = normalize_text(item.get("publisher", "Publisher")) or "Publisher"
        excerpt = _short_attributed_text(available[0], max_words=max_words)
        if lang == "hi":
            excerpt = clean_hindi_excerpt(translate_to_hindi(excerpt))
        if excerpt:
            paragraphs.append(f'{publisher}: “{excerpt}”')
    return "\n\n".join(paragraphs)


def generate_ai_article(title, lang, source_evidence):
    """Generate a detailed article only from retrieved publisher evidence."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        log.warning("OPENAI_API_KEY is not configured; skipping AI long-form article for %s", title[:90])
        return ""
    if not source_evidence: return ""
    language = "Hindi" if lang == "hi" else "English"
    evidence = []
    for item in source_evidence[:5]:
        paragraphs = [tidy(x)[:1000] for x in item.get("paragraphs", []) if tidy(x)]
        if paragraphs: evidence.append({"publisher": normalize_text(item.get("publisher", "Publisher")), "headline": normalize_text(item.get("title", ""))[:250], "url": item.get("url", ""), "reported_text": paragraphs[:4]})
    evidence_word_count = sum(len(paragraph.split()) for item in evidence for paragraph in item["reported_text"])
    # A single publisher is sufficient only when its accessible article provides substantial evidence.
    # Do not expand a short feed teaser into an invented long-form story.
    if not evidence or evidence_word_count < 120: return ""
    prompt = ("Write a detailed news article in " + language + " about: " + title + "\n\n"
        "Use ONLY facts explicitly present in the source evidence JSON below. No outside knowledge, assumptions, predictions, invented quotes, statistics, dates, or motives. Attribute claims to the source that reported them. Paraphrase faithfully and do not repeat the headline as filler. Use 4-7 substantial paragraphs and aim for 450-650 words only when evidence supports that much; otherwise write a shorter accurate article. Explain what happened, relevant context found in the sources, what each source reports, and what remains unclear. Return plain text paragraphs separated by blank lines, with no Markdown headings or HTML.\n\n"
        "SOURCE EVIDENCE JSON:\n" + json.dumps(evidence, ensure_ascii=False))
    payload = json.dumps({"model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"), "messages": [{"role": "system", "content": "You are a careful news editor. Accuracy and attribution matter more than length."}, {"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 1600}).encode("utf-8")
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=payload, headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=35) as response: result = json.loads(response.read(1000000).decode("utf-8"))
        article = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        paragraphs = [tidy(x) for x in re.split(r"\n\s*\n", article) if tidy(x)]
        article = "\n\n".join(paragraphs)
        if len(article.split()) < 180 or len(paragraphs) < 3:
            log.warning("AI article rejected: output was too short or not paragraph-structured"); return ""
        return article[:9000]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, KeyError, IndexError) as exc:
        log.warning("AI article generation unavailable for %s: %s", title[:90], exc); return ""

def enrich_candidate_summary(c, cfg, max_sources=5):
    """Enrich a thin digest from multiple source pages without inventing facts.

    Scan beyond the first few page paragraphs because publisher pages often put
    navigation, related stories, or promos before the article body. Keep no more
    than 22 excerpt words from any one publisher and preserve source attribution.
    """
    additions = []
    source_evidence = []
    sources = c.get("sources", [])[:max_sources]
    conflicting_titles = set()
    for i, left in enumerate(sources):
        for right in sources[i + 1:]:
            if source_titles_conflict(left.get("title", ""), right.get("title", "")):
                conflicting_titles.add(normalize_text(left.get("title", "")).casefold())
                conflicting_titles.add(normalize_text(right.get("title", "")).casefold())
                log.warning("Conflicting event identifiers across sources; excluding both from article evidence: %s | %s", left.get("title", "")[:100], right.get("title", "")[:100])
    # A preview/prediction for a future meeting must not be mixed into a match-result digest.
    story_is_preview = source_is_preview(c.get("title", ""))
    source_has_result = any(source_title_reports_result(s.get("title", "")) for s in sources)
    for source in sources:
        source_title = normalize_text(source.get("title", ""))
        if source_title.casefold() in conflicting_titles:
            continue
        source_preview = source_is_preview(source_title)
        if source_is_unanchored_result_for_highlights(c.get("title", ""), source_title):
            log.info("Excluding outcome report without a matching event identifier from generic highlights story: %s", source_title[:100])
            continue
        if source_preview and source_has_result and not story_is_preview:
            log.info("Excluding preview source from result-focused article evidence: %s", source_title[:100])
            continue
        url = safe_url(source.get("url", ""))
        if not url:
            continue
        try:
            original_url = url
            embedded_url = embedded_source_url(url) if host_of(url).casefold().startswith("news.google.") else None
            if embedded_url:
                url = embedded_url
            final_url, raw = fetch_source_page(url, timeout=min(cfg["timeout_seconds"], 5))
            parser = _MetaDescription()
            parser.feed(raw.decode("utf-8", errors="replace"))
            final_host = host_of(final_url).casefold()
            if final_host.startswith("news.google."):
                publisher_key = re.sub(r"[^a-z0-9]", "", source.get("publisher", "").casefold())
                target = None
                for link in parser.links:
                    link_host = host_of(link["url"]).casefold()
                    host_key = re.sub(r"[^a-z0-9]", "", link_host.split(".")[0])
                    if (link_host and not link_host.endswith("google.com")
                            and publisher_key and (publisher_key in host_key or host_key in publisher_key)):
                        target = link["url"]
                        break
                    if link_host and not link_host.endswith("google.com") and jaccard(
                            tokens(link.get("text", "")), tokens(source.get("title", ""))) >= 0.45:
                        target = link["url"]
                        break
                if target:
                    final_url, raw = fetch_source_page(target, timeout=min(cfg["timeout_seconds"], 5))
                    parser = _MetaDescription()
                    parser.feed(raw.decode("utf-8", errors="replace"))
                    final_host = host_of(final_url).casefold()
            if final_url != original_url and not final_host.startswith("news.google."):
                source["url"] = final_url
            # Inspect many more candidate paragraphs, then select the most relevant
            # one rather than blindly accepting whichever matching text appears first.
            options = parser.descriptions[:8] + parser.paragraphs[:40]
            publisher = normalize_text(source.get("publisher") or host_of(final_url))
            ranked = []
            for option in options:
                option = tidy(option)
                if (len(option) < 40
                        or option.casefold() == c.get("title", "").casefold()
                        or not _snippet_matches_story(option, c.get("title", ""), source.get("title", ""))
                        or is_boilerplate_summary(option)
                        or is_generic_sports_boilerplate(option)):
                    continue
                terms = {w for w in tokens(option) if len(w) >= 4}
                story_terms = {w for w in tokens(c.get("title", "") + " " + source.get("title", "")) if len(w) >= 4}
                score = len(terms & story_terms)
                # Prefer useful context over a one-line teaser or a repeated headline.
                ranked.append((score, min(len(option.split()), 80), option))
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            if ranked:
                evidence_paragraphs = []
                for _, _, option in ranked:
                    cleaned = tidy(option)
                    if any(jaccard(tokens(cleaned), tokens(old)) > 0.8 for old in evidence_paragraphs):
                        continue
                    evidence_paragraphs.append(cleaned[:1000])
                    if len(evidence_paragraphs) >= 4: break
                if evidence_paragraphs:
                    source_evidence.append({"publisher": publisher, "title": source.get("title", ""), "url": final_url, "paragraphs": evidence_paragraphs})
                    short = _short_attributed_text(evidence_paragraphs[0], max_words=45)
                    if c.get("lang") == "hi":
                        short = clean_hindi_excerpt(translate_to_hindi(short))
                    if not any(short.casefold() in old.casefold() or old.casefold() in short.casefold() for old in additions):
                        additions.append(f"{publisher}: {short}")
        except (FetchError, ValueError, UnicodeError) as e:
            log.debug("source content unavailable for %s: %s", url, e)

    # Five publishers can contribute distinct, attributed details; unavailable source
    # pages remain visibly limited rather than being filled with guessed context.
    parts = []
    feed_summary = tidy(c.get("summary", ""))
    if c.get("lang") == "hi" and feed_summary:
        feed_summary = clean_hindi_excerpt(translate_to_hindi(feed_summary))
    if feed_summary and len(feed_summary) >= 40 and not is_boilerplate_summary(feed_summary):
        parts.append(_short_attributed_text(feed_summary, max_words=45))
    parts.extend(additions)
    combined = []
    for part in parts:
        if part and not any(part.casefold() in old.casefold() or old.casefold() in part.casefold()
                            for old in combined):
            combined.append(part)
    if not combined and c.get("lang") == "hi":
        # If source pages block extraction, translate source headlines with attribution.
        for source in sources[:5]:
            headline = normalize_text(source.get("title", ""))
            if headline:
                cleaned_headline = clean_hindi_excerpt(translate_to_hindi(headline))
                if cleaned_headline:
                    combined.append(f'{normalize_text(source.get("publisher", "स्रोत"))}: {cleaned_headline}')
    if c.get("lang") == "hi" and combined:
        # Keep only Hindi-script excerpts; do not leave English paragraphs under a Hindi label.
        cleaned_parts = []
        for part in combined:
            match = re.match(r"^([^:]{2,100}):\s+(.+)$", part, re.S)
            if match:
                label, excerpt = match.group(1), clean_hindi_excerpt(translate_to_hindi(match.group(2)))
                if excerpt:
                    cleaned_parts.append(f"{label}: {excerpt}")
            else:
                excerpt = clean_hindi_excerpt(translate_to_hindi(part))
                if excerpt:
                    cleaned_parts.append(excerpt)
        combined = cleaned_parts
    if combined:
        if len(tidy(c.get("summary", ""))) < cfg["min_summary_chars"]: c["summary"] = "\n\n".join(combined)[:2400]
        c["boilerplate_summary"] = False
    if source_evidence:
        c["source_evidence"] = source_evidence
        article = generate_ai_article(c.get("title", ""), c.get("lang", "en"), source_evidence)
        if article:
            c["article_body"] = article
        else:
            # No paid API key: publish a transparent, source-attributed digest instead
            # of inventing long-form prose or leaving the article body nearly empty.
            evidence_digest = build_evidence_digest(source_evidence, lang=c.get("lang", "en"))
            if evidence_digest:
                c["article_body"] = evidence_digest

def parse_feed(data: bytes, feed: dict) -> list:
    low = data.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise FeedError("DTD/entity declarations are not allowed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FeedError(f"malformed XML: {e}")
    kind = _local(root.tag)
    if kind in ("rss", "RDF"):
        items = [e for e in root.iter() if _local(e.tag) == "item"]
    elif kind == "feed":
        items = [e for e in root if _local(e.tag) == "entry"]
    else:
        raise FeedError(f"unrecognised feed root <{kind}>")
    out = []
    for it in items:
        try:
            c = _parse_item(it, feed)
        except Exception as e:  # one bad entry must not lose the feed
            log.warning("skipping bad entry in %s: %s", feed.get("name", feed["url"]), e)
            continue
        if c:
            out.append(c)
    return out


# ---------------------------------------------------------------- quality gate
def publishers(c) -> set:
    return {s["publisher"].casefold() for s in c["sources"]}


def score(c, cfg, now) -> float:
    age = (now - c["published"]).total_seconds() / 3600 if c["published"] else cfg["max_age_hours"]
    recency = max(0.0, 40 * (1 - age / cfg["max_age_hours"]))
    return round(recency + 10 * min(len(publishers(c)), 4) + (10 if c["summary"] else 0) + c.get("weight", 0), 2)


def check_quality(c, cfg, now) -> list:
    """Return rejection reasons; an empty list means the candidate may be published."""
    r, t = [], c["title"]
    if len(t) < cfg["min_title_chars"] or len(t) > 160:
        r.append("title_length")
    if CLICKBAIT_RE.search(t):
        r.append("clickbait_title")
    latin = [ch for ch in t if ch.isascii() and ch.isalpha()]
    if len(latin) > 20 and sum(ch.isupper() for ch in latin) / len(latin) > 0.7:
        r.append("shouting_title")
    if SPECULATION_RE.search(t) or SPECULATION_RE.search(c["summary"]):
        r.append("unsupported_speculation")
    if not c["sources"]:
        r.append("no_valid_source")
    if c["published"] is None:
        r.append("no_publication_date")
    else:
        age = now - c["published"]
        if age > timedelta(hours=cfg["max_age_hours"]):
            r.append("too_old")
        if age < timedelta(hours=-1):
            r.append("date_in_future")
    if not language_ok(c["lang"], t):
        r.append("language_mismatch")
    if c.get("boilerplate_summary") or len(tidy(c.get("summary", ""))) < cfg["min_summary_chars"]:
        r.append("insufficient_value")
    return r


def is_duplicate(c, known: list, threshold: float) -> bool:
    """known: [{id, lang, title}] from posts, publication history and this batch."""
    cid, tk = make_id(c["lang"], c["title"]), tokens(c["title"])
    return any(k.get("id") == cid or (k.get("lang") == c["lang"] and jaccard(tk, tokens(k.get("title", ""))) >= threshold)
               for k in known)


# ---------------------------------------------------------------- post model
T = {
    "en": {
        "home": "Home", "english": "English", "hindi": "हिंदी", "categories": "Categories", "about": "About",
        "contact": "Contact", "privacy": "Privacy Policy", "editorial": "Editorial Policy", "skip": "Skip to main content",
        "search": "Search", "search_ph": "Search topics", "all": "All", "read_more": "Read the digest",
        "sources": "Sources", "latest": "Latest articles", "trending": "Trending now", "related": "Related topics",
        "hero_title": "What India is talking about, with the sources attached",
        "hero_text": "TrendPulse India collects headlines from public news and trends feeds, groups the coverage, and links every digest back to the original reports. Available in English and Hindi.",
        "empty": "No articles yet. They will appear after the automated pipeline runs for the first time.",
        "nomatch": "No articles match your search.", "results": "{n} articles shown",
        "coverage": "Coverage from {n} sources: {names}.", "reported_by": "Reported by",
        "feed_summary": "Summary from the source feed", "published": "Published",
        "what_happened": "What happened", "each_source": "What each source reports", "unconfirmed": "What is not confirmed",
        "un_headline": "Only headlines and short feed summaries were available. Details beyond them are not confirmed here; read the original reports.",
        "un_single": "Only one publisher is cited so far, so this has not been cross-checked against other reports.",
        "un_differ": "These sources word the story differently, so check their reports for exact details: {names}.",
        "disclaimer": "This page is an automatic digest of headlines and feed summaries from the sources listed. TrendPulse India has not independently verified them. Read the original reports for full details.",
        "choose": "Read in English", "choose_hi": "हिंदी में पढ़ें", "nav": "Main navigation", "footer": "Footer",
        "cats_label": "Filter by category", "locale": "en_IN", "tagline": "Trending topics from India, with sources",
    },
    "hi": {
        "home": "होम", "english": "English", "hindi": "हिंदी", "categories": "श्रेणियाँ", "about": "हमारे बारे में",
        "contact": "संपर्क", "privacy": "गोपनीयता नीति", "editorial": "संपादकीय नीति", "skip": "मुख्य सामग्री पर जाएँ",
        "search": "खोजें", "search_ph": "विषय खोजें", "all": "सभी", "read_more": "पूरा सार पढ़ें",
        "sources": "स्रोत", "latest": "ताज़ा लेख", "trending": "अभी चर्चा में", "related": "संबंधित विषय",
        "hero_title": "भारत में क्या चर्चा में है, स्रोतों के साथ",
        "hero_text": "TrendPulse India सार्वजनिक समाचार और ट्रेंड फ़ीड से सुर्खियाँ इकट्ठा करता है, कवरेज को एक जगह जोड़ता है और हर सार को मूल रिपोर्ट से जोड़ता है। अंग्रेज़ी और हिंदी दोनों में उपलब्ध।",
        "empty": "अभी कोई लेख उपलब्ध नहीं है। ऑटोमेशन पहली बार चलने के बाद लेख यहाँ दिखेंगे।",
        "nomatch": "आपकी खोज से कोई लेख मेल नहीं खाता।", "results": "{n} लेख दिखाए जा रहे हैं",
        "coverage": "{n} स्रोतों की रिपोर्ट: {names}।", "reported_by": "रिपोर्ट करने वाले स्रोत",
        "feed_summary": "स्रोत फ़ीड का सारांश", "published": "प्रकाशित",
        "what_happened": "क्या हुआ", "each_source": "हर स्रोत क्या रिपोर्ट करता है", "unconfirmed": "क्या पुष्टि नहीं हुई है",
        "un_headline": "सिर्फ़ सुर्खियाँ और छोटे फ़ीड सारांश उपलब्ध थे। उनसे आगे की जानकारी यहाँ पुष्ट नहीं है; मूल रिपोर्ट पढ़ें।",
        "un_single": "अभी सिर्फ़ एक प्रकाशक का हवाला है, इसलिए इसे दूसरी रिपोर्टों से मिलाकर नहीं देखा गया है।",
        "un_differ": "ये स्रोत खबर को अलग-अलग शब्दों में बताते हैं, इसलिए सटीक ब्योरे के लिए उनकी रिपोर्ट देखें: {names}।",
        "disclaimer": "यह पृष्ठ ऊपर दिए गए स्रोतों की सुर्खियों और फ़ीड सारांश से स्वचालित रूप से तैयार किया गया है। TrendPulse India ने इनकी स्वतंत्र पुष्टि नहीं की है। पूरी जानकारी के लिए मूल रिपोर्ट पढ़ें।",
        "choose": "अंग्रेज़ी में पढ़ें", "choose_hi": "हिंदी में पढ़ें", "nav": "मुख्य नेविगेशन", "footer": "फ़ुटर",
        "cats_label": "श्रेणी के अनुसार छाँटें", "locale": "hi_IN", "tagline": "भारत के ट्रेंडिंग विषय, स्रोतों के साथ",
    },
}

INFO = {
    "en": {
        "about": ("About TrendPulse India", "What TrendPulse India is and how it works.", [
            "<p>TrendPulse India is an automated website that gathers publicly available headlines and trend signals about India and presents them as short, source-linked digests in English and Hindi.</p>",
            "<p>It does not produce original reporting. Every digest links to the publishers whose headlines it summarises, so you can read the full story at the source.</p>",
            "<p>The site is run as a small project by its owner. See the <a href=\"contact.html\">Contact page</a> to reach us.</p>"]),
        "contact": ("Contact", "How to report an error or contact TrendPulse India.", [
            "<p>To report an error, request a correction or removal, or ask a question, email <a href=\"mailto:{email}\">{email}</a>.</p>",
            "<p>Please include the page address and a short description of the issue.</p>"]),
        "privacy": ("Privacy Policy", "How TrendPulse India handles visitor data.", [
            "<ul><li>The site has no accounts, comments or forms.</li>",
            "<li>At present it sets no cookies and uses no analytics tools.</li>",
            "<li>The hosting provider may keep technical logs such as IP addresses under its own policy.</li>",
            "<li>Articles link to external sites that have their own privacy policies.</li>",
            "<li>If advertising or analytics is added in future, this policy will be updated before it goes live, including cookies and third-party vendors.</li></ul>"]),
        "editorial": ("Editorial Policy", "How TrendPulse India selects and compiles its digests.", [
            "<p>Articles here are not original research. They are digests compiled automatically from headlines and summaries in public RSS feeds.</p>",
            "<ul><li>Every digest links to the original reports.</li>",
            "<li>A topic is published only if at least two different publishers cover it, or its feed summary adds enough distinct detail.</li>",
            "<li>Misleading or sensational titles, rumours and unconfirmed claims, old or undated items, and near-duplicates of published topics are rejected.</li>",
            "<li>We do not independently verify the sources. Read the original reports for full details.</li></ul>",
            "<p>If you spot an error, tell us through the <a href=\"contact.html\">Contact page</a>.</p>"]),
    },
    "hi": {
        "about": ("TrendPulse India के बारे में", "TrendPulse India क्या है और कैसे काम करता है।", [
            "<p>TrendPulse India एक स्वचालित वेबसाइट है, जो भारत से जुड़ी सार्वजनिक सुर्खियों और ट्रेंड संकेतों को इकट्ठा करती है और उन्हें अंग्रेज़ी और हिंदी में स्रोत-लिंक वाले छोटे सारों के रूप में पेश करती है।</p>",
            "<p>यह मौलिक रिपोर्टिंग नहीं करती। हर सार उन प्रकाशकों से जुड़ा है जिनकी सुर्खियों पर वह आधारित है, ताकि आप पूरी खबर मूल स्रोत पर पढ़ सकें।</p>",
            "<p>यह साइट इसके मालिक द्वारा एक छोटे प्रोजेक्ट के रूप में चलाई जाती है। संपर्क के लिए <a href=\"contact.html\">संपर्क पृष्ठ</a> देखें।</p>"]),
        "contact": ("संपर्क", "गलती की सूचना देने या TrendPulse India से संपर्क करने का तरीका।", [
            "<p>किसी गलती की सूचना देने, सुधार या हटाने का अनुरोध करने, या कोई सवाल पूछने के लिए <a href=\"mailto:{email}\">{email}</a> पर ईमेल करें।</p>",
            "<p>कृपया पृष्ठ का पता और समस्या का संक्षिप्त विवरण साथ भेजें।</p>"]),
        "privacy": ("गोपनीयता नीति", "TrendPulse India आगंतुकों के डेटा को कैसे संभालता है।", [
            "<ul><li>इस साइट पर खाते, टिप्पणियाँ या फ़ॉर्म नहीं हैं।</li>",
            "<li>फ़िलहाल साइट कोई कुकी सेट नहीं करती और कोई एनालिटिक्स टूल इस्तेमाल नहीं करती।</li>",
            "<li>होस्टिंग प्रदाता तकनीकी लॉग (जैसे IP पता) अपनी नीति के अनुसार रख सकता है।</li>",
            "<li>लेख बाहरी साइटों से जुड़ते हैं, जिनकी अपनी गोपनीयता नीतियाँ हैं।</li>",
            "<li>भविष्य में विज्ञापन या एनालिटिक्स जोड़े गए, तो उनके शुरू होने से पहले यह नीति कुकी और तृतीय-पक्ष सेवाओं की जानकारी के साथ अपडेट की जाएगी।</li></ul>"]),
        "editorial": ("संपादकीय नीति", "TrendPulse India सार कैसे चुनता और तैयार करता है।", [
            "<p>यहाँ के लेख मौलिक शोध नहीं हैं। ये सार्वजनिक RSS फ़ीड की सुर्खियों और सारांश से स्वचालित रूप से तैयार किए गए संकलन हैं।</p>",
            "<ul><li>हर सार में मूल रिपोर्टों के लिंक दिए जाते हैं।</li>",
            "<li>कोई विषय तभी प्रकाशित होता है जब उसे कम से कम दो अलग प्रकाशक कवर करें, या उसके फ़ीड सारांश में पर्याप्त अलग जानकारी हो।</li>",
            "<li>भ्रामक या सनसनीखेज़ शीर्षक, अफ़वाह और अपुष्ट दावे, पुरानी या बिना तारीख की खबरें, और पहले प्रकाशित विषयों की नकल अस्वीकार कर दी जाती है।</li>",
            "<li>हम स्रोतों की स्वतंत्र पुष्टि नहीं करते। पूरी जानकारी के लिए मूल रिपोर्ट पढ़ें।</li></ul>",
            "<p>कोई गलती दिखे तो <a href=\"contact.html\">संपर्क पृष्ठ</a> के ज़रिए बताएँ।</p>"]),
    },
}
INFO_PAGES = ("about", "contact", "privacy", "editorial")


def short_summary(c) -> str:
    if c["summary"]:
        s = c["summary"]
        return s if len(s) <= 200 else s[:200].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    names = sorted({s["publisher"] for s in c["sources"]})
    return T[c["lang"]]["coverage"].format(n=len(names), names=", ".join(names[:3]))


def build_post(c, now) -> dict:
    lang = c["lang"]
    return {
        "id": make_id(lang, c["title"]), "lang": lang, "slug": make_slug(lang, c["title"]),
        "url": f"{lang}/posts/{make_slug(lang, c['title'])}.html", "title": c["title"],
        "summary": short_summary(c), "source_summary": c["summary"][:2400], "article_body": c.get("article_body", ""), "category": c["category"],
        "published_at": c["published"].isoformat(), "generated_at": now.isoformat(), "kind": "digest",
        "keyword": c["keyword"], "score": c.get("score", 0),
        "sources": [{"title": s["title"], "url": s["url"], "publisher": s["publisher"]} for s in c["sources"]],
    }


def load_review(path):
    ok = lambda d: isinstance(d, dict) and isinstance(d.get("items"), list)
    return load_json(path, {"version": 1, "items": []}, ok)


def review_item(c, now, reasons) -> dict:
    return {"id": make_id(c["lang"], c["title"]), "lang": c["lang"], "title": c["title"], "category": c["category"],
            "published_at": c["published"].isoformat(), "summary": c["summary"], "sources": c["sources"],
            "queued_at": now.isoformat(), "reasons": reasons}


def known_entries(posts_doc, pub_doc) -> list:
    k = [{"id": p.get("id"), "lang": p.get("lang"), "title": p.get("title", "")} for p in posts_doc["posts"]]
    return k + [{"id": i.get("id"), "lang": i.get("id", "")[:2], "title": i.get("title", "")} for i in pub_doc["items"]]


ID_RE = re.compile(r"^(en|hi)-[0-9a-f]{10}$")


def unpublish(root, post_id) -> dict:
    """Remove one post from the site on purpose. A copy is kept in unpublished.json; published.json keeps the id,
    so the pipeline will not regenerate it."""
    if not ID_RE.match(post_id or ""):
        raise ValueError("post id must look like en-0123456789 or hi-0123456789")
    root = Path(root); cfg = load_config(root / "config.json")
    doc = load_posts(root / "posts.json")
    post = next((p for p in doc["posts"] if p.get("id") == post_id), None)
    if post is None:
        raise KeyError(f"no post with id {post_id}")
    bak = load_json(root / "unpublished.json", {"version": 1, "items": []}, lambda d: isinstance(d, dict) and isinstance(d.get("items"), list))
    bak["items"].append({**post, "unpublished_at": datetime.now(timezone.utc).isoformat()})
    write_json(root / "unpublished.json", bak)                     # 1. backup first
    remaining = [p for p in doc["posts"] if p.get("id") != post_id]
    write_json(root / "posts.json", {"version": 1, "posts": remaining})  # 2. remove from the store
    if POST_URL_RE.match(post.get("url", "")):                      # 3. delete only its own page (path pattern validated)
        (root / post["url"]).unlink(missing_ok=True)
    render_site(root, cfg, remaining)                               # 4. refresh lists and sitemap
    return post


def approve(root, item_id, now=None) -> dict:
    """Publish one reviewed item from review.json (a human decision; the quality gate is not re-run)."""
    root = Path(root); cfg = load_config(root / "config.json"); now = now or datetime.now(timezone.utc)
    rev = load_review(root / "review.json")
    it = next((i for i in rev["items"] if i["id"] == item_id), None)
    if not it:
        raise KeyError(f"no review item with id {item_id}")
    posts_doc, pub_doc = load_posts(root / "posts.json"), load_published(root / "published.json")
    c = {"title": it["title"], "keyword": None, "published": parse_date(it["published_at"]), "summary": it["summary"],
         "sources": it["sources"], "lang": it["lang"], "category": it["category"], "score": 0}
    if is_duplicate(c, known_entries(posts_doc, pub_doc), cfg["duplicate_similarity"]):
        raise ValueError("already published or near-duplicate of a published topic")
    post = build_post(c, now)
    if not valid_post(post):
        raise ValueError("item does not form a valid post")
    posts_doc["posts"].append(post)
    render_site(root, cfg, posts_doc["posts"])
    write_json(root / "posts.json", {"version": 1, "posts": posts_doc["posts"]})
    pub_doc["items"].append({"id": post["id"], "title": post["title"], "url": post["url"], "published_at": post["published_at"]})
    write_json(root / "published.json", pub_doc)
    rev["items"] = [i for i in rev["items"] if i["id"] != item_id]
    write_json(root / "review.json", rev)
    return post


def valid_post(p) -> bool:
    try:
        return (isinstance(p, dict) and p["lang"] in LANGS and bool(POST_URL_RE.match(p["url"]))
                and p["url"] == f"{p['lang']}/posts/{p['slug']}.html" and bool(p["title"])
                and p["category"] in CATEGORIES and parse_date(p["published_at"]) is not None
                and isinstance(p["sources"], list) and len(p["sources"]) > 0
                and all(safe_url(s.get("url")) for s in p["sources"]))
    except (KeyError, TypeError, AttributeError):
        return False


# ---------------------------------------------------------------- rendering
esc = lambda s: html.escape(str(s), quote=True)


def cat_label(key, lang):
    return CATEGORIES.get(key, (key, key))[0 if lang == "en" else 1]


def ist_date(iso: str) -> str:
    return parse_date(iso).astimezone(IST).strftime("%Y-%m-%d")


def canon_path(path: str) -> str:
    return "" if path == "index.html" else (path[:-10] if path.endswith("/index.html") else path)


def page(cfg, lang, path, title, desc, body, alternates=(), jsonld=None, noindex=False, active="", app=False, og_type="website"):
    up = "../" * path.count("/")
    L, name = T[lang], cfg["site_name"]
    canonical = f"{cfg['site_url']}/{canon_path(path)}"
    # Listing titles already include the brand; avoid repeating it in SERP titles.
    full_title = title if title == name or title.startswith(name + " — ") or title.startswith(name + " | ") else f"{title} | {name}"
    alt = "".join(f'<link rel="alternate" hreflang="{h}" href="{esc(cfg["site_url"] + "/" + canon_path(p))}">\n' for h, p in alternates)
    ld = ""
    if jsonld:
        safe = json.dumps(jsonld, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        ld = '<script type="application/ld+json">' + safe + "</script>\n"
    cl = "en" if lang not in LANGS else lang
    nav = [("home", up + "index.html", ""), ("english", up + "en/index.html", ""), ("hindi", up + "hi/index.html", "hi"), ("about", f"{up}{cl}/about.html", "")]
    navh = "".join(
        f'<li><a href="{esc(h)}"{(" hreflang=hi lang=hi" if lg else "")}{(" aria-current=page" if k == active else "")}>{esc(L[k])}</a></li>'
        for k, h, lg in nav)
    foot = "".join(f'<li><a href="{up}{cl}/{p}.html">{esc(L[p])}</a></li>' for p in INFO_PAGES)
    script = f'<script src="{up}assets/app.js" defer></script>\n' if app else ""
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(full_title)}</title>
<meta name="description" content="{esc(desc)}">
<meta name="robots" content="{'noindex,follow' if noindex else 'index,follow,max-snippet:-1,max-image-preview:large,max-video-preview:-1'}">
<link rel="canonical" href="{esc(canonical)}">
{alt}<meta property="og:site_name" content="{esc(name)}">
<meta property="og:type" content="{og_type}">
<meta property="og:title" content="{esc(full_title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{esc(canonical)}">
<meta property="og:locale" content="{L['locale']}">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{esc(full_title)}">
<meta name="twitter:description" content="{esc(desc)}">
<meta name="twitter:url" content="{esc(canonical)}">
<link rel="stylesheet" href="{up}assets/style.css">
{ld}{script}</head>
<body>
<a class="skip" href="#main">{esc(L['skip'])}</a>
<header class="site-header"><div class="wrap bar">
<a class="brand" href="{up}index.html">{esc(name)}</a>
<nav aria-label="{esc(L['nav'])}"><ul>{navh}</ul></nav>
</div></header>
<main id="main" class="wrap">
{body}
</main>
<footer class="site-footer"><div class="wrap">
<nav aria-label="{esc(L['footer'])}"><ul>{foot}</ul></nav>
<p>© {esc(name)}.</p>
</div></footer>
</body>
</html>
"""


def card(p, up, compact=False):
    L = T[p["lang"]]
    cat = cat_label(p["category"], p["lang"])
    names = ", ".join(dict.fromkeys(s["publisher"] for s in p["sources"]))
    text = f"{p['title']} {tidy(p['summary'])} {cat}".casefold()
    return (f'<article class="card{" compact" if compact else ""}" lang="{p["lang"]}" data-cat="{esc(p["category"])}" data-text="{esc(text)}">'
            f'<p class="meta"><span class="badge">{esc(cat)}</span> <time datetime="{esc(p["published_at"])}">{ist_date(p["published_at"])}</time></p>'
            f'<h3><a href="{esc(up + p["url"])}">{esc(p["title"])}</a></h3>'
            f'<p>{esc(tidy(p["summary"]))}</p><p class="src">{esc(L["sources"])}: {esc(names)}</p>'
            f'<a class="more" href="{esc(up + p["url"])}">{esc(L["read_more"])}<span class="sr"> — {esc(p["title"])}</span></a></article>')



def trend_topic_relevant(post) -> bool:
    """Hide trend cards whose headline does not support the displayed search phrase."""
    keyword = normalize_text(post.get("keyword", "")).casefold()
    headline = tokens(post.get("title", ""))
    if not keyword or not headline:
        return False
    raw = tokens(keyword)
    if "lottery" in raw or "sambad" in raw:
        return "lottery" in headline or "sambad" in headline
    aliases = {
        "ban": "bangladesh", "zim": "zimbabwe", "nz": "zealand",
        "aus": "australia", "ind": "india", "wi": "west",
    }
    stop = {"vs", "v", "the", "in", "and", "to", "of", "for", "a", "an", "team", "match", "live"}
    expanded = set()
    for token in raw:
        if token in stop:
            continue
        expanded.update(tokens(token))
        expanded.update(tokens(aliases.get(token, token)))
    overlap = len(expanded & headline)
    # Require two matching entities for longer multi-part queries (e.g. team vs team),
    # so a single generic word like "Delhi" cannot make an unrelated story look relevant.
    required = 2 if len(expanded) >= 3 else 1
    return overlap >= required


def trend_topic_card(p, up, lang, posts=()):
    """Render a localized Google Trends card while preserving official names."""
    keyword = normalize_text(p.get("keyword", ""))
    if not keyword:
        return ""
    display_keyword = keyword
    display_title = normalize_text(p.get("title", ""))
    card_lang = "en"
    if lang == "hi":
        # Common search phrases and current high-traffic headlines use reviewed
        # Hindi wording; the local Argos model remains the fallback for new topics.
        keyword_hi = {
            "oman vs nepal": "ओमान बनाम नेपाल",
            "inter kashi vs odisha": "इंटर काशी बनाम ओडिशा",
            "ban vs zim": "बांग्लादेश बनाम ज़िम्बाब्वे",
            "everton vs chelsea": "एवर्टन बनाम चेल्सी",
        }
        title_hi = {
            "inter kashi vs odisha fc, northeast united vs kerala blasters in isl; preliminary rounds in asian para games: indian sports live, october 17":
                "आईएसएल में इंटर काशी और ओडिशा एफसी, नॉर्थईस्ट यूनाइटेड और केरल ब्लास्टर्स के मुकाबले; एशियन पैरा गेम्स के प्रारंभिक दौर की अपडेट।",
            "zimbabwe look to extend recent dominance over bangladesh":
                "ज़िम्बाब्वे की कोशिश होगी कि वह बांग्लादेश के खिलाफ हालिया बढ़त बरकरार रखे।",
            "everton vs chelsea live! premier league match updates, news and score":
                "एवर्टन और चेल्सी के बीच प्रीमियर लीग मुकाबले की लाइव अपडेट, खबरें और स्कोर।",
            "australia wary of new-look new zealand ahead of t20i series":
                "टी20 अंतरराष्ट्रीय सीरीज़ से पहले ऑस्ट्रेलिया की नज़र न्यूज़ीलैंड की नई टीम पर है।",
            "pakistan, sri lanka look to find the right mix as world cup countdown begins":
                "विश्व कप की उलटी गिनती शुरू होने के साथ पाकिस्तान और श्रीलंका सही संयोजन तलाश रहे हैं।",
            "míchel makes major changes to ajax's line-up in groningen":
                "ग्रोनिंगन में मुकाबले के लिए मिचेल ने अजाक्स की टीम में बड़े बदलाव किए।",
            "juventus squad: boga returns":
                "जुवेंटस की टीम में बोगा की वापसी हुई है।",
        }
        display_keyword = keyword_hi.get(keyword.casefold(), translate_to_hindi(keyword))
        translated_title = title_hi.get(display_title.casefold(), translate_to_hindi(display_title))
        # Never silently present an English-only headline as Hindi when the
        # offline translation model is unavailable. Keep the story link useful.
        if translated_title and (deva_ratio(translated_title) >= 0.20 or not display_title):
            display_title = translated_title
        else:
            display_title = "इस ट्रेंड से जुड़ी खबर पढ़ें।"
        card_lang = "hi"
    search_url = "https://www.google.com/search?hl=" + ("hi" if lang == "hi" else "en") + "&q=" + urllib.parse.quote(keyword)
    # On the Hindi page, prefer a real Hindi digest for the same trend.
    # Fall back to Hindi search only when no matching Hindi article exists.
    hindi_match = None
    if lang == "hi":
        hindi_match = next((
            candidate for candidate in posts
            if candidate.get("lang") == "hi"
            and normalize_text(candidate.get("keyword", "")).casefold() == keyword.casefold()
            and candidate.get("article_body")
            and deva_ratio(candidate.get("title", "")) >= 0.20
        ), None)
    if lang == "hi" and hindi_match:
        article_url = up + hindi_match["url"]
        read_label = "हिंदी में लेख पढ़ें"
    elif lang == "hi":
        article_url = search_url
        read_label = "हिंदी में संबंधित खबर खोजें"
    else:
        article_url = up + p["url"]
        read_label = T[lang]["trend_read"]
    kicker = "Google Trends · भारत" if lang == "hi" else "Google Trends · India"
    return (f'<article class="trend-topic-card" lang="{card_lang}">'
            f'<p class="trend-kicker">{esc(kicker)}</p>'
            f'<h3><a href="{esc(article_url)}">{esc(display_keyword)}</a></h3>'
            f'<p class="trend-related">{esc(display_title)}</p>'
            f'<p class="trend-actions"><a class="more" href="{esc(article_url)}">{esc(read_label)}</a> '
            f'<a class="more" href="{esc(search_url)}" target="_blank" rel="noopener noreferrer">{esc(T[lang]["trend_search"])}</a></p>'
            f'</article>')


def list_page(cfg, scope, posts, path, alternates=()):
    """scope: 'en' | 'hi' | 'all' (home page, English UI, mixed languages)."""
    lang = scope if scope in LANGS else "en"
    L, up = T[lang], "../" * path.count("/")
    items = sorted([p for p in posts if scope == "all" or p["lang"] == scope], key=lambda p: p["published_at"], reverse=True)
    items = items[: (12 if scope == "all" else cfg["max_list_items"])]
    # Thin/noindex digests are omitted from visible cards, including Trending.
    # Keep the list window stable; older stories can enter naturally as newer items age out.
    visible_items = [p for p in items if indexable(p, cfg)]
    top = [p for p in sorted(items, key=lambda p: p.get("score", 0), reverse=True)[:3] if indexable(p, cfg)]
    cats = [c for c in CATEGORIES if any(p["category"] == c for p in items)]
    chooser = ""
    if scope == "all":
        chooser = (f'<p class="choose"><a class="btn" href="en/index.html">{esc(L["choose"])}</a> '
                   f'<a class="btn" lang="hi" hreflang="hi" href="hi/index.html">{esc(L["choose_hi"])}</a></p>')
    btns = f'<button type="button" class="cat-btn" data-cat="all" aria-pressed="true">{esc(L["all"])}</button>' + "".join(
        f'<button type="button" class="cat-btn" data-cat="{c}" aria-pressed="false">{esc(cat_label(c, lang))}</button>' for c in cats)
    trending = ""
    if top:
        trending = f'<section aria-labelledby="trend"><h2 id="trend">{esc(L["trending"])}</h2><div class="grid">' + "".join(card(p, up, True) for p in top) + "</div></section>"

    if visible_items:
        grid = '<div class="grid" id="grid">' + "".join(card(p, up) for p in visible_items) + "</div>"
    else:
        grid = f'<div id="grid"></div><p class="empty">{esc(L["empty"])}</p>'
    body = f"""<section class="hero"><h1>{esc(L['hero_title'])}</h1><p>{esc(L['hero_text'])}</p>{chooser}</section>
{trending}
<section aria-labelledby="latest"><h2 id="latest">{esc(L['latest'])}</h2>{grid}</section>"""
    title = cfg["site_name"] if scope == "all" else f"{cfg['site_name']} — {L['tagline']}"
    return page(cfg, lang, path, title, L["tagline"], body, alternates, active={"all": "home"}.get(scope, scope), app=False)


def article_page(cfg, p, posts):
    lang, L, path = p["lang"], T[p["lang"]], p["url"]
    rel = [q for q in posts if q["lang"] == lang and q["category"] == p["category"] and q["id"] != p["id"]]
    rel = sorted(rel, key=lambda q: q["published_at"], reverse=True)[:3]
    # Keep publisher-provided snippets attached to their own source links.
    # These are excerpts from feeds/publisher metadata, not independently verified facts.
    attributed = {}
    article_body = tidy(p.get("article_body", ""))
    sources = p["sources"]
    fallback_digest = article_body.startswith((
        "Event-specific details could not be verified",
        "उपलब्ध स्रोतों से इस मैच के विशिष्ट विवरण की पुष्टि नहीं हो सकी",
    ))
    if fallback_digest:
        # A transparent fallback must not still list preview/result headlines as if
        # they substantiate this event. Keep only sources anchored to the article title.
        matched = [
            source for source in sources
            if jaccard(tokens(source.get("title", "")), tokens(p.get("title", ""))) >= 0.4
            and not source_is_preview(source.get("title", ""))
            and not source_is_unanchored_result_for_highlights(
                p.get("title", ""), source.get("title", "")
            )
        ]
        if matched:
            sources = matched
    happened_raw = p.get("source_summary") or p["summary"]
    happened_parts = [tidy(part) for part in re.split(r"\n\s*\n", happened_raw) if tidy(part)]
    if article_body: happened_parts = [tidy(part) for part in re.split(r"\n\s*\n", article_body) if tidy(part)]
    for part in happened_parts:
        match = re.match(r"^([^:]{2,100}):\s+(.+)$", part, re.S)
        if match:
            publisher_key = normalize_text(match.group(1)).casefold()
            snippet = normalize_text(match.group(2))
            if snippet and publisher_key not in attributed:
                attributed[publisher_key] = snippet
    src_items = []
    for source in sources:
        publisher = normalize_text(source["publisher"])
        snippet = attributed.get(publisher.casefold(), "")
        detail = f'<p class="source-detail">{esc(_short_attributed_text(snippet, max_words=22))}</p>' if snippet and not article_body else ""
        src_items.append(f'<li><strong>{esc(publisher)}</strong>: <a href="{esc(source["url"])}" rel="noopener noreferrer">{esc(source["title"] or publisher)}</a>{detail}</li>')
    srcs = "".join(src_items)
    if not happened_parts:
        happened_parts = [tidy(p["summary"])]
    happened_html = "".join(render_paragraph(part, lang) for part in happened_parts)
    pubs = {s["publisher"].casefold() for s in sources}
    differ = [s["publisher"] for s in sources if s["title"] and jaccard(tokens(s["title"]), tokens(p["title"])) < 0.4]
    unc = f'<li>{esc(L["un_headline"])}</li>'
    if len(pubs) < 2:
        unc += f'<li>{esc(L["un_single"])}</li>'
    if differ:
        unc += f'<li>{esc(L["un_differ"].format(names=", ".join(dict.fromkeys(differ))))}</li>'
    fs = f'<h2>{esc(L["what_happened"])}</h2>{happened_html}'
    rl = ""
    if rel:
        rl = f'<h2>{esc(L["related"])}</h2><ul>' + "".join(f'<li><a href="{esc(q["url"].rsplit("/", 1)[-1])}">{esc(q["title"])}</a></li>' for q in rel) + "</ul>"
    body = (f'<article class="post"><nav aria-label="breadcrumb"><a href="../index.html">{esc(L["latest"])}</a></nav>'
            f'<h1>{esc(p["title"])}</h1><p class="meta"><span class="badge">{esc(cat_label(p["category"], lang))}</span> '
            f'{esc(L["published"])}: <time datetime="{esc(p["published_at"])}">{ist_date(p["published_at"])}</time></p>'
            f'<p class="notice">{esc(L["disclaimer"])}</p>{fs}<h2>{esc(L["each_source"])}</h2><ul>{srcs}</ul><h2>{esc(L["unconfirmed"])}</h2><ul>{unc}</ul>{rl}</article>')
    canonical = f"{cfg['site_url']}/{p['url']}"
    description = seo_description(article_body or p.get("source_summary") or p.get("summary", ""))
    modified = p.get("generated_at") or p["published_at"]
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Article", "headline": p["title"][:110], "description": description,
             "datePublished": p["published_at"], "dateModified": modified,
             "inLanguage": "hi-IN" if lang == "hi" else "en-IN",
             "articleSection": cat_label(p["category"], lang),
             "mainEntityOfPage": canonical,
             "publisher": {"@type": "Organization", "name": cfg["site_name"]},
             "isBasedOn": [source["url"] for source in sources],
             "isAccessibleForFree": True},
            {"@type": "BreadcrumbList", "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": L["home"],
                 "item": f"{cfg['site_url']}/{lang}/"},
                {"@type": "ListItem", "position": 2, "name": cat_label(p["category"], lang),
                 "item": f"{cfg['site_url']}/{lang}/index.html#categories"},
                {"@type": "ListItem", "position": 3, "name": p["title"], "item": canonical}
            ]}
        ]
    }
    return page(cfg, lang, path, p["title"], description, body, jsonld=ld, og_type="article",
                noindex=not indexable(p, cfg))


def sitemap_xml(cfg, posts) -> str:
    base = cfg["site_url"]
    urls = [("", None), ("en/", None), ("hi/", None)] + [(f"{l}/{pg}.html", None) for l in LANGS for pg in INFO_PAGES]
    urls += [(p["url"], ist_date(p.get("generated_at") or p["published_at"])) for p in sorted(posts, key=lambda p: p["url"]) if indexable(p, cfg)]
    rows = "".join(f"  <url><loc>{esc(base + '/' + u)}</loc>" + (f"<lastmod>{m}</lastmod>" if m else "") + "</url>\n" for u, m in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{rows}</urlset>\n'


def render_site(root, cfg, posts, write=True) -> dict:
    """Return {relative_path: content}; write only changed files when write=True."""
    posts = sorted([p for p in posts if valid_post(p)], key=lambda p: p["published_at"])
    files = {"index.html": list_page(cfg, "all", posts, "index.html", [("en", "en/index.html"), ("hi", "hi/index.html"), ("x-default", "index.html")])}
    files["en/index.html"] = list_page(cfg, "en", posts, "en/index.html", [("en", "en/index.html"), ("hi", "hi/index.html"), ("x-default", "index.html")])
    files["hi/index.html"] = list_page(cfg, "hi", posts, "hi/index.html", [("en", "en/index.html"), ("hi", "hi/index.html"), ("x-default", "index.html")])
    for lang in LANGS:
        for pg in INFO_PAGES:
            title, desc, parts = INFO[lang][pg]
            body = f'<article class="post"><h1>{esc(title)}</h1>' + "".join(parts).format(email=esc(cfg["contact_email"])) + "</article>"
            path = f"{lang}/{pg}.html"
            files[path] = page(cfg, lang, path, title, desc, body,
                               [("en", f"en/{pg}.html"), ("hi", f"hi/{pg}.html")], active="about" if pg == "about" else "")
    for p in posts:
        files[p["url"]] = article_page(cfg, p, posts)
    files["sitemap.xml"] = sitemap_xml(cfg, posts)
    files["robots.txt"] = f"User-agent: *\nAllow: /\n\nSitemap: {cfg['site_url']}/sitemap.xml\n"
    if write:
        for rel, content in files.items():
            f = Path(root) / rel
            if not f.exists() or f.read_text(encoding="utf-8") != content:
                atomic_write(f, content)
    return files


def check_generated(root) -> bool:
    root = Path(root)
    cfg = load_config(root / "config.json")
    posts = load_posts(root / "posts.json")["posts"]
    ok = True
    for rel, content in render_site(root, cfg, posts, write=False).items():
        f = root / rel
        if not f.exists() or f.read_text(encoding="utf-8") != content:
            log.error("generated file out of date: %s", rel); ok = False
    return ok


def build_site(root, dest) -> None:
    root, dest = Path(root), Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in ("index.html", "sitemap.xml", "robots.txt"):
        shutil.copy2(root / name, dest / name)
    for d in ("en", "hi", "assets"):
        shutil.copytree(root / d, dest / d)


# ---------------------------------------------------------------- pipeline
def collect(cfg, fetcher, stats) -> list:
    feeds = [f for f in cfg["feeds"] if f.get("enabled", True)]
    out = []
    for f in feeds:
        try:
            data = fetcher(f["url"])
            out.extend(parse_feed(data, f))
            stats["feeds_ok"] += 1
        except (FetchError, FeedError) as e:
            stats["feeds_failed"] += 1
            log.warning("feed failed [%s]: %s", f.get("name", f["url"]), e)
        except Exception as e:
            stats["feeds_failed"] += 1
            log.exception("unexpected error in feed %s: %s", f.get("name", f["url"]), e)
    stats["all_feeds_failed"] = bool(feeds) and stats["feeds_ok"] == 0
    return out


def run(root, fetcher=None, now=None, render_only=False, dry_run=False, refresh_only=False) -> dict:
    root = Path(root)
    cfg = load_config(root / "config.json")
    now = now or datetime.now(timezone.utc)
    live_fetcher = fetcher is None
    fetcher = fetcher or (lambda u: fetch_url(u, cfg["timeout_seconds"], cfg["retries"], cfg["retry_backoff_seconds"]))
    posts_doc, pub_doc = load_posts(root / "posts.json"), load_published(root / "published.json")
    stats = {"queued_for_review": 0, "feeds_ok": 0, "feeds_failed": 0, "candidates": 0, "published": [], "rejected": [], "duplicates": 0,
             "errors": 0, "all_feeds_failed": False}
    new_posts, queue = [], []
    if not render_only and not refresh_only:
        cands = collect(cfg, fetcher, stats)
        stats["candidates"] = len(cands)
        if not stats["all_feeds_failed"]:
            for c in cands:
                c["score"] = score(c, cfg, now)
            known = known_entries(posts_doc, pub_doc)
            enriched_count = 0
            for c in sorted(cands, key=lambda c: c["score"], reverse=True):
                if len(new_posts) >= cfg["max_new_posts_per_run"]:
                    break
                try:
                    if live_fetcher and enriched_count < 8:
                        enriched_count += 1
                        enrich_candidate_summary(c, cfg)
                        c["score"] = score(c, cfg, now)
                    reasons = check_quality(c, cfg, now)
                    if reasons:
                        if reasons == ["insufficient_value"] and not is_duplicate(c, known, cfg["duplicate_similarity"]):
                            queue.append(review_item(c, now, reasons))  # too thin to publish: human review
                        stats["rejected"].append({"title": c["title"], "reasons": reasons}); continue
                    if is_duplicate(c, known, cfg["duplicate_similarity"]):
                        stats["duplicates"] += 1; continue
                    post = build_post(c, now)
                    if not valid_post(post):
                        stats["rejected"].append({"title": c["title"], "reasons": ["invalid_generated_post"]}); continue
                    new_posts.append(post)
                    known.append({"id": post["id"], "lang": post["lang"], "title": post["title"]})
                except Exception as e:  # isolate failures to this one topic
                    stats["errors"] += 1
                    log.exception("candidate failed (%s): %s", c.get("title"), e)
    if dry_run:
        stats["published"] = [p["id"] for p in new_posts]
        return stats
    # Refresh thin published posts fairly. Failed attempts are cooled down for 24 hours
    # so a blocked publisher does not prevent later posts from being tried on future runs.
    updated_existing = False
    if live_fetcher and not render_only and not dry_run:
        retried = 0
        cooldown = timedelta(hours=24)
        # Prioritise the weakest summaries first; oldest-first can leave newer thin
        # stories waiting behind many already-attempted articles.
        def refresh_priority(item):
            summary = tidy(item.get("source_summary") or "")
            attempted_at = parse_date(item.get("summary_refresh_attempted_at", ""))
            recent_attempt = bool(attempted_at and now - attempted_at < cooldown)
            # Prefer untried items first. Within previously attempted items,
            # refresh near-threshold summaries before empty pages that may be blocked.
            # The cooldown skip below still applies only to the current version;
            # a new refresh version can therefore retry recent attempts safely.
            language_issue = content_language_needs_refresh(summary, item.get("lang", "en"))
            return (
                0 if source_identity_needs_cleanup(item) else 1,
                recent_attempt,
                0 if language_issue else 1,
                -len(summary.split()),
                item.get("published_at", ""),
            )

        for p in sorted(posts_doc["posts"], key=refresh_priority):
            current = tidy(p.get("source_summary") or "")
            # A summary can pass the word-count gate but still lack the full reader-facing article.
            placeholder = (not p.get("article_body") or not current
                           or current.casefold().startswith(("coverage from ", "स्रोतों की रिपोर्ट:"))
                           or is_boilerplate_summary(current)
                           or content_language_needs_refresh(current, p.get("lang", "en")))
            # A refresh-version bump must actually reprocess previously published
            # articles, even when their old summaries pass today's length checks.
            # Otherwise changing SUMMARY_REFRESH_VERSION only refreshes thin posts.
            current_version = p.get("summary_refresh_version", 0)
            if (not placeholder and len(current) >= cfg["min_summary_chars"]
                    and not summary_needs_refresh(current)
                    and current_version == SUMMARY_REFRESH_VERSION):
                continue
            attempted = parse_date(p.get("summary_refresh_attempted_at", ""))
            if (p.get("summary_refresh_version") == SUMMARY_REFRESH_VERSION
                    and attempted and now - attempted < cooldown):
                continue
            if retried >= 30:
                break
            retried += 1
            candidate = {
                "title": p.get("title", ""), "summary": "",
                "sources": p.get("sources", []), "lang": p.get("lang", "en"),
            }
            try:
                enrich_candidate_summary(candidate, cfg, max_sources=5)
                detail_raw = candidate.get("summary", "")
                detail_parts = [tidy(part) for part in re.split(r"\n\s*\n", detail_raw) if tidy(part)]
                detail = tidy(detail_raw)
                stale_match_digest = (
                    is_generic_sports_boilerplate(current)
                    or source_is_unanchored_result_for_highlights(
                        p.get("title", ""),
                        " ".join(source.get("title", "") for source in p.get("sources", [])),
                    )
                )
                p["summary_refresh_attempted_at"] = now.isoformat()
                p["summary_refresh_version"] = SUMMARY_REFRESH_VERSION
                updated_existing = True
                detail_is_unusable = (
                    is_generic_sports_boilerplate(detail)
                    or source_is_unanchored_result_for_highlights(p.get("title", ""), detail)
                )
                if len(detail) >= 40 and detail != current and not (stale_match_digest and detail_is_unusable):
                    preserved = "\n\n".join(detail_parts)
                    p["source_summary"] = preserved[:2400].rsplit(" ", 1)[0] if len(preserved) > 2400 else preserved
                    p["summary"] = short_summary({"summary": preserved, "lang": p.get("lang", "en"), "sources": p.get("sources", [])})
                    if candidate.get("article_body"):
                        p["article_body"] = candidate["article_body"]
                    p["generated_at"] = now.isoformat()
                elif stale_match_digest:
                    # Do not keep publishing known-unreliable text if source pages no longer
                    # provide event-specific evidence. A transparent limitation is better
                    # than blending a generic organisation description with an old result.
                    if p.get("lang") == "hi":
                        fallback = "उपलब्ध स्रोतों से इस मैच के विशिष्ट विवरण की पुष्टि नहीं हो सकी। अलग-अलग मैचों की रिपोर्ट को मिलाने से बचने के लिए असंबंधित जानकारी हटा दी गई है। मूल स्रोतों में अधिक जानकारी उपलब्ध हो सकती है।"
                    else:
                        fallback = "Event-specific details could not be verified from the accessible sources. To avoid mixing reports from different matches, unrelated and generic text has been omitted. Check the original source links for confirmed details."
                    p["source_summary"] = fallback
                    p["summary"] = fallback
                    p["article_body"] = fallback
                    p["generated_at"] = now.isoformat()
            except Exception as e:
                log.debug("could not refresh thin published digest %s: %s", p.get("id"), e)
                p["summary_refresh_attempted_at"] = now.isoformat()
                updated_existing = True
    all_posts = posts_doc["posts"] + new_posts
    # Order matters: pages -> posts.json -> published.json (history is marked last)
    render_site(root, cfg, all_posts)
    if new_posts or updated_existing:
        write_json(root / "posts.json", {"version": 1, "posts": all_posts})
    if new_posts:
        pub_doc["items"] += [{"id": p["id"], "title": p["title"], "url": p["url"], "published_at": p["published_at"]} for p in new_posts]
        write_json(root / "published.json", pub_doc)
    if queue:
        rev = load_review(root / "review.json")
        have = {i["id"] for i in rev["items"]} | {k["id"] for k in known_entries(posts_doc, pub_doc)} | {p["id"] for p in new_posts}
        fresh = [q for q in queue if q["id"] not in have]
        cutoff = (now - timedelta(days=7)).isoformat()
        kept = [i for i in rev["items"] if i.get("queued_at", cutoff) >= cutoff]  # stale queue entries expire after 7 days
        if fresh or len(kept) != len(rev["items"]):
            write_json(root / "review.json", {"version": 1, "items": kept + fresh})
        stats["queued_for_review"] = len(fresh)
    stats["published"] = [p["id"] for p in new_posts]
    log.info("run complete: %d new post(s), %d feed(s) ok, %d failed", len(new_posts), stats["feeds_ok"], stats["feeds_failed"])
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TrendPulse India content pipeline")
    ap.add_argument("--root", default=str(Path(__file__).parent))
    ap.add_argument("--render-only", action="store_true", help="regenerate pages without fetching feeds")
    ap.add_argument("--refresh-only", action="store_true", help="refresh thin stored articles without collecting or publishing new stories")
    ap.add_argument("--dry-run", action="store_true", help="fetch and evaluate, write nothing")
    ap.add_argument("--check", action="store_true", help="verify generated files are up to date")
    ap.add_argument("--build-site", metavar="DIR", help="copy deployable files to DIR")
    ap.add_argument("--list-review", action="store_true", help="show items waiting for manual review")
    ap.add_argument("--approve", metavar="ID", help="publish one item from review.json after you checked its sources")
    ap.add_argument("--unpublish", metavar="ID", help="remove one published post (backup kept in unpublished.json)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if a.build_site:
            build_site(a.root, a.build_site); return 0
        if a.list_review:
            for i in load_review(Path(a.root) / "review.json")["items"]:
                print(i["id"], "|", i["lang"], "|", i["title"], "|", ", ".join(s["publisher"] for s in i["sources"]))
            return 0
        if a.unpublish:
            try:
                print("unpublished:", unpublish(a.root, a.unpublish)["title"]); return 0
            except (KeyError, ValueError) as e:
                log.error("cannot unpublish: %s", e); return 4
        if a.approve:
            try:
                print("published:", approve(a.root, a.approve)["url"]); return 0
            except (KeyError, ValueError) as e:
                log.error("cannot approve: %s", e); return 4
        if a.check:
            return 0 if check_generated(a.root) else 1
        stats = run(a.root, render_only=a.render_only, dry_run=a.dry_run, refresh_only=a.refresh_only)
    except ConfigError as e:
        log.error("configuration error: %s", e); return 3
    print(json.dumps({k: v for k, v in stats.items() if k != "rejected"} | {"rejected_count": len(stats["rejected"])}, ensure_ascii=False))
    for r in stats["rejected"][:20]:
        log.info("rejected: %s -> %s", r["title"], ",".join(r["reasons"]))
    if stats["all_feeds_failed"]:
        log.error("every enabled feed failed; nothing was published")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
