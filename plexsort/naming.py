"""Plex naming conventions.

Movies:   Movies/Title (Year) {tmdb-123}/Title (Year).mkv
          Movies/Title (Year) {tmdb-123}/Featurettes/Making Of.mkv
TV:       TV Shows/Show (Year) {tvdb-123}/Season 01/Show (Year) - s01e01 - Episode Title.mkv
          TV Shows/Show (Year) {tvdb-123}/Season 00/...          (specials)
          TV Shows/Show (Year) {tvdb-123}/Other/NCOP 01.mkv     (extras)
Subs:     next to the video: <video name>.en.srt, <video name>.en.forced.srt
Music:    Music/Album Artist/Album (Year)/01 - Track Title.flac
          Music/Album Artist/Album (Year)/2-01 - Track Title.flac        (multi-disc)
          Music/Various Artists/Album (Year)/01 - Track Artist - Title.mp3 (compilations)
          Music/Album Artist/Album (Year)/cover.jpg, rip.log, Scans/...
"""

from __future__ import annotations

import os
import re

from .parse import Item, Sub

EXTRA_DIRS = {"trailer": "Trailers", "featurette": "Featurettes", "behindthescenes": "Behind The Scenes",
              "deleted": "Deleted Scenes", "interview": "Interviews", "scene": "Scenes", "short": "Shorts",
              "other": "Other"}
MAX_NAME_BYTES = 240


def sanitize(name: str) -> str:
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r"\s*:\s*", " - ", name)
    name = re.sub(r'[<>"|?*\x00-\x1f❔]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name


def _fit(name: str, ext: str) -> str:
    while len((name + ext).encode()) > MAX_NAME_BYTES and len(name) > 10:
        name = name[:-1]
    return name.rstrip(" .-") + ext


def id_tag(category: str, ids: dict) -> str:
    order = ["tvdb", "tmdb", "imdb"] if category == "show" else ["tmdb", "imdb", "tvdb"]
    for key in order:
        value = ids.get(key)
        if isinstance(value, (list, tuple)):   # never write '{tmdb-[732203]}'
            value = value[0] if value else None
        if value:
            return f" {{{key}-{value}}}"
    return ""


def title_name(title: str, year: int | None) -> str:
    return sanitize(title) + (f" ({year})" if year else "")


def title_folder(category: str, title: str, year: int | None, ids: dict, tags: bool = True) -> str:
    return title_name(title, year) + (id_tag(category, ids) if tags else "")


def episode_code(season: int, episode: int, end: int | None = None) -> str:
    code = f"s{season:02d}e{episode:02d}"
    if end and end != episode:
        code += f"-e{end:02d}"
    return code


ROOT_KEYS = {"movie": ("movies_dirs", "movies_dir"), "show": ("tv_dirs", "tv_dir"), "music": ("music_dirs", "music_dir")}


def library_roots(category: str, cfg: dict) -> list[str]:
    """All library folders for a category, preferred first."""
    many, one = ROOT_KEYS[category]
    roots = cfg.get(many) or ([cfg[one]] if cfg.get(one) else [])
    roots = [os.path.abspath(os.path.expanduser(r)) for r in roots if r]
    if not roots and category == "music":
        roots = [os.path.join(os.path.dirname(library_dir("movie", cfg)), "Music")]
    return roots


def library_dir(category: str, cfg: dict) -> str:
    """The preferred library folder for a category."""
    roots = library_roots(category, cfg)
    if not roots:
        raise ValueError(f"no library folder configured for {category}")
    return roots[0]


def root_of(path: str, category: str, cfg: dict) -> str:
    """The library folder that contains `path` (or the preferred one)."""
    path = os.path.abspath(path)
    hits = [r for r in library_roots(category, cfg) if path == r or path.startswith(r.rstrip("/") + "/")]
    return max(hits, key=len) if hits else library_dir(category, cfg)


def item_root(it: Item, cfg: dict) -> str:
    return it.root or library_dir(it.category, cfg)


def album_folder(artist: str, album: str, year: int | None, cfg: dict, root: str | None = None) -> str:
    return os.path.join(root or library_dir("music", cfg), sanitize(artist) or "Unknown Artist",
                        title_name(album or "Unknown Album", year))


def title_dir_name(it: Item, cfg: dict) -> str:
    """Folder (relative to the library root) that a title lives in."""
    if it.category == "music":
        return os.path.join(sanitize(it.artist) or "Unknown Artist", title_name(it.title or "Unknown Album", it.year))
    return title_folder(it.category, it.title, it.year, it.ids, cfg.get("id_tags", True))


def display_folder(category: str, title: str, year: int | None, ids: dict, artist: str = "",
                   tags: bool = True) -> str:
    if category == "music":
        return f"{sanitize(artist) or 'Unknown Artist'}/{title_name(title or 'Unknown Album', year)}"
    return title_folder(category, title, year, ids, tags)


def track_name(it: Item) -> str:
    num = (f"{it.disc}-" if it.disc else "") + (f"{it.track:02d} - " if it.track is not None else "")
    artist = f"{sanitize(it.track_artist)} - " if it.track_artist else ""
    title = sanitize(it.track_title) or sanitize(os.path.splitext(os.path.basename(it.src))[0])
    return _fit(num + artist + title, os.path.splitext(it.src)[1].lower())


def art_destination(track_dst: str, inside: str) -> str:
    parts = [sanitize(p) for p in inside.split("/") if p]
    return os.path.join(os.path.dirname(track_dst), *parts)


def destination(it: Item, cfg: dict) -> str:
    if it.category == "music":
        return os.path.join(album_folder(it.artist, it.title, it.year, cfg, item_root(it, cfg)), track_name(it))
    folder = os.path.join(item_root(it, cfg), title_dir_name(it, cfg))
    name = title_name(it.title, it.year)
    ext = os.path.splitext(it.src)[1].lower()
    if it.kind == "episode":
        season = it.season if it.season is not None else 1
        base = f"{name} - {episode_code(season, it.episode or 0, it.episode_end)}"
        if it.ep_title:
            base += f" - {sanitize(it.ep_title)}"
        return os.path.join(folder, f"Season {season:02d}", _fit(base, ext))
    if it.kind == "extra":
        extra = sanitize(it.extra_name) or sanitize(os.path.splitext(os.path.basename(it.src))[0])
        return os.path.join(folder, EXTRA_DIRS.get(it.extra_type, "Other"), _fit(extra, ext))
    base = name + (f" - pt{it.part}" if it.part else "")
    return os.path.join(folder, _fit(base, ext))


def sub_destination(video_dst: str, sub: Sub) -> str:
    stem = os.path.splitext(video_dst)[0]
    ext = os.path.splitext(sub.src)[1].lower()
    suffix = (f".{sub.lang}" if sub.lang else "") + (".forced" if sub.forced else "") + (".sdh" if sub.sdh else "")
    return stem + suffix + ext


def title_root(dst: str, category: str, cfg: dict) -> str:
    """The title folder that contains a destination path."""
    base = root_of(dst, category, cfg).rstrip("/") + "/"
    rel = dst[len(base):] if dst.startswith(base) else dst
    depth = 2 if category == "music" else 1   # Artist/Album
    return base + "/".join(rel.split("/")[:depth])
