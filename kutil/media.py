"""File types the tools agree on."""

import os

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".webm", ".flv", ".ogm", ".divx", ".vob"}
AUDIO_EXT = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".aiff", ".aif", ".wma", ".ape", ".wv", ".alac", ".dsf", ".dff", ".mka"}
# The only leftovers a tool may clear away. Anything else could be something you want.
JUNK_EXT = {".nfo", ".txt", ".url", ".lnk", ".html", ".htm", ".sfv", ".md5", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
            ".torrent", ".opf", ".db", ".ini"}
JUNK_NAMES = {".ds_store"}


def is_junk(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in JUNK_EXT or os.path.basename(path).lower() in JUNK_NAMES
