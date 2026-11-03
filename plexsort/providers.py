"""Online metadata providers used to verify titles before sorting.

* TVmaze   - TV shows + episode lists, keyless; gives TheTVDB/IMDb ids.
* Wikidata - movies and TV, keyless; carries IMDb/TMDB/TVDB ids for Plex.
* TMDB     - movies (and TV), optional: needs a free API key (config: tmdb_api_key).
* AniList  - anime series/movies/OVAs, keyless GraphQL.
* Jikan    - MyAnimeList mirror, keyless; fallback for anime.
* anime-lists (Fribb) - maps AniList/MAL ids to TVDB/TMDB/IMDb, which Plex understands.
* MusicBrainz - albums (releases) and tracklists, keyless (1 request/second).
* Cover Art Archive - album covers for MusicBrainz releases.

All responses are cached in the library database.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .parse import Group, Item, normalize

USER_AGENT = f"plexsort/{__version__}"
DAY = 86400
ANIME_MAP_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"


class ProviderError(Exception):
    pass


@dataclass
class Match:
    provider: str
    pid: str
    category: str                  # show | movie
    title: str
    year: int | None
    ids: dict = field(default_factory=dict)
    info: str = ""
    alt_titles: list = field(default_factory=list)
    anime: bool = False
    episodes: int | None = None
    tvdb_season: int | None = None
    score: float = 0.0
    popularity: float = 0.0        # 0..1 where the provider reports it (tie-breaker)
    artist: str = ""               # music: album artist
    tracks: int | None = None      # music: track count of the release

    def label(self) -> str:
        y = f" ({self.year})" if self.year else ""
        if self.category == "music":
            return f"{self.artist} – {self.title}{y}"
        return f"{self.title}{y}"


@dataclass
class Episode:
    season: int
    number: int | None
    title: str
    special: bool = False


class Http:
    def __init__(self, db=None, timeout: float = 12.0):
        self.db = db
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._down: dict[str, float] = {}   # host -> time it failed (skip it for a while)

    def request(self, url: str, *, params: dict | None = None, data: dict | None = None,
                headers: dict | None = None, ttl: float = 7 * DAY, min_interval: float = 0.25,
                timeout: float | None = None):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        body = json.dumps(data).encode() if data is not None else None
        key = url + (body.decode() if body else "")
        if self.db and ttl:
            with self._lock:
                cached = self.db.cache_get(key, ttl)
            if cached is not None:
                return cached
        host = urllib.parse.urlsplit(url).netloc
        if time.monotonic() - self._down.get(host, -1e9) < 600:
            raise ProviderError(f"{host}: unreachable (skipping for now)")
        with self._lock:
            host_lock = self._host_locks.setdefault(host, threading.Lock())
        with host_lock:
            return self._fetch(url, host, body, key, headers, ttl, min_interval, timeout)

    def _fetch(self, url, host, body, key, headers, ttl, min_interval, timeout):
        wait = self._last.get(host, 0) + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/json")
        if body:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        for attempt in range(3):
            self._last[host] = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    result = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    result = None
                    break
                if e.code == 429 and attempt < 2:
                    time.sleep(float(e.headers.get("Retry-After") or 2) + 0.5)
                    continue
                if e.code == 401:
                    raise ProviderError(f"{host}: unauthorized - check the API key") from None
                raise ProviderError(f"{host}: HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                reason = getattr(e, "reason", e)
                self._down[host] = time.monotonic()
                raise ProviderError(f"{host}: {reason}") from None
            except json.JSONDecodeError:
                raise ProviderError(f"{host}: invalid response") from None
        if self.db and ttl:
            with self._lock:
                self.db.cache_put(key, result)
        return result


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

class TVmaze:
    name = "TVmaze"
    base = "https://api.tvmaze.com"

    def __init__(self, http: Http):
        self.http = http

    def _match(self, show: dict) -> Match:
        ext = show.get("externals") or {}
        ids = {"tvmaze": show["id"]}
        if ext.get("thetvdb"):
            ids["tvdb"] = ext["thetvdb"]
        if ext.get("imdb"):
            ids["imdb"] = ext["imdb"]
        network = (show.get("network") or show.get("webChannel") or {}).get("name", "")
        country = ((show.get("network") or {}).get("country") or {}).get("code", "")
        bits = [b for b in (network, country, ", ".join(show.get("genres") or [])[:40], show.get("status")) if b]
        anime = show.get("type") == "Animation" and country == "JP" or "Anime" in (show.get("genres") or [])
        m = Match("TVmaze", str(show["id"]), "show", show["name"], _year(show.get("premiered")), ids,
                  " · ".join(bits), anime=anime)
        m.popularity = (show.get("weight") or 0) / 100  # TVmaze's 0-100 popularity weight
        return m

    def search(self, query: str) -> list[Match]:
        data = self.http.request(f"{self.base}/search/shows", params={"q": query}) or []
        return [self._match(r["show"]) for r in data]

    def by_tvdb(self, tvdb_id) -> Match | None:
        data = self.http.request(f"{self.base}/lookup/shows", params={"thetvdb": tvdb_id})
        return self._match(data) if data else None

    def episodes(self, tvmaze_id) -> list[Episode]:
        data = self.http.request(f"{self.base}/shows/{tvmaze_id}/episodes", params={"specials": 1}) or []
        return [Episode(e["season"], e.get("number"), e.get("name") or "", e.get("type") != "regular"
                        or e.get("number") is None) for e in data]


class TMDB:
    name = "TMDB"
    base = "https://api.themoviedb.org/3"

    def __init__(self, http: Http, key: str):
        self.http = http
        self.key = key.strip()

    def _get(self, path: str, params: dict) -> dict:
        headers = {}
        params = dict(params)
        if self.key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        return self.http.request(f"{self.base}{path}", params=params, headers=headers) or {}

    def search_movie(self, query: str, year: int | None = None) -> list[Match]:
        params = {"query": query, "include_adult": "false"}
        if year:
            params["year"] = year
        out = []
        for r in self._get("/search/movie", params).get("results", [])[:10]:
            anime = r.get("original_language") == "ja" and 16 in (r.get("genre_ids") or [])
            out.append(Match("TMDB", str(r["id"]), "movie", r.get("title") or r.get("original_title", "?"),
                             _year(r.get("release_date")), {"tmdb": r["id"]},
                             (r.get("original_title") or "") + " · " + (r.get("overview") or "")[:80],
                             alt_titles=[r.get("original_title") or ""], anime=anime))
        return out

    def search_tv(self, query: str, year: int | None = None) -> list[Match]:
        params = {"query": query}
        if year:
            params["first_air_date_year"] = year
        out = []
        for r in self._get("/search/tv", params).get("results", [])[:10]:
            anime = r.get("original_language") == "ja" and 16 in (r.get("genre_ids") or [])
            out.append(Match("TMDB", str(r["id"]), "show", r.get("name") or "?", _year(r.get("first_air_date")),
                             {"tmdb": r["id"]}, (r.get("overview") or "")[:80],
                             alt_titles=[r.get("original_name") or ""], anime=anime))
        return out


WD_MOVIE_FILTER = "haswbstatement:P4947|P31=Q11424|P31=Q202866|P31=Q20650540"
WD_TV_FILTER = "haswbstatement:P4835|P4983|P31=Q5398426|P31=Q63952888"


class Wikidata:
    """Wikidata's search + entity API: keyless, and it stores IMDb (P345), TMDB (P4947 movie / P4983 TV)
    and TVDB (P4835) ids."""
    name = "Wikidata"
    api = "https://www.wikidata.org/w/api.php"

    def __init__(self, http: Http):
        self.http = http

    def _search(self, query: str, flt: str) -> list[str]:
        data = self.http.request(self.api, params={"action": "query", "list": "search", "srsearch": f"{query} {flt}",
                                                   "srlimit": 10, "format": "json"}, min_interval=0.3) or {}
        return [r["title"] for r in (data.get("query") or {}).get("search") or []]

    def _entities(self, qids: list[str]) -> dict:
        if not qids:
            return {}
        data = self.http.request(self.api, params={"action": "wbgetentities", "ids": "|".join(qids),
                                                   "props": "labels|aliases|descriptions|claims",
                                                   "languages": "en", "languagefallback": 1, "format": "json"},
                                 min_interval=0.3) or {}
        return data.get("entities") or {}

    @staticmethod
    def _values(claims: dict, prop: str) -> list:
        out = []
        for c in claims.get(prop, []):
            v = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
            if v is not None:
                out.append(v)
        return out

    def _year(self, claims: dict, props: tuple) -> int | None:
        years = []
        for prop in props:
            for v in self._values(claims, prop):
                t = v.get("time", "") if isinstance(v, dict) else ""
                if re.match(r"\+\d{4}", t):
                    years.append(int(t[1:5]))
            if years:
                return min(years)
        return None

    def _search_kind(self, query: str, category: str) -> list[Match]:
        qids = self._search(query, WD_MOVIE_FILTER if category == "movie" else WD_TV_FILTER)
        ents = self._entities(qids)
        out = []
        for qid in qids:
            e = ents.get(qid) or {}
            claims = e.get("claims") or {}
            label = ((e.get("labels") or {}).get("en") or {}).get("value")
            if not label:
                continue
            desc = ((e.get("descriptions") or {}).get("en") or {}).get("value", "")
            aliases = [a["value"] for a in (e.get("aliases") or {}).get("en", [])]
            originals = [v.get("text") for v in self._values(claims, "P1476") if isinstance(v, dict)]
            ids = {}
            if imdb := self._values(claims, "P345"):
                ids["imdb"] = imdb[0]
            if category == "movie":
                if tm := self._values(claims, "P4947"):
                    ids["tmdb"] = int(tm[0]) if str(tm[0]).isdigit() else tm[0]
                year = self._year(claims, ("P577",))
            else:
                tv = [v for v in self._values(claims, "P4835") if str(v).isdigit()]
                if not tv and not self._values(claims, "P4983") and not self._values(claims, "P580"):
                    continue  # franchises / lists, not an actual series
                if tv:
                    ids["tvdb"] = int(tv[0])
                if tm := self._values(claims, "P4983"):
                    ids["tmdb"] = int(tm[0]) if str(tm[0]).isdigit() else tm[0]
                year = self._year(claims, ("P580", "P577"))
            ids["wikidata"] = qid
            out.append(Match("Wikidata", qid, category, label, year, ids, desc[:80],
                             alt_titles=[a for a in aliases + originals if a][:10],
                             anime="anime" in desc.lower()))
        return out

    def search_movie(self, query: str) -> list[Match]:
        return self._search_kind(query, "movie")

    def search_tv(self, query: str) -> list[Match]:
        return self._search_kind(query, "show")


ANILIST_QUERY = """
query ($s: String) {
  Page(perPage: 10) {
    media(search: $s, type: ANIME) {
      id idMal format episodes seasonYear status
      title { romaji english native }
      synonyms
      startDate { year }
    }
  }
}"""


class AniList:
    name = "AniList"

    def __init__(self, http: Http, animemap: "AnimeMap"):
        self.http = http
        self.map = animemap

    def search(self, query: str) -> list[Match]:
        data = self.http.request("https://graphql.anilist.co", data={"query": ANILIST_QUERY, "variables": {"s": query}},
                                 min_interval=0.7) or {}
        out = []
        for m in ((data.get("data") or {}).get("Page") or {}).get("media") or []:
            t = m.get("title") or {}
            title = t.get("english") or t.get("romaji") or "?"
            ids = {"anilist": m["id"]}
            if m.get("idMal"):
                ids["mal"] = m["idMal"]
            mapped = self.map.lookup(anilist=m["id"], mal=m.get("idMal"))
            ids.update(mapped.get("ids", {}))
            fmt = m.get("format") or "?"
            category = "movie" if fmt == "MOVIE" else "show"
            eps = m.get("episodes")
            out.append(Match("AniList", str(m["id"]), category, title,
                             (m.get("startDate") or {}).get("year") or m.get("seasonYear"), ids,
                             f"{fmt} · {eps or '?'} ep · romaji: {t.get('romaji') or '-'}",
                             alt_titles=[x for x in (t.get("romaji"), t.get("english"), *(m.get("synonyms") or []))
                                         if x],
                             anime=True, episodes=eps, tvdb_season=mapped.get("tvdb_season")))
        return out


class Jikan:
    name = "MyAnimeList"

    def __init__(self, http: Http, animemap: "AnimeMap"):
        self.http = http
        self.map = animemap

    def search(self, query: str) -> list[Match]:
        data = self.http.request("https://api.jikan.moe/v4/anime", params={"q": query, "limit": 10},
                                 min_interval=1.0, timeout=5) or {}
        out = []
        for a in data.get("data") or []:
            ids = {"mal": a["mal_id"]}
            mapped = self.map.lookup(mal=a["mal_id"])
            ids.update(mapped.get("ids", {}))
            category = "movie" if a.get("type") == "Movie" else "show"
            year = a.get("year") or _year(((a.get("aired") or {}).get("from") or "")[:4])
            out.append(Match("MyAnimeList", str(a["mal_id"]), category, a.get("title_english") or a.get("title") or "?",
                             year, ids, f"{a.get('type')} · {a.get('episodes') or '?'} ep",
                             alt_titles=[x for x in (a.get("title"), a.get("title_english"), a.get("title_japanese"))
                                         if x], anime=True, episodes=a.get("episodes"),
                             tvdb_season=mapped.get("tvdb_season")))
        return out


@dataclass
class Track:
    disc: int
    number: int
    title: str
    artist: str


def _credit(credits: list | None) -> str:
    return "".join((c.get("name") or (c.get("artist") or {}).get("name", "")) + (c.get("joinphrase") or "")
                   for c in credits or []).strip()


def _lucene(s: str) -> str:
    return s.replace("\\", " ").replace('"', " ")


class MusicBrainz:
    name = "MusicBrainz"
    base = "https://musicbrainz.org/ws/2"

    def __init__(self, http: Http):
        self.http = http

    def search(self, album: str, artist: str | None = None) -> list[Match]:
        queries = []
        if artist and normalize(artist) not in ("unknown artist", ""):
            name = "Various Artists" if normalize(artist) == "various artists" else artist
            queries.append(f'release:"{_lucene(album)}" AND artist:"{_lucene(name)}"')
        queries.append(f'release:"{_lucene(album)}"')
        for q in queries:
            data = self.http.request(f"{self.base}/release/", params={"query": q, "fmt": "json", "limit": 25},
                                     min_interval=1.1) or {}
            releases = data.get("releases") or []
            if releases:
                return [self._match(r) for r in releases]
        return []

    def _match(self, r: dict) -> Match:
        media = r.get("media") or []
        formats = "+".join(sorted({m.get("format") or "?" for m in media})) if media else "?"
        rg = r.get("release-group") or {}
        ids = {"mbid": r["id"]}
        if rg.get("id"):
            ids["mbrg"] = rg["id"]
        info = " · ".join(x for x in (formats, f"{len(media)} disc(s)" if len(media) > 1 else "", r.get("country") or "",
                                       rg.get("primary-type") or "", (r.get("disambiguation") or "")[:30]) if x)
        return Match("MusicBrainz", r["id"], "music", r.get("title") or "?", _year(r.get("date")), ids, info,
                     artist=_credit(r.get("artist-credit")), tracks=r.get("track-count"))

    def tracklist(self, mbid: str) -> tuple[list[Track], int | None, str]:
        data = self.http.request(f"{self.base}/release/{mbid}",
                                 params={"inc": "recordings+artist-credits+release-groups", "fmt": "json"},
                                 min_interval=1.1, ttl=30 * DAY) or {}
        album_artist = _credit(data.get("artist-credit"))
        year = _year((data.get("release-group") or {}).get("first-release-date")) or _year(data.get("date"))
        tracks = []
        for disc_no, medium in enumerate(data.get("media") or [], start=1):
            for t in medium.get("tracks") or []:
                number = t.get("position") or _year_safe_int(t.get("number"))
                tracks.append(Track(medium.get("position") or disc_no, number, t.get("title") or "",
                                    _credit(t.get("artist-credit")) or album_artist))
        return tracks, year, album_artist


def _year_safe_int(v) -> int:
    m = re.match(r"\d+", str(v or ""))
    return int(m.group(0)) if m else 0


def download_cover(mbid: str, dest: str, timeout: float = 20) -> bool:
    """Save the release's front cover (Cover Art Archive) to `dest`. Returns True on success."""
    req = urllib.request.Request(f"https://coverartarchive.org/release/{mbid}/front-1200",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(20 * 1024 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    if not data[:3] == b"\xff\xd8\xff" and not data[:8] == b"\x89PNG\r\n\x1a\n":
        return False
    if data[:3] != b"\xff\xd8\xff":
        dest = os.path.splitext(dest)[0] + ".png"
    with open(dest, "xb") as f:
        f.write(data)
    return True


class AnimeMap:
    """Fribb/anime-lists: AniList/MAL ids -> TVDB/TMDB/IMDb ids (cached on disk, refreshed every 2 weeks)."""

    def __init__(self, http: Http, cache_dir: str):
        self.http = http
        self.path = Path(cache_dir) / "anime-list-full.json"
        self._by_anilist: dict | None = None
        self._by_mal: dict = {}
        self.error = ""

    def _load(self) -> None:
        if self._by_anilist is not None:
            return
        self._by_anilist = {}
        try:
            fresh = self.path.exists() and time.time() - self.path.stat().st_mtime < 14 * DAY
            if not fresh:
                req = urllib.request.Request(ANIME_MAP_URL, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                json.loads(raw)  # validate before replacing the cache
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_bytes(raw)
                os.replace(tmp, self.path)
            entries = json.loads(self.path.read_text())
        except (OSError, ValueError, urllib.error.URLError) as e:
            if not self.path.exists():
                self.error = f"anime id map unavailable: {e}"
                return
            entries = json.loads(self.path.read_text())
        for e in entries:
            if e.get("anilist_id"):
                self._by_anilist[e["anilist_id"]] = e
            if e.get("mal_id"):
                self._by_mal[e["mal_id"]] = e

    def lookup(self, anilist=None, mal=None) -> dict:
        self._load()
        e = (self._by_anilist or {}).get(anilist) or self._by_mal.get(mal)
        if not e:
            return {}
        ids = {}
        if e.get("tvdb_id"):
            ids["tvdb"] = e["tvdb_id"]
        tmdb = e.get("themoviedb_id")
        if isinstance(tmdb, dict):
            if e.get("type") == "MOVIE" and tmdb.get("movie"):
                ids["tmdb"] = tmdb["movie"]
            elif e.get("type") != "MOVIE" and tmdb.get("tv"):
                ids["tmdb"] = tmdb["tv"]
        elif isinstance(tmdb, int):
            ids["tmdb"] = tmdb
        imdb = e.get("imdb_id")
        if isinstance(imdb, list) and imdb:
            ids["imdb"] = imdb[0]
        elif isinstance(imdb, str) and imdb:
            ids["imdb"] = imdb
        if e.get("anidb_id"):
            ids["anidb"] = e["anidb_id"]
        season = (e.get("season") or {}).get("tvdb")
        return {"ids": ids, "tvdb_season": season}


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def query_variants(q: str) -> list[str]:
    q = re.sub(r"\s+", " ", q).strip()
    out = [q]
    no_paren = re.sub(r"\s*\([^)]*\)", " ", q).strip()
    out.append(no_paren)
    paren = re.findall(r"\(([^)]*[a-zA-Z][^)]*)\)", q)
    for sep in (" - ", ", ", ": "):
        if sep in no_paren:
            head = no_paren.split(sep)[0].strip()
            out.append(head)
            if paren:  # "Show The Movie - 01 (Gurren-hen)" -> "Show Gurren-hen"
                base = re.sub(r"(?i)\s+the movie\b.*$", "", head)
                out.append(f"{base} {paren[-1]}")
    stripped = re.sub(r"(?i)\b(the (?:movie|series|animation)|tv series|the animated series)\b", " ", no_paren)
    out.append(re.sub(r"\s+", " ", stripped).strip(" -,:"))
    words = no_paren.split()
    if len(words) > 3:
        out.append(" ".join(words[:3]))
    seen, result = set(), []
    for v in out:
        key = v.lower()
        if v and key not in seen and len(v) >= 2:
            seen.add(key)
            result.append(v)
    return result


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    if ta and tb and (ta <= tb or tb <= ta):
        ratio = max(ratio, 0.75 + 0.25 * min(len(ta), len(tb)) / max(len(ta), len(tb)))
    return ratio


STOP = {"the", "a", "an", "of", "movie", "series", "tv", "and", "no", "season", "part"}


def _tokens(s: str) -> list[str]:
    return [t for t in normalize(s).split() if t not in STOP and not t.isdigit()]


def _grams(tokens: list[str]) -> set:
    if len(tokens) < 2:
        return set(tokens)
    return {(a, b) for a, b in zip(tokens, tokens[1:])}


def overlap(query: str, names: list[str]) -> float:
    """How much of the query's word sequence appears in any of the names (word pairs)."""
    q = _grams(_tokens(query))
    if not q:
        return 0.0
    best = 0.0
    for n in names:
        toks = _tokens(n)
        g = _grams(toks) | (set(toks) if len(_tokens(query)) < 2 else set())
        best = max(best, len(q & g) / len(q))
    return best


def score_match(m: Match, queries: list[str], year: int | None, category: str) -> float:
    names = [n for n in [m.title, *m.alt_titles] if n]
    sim = max(similarity(q, n) for q in queries for n in names)
    ov = overlap(queries[0], names)
    if year and m.year:
        y = 1.0 if year == m.year else 0.6 if abs(year - m.year) == 1 else 0.0
    else:
        y = 0.5
    score = 0.55 * sim + 0.20 * ov + 0.25 * y
    if m.category != category:
        score -= 0.3
    return round(max(0.0, score), 3)


def score_music(m: Match, queries: list[str], artist: str | None, year: int | None, tracks: int | None) -> float:
    album_sim = max(similarity(q, m.title) for q in queries)
    if artist and normalize(artist) not in ("unknown artist", ""):
        artist_sim = similarity(artist, m.artist)
        if normalize(artist) == "various artists" and normalize(m.artist) in ("various artists", "va"):
            artist_sim = 1.0
    else:
        artist_sim = 0.5
    if year and m.year:
        y = 1.0 if year == m.year else 0.6 if abs(year - m.year) <= 1 else 0.3
    else:
        y = 0.5
    if tracks and m.tracks:
        t = 1.0 if tracks == m.tracks else max(0.0, 1 - abs(tracks - m.tracks) / max(tracks, m.tracks)) * 0.7
    else:
        t = 0.5
    return round(0.45 * album_sim + 0.25 * artist_sim + 0.12 * y + 0.18 * t, 3)


COUNTRY_RE = re.compile(r"\s+\(?(US|UK|AU|NZ|CA|IE)\)?$")
COUNTRY_WORDS = {"US": ("american", "united states"), "UK": ("british", "english", "united kingdom", "gb"),
                 "AU": ("australian", "australia"), "NZ": ("new zealand",), "CA": ("canadian", "canada"),
                 "IE": ("irish", "ireland")}


def country_hint(m: Match, country: str) -> float:
    info = m.info.lower()
    codes = {country.lower()} | ({"gb"} if country == "UK" else set())
    if any(re.search(rf"(^|[^a-z]){c}([^a-z]|$)", info) for c in codes) or any(w in info for w in COUNTRY_WORDS[country]):
        return 0.06
    return -0.03


class Matcher:
    def __init__(self, cfg: dict, db=None, http: Http | None = None):
        self.http = http or Http(db)
        cache_dir = os.path.join(os.path.dirname(cfg["database"]), "cache")
        self.animemap = AnimeMap(self.http, cache_dir)
        self.tvmaze = TVmaze(self.http)
        self.anilist = AniList(self.http, self.animemap)
        self.jikan = Jikan(self.http, self.animemap)
        self.tmdb = TMDB(self.http, cfg["tmdb_api_key"]) if cfg.get("tmdb_api_key") else None
        self.musicbrainz = MusicBrainz(self.http)
        self.wikidata = Wikidata(self.http)
        self.errors: list[str] = []

    def sources(self, category: str, anime: bool) -> list[tuple[str, callable]]:
        if category == "music":
            return [("MusicBrainz", self.musicbrainz.search)]
        tv = [("TVmaze", self.tvmaze.search), ("Wikidata", self.wikidata.search_tv)]
        tmdb_tv = [("TMDB", self.tmdb.search_tv)] if self.tmdb else []
        wd_movie = [("Wikidata", self.wikidata.search_movie)]
        tmdb_movie = [("TMDB", self.tmdb.search_movie)] if self.tmdb else []
        movie = tmdb_movie + wd_movie          # TMDB first when a key is configured
        anime_src = [("AniList", self.anilist.search), ("MyAnimeList", self.jikan.search)]
        if category == "show":
            return (anime_src[:1] + tv + tmdb_tv + anime_src[1:]) if anime else (tv + tmdb_tv + anime_src)
        return (anime_src[:1] + movie + anime_src[1:]) if anime else (movie + anime_src)

    def search(self, query: str, category: str, year: int | None = None, anime: bool = False,
               provider: str | None = None, stop_at: float | None = None, artist: str | None = None,
               tracks: int | None = None) -> list[Match]:
        """Search providers (all, or just `provider`) and return matches sorted by score."""
        self.errors = []
        country = None
        if category == "show" and (cm := COUNTRY_RE.search(query)):
            country, query = cm.group(1), query[:cm.start()]
        variants = query_variants(query)
        results: list[Match] = []
        for name, fn in self.sources(category, anime):
            if provider and name != provider:
                continue
            if name == "MyAnimeList" and not provider and any(r.provider == "AniList" for r in results):
                continue  # Jikan is only a fallback for when AniList has nothing
            found: list[Match] = []
            for v in variants[:5]:
                try:
                    found = fn(v, year) if name == "TMDB" else fn(v, artist) if name == "MusicBrainz" else fn(v)
                except ProviderError as e:
                    self.errors.append(f"{name}: {e}")
                    break
                if found:
                    break
            for m in found:
                m.score = (score_music(m, variants, artist, year, tracks) if category == "music"
                           else score_match(m, variants, year, category))
                if country:
                    m.score = round(m.score + country_hint(m, country), 3)
                m.score = round(m.score + 0.06 * m.popularity, 3)
            results.extend(found)
            best = max((m.score for m in results), default=0)
            if stop_at is not None and best >= stop_at:
                break
        # de-duplicate: prefer the entry with the most ids at the same score
        def ident_of(m: Match):
            if m.category == "music":
                return (normalize(m.artist), normalize(m.title), m.year, m.tracks, m.info)
            if m.category == "movie":
                return m.ids.get("imdb") or m.ids.get("tmdb") or (m.provider, m.pid)
            return m.ids.get("tvdb") or m.ids.get("tmdb") or m.ids.get("imdb") or (m.provider, m.pid)
        # results that several providers agree on are more likely the mainstream title
        providers_per: dict = {}
        for m in results:
            providers_per.setdefault((m.category, ident_of(m)), set()).add(m.provider)
        for m in results:
            if len(providers_per[(m.category, ident_of(m))]) > 1:
                m.score = round(m.score + 0.05, 3)
            for k, v in [(k, v) for r in results if r is not m and ident_of(r) == ident_of(m)
                         for k, v in r.ids.items()]:
                m.ids.setdefault(k, v)   # merge ids found by the other providers
        uniq: dict[tuple, Match] = {}
        for m in sorted(results, key=lambda m: (-m.score, -len(m.ids), m.year or 9999)):
            if m.category == "music":
                ident = (normalize(m.artist), normalize(m.title), m.year, m.tracks, m.info)
            elif m.category == "movie":
                ident = m.ids.get("imdb") or m.ids.get("tmdb") or (m.provider, m.pid)
            else:
                ident = m.ids.get("tvdb") or m.ids.get("tmdb") or m.ids.get("imdb") or (m.provider, m.pid)
            uniq.setdefault((m.category, ident), m)
        def edition_rank(m: Match) -> int:  # on ties prefer CD/digital, single-disc editions
            if m.category != "music":
                return 0
            fmt = m.info.split(" · ")[0]
            return (0 if fmt in ("CD", "Digital Media") else 1) + (1 if "disc(s)" in m.info else 0)
        return sorted(uniq.values(), key=lambda m: (-m.score, edition_rank(m), m.year or 9999))

    def best(self, group: Group) -> Match | None:
        if group.category == "movie" and not self.tmdb and not group.anime:
            # No TMDB key: still try AniList (anime films are common in packs), but don't auto-accept weak hits.
            pass
        if group.category == "music":
            results = self.search(group.title, "music", group.year, artist=group.artist,
                                  tracks=sum(1 for i in group.items if i.kind == "track"))
            if not results:
                return None
            top = results[0]
            # editions of the same album are equivalent; only a *different* album counts as competition
            same = lambda m: normalize(m.title) == normalize(top.title) and normalize(m.artist) == normalize(top.artist)
            runner = next((m.score for m in results[1:] if not same(m)), 0)
            return top if top.score >= 0.85 and top.score - runner >= 0.04 else None
        results = self.search(group.title, group.category, group.year, group.anime, stop_at=0.9)
        if not results:
            return None
        top = results[0]
        runner = results[1].score if len(results) > 1 else 0
        if top.score >= 0.85 and (top.score - runner >= 0.04 or top.score >= 0.97):
            return top
        return None

    def episodes(self, match: Match) -> list[Episode]:
        """Episode list for a show match (always from TVmaze, which has TVDB-style seasons)."""
        try:
            if match.provider == "TVmaze":
                return self.tvmaze.episodes(match.pid)
            if match.ids.get("tvmaze"):
                return self.tvmaze.episodes(match.ids["tvmaze"])
            if match.ids.get("tvdb"):
                show = self.tvmaze.by_tvdb(match.ids["tvdb"])
                if show:
                    match.ids.setdefault("tvmaze", show.ids["tvmaze"])
                    return self.tvmaze.episodes(show.pid)
        except ProviderError as e:
            self.errors.append(str(e))
        return []


def apply_music_match(items: list[Item], match: Match, matcher: Matcher | None = None) -> None:
    tracks, year, album_artist = ([], None, "")
    if matcher is not None:
        try:
            tracks, year, album_artist = matcher.musicbrainz.tracklist(match.pid)
        except ProviderError as e:
            matcher.errors.append(str(e))
    album_artist = album_artist or match.artist
    multi = len({t.disc for t in tracks}) > 1
    by_pos = {(t.disc, t.number): t for t in tracks}
    for it in items:
        it.title = match.title
        it.artist = album_artist
        it.year = year or match.year
        it.ids = dict(match.ids)
        it.verified = match.provider
        it.category = "music"
        if it.kind != "track" or not tracks:
            continue
        t = by_pos.get((it.disc or 1, it.track)) if it.track is not None else None
        if t is None or (it.track_title and similarity(t.title, it.track_title) < 0.5):
            by_title = max(tracks, key=lambda tr: similarity(tr.title, it.track_title or ""))
            if it.track_title and similarity(by_title.title, it.track_title) >= 0.75:
                t = by_title
            elif t is not None and it.track_title and similarity(t.title, it.track_title) < 0.5 and \
                    not normalize(it.track_title).isdigit():
                t = None
        if t:
            it.track_title = t.title
            it.track = t.number
            it.disc = t.disc if multi else None
            it.track_artist = t.artist if normalize(t.artist) != normalize(album_artist) else ""
            it.ep_found = True
        else:
            it.ep_found = False


def apply_match(items: list[Item], match: Match, matcher: Matcher | None = None) -> None:
    """Rename a group of items to the verified title and fill in episode details."""
    if match.category == "music":
        return apply_music_match(items, match, matcher)
    for it in items:
        it.title = match.title
        it.year = match.year
        it.ids = {k: v for k, v in match.ids.items()}
        it.verified = match.provider
        it.anime = it.anime or match.anime
        if match.category == "movie" and it.kind == "episode":
            it.kind = "movie"
            it.season = it.episode = it.episode_end = None
        elif match.category == "show" and it.kind == "movie":
            it.kind, it.season, it.episode, it.absolute = "episode", 1, it.part or 1, True
        it.category = match.category
    if match.category != "show" or matcher is None:
        return
    episodes = matcher.episodes(match)
    if not episodes:
        return
    regular = sorted([e for e in episodes if not e.special and e.number is not None],
                     key=lambda e: (e.season, e.number))
    by_num = {(e.season, e.number): e for e in regular}
    season1 = sum(1 for e in regular if e.season == regular[0].season) if regular else 0
    for it in items:
        if it.kind != "episode" or it.season == 0 or it.episode is None:
            continue
        if it.absolute and match.tvdb_season and match.tvdb_season > 1 and (it.season or 1) == 1:
            it.season = match.tvdb_season
        if it.absolute and (it.season or 1) == 1 and it.episode > season1 and it.episode <= len(regular):
            e = regular[it.episode - 1]  # absolute numbering across seasons
            it.season, it.episode = e.season, e.number
            if it.episode_end:
                it.episode_end = None
        e = by_num.get((it.season or 1, it.episode))
        if e:
            it.ep_found = True
            if e.title:
                it.ep_title = e.title
        else:
            it.ep_found = False
