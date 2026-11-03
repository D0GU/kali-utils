"""Music: turn audio files into album/track items.

Embedded tags win; otherwise folder names ("Artist - Album (Year) [FLAC]", "Artist/Album/CD1")
and file names ("01 - Title", "1-03 Artist - Title") are used. Cover images and .cue/.log
files are attached to their album so they travel with it.
"""

from __future__ import annotations

import os
import re
from collections import Counter

from .parse import YEAR_RE, Item, despace, find_year, main_artist, normalize, smart_case, strip_numbering

AUDIO_EXT = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".aiff", ".aif", ".wma", ".ape",
             ".wv", ".alac", ".dsf", ".dff", ".mka"}
ART_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
ALBUM_FILE_EXT = {".cue", ".log"}
COVER_NAMES = {"cover", "folder", "front", "albumart", "album", "poster"}

DISC_DIR_RE = re.compile(r"(?i)^(?:cd|disc|disk|dvd)[ ._-]*(\d{1,2})(?!\d)")
TRACK_RE = re.compile(r"^(?:(\d{1,2})[-.](?=\d))?(\d{1,3})(?=[\s._)\]-]|$)[\s._)\]-]*")
AUDIO_TAG_RE = re.compile(r"(?i)(?<![a-z0-9])(flac|mp3|aac|alac|ogg|opus|wav|320|256|192|v0|v2|vbr|cbr|lossless|"
                          r"hi-?res|web(?:-?dl)?|\d*cd(?:rip)?|vinyl|\d{2}[ -]?bits?|\d{2}[-./]\d{1,3}(?:[ .]?khz)?|"
                          r"\d{2,3}(?:\.\d)?[ .]?khz|24-96|24-192|16-44|kbps|scene|retail)(?![a-z0-9])")
GENERIC_MUSIC_DIR_RE = re.compile(r"(?i)^(music|albums?|discography|mp3|flac|audio|ost|soundtracks?|"
                                  r"various|singles?|scans?|artwork|covers?)$")


def _int(value) -> int | None:
    m = re.match(r"\s*(\d+)", str(value or ""))
    return int(m.group(1)) if m else None


def parse_album_dir(name: str) -> tuple[str | None, str, int | None]:
    """'Radiohead - OK Computer (1997) [FLAC 24-96]' -> ('Radiohead', 'OK Computer', 1997)."""
    s = despace(name)
    year = find_year(s)

    def drop(m):
        inner = m.group(1).strip()
        if AUDIO_TAG_RE.search(inner) or YEAR_RE.fullmatch(inner) or re.fullmatch(r"(19|20)\d\d[^a-zA-Z]*", inner):
            return " "
        return m.group(0)
    s = re.sub(r"[\[({]([^\])}]*)[\])}]", drop, s)
    s = re.sub(r"^\s*(19|20)\d\d\s*[-.]\s*", "", s)                  # "1997 - Album"
    s = re.sub(r"\s+(19|20)\d\d\s*$", "", s)                           # "Album 1997"
    s = re.sub(r"\s+[-–]\s*$", "", re.sub(r"\s+", " ", s)).strip(" -_.")
    while (m := re.search(r"\s+(\S+)$", s)) and AUDIO_TAG_RE.fullmatch(m.group(1)):
        s = s[:m.start()]                                              # trailing "MP3 320"
    parts = [p.strip() for p in re.split(r"\s+[-–]\s+", s) if p.strip()]
    parts = [p for p in parts if not re.fullmatch(r"(19|20)\d\d", p) and not AUDIO_TAG_RE.fullmatch(p)]
    if len(parts) >= 2:
        artist = smart_case(parts[0])
        if artist.lower() in ("va", "v.a.", "various", "various artist"):
            artist = "Various Artists"
        return artist, smart_case(" - ".join(parts[1:])), year
    return None, smart_case(parts[0]) if parts else name, year


def classify_track(rel: str, size: int, root_name: str, tags: dict) -> Item:
    parts = rel.split("/")
    dirs = parts[:-1]
    stem = os.path.splitext(parts[-1])[0]
    it = Item(src=rel, size=size, kind="track", category="music")
    it.note = "audio tags" if tags else "folder and file names"

    disc_dir = None
    album_dirs = list(dirs)
    if album_dirs and DISC_DIR_RE.match(album_dirs[-1]):
        disc_dir = int(DISC_DIR_RE.match(album_dirs[-1]).group(1))
        album_dirs = album_dirs[:-1]
    it.album_dir = "/".join(album_dirs)
    album_dir_name = album_dirs[-1] if album_dirs else root_name
    parent_name = album_dirs[-2] if len(album_dirs) >= 2 else (root_name if album_dirs else None)

    m = TRACK_RE.match(stem)
    file_disc = int(m.group(1)) if m and m.group(1) else None
    file_track = int(m.group(2)) if m else None
    if file_track is not None and file_track >= 100 and file_disc is None:
        file_disc, file_track = divmod(file_track, 100)  # "101 Title" = disc 1, track 1
    it.disc = _int(tags.get("discnumber")) or disc_dir or file_disc
    it.disc_total = _int(tags.get("disctotal"))
    it.track = _int(tags.get("tracknumber")) if tags.get("tracknumber") else file_track

    rest = despace(stem[m.end():] if m else stem).strip(" -._")
    d_artist, d_album, d_year = parse_album_dir(album_dir_name)
    if not d_artist and parent_name:
        pname = strip_numbering(despace(parent_name))
        if not GENERIC_MUSIC_DIR_RE.match(pname) and pname != root_name or (parent_name == root_name and
                                                                             not d_artist and album_dirs):
            d_artist = parse_album_dir(pname)[1] if pname else None
    it.albumartist_tag = bool(tags.get("albumartist"))
    it.artist = tags.get("albumartist") or tags.get("artist") or d_artist or ""
    it.track_artist = tags.get("artist", "")
    title = tags.get("title")
    if not title:
        if it.artist and rest.lower().startswith(it.artist.lower() + " - "):
            rest = rest[len(it.artist) + 3:]
        elif " - " in rest:
            it.file_prefix = rest.split(" - ", 1)[0].strip()
        title = rest
    it.track_title = title or stem
    it.title = tags.get("album") or d_album or parse_album_dir(root_name)[1]
    it.year = _int(tags.get("date", "")[:4]) if re.match(r"\d{4}", tags.get("date", "")) else d_year
    it.compilation = str(tags.get("compilation", "")).lower() in ("1", "true", "yes")
    return it


def unify_albums(tracks: list[Item]) -> None:
    """Make album/artist/year consistent per album folder and sort out compilations and discs."""
    by_dir: dict[str, list[Item]] = {}
    for it in tracks:
        by_dir.setdefault(it.album_dir, []).append(it)
    for members in by_dir.values():
        def common(values):
            values = [v for v in values if v]
            return Counter(values).most_common(1)[0][0] if values else None
        album = common(m.title for m in members) or "Unknown Album"
        year = common(m.year for m in members)
        artists = {normalize(main_artist(m.artist)) for m in members if m.artist}
        has_aa = any(m.albumartist_tag for m in members)
        if any(m.compilation for m in members) or (len(artists) > 1 and not has_aa):
            album_artist = "Various Artists"
            for m in members:
                m.track_artist = m.track_artist or m.artist
        else:
            album_artist = main_artist(common(m.artist for m in members) or "")
            if not album_artist:
                prefixes = Counter(getattr(m, "file_prefix", "") for m in members)
                prefix, n = prefixes.most_common(1)[0]
                if prefix and n >= max(2, len(members) // 2):
                    album_artist = prefix
                    for m in members:
                        if getattr(m, "file_prefix", "") == prefix:
                            m.track_title = m.track_title[len(prefix) + 3:] if m.track_title.startswith(prefix + " - ") \
                                else m.track_title
                else:
                    album_artist = "Unknown Artist"
        for m in members:
            m.title, m.year, m.artist = album, year, album_artist
            if m.track_artist and normalize(main_artist(m.track_artist)) == normalize(album_artist):
                m.track_artist = ""
        discs = {m.disc for m in members if m.disc}
        if len(discs) <= 1 and not any((m.disc_total or 0) > 1 for m in members):
            for m in members:
                m.disc = None
        if all(m.track is None for m in members):
            for n, m in enumerate(sorted(members, key=lambda m: (m.disc or 0, m.src.lower())), start=1):
                m.track = n
                m.note += " · numbered by file order"


def attach_album_files(tracks: list[Item], others: list[tuple[str, int]]) -> set[str]:
    """Attach cover images and .cue/.log files to the album whose folder contains them."""
    albums: dict[str, Item] = {}
    for it in sorted(tracks, key=lambda i: (i.disc or 0, i.track or 0, i.src)):
        albums.setdefault(it.album_dir, it)
    attached: set[str] = set()
    for rel, _ in others:
        ext = os.path.splitext(rel)[1].lower()
        if ext not in ART_EXT | ALBUM_FILE_EXT:
            continue
        d = os.path.dirname(rel)
        candidates = [ad for ad in albums if ad == d or (ad == "" or d.startswith(ad + "/"))]
        if not candidates:
            continue
        ad = max(candidates, key=len)
        if ad == "" and d and not any(DISC_DIR_RE.match(p) for p in d.split("/")) and len(albums) > 1:
            continue  # image in some other folder of a multi-album torrent
        inside = rel[len(ad) + 1:] if ad else rel
        inside_parts = [p for p in inside.split("/") if not DISC_DIR_RE.match(p)]  # flatten disc folders
        albums[ad].art.append((rel, "/".join(inside_parts)))
        attached.add(rel)
    # an album with exactly one loose image that isn't called cover/folder/front: name it cover.*
    for it in albums.values():
        top = [(src, inside) for src, inside in it.art if "/" not in inside and os.path.splitext(inside)[1].lower() in ART_EXT]
        if len(top) == 1 and os.path.splitext(top[0][1])[0].lower() not in COVER_NAMES:
            src, inside = top[0]
            it.art[it.art.index(top[0])] = (src, "cover" + os.path.splitext(inside)[1].lower())
    return attached


def build_tracks(root_name: str, audio: list[tuple[str, int]], others: list[tuple[str, int]],
                 tags_for=None) -> tuple[list[Item], set[str]]:
    tracks = [classify_track(rel, size, root_name, (tags_for(rel) if tags_for else {}) or {}) for rel, size in audio]
    unify_albums(tracks)
    attached = attach_album_files(tracks, others) if tracks else set()
    return tracks, attached
