"""Guess what each file in a torrent is: movie, episode, special, extra, or junk.

Torrent names don't follow Plex conventions, so this is heuristic. Everything
here produces a *plan*: items that the user can review and correct (and that
online providers verify) before anything is moved.
"""

from __future__ import annotations

import os
import re
import string
from collections import Counter
from dataclasses import dataclass, field

from kutil.media import VIDEO_EXT
from kutil.text import normalize

SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".smi"}

KINDS = ["movie", "episode", "extra", "track", "skip"]
EXTRA_TYPES = ["other", "trailer", "featurette", "behindthescenes", "deleted", "interview", "scene", "short"]

_Q = [r"2160p", r"1080[pi]", r"720p", r"576p", r"480p", r"4k", r"uhd", r"hdr(?:10)?\+?", r"dolby ?vision",
      r"hevc", r"x\.?26[45]", r"h\.?26[45]", r"avc", r"xvid", r"divx", r"\d{1,2}[ -]?bits?",
      r"blu-?ray", r"bd(?:rip|remux)?", r"br-?rip", r"web-?dl", r"web-?rip", r"web", r"hdtv", r"dvd(?:rip|scr|5|9)?",
      r"remux", r"aac(?:2\.0)?", r"ac-?3", r"e-?ac-?3", r"dts(?:-hd)?(?: ?ma)?", r"truehd", r"atmos", r"ddp?(?:5\.1|2\.0)?",
      r"flac", r"opus", r"mp3", r"(?:dual|multi|tri)[ .-]?audio", r"multi[ .-]?subs?", r"subbed", r"dubbed",
      r"proper", r"repack", r"internal", r"limited", r"hc", r"hardsub", r"10bit", r"8bit", r"nf", r"amzn",
      r"dsnp", r"hmax", r"hulu", r"atvp"]
QUALITY_RE = re.compile(r"(?i)(?<![a-z0-9])(?:" + "|".join(_Q) + r")(?![a-z0-9])")
YEAR_RE = re.compile(r"(?<!\d)(19[2-9]\d|20[0-4]\d)(?!\d|p\b)")
YEAR_RANGE_RE = re.compile(r"(?<!\d)(19[2-9]\d|20[0-4]\d)\s*[-–~]\s*(19[2-9]\d|20[0-4]\d)(?!\d)")
SXXEYY_RE = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,4})(?:[ ._-]*(?:-|e|-e)[ ._-]*(\d{1,4}))?(?![0-9])")
NXNN_RE = re.compile(r"(?i)(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![0-9a-z])")
# A release's checksum, such as [732E889F]. It's hex, so it can look like E889 or S2.
CHECKSUM_RE = re.compile(r"\s*[\[(][0-9A-Fa-f]{8}[\])]")
EPWORD_RE = re.compile(r"(?i)(?<![a-z0-9])(?:ep(?:isode)?\.?[ ._-]*|e)(\d{1,4})(?:\s*[-~]\s*(\d{1,4}))?(?![0-9])")
ANIME_DASH_RE = re.compile(r"(?:^|\s)-\s+(\d{1,4})(?:v\d)?(?=\s|$|\[|\()")
LEADING_NUM_RE = re.compile(r"^(\d{1,4})(?:v\d)?(?:\s*[-.]\s+|\s+|$)")
SEASON_RE = re.compile(r"(?i)(?<![a-z])(?:season|staffel|saison|temporada)[ ._-]*(\d{1,2})(?!\d)|"
                       r"(?<![a-z0-9])s(\d{1,2})(?![0-9e])|^series[ ._-]*(\d{1,2})$")
SPECIAL_RE = re.compile(r"(?i)(?<![a-z])(ova|oad|ona|ovas|specials?|sp\d{0,2})(?![a-z])")
MOVIE_RE = re.compile(r"(?i)(?<![a-z])(movies?|films?|feature)(?![a-z])")
SAMPLE_RE = re.compile(r"(?i)(?<![a-z])sample(?![a-z])")
PART_RE = re.compile(r"(?i)(?<![a-z])(?:cd|part|pt|disc|disk)[ ._-]*(\d)(?!\d)")
EXTRA_RES = [
    ("trailer", re.compile(r"(?i)(?<![a-z])(trailers?|teasers?|pv\d*|promos?|cm\d*|tv ?spots?)(?![a-z])")),
    ("featurette", re.compile(r"(?i)(?<![a-z])(featurettes?|making[ ._-]of)(?![a-z])")),
    ("behindthescenes", re.compile(r"(?i)(?<![a-z])(behind[ ._-]the[ ._-]scenes|bts)(?![a-z])")),
    ("deleted", re.compile(r"(?i)(?<![a-z])(deleted[ ._-]scenes?)(?![a-z])")),
    ("interview", re.compile(r"(?i)(?<![a-z])(interviews?)(?![a-z])")),
    ("other", re.compile(r"(?i)(?<![a-z])(nc ?op\d*|nc ?ed\d*|creditless|clean (?:opening|ending)|"
                         r"music[ ._-]videos?|extras?|bonus|menus?|bloopers|gag reel|openings?|endings?)(?![a-z])")),
]
GENERIC_DIR_RE = re.compile(r"(?i)^(series|seasons?|episodes?|tv|tv series|tv show|anime|shows?|complete|"
                            r"disc ?\d*|cd ?\d*|bd ?\d*|dvd ?\d*|subs?|subtitles?|video|videos|media|main)$")
MARKETING_RE = re.compile(r"(?i)\s*(?:[-–:]\s*(?:the\s+)?(?:\w+\s+)?pack\b|[-–:]?\s*(?:the\s+)?"
                          r"(?:complete|collection|box ?set|anthology|(?:tv|anime|movie)?\s*series pack)\b).*$")
# "Show S01", "Show (S1+S2)", "Show Season 2", "Show Seasons 1-3": everything from there on isn't the title
SEASON_TOKEN_RE = re.compile(r"(?i)(?<=\S)[\s._\-(\[]+(?:s\d{1,2}(?:\s*[-+&~]\s*s?\d{1,2})*(?:\s*e\d+)?|"
                             r"seasons?\s*\d{1,2}(?:\s*[-+&~]\s*\d{1,2})*)"
                             r"(?=\s*(?:$|[)\]\[(-]|complete\b)).*$")
YEAR_OR_RANGE_RE = re.compile(r"\(?\s*(?:19|20)\d\d(?:\s*[-–~]\s*(?:19|20)\d\d)?\s*\)?")
ANIME_HINT_RE = re.compile(r"(?i)(^\s*\[[^\]]+\])|(?<![a-z])(ova|oad|ncop|nced|creditless|anime|subsplease|"
                           r"erai-raws|horriblesubs|dual[ .-]?audio|\(cbb\)|bd ?1080p|fansub|-hen)(?![a-z])")

LANGS = {"en": "en", "eng": "en", "english": "en", "ja": "ja", "jpn": "ja", "jap": "ja", "japanese": "ja",
         "de": "de", "ger": "de", "deu": "de", "german": "de", "fr": "fr", "fre": "fr", "fra": "fr",
         "french": "fr", "es": "es", "spa": "es", "spanish": "es", "it": "it", "ita": "it", "italian": "it",
         "pt": "pt", "por": "pt", "portuguese": "pt", "nl": "nl", "dut": "nl", "nld": "nl", "dutch": "nl",
         "ru": "ru", "rus": "ru", "russian": "ru", "zh": "zh", "chi": "zh", "zho": "zh", "chinese": "zh",
         "ko": "ko", "kor": "ko", "korean": "ko", "sv": "sv", "swe": "sv", "swedish": "sv", "pl": "pl",
         "pol": "pl", "polish": "pl", "ar": "ar", "ara": "ar", "arabic": "ar", "da": "da", "dan": "da",
         "danish": "da", "fi": "fi", "fin": "fi", "finnish": "fi", "no": "no", "nor": "no",
         "norwegian": "no"}

MIN_SAMPLE_FREE_SIZE = 400 * 1024 * 1024


@dataclass
class Sub:
    src: str
    lang: str = ""
    forced: bool = False
    sdh: bool = False


@dataclass
class Item:
    src: str                      # path relative to the source root
    size: int = 0
    kind: str = "movie"           # movie | episode | extra | skip
    category: str = "movie"       # movie | show (which library; for extras, the owner)
    title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    absolute: bool = False        # episode number has no season context (anime style)
    ep_title: str = ""
    part: int | None = None
    extra_type: str = "other"
    extra_name: str = ""
    ids: dict = field(default_factory=dict)   # tvdb / tmdb / imdb / anilist / tvmaze ...
    verified: str = ""            # provider name once matched online
    ep_found: bool | None = None  # episode exists in the provider's list
    anime: bool = False
    subs: list[Sub] = field(default_factory=list)
    note: str = ""                # why it was classified this way
    title_source: str = ""        # filename | folder | root
    # music (category "music": title = album, artist = album artist)
    artist: str = ""
    track_artist: str = ""        # set when it differs from the album artist (compilations, features)
    disc: int | None = None
    disc_total: int | None = None
    track: int | None = None
    track_title: str = ""
    album_dir: str = ""
    art: list = field(default_factory=list)     # [(src rel path, path inside the album folder)]
    compilation: bool = False
    albumartist_tag: bool = False
    # destination library folder (chosen by space-aware placement, or by the user)
    root: str = ""
    root_manual: bool = False

    def group_key(self) -> tuple:
        if self.category == "music":
            return ("music", normalize(self.artist) + " / " + normalize(self.title), self.year)
        return (self.category, normalize(self.title), self.year)


@dataclass
class Group:
    category: str
    title: str
    year: int | None
    items: list[Item]
    artist: str = ""

    @property
    def key(self) -> tuple:
        return self.items[0].group_key() if self.items else (self.category, normalize(self.title), self.year)

    @property
    def verified(self) -> str:
        provs = {i.verified for i in self.items}
        return provs.pop() if len(provs) == 1 else ""

    @property
    def ids(self) -> dict:
        return self.items[0].ids if self.items else {}

    @property
    def anime(self) -> bool:
        return any(i.anime for i in self.items)


@dataclass
class Plan:
    name: str                     # source display name
    root: str                     # absolute path of the torrent folder or single file
    items: list[Item]
    leftovers: list[tuple[str, int]]   # (relative path, size) files that won't be moved
    torrent: dict | None = None
    incomplete: list[str] = field(default_factory=list)

    def groups(self) -> list[Group]:
        groups: dict[tuple, Group] = {}
        for it in self.items:
            if it.kind == "skip":
                continue
            g = groups.get(it.group_key())
            if g is None:
                g = groups[it.group_key()] = Group(it.category, it.title, it.year, [], it.artist)
            g.items.append(it)
        rank = {"show": 0, "movie": 1, "music": 2}
        order = sorted(groups.values(), key=lambda g: (rank.get(g.category, 3), normalize(g.artist),
                                                       normalize(g.title), g.year or 0))
        for g in order:
            g.items.sort(key=item_sort_key)
        return order

    def skipped(self) -> list[Item]:
        return [i for i in self.items if i.kind == "skip"]


def item_sort_key(it: Item):
    if it.kind == "track":
        return (0, it.disc or 1, it.track if it.track is not None else 999, 0, it.src.lower())
    kind_order = {"episode": 0, "movie": 0, "extra": 2, "skip": 3}[it.kind]
    season = it.season if it.season is not None else 99
    if season == 0:
        kind_order = 1
    return (kind_order, season, it.episode or 0, it.part or 0, it.src.lower())


# --------------------------------------------------------------------------
# Name helpers
# --------------------------------------------------------------------------

def strip_numbering(name: str) -> str:
    """'01. Series' -> 'Series'; '03 - Extras' -> 'Extras'."""
    return re.sub(r"^\d{1,2}\s*[.)]\s*|^\d{1,2}\s+-\s+", "", name).strip()


def strip_tags(s: str) -> str:
    """Drop leading [Group] / (Group) tags."""
    out = re.sub(r"^(\s*[\[(][^\])]*[\])])+\s*", "", s)
    return out if out.strip() else s


def despace(s: str) -> str:
    if s.count(".") > s.count(" ") + 1:
        s = re.sub(r"(?<!\d)\.(?!\d)|\.(?=\D)|(?<=\D)\.", " ", s)
    s = s.replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def smart_case(s: str) -> str:
    letters = [c for c in s if c.isalpha()]
    if len(letters) > 3 and all(c.isupper() for c in letters):
        return string.capwords(s.lower())
    return s


def main_artist(name: str) -> str:
    """'Daft Punk feat. Pharrell Williams' -> 'Daft Punk' (one artist should stay one artist in Plex)."""
    return re.split(r"(?i)\s+(?:feat\.?|ft\.?|featuring|starring)\s+", name or "", maxsplit=1)[0].strip()


def clean_title(raw: str, cut_marketing: bool = True, recase: bool = True, keep_years: bool = False) -> tuple[str, int | None]:
    """Turn a release name into (title, year). A year ends a movie or show's title, and what follows is release junk.
    Episode and extra names are different ('At Anime Expo 2018 Interview With ...'), so they pass keep_years."""
    s = despace(strip_tags(raw))
    year = None
    cut = len(s)
    if m := QUALITY_RE.search(s):
        cut = m.start()
    if keep_years:
        pass
    elif m := YEAR_RANGE_RE.search(s):
        if m.start() > 0:
            year = int(m.group(1))
            cut = min(cut, m.start())
    else:
        years = [m for m in YEAR_RE.finditer(s) if m.start() > 0]
        if years:
            # the last year before any quality junk is usually the release year
            before = [m for m in years if m.start() <= cut] or years[:1]
            year = int(before[-1].group(1))
            cut = min(cut, before[-1].start())
    title = s[:cut]
    # cut an unclosed bracket left over from "(1993 BluRay ...)" style names
    title = re.sub(r"[\[({][^\])}]*$", "", title)
    title = re.sub(r"\[[^\]]*\]", "", title)
    if keep_years:
        title = title.rstrip(" ._-")
        title = re.sub(r"\s*\((?:19|20)\d\d(?:\s*[-–]\s*(?:\d\d){1,2})?\)$", "", title)   # a '(2019)' tag isn't part of the name
        if cut < len(s):     # a bare year right before the release junk is the release's, but 'Anime Expo 2018' alone keeps it
            title = re.sub(r"(?:^|\s+)(?:19|20)\d\d$", "", title)
    if cut_marketing:
        title = SEASON_TOKEN_RE.sub("", title.rstrip(" ._-")) or title
        title = MARKETING_RE.sub("", title) or title   # "The Complete Savages" must not vanish entirely
    title = title.strip(" -_.,:;([{~")
    title = re.sub(r"\s+", " ", title)
    return (smart_case(title) if recase else title), year


def find_year(s: str) -> int | None:
    if m := YEAR_RANGE_RE.search(s):
        return int(m.group(1))
    m = YEAR_RE.search(s)
    return int(m.group(1)) if m else None


def extra_type_of(s: str) -> str | None:
    for kind, rx in EXTRA_RES:
        if rx.search(s):
            return kind
    return None


def episode_title_after(text: str) -> str:
    title, _ = clean_title(text.lstrip(" -._:"), cut_marketing=False, keep_years=True)
    return title


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def _meaningful_dir(dirs: list[str]) -> tuple[str, int | None] | None:
    """Innermost directory name that looks like a title rather than a marker."""
    for d in reversed(dirs):
        name = strip_numbering(despace(strip_tags(d)))
        if not name or name.isdigit() or GENERIC_DIR_RE.match(name):
            continue
        bare = YEAR_OR_RANGE_RE.sub("", name).strip(" -_.")   # "Season 17 (2013-2014)" -> "Season 17"
        if SEASON_RE.search(bare) and len(SEASON_RE.sub("", bare).strip(" -_.")) < 3:
            continue
        if (MOVIE_RE.fullmatch(bare) or SPECIAL_RE.fullmatch(bare) or extra_type_of(bare) == bare.lower()
                or GENERIC_DIR_RE.match(bare) or not bare):
            continue
        title, year = clean_title(name)
        if title:
            return title, year
    return None


def _season_from_dirs(dirs: list[str]) -> int | None:
    for d in reversed(dirs):
        name = strip_numbering(despace(d))
        if m := SEASON_RE.search(name):
            return int(next(g for g in m.groups() if g))
    return None


def _dir_year(dirs: list[str], skip_seasons: bool = False) -> int | None:
    for d in reversed(dirs):
        if skip_seasons and SEASON_RE.search(YEAR_OR_RANGE_RE.sub("", strip_numbering(despace(d))).strip()):
            continue   # "Season 17 (2013)" dates that season, not the show
        if y := find_year(d):
            return y
    return None


# 'B8 - ', 'NC11 - ', 'LE3E1 - ', 'B15-1 - ', 'BUSA - ', with a season first in some files: a short capitalised code, then a name
CODED_SPECIAL_RE = re.compile(r"(?:S\d{1,2}\s*-\s*)?((?:[A-Z]\d+[A-Z0-9]*|[A-Z]{2,6}\d*[A-Z0-9]*)(?:-\d+)?\s*-\s*\S.*)")


BARE_SPECIAL_RE = re.compile(r"S\d{1,2}")


def _after_root(tagless: str, root_name: str) -> str:
    """What follows the torrent's own name at the start of a file name, or "" if it doesn't start that way."""
    # the name as written, or without release tags: '[RH] Show [BDRip] [1080p]' names files 'Show - 01'
    for root in (despace(strip_tags(root_name)), clean_title(root_name, recase=False)[0]):
        if root and tagless.casefold().startswith(root.casefold() + " "):
            return tagless[len(root):].strip(" -_.")
    return ""


def _extra_of_root(item: Item, name: str, extra_type: str, root_name: str, note: str) -> Item:
    item.kind, item.extra_type, item.note = "extra", extra_type, note
    item.extra_name = clean_title(name, cut_marketing=False, recase=False, keep_years=True)[0] or name
    _assign_show_title(item, "", [], root_name)     # no folders: 'Misc - Extras' or a season's years aren't the show's
    return item


def classify_video(rel: str, size: int, root_name: str, siblings: dict[str, list[str]]) -> Item:
    parts = rel.split("/")
    fname = parts[-1]
    stem = os.path.splitext(fname)[0]
    dirs = parts[:-1]
    item = Item(src=rel, size=size)
    tagless = despace(strip_tags(CHECKSUM_RE.sub("", stem)))
    dir_names = [strip_numbering(despace(strip_tags(d))) for d in dirs]
    item.anime = bool(ANIME_HINT_RE.search(stem) or ANIME_HINT_RE.search(root_name)
                      or any(ANIME_HINT_RE.search(d) for d in dirs))

    # 1. samples
    if (SAMPLE_RE.search(stem) and size < MIN_SAMPLE_FREE_SIZE) or any(SAMPLE_RE.fullmatch(d) for d in dir_names):
        item.kind, item.note = "skip", "sample"
        return item

    # 2. a coded special of the show the torrent is named after: 'Show - B8 - Name', 'Show S19 - LE9P - Name'
    after_root = _after_root(tagless, root_name)
    if after_root and not SXXEYY_RE.search(tagless) and (m := CODED_SPECIAL_RE.fullmatch(after_root)):
        return _extra_of_root(item, m.group(1), "other", root_name, "coded special")
    if after_root and BARE_SPECIAL_RE.fullmatch(after_root):   # 'Show - S1': no way to tell which special it is online
        return _extra_of_root(item, after_root, "other", root_name, "coded special")

    # 3. markers from the innermost folder outwards, then the file name itself
    marker = None
    for d in reversed(dir_names):
        bare = re.sub(r"\(?\s*(19|20)\d\d\s*\)?", "", d).strip(" -_.")
        if SPECIAL_RE.search(bare) and len(bare) <= 20:
            marker = ("special", d)
        elif (et := extra_type_of(bare)) and len(bare) <= 25:
            marker = ("extra", et)
        elif MOVIE_RE.search(bare) and len(bare) <= 20:
            marker = ("movie", d)
        if marker:
            break
    sxe = SXXEYY_RE.search(tagless) or NXNN_RE.search(tagless)
    if not marker and not sxe:
        if (et := extra_type_of(tagless)) and not EPWORD_RE.search(tagless):
            marker = ("extra", et)
        elif SPECIAL_RE.search(tagless):
            marker = ("special", tagless)
        elif re.search(r"(?i)(?<![a-z])the movie(?![a-z])", tagless):
            marker = ("movie", tagless)

    # only 'Show - Name': in 'Overlord 2 - Name' the number belongs with the show's name
    if marker and marker[0] == "extra" and after_root and not sxe and tagless[len(tagless) - len(after_root) - 3:].startswith(" - "):
        return _extra_of_root(item, after_root, marker[1], root_name, "extra (folder/name marker)")
    if marker and marker[0] == "extra":
        item.kind, item.extra_type = "extra", marker[1]
        item.extra_name = clean_title(stem, cut_marketing=False, recase=False, keep_years=True)[0] or stem
        item.note = "extra (folder/name marker)"
        _assign_show_title(item, "", dirs, root_name)
        return item

    # 4. episode numbering
    ep_match = None
    if sxe:
        item.kind = "episode"
        item.season = int(sxe.group(1))
        item.episode = int(sxe.group(2))
        if sxe.lastindex and sxe.lastindex >= 3 and sxe.group(3):
            item.episode_end = int(sxe.group(3))
        ep_match = sxe
        item.note = "SxxEyy in file name"
    else:
        for rx, note in ((EPWORD_RE, "Ep/Episode number"), (ANIME_DASH_RE, "' - NN' anime numbering")):
            if m := rx.search(tagless):
                ep_match, item.note = m, note
                break
        if not ep_match:
            m = LEADING_NUM_RE.match(tagless)
            folder = "/".join(dirs)
            numbered = [s for s in siblings.get(folder, []) if LEADING_NUM_RE.match(despace(strip_tags(s)))]
            if m and len(numbered) >= 2 and not (1900 <= int(m.group(1)) <= 2100):
                ep_match, item.note = m, "numbered files in a folder"
        if ep_match:
            item.kind = "episode"
            item.episode = int(ep_match.group(1))
            if ep_match.lastindex and ep_match.lastindex >= 2 and ep_match.group(2):
                item.episode_end = int(ep_match.group(2))
            season = _season_from_dirs(dirs) or _season_from_dirs([tagless[:ep_match.start()]])
            item.season = season if season is not None else 1
            item.absolute = season is None

    if marker and marker[0] == "special":
        item.kind = "episode"
        item.season = 0
        item.absolute = False
        item.note = "special/OVA (folder/name marker)"
        if item.episode is None and (m := LEADING_NUM_RE.match(tagless) or EPWORD_RE.search(tagless)):
            item.episode = int(m.group(1))
        item.ep_title = clean_title(re.sub(r"^\d+(?:\.\d+)?\s*-\s*", "", tagless), cut_marketing=False, keep_years=True)[0]
        _assign_show_title(item, "", dirs, root_name)
        return item

    if marker and marker[0] == "movie" and not sxe:
        item.kind = "movie"
        item.episode = item.episode_end = item.season = None
        item.absolute = False
        item.note = "movie (folder/name marker)"
        _assign_movie_title(item, stem, dirs, root_name)
        return item

    if item.kind == "episode":
        prefix = tagless[:ep_match.start()] if ep_match else ""
        prefix = SEASON_RE.sub("", prefix) if ep_match is not sxe else prefix
        _assign_show_title(item, prefix, dirs, root_name)
        if not item.ep_title:
            item.ep_title = episode_title_after(tagless[ep_match.end():])
        return item

    # 4. otherwise it's a movie
    item.kind = "movie"
    item.note = "single video" if sum(len(v) for v in siblings.values()) <= 1 else "no episode numbering"
    _assign_movie_title(item, stem, dirs, root_name)
    return item


def _assign_show_title(item: Item, prefix: str, dirs: list[str], root_name: str) -> None:
    item.category = "show"
    title, year = clean_title(prefix) if prefix.strip(" -_.") else ("", None)
    if len(title) >= 2 and not title.isdigit():
        item.title_source = "filename"
    elif found := _meaningful_dir(dirs):
        title, year = found
        item.title_source = "folder"
    else:
        title, year = clean_title(root_name)
        item.title_source = "root"
    item.title = title
    item.year = _dir_year(dirs, skip_seasons=True) or year
    if item.year is None and item.title_source != "root" and not YEAR_RANGE_RE.search(root_name):
        item.year = find_year(root_name)


def _assign_movie_title(item: Item, stem: str, dirs: list[str], root_name: str) -> None:
    item.category = "movie"
    if m := PART_RE.search(stem):
        item.part = int(m.group(1))
        stem_for_title = stem[:m.start()]
    else:
        stem_for_title = stem
    title, year = clean_title(stem_for_title)
    generic = (len(title) < 2 or title.isdigit() or GENERIC_DIR_RE.match(title)
               or re.fullmatch(r"(?i)(movie|film|feature|video)", title))
    if not generic:
        item.title_source = "filename"
    elif found := _meaningful_dir(dirs):
        title, year = found[0], found[1] or year
        item.title_source = "folder"
    else:
        title, year2 = clean_title(root_name)
        year = year or year2
        item.title_source = "root"
    item.title = title
    item.year = year or _dir_year(dirs) or (find_year(root_name) if not YEAR_RANGE_RE.search(root_name) else None)


def _parse_sub(rel: str, video_stem: str) -> Sub:
    stem = os.path.splitext(os.path.basename(rel))[0]
    rest = stem[len(video_stem):] if video_stem and stem.startswith(video_stem) else stem
    tokens = [t for t in re.split(r"[\s._\-\[\]()]+", rest.lower()) if t]
    sub = Sub(src=rel)
    for t in tokens:
        if t in LANGS and not sub.lang:
            sub.lang = LANGS[t]
        elif t == "forced":
            sub.forced = True
        elif t in ("sdh", "cc", "hi"):
            sub.sdh = True
    if not sub.lang:  # e.g. "2_English.srt"
        for t in tokens:
            t2 = re.sub(r"^\d+", "", t)
            if t2 in LANGS:
                sub.lang = LANGS[t2]
                break
    return sub


def build_items(root_name: str, files: list[tuple[str, int]], tags_for=None
                ) -> tuple[list[Item], list[tuple[str, int]], list[str]]:
    """Classify every file. Returns (items, leftovers, incomplete files).

    `tags_for(rel)` may return embedded audio tags for an audio file."""
    from .music import AUDIO_EXT, build_tracks
    videos, subs, leftovers, incomplete, audio = [], [], [], [], []
    for rel, size in files:
        if rel.endswith(".part"):
            incomplete.append(rel)
            rel_eff = rel[:-5]
        else:
            rel_eff = rel
        ext = os.path.splitext(rel_eff)[1].lower()
        if ext in VIDEO_EXT:
            videos.append((rel_eff, size))
        elif ext in SUB_EXT:
            subs.append((rel_eff, size))
        elif ext in AUDIO_EXT:
            audio.append((rel_eff, size))
        else:
            leftovers.append((rel, size))

    siblings: dict[str, list[str]] = {}
    for rel, _ in videos:
        folder, _, name = rel.rpartition("/")
        siblings.setdefault(folder, []).append(os.path.splitext(name)[0])

    items = [classify_video(rel, size, root_name, siblings) for rel, size in videos]
    _unify(items, root_name)
    _number_specials(items)

    # attach subtitles to videos
    by_dir: dict[str, list[Item]] = {}
    for it in items:
        by_dir.setdefault(os.path.dirname(it.src), []).append(it)
    real = [it for it in items if it.kind != "skip"]
    for rel, size in subs:
        stem = os.path.splitext(os.path.basename(rel))[0]
        target = None
        candidates = [it for it in real if stem.startswith(os.path.splitext(os.path.basename(it.src))[0])]
        if not candidates:  # Subs/<video stem>/2_English.srt
            candidates = [it for it in real if os.path.splitext(os.path.basename(it.src))[0] in rel.split("/")]
        if candidates:
            target = max(candidates, key=lambda it: len(it.src))
        elif len([i for i in by_dir.get(os.path.dirname(rel), []) if i.kind != "skip"]) == 1:
            target = [i for i in by_dir[os.path.dirname(rel)] if i.kind != "skip"][0]
        elif len(real) == 1:
            target = real[0]
        if target:
            target.subs.append(_parse_sub(rel, os.path.splitext(os.path.basename(target.src))[0]))
        else:
            leftovers.append((rel, size))

    if audio:
        tracks, attached = build_tracks(root_name, audio, leftovers, tags_for)
        items.extend(tracks)
        leftovers = [(rel, size) for rel, size in leftovers if rel not in attached]
    return items, leftovers, incomplete


def _unify(items: list[Item], root_name: str) -> None:
    """Make titles consistent within one torrent."""
    shows = [i for i in items if i.kind == "episode" and i.season != 0]
    # merge show titles that differ only in parentheticals / case
    if shows:
        base = Counter(normalize(re.sub(r"\([^)]*\)", "", i.title)) for i in shows)
        for it in shows:
            key = normalize(re.sub(r"\([^)]*\)", "", it.title))
            it._key = key  # type: ignore[attr-defined]
        for key in base:
            members = [i for i in shows if i._key == key]  # type: ignore[attr-defined]
            title = Counter(i.title for i in members).most_common(1)[0][0]
            years = [i.year for i in members if i.year]
            year = min(years) if years else None   # a show's year is when it began
            for i in members:
                i.title, i.year = title, year
    show_titles = Counter((i.title, i.year) for i in shows)
    movies = [i for i in items if i.kind == "movie"]
    for it in items:
        if it.kind == "extra" or (it.kind == "episode" and it.season == 0):
            if len(show_titles) == 1:
                (title, year), = show_titles
                it.category, it.title, it.year = "show", title, year
            elif not show_titles and it.kind == "extra" and len({(m.title, m.year) for m in movies}) == 1:
                it.category, it.title, it.year = "movie", movies[0].title, movies[0].year
            elif not show_titles and it.kind == "episode":
                it.category = "show"
        if it.kind == "episode" and it.absolute and it.season is None:
            it.season = 1


def _number_specials(items: list[Item]) -> None:
    groups: dict[tuple, list[Item]] = {}
    for it in items:
        if it.kind == "episode" and it.season == 0:
            groups.setdefault(it.group_key(), []).append(it)
    for members in groups.values():
        used = {i.episode for i in members if i.episode is not None}
        dupes = len(used) != len([i for i in members if i.episode is not None])
        if dupes or any(i.episode is None for i in members):
            for n, it in enumerate(sorted(members, key=lambda i: i.src.lower()), start=1):
                it.episode = n


def media_counts(items) -> str:
    """'3 video(s), 12 track(s)' - names what the files actually are."""
    tracks = sum(1 for i in items if i.category == "music")
    videos = len(items) - tracks
    parts = [f"{videos} video(s)" if videos else "", f"{tracks} track(s)" if tracks else ""]
    return ", ".join(p for p in parts if p) or "0 files"
