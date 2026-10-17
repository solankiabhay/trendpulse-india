"""Static HTML checks shared by site tests (run against the repo and a generated temp site)."""
import posixpath
import re
from html.parser import HTMLParser
from pathlib import Path


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lang = self.title = self.desc = self.canonical = None
        self.h1 = 0; self.links = []; self.ids = set(); self.hreflangs = {}; self._t = False; self.labels_for = set()
        self.inputs = []; self.imgs_no_alt = 0; self.jsonld = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html": self.lang = a.get("lang")
        if tag == "title": self._t = True; self.title = ""
        if tag == "h1": self.h1 += 1
        if tag == "meta" and a.get("name") == "description": self.desc = a.get("content")
        if tag == "link" and a.get("rel") == "canonical": self.canonical = a.get("href")
        if tag == "link" and a.get("rel") == "alternate" and a.get("hreflang"): self.hreflangs[a["hreflang"]] = a.get("href")
        if tag == "link" and a.get("rel") == "stylesheet": self.links.append(a.get("href"))
        if tag == "script" and a.get("src"): self.links.append(a["src"])
        if tag == "script" and a.get("type") == "application/ld+json": self.jsonld = True
        if tag == "a" and a.get("href") is not None: self.links.append(a["href"])
        if tag == "img" and not a.get("alt") is not None: self.imgs_no_alt += 1 if "alt" not in a else 0
        if tag == "label" and a.get("for"): self.labels_for.add(a["for"])
        if tag == "input": self.inputs.append(a)
        if "id" in a: self.ids.add(a["id"])

    def handle_endtag(self, tag):
        if tag == "title": self._t = False

    def handle_data(self, d):
        if self._t: self.title += d


def parse(path):
    p = Page(); p.feed(Path(path).read_text(encoding="utf-8")); return p


def check_tree(root, site_url):
    """Return a list of problems found in every .html file under root (excluding tests)."""
    root, problems = Path(root), []
    files = [f for f in root.rglob("*.html") if "tests" not in f.parts and "_site" not in f.parts]
    parsed = {f: parse(f) for f in files}
    for f, p in parsed.items():
        rel = f.relative_to(root).as_posix()
        if p.lang not in ("en", "hi"): problems.append(f"{rel}: bad html lang {p.lang!r}")
        if not p.title or not p.title.strip(): problems.append(f"{rel}: missing title")
        if not p.desc: problems.append(f"{rel}: missing meta description")
        if not (p.canonical or "").startswith(site_url): problems.append(f"{rel}: bad canonical {p.canonical!r}")
        if p.h1 != 1: problems.append(f"{rel}: expected one h1, found {p.h1}")
        if p.imgs_no_alt: problems.append(f"{rel}: img without alt")
        for i in p.inputs:
            if i.get("type") in ("search", "text") and i.get("id") not in p.labels_for: problems.append(f"{rel}: input without label")
        for href in p.links:
            if re.match(r"^(https?:|mailto:)", href or ""): continue
            target, _, frag = href.partition("#")
            dest = (f if not target else (f.parent / target)).resolve()
            if target and not dest.exists(): problems.append(f"{rel}: broken link {href}"); continue
            if frag and dest in {x.resolve() for x in parsed} and frag not in parsed[next(k for k in parsed if k.resolve() == dest)].ids:
                problems.append(f"{rel}: missing fragment {href}")
    return problems
