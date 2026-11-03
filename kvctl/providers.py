"""Online book lookup. Open Library needs no key. Self-published books are often missing, which is expected:
the book's own metadata stays in charge and is only topped up."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from kutil.net import Http, NetError
from kutil.text import normalize, similarity

from . import __version__
from .calibre import author_key, first_last

USER_AGENT = f"kvctl/{__version__} ( https://github.com/D0GU/kali-utils )"


@dataclass
class BookMatch:
    provider: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    isbn: str = ""
    cover_id: int | None = None
    publisher: str = ""
    language: str = ""
    key: str = ""
    score: float = 0.0

    def label(self) -> str:
        who = ", ".join(self.authors[:2]) or "?"
        return f"{self.title} - {who}" + (f" ({self.year})" if self.year else "")


def title_variants(title: str) -> list[str]:
    title = re.sub(r"\s+", " ", title).strip()
    out = [title]
    for sep in (":", " - ", " – "):
        if sep in title:
            out.append(title.split(sep)[0].strip())
    out.append(re.sub(r"\s*[(\[][^)\]]*[)\]]", "", title).strip())
    seen, result = set(), []
    for v in out:
        if v and v.lower() not in seen:
            seen.add(v.lower())
            result.append(v)
    return result


def score(m: BookMatch, title: str, authors: list[str]) -> float:
    t = max(similarity(v, m.title) for v in title_variants(title))
    if authors:
        keys = {author_key(a) for a in authors}
        a = max((1.0 if author_key(x) in keys else max(similarity(first_last(y), first_last(x)) for y in authors)
                 for x in m.authors), default=0.0)
    else:
        a = 0.5
    return round(0.65 * t + 0.35 * a, 3)


class OpenLibrary:
    name = "Open Library"
    url = "https://openlibrary.org/search.json"

    def __init__(self, http: Http):
        self.http = http

    def search(self, title: str, author: str = "") -> list[BookMatch]:
        params = {"title": title, "limit": 10,
                  "fields": "key,title,author_name,first_publish_year,isbn,cover_i,publisher,language"}
        if author:
            params["author"] = author
        data = self.http.request(self.url, params=params, min_interval=1.0) or {}
        out = []
        for d in data.get("docs") or []:
            isbns = d.get("isbn") or []
            out.append(BookMatch(self.name, d.get("title") or "?", d.get("author_name") or [], d.get("first_publish_year"),
                                 next((i for i in isbns if len(i) == 13), isbns[0] if isbns else ""), d.get("cover_i"),
                                 (d.get("publisher") or [""])[0], (d.get("language") or [""])[0], d.get("key") or ""))
        return out


class Lookup:
    def __init__(self, db=None):
        self.http = Http(db, 15.0, USER_AGENT)
        self.openlibrary = OpenLibrary(self.http)
        self.errors: list[str] = []

    def search(self, title: str, authors: list[str] | None = None) -> list[BookMatch]:
        self.errors = []
        authors = authors or []
        first = first_last(authors[0]) if authors else ""
        found: list[BookMatch] = []
        attempts = [(v, first) for v in title_variants(title)] + ([(v, "") for v in title_variants(title)] if first else [])
        for v, who in attempts:
            try:
                found = self.openlibrary.search(v, who)
            except NetError as e:
                self.errors.append(str(e))
                break
            if found:
                break
        for m in found:
            m.score = score(m, title, authors)
        return sorted(found, key=lambda m: -m.score)

    def best(self, title: str, authors: list[str]) -> BookMatch | None:
        results = self.search(title, authors)
        if not results:
            return None
        top = results[0]
        same = lambda m: normalize(m.title) == normalize(top.title)
        runner = next((m.score for m in results[1:] if not same(m)), 0)
        return top if top.score >= 0.85 and top.score - runner >= 0.04 else None

    def cover(self, cover_id: int) -> bytes | None:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg?default=false",
                                     headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read(20 * 1024 * 1024)
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        return data if data[:3] == b"\xff\xd8\xff" else None
